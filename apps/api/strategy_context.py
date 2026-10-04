"""Small case features for legal retrieval; never customer prose or identifiers."""
from __future__ import annotations

from . import evidence_mapping, verification

EVIDENCE_FIELDS = {
    'income_evidence_verified': ('monthly_income',),
    'debt_evidence_verified': ('total_debt',),
    'housing_evidence_verified': ('housing_deposit', 'housing_cost'),
    'living_expenses_evidence_verified': ('living_expenses',),
    'bank_balance_evidence_verified': ('bank_balance',),
    'insurance_evidence_verified': ('insurance_surrender',),
    'retirement_evidence_verified': ('retirement_expected',),
    'household_evidence_verified': ('household_size',),
}


def _current_ocr_review(case, packet):
    """Reuse the actual OCR gate's evidence binding, never a supplied passed flag."""
    from . import automation, store
    sources = automation._sources(case, verified_only=True, packet=packet)
    items = automation._verification_items(case, sources, packet=packet)
    if not sources or not items:
        return False
    corrected = [{key: row.get(key) for key in ('id', 'key', 'value', 'status', 'origin', 'document_id')}
                 for row in case.get('extraction_candidates', [])
                 if row.get('origin') == 'human_correction' or row.get('status') == 'rejected']
    signature = store.digest([sources, items, automation.VERSION] + ([corrected] if corrected else []))
    expected = sorted(item['id'] for item in items)
    return any(row.get('kind') == 'ocr' and row.get('version') == verification.VERSION
               and row.get('signature') == signature and row.get('passed') is True
               and row.get('status') == 'passed' and not row.get('error')
               and not row.get('stale')
               and not row.get('unattempted_batch_count')
               and sorted(row.get('checked_item_ids', [])) == expected
               for row in reversed(case.get('verification_runs', [])))


def _current_auto_scope_ids(case):
    """Require the same current collection signature as the scope review gate."""
    from . import automation, court_rules, request_collection, store
    verified = set()
    for request in case.get('requests', []):
        if request.get('status') != 'fulfilled' or request.get('no_longer_required'):
            continue
        review = request.get('automatic_validation') or {}
        if (review.get('status') != 'passed' or review.get('passed') is not True
                or request.get('id') not in review.get('checked_item_ids', [])):
            continue
        documents = request_collection.active_documents(case, request)
        scope = {key: request.get(key) for key in ('catalog_id', 'title', 'institution', 'account_key',
                 'period', 'period_start', 'period_end', 'issuance_options', 'source_refs')}
        if request.get('managed_by') == court_rules.MANAGED_BY:
            if request.get('scope_unresolved') or request.get('metadata_validation', {}).get('status') != 'matched':
                continue
            scope['unknown_metadata_fields'] = request.get('metadata_validation', {}).get('unknown_fields', [])
            account = next((row for row in case.get('financial_accounts', case.get('accounts', []))
                            if isinstance(row, dict) and str(row.get('id') or row.get('account_key') or '')
                            == str(request.get('account_key'))), {})
            scope['expected_account_number'] = account.get('account_number', account.get('number'))
        signature = store.digest([[{key: document.get(key) for key in ('id', 'sha256', 'version', 'text', 'document_metadata')}
                                   for document in documents], scope, case.get('client_name'), automation.VERSION])
        if review.get('signature') == signature:
            verified.update(document['id'] for document in documents
                            if document.get('auto_verified') is True and document.get('scope_confirmed') is True
                            and document.get('person_confirmed') is True)
    return verified


def _evidence_flags(case, packet, selected_packet):
    """Receipt status of current values; not exhaustive collection/legal approval."""
    from . import automation, extraction_readiness
    current = [document for document in extraction_readiness.active_documents(case)
               if document.get('status') == 'verified'
               and document.get('automated_check', {}).get('coverage_status') != 'identity_conflict'
               and extraction_readiness.document_state(case, document)['can_review']]
    reviewed = {document['id'] for document in current
                if automation.manual_document_review_current(case, document)}
    if any(document['id'] not in reviewed for document in current) and _current_ocr_review(case, packet):
        reviewed.update({document['id'] for document in current} & _current_auto_scope_ids(case))
    values, origins = packet.get('form_values', {}), packet.get('origins', {})
    def verified_field(key):
        source_ids = set(origins.get(key, {}).get('source_ids') or [])
        return (key in values and values[key] == selected_packet.get('form_values', {}).get(key)
                and bool(source_ids) and source_ids <= reviewed)
    return {flag: all(verified_field(key) for key in keys) for flag, keys in EVIDENCE_FIELDS.items()}


def current_calculation(case):
    return next((row for row in reversed(case.get('legal_calculations', []))
                 if not row.get('stale') and row.get('input_revision') == case.get('input_revision')), None)


def build(case, calculation=None, packet=None):
    # A caller can reuse displayed values, but cannot supply verification
    # provenance. Receipt flags always use a fresh source-derived packet.
    current_packet = evidence_mapping.build(case)
    packet = packet if packet is not None else current_packet
    calculation = calculation if calculation is not None else current_calculation(case) or {}
    inputs = calculation.get('inputs') or packet.get('inputs') or {}
    income = inputs.get('income') or {}
    result = {'court_id': case.get('court_id'),
              'case_type': case.get('case_type', 'personal_rehabilitation'),
              'employment_type': income.get('kind', 'unknown'),
              'income_basis': income.get('basis', 'unknown'),
              'procedure_stage': 'initial_application',
              'facts': {key: value for key, value in packet.get('form_values', {}).items()
                        if key in verification.NUMERIC_FACTS and verification._numeric(value)}}
    # Workflow display strings and an unverified outcome must not change the
    # legal stage. Only a verified operative decision may select a later stage.
    decisions = [row for row in case.get('court_outcomes', [])
                 if row.get('decision_verified') is True and row.get('evidence_verified') is True]
    if any(row.get('decision_type') == 'discharge' for row in decisions):
        result['procedure_stage'] = 'discharge'
    elif any(row.get('decision_type') == 'initial_plan_approval' for row in decisions):
        result['procedure_stage'] = 'post_approval'
    for key in verification.FEATURE_FLAGS - verification.EVIDENCE_FEATURE_FLAGS:
        if type(inputs.get(key)) is bool:
            result[key] = inputs[key]
        # Flags can also come from explicitly confirmed structured facts. The
        # caller's arbitrary notes, inferred names and legal prose are excluded.
        rows = [row for row in case.get('facts', []) if row.get('key') == key
                and row.get('status') == 'confirmed' and not row.get('stale')
                and type(row.get('value')) is bool and row.get('evidence_ids')]
        source_ids = {doc['id'] for doc in case.get('documents', [])
                      if doc.get('status') == 'verified'}
        rows = [row for row in rows if set(row['evidence_ids']) <= source_ids]
        choices = {row['value'] for row in rows}
        if len(choices) == 1:
            result[key] = choices.pop()
    result.update(_evidence_flags(case, current_packet, packet))
    return result


def graph_features(structured_case, calculation=None):
    """Reuse the boundary's closed vocabulary without recursively building it."""
    features = {key: value for key, value in structured_case.items()
                if key in verification.FEATURE_ENUMS and isinstance(value, str)
                and value in verification.FEATURE_ENUMS[key]}
    features.update({key: structured_case[key] for key in verification.FEATURE_FLAGS
                     if type(structured_case.get(key)) is bool})
    if structured_case.get('court_id') in {f'CT{i:02}' for i in range(1, 16)}:
        features['court_id'] = structured_case['court_id']
    features['numeric_facts'] = {key: value for key, value in structured_case.get('facts', {}).items()
                                if key in verification.NUMERIC_FACTS and verification._numeric(value)}
    summary = (calculation or {}).get('summary', calculation or {})
    features['calculation'] = {key: value for key, value in summary.items()
                               if key in verification.CALCULATION_VALUES and verification._numeric(value)}
    features['risk_codes'] = sorted({row.get('code') for row in (calculation or {}).get('blockers', [])
                                     if row.get('code') in verification.RISK_CODES})
    return features


def finding_cards(review):
    """Resolve model citation aliases to inspected public links for the UI."""
    refs = {ref['ref']: ref for ref in review.get('reference_sources', []) if ref.get('ref')}
    titles = {'income': '변제 여력과 소득 보완', 'liquidation': '청산가치와 변제안 조정',
              'debt_limit': '채무 구분과 한도 확인', 'evidence': '판단을 돕는 추가 증빙',
              'prior_proceeding': '과거 절차와 신청 요건 확인',
              'asset_disposal': '재산 처분과 대금 사용처 검토', 'other': '사건별 추가 검토'}
    return [{'code': row.get('code'), 'title': titles.get(row.get('code'), '사건별 추가 검토'),
             'description': row.get('strategy', ''), 'reason': row.get('reason', ''),
             'source_refs': [refs[ref] for ref in row.get('source_refs', []) if ref in refs],
             'origin': 'ai_strategy_review', 'verification_status': review.get('status'),
             'category': 'supporting' if row.get('code') == 'evidence' else 'legal'}
            for row in review.get('findings', [])]
