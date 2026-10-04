"""Deterministic document observations, arithmetic and explicitly proposed law inputs.

Re-extract from verified original pages on every build. Stored candidates, model
answers and generated prose are never proof. A code-checked observation does not
mean that a semantic AI review or a legal approval has passed.
"""
from __future__ import annotations

import copy
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from . import document_facts, human_review_evidence, legal_calculator, store

VERSION = 'typed-evidence-mapping-v6-original-detail-fields'
DIRECT = {'client_name', 'resident_id', 'address', 'phone', 'employer', 'employer_address',
          'employer_phone', 'employment_period', 'employment_start', 'job_title', 'housing_type',
          'housing_deposit', 'housing_cost', 'household_size', 'dependent_count', 'employment_type',
          'living_expenses', 'insurance_surrender', 'insurance_name', 'insurance_policy',
          'tax_arrears', 'prior_proceedings', 'income_seizure'}


def sources(case):
    result = []
    for doc in case.get('documents', []):
        if doc.get('status') != 'verified' or doc.get('automated_check', {}).get('coverage_status') == 'identity_conflict':
            continue
        pages = doc.get('page_texts') or [{'page': 1, 'text': doc.get('text', '')}]
        for index, page in enumerate(pages):
            text = page.get('text', '') if isinstance(page, dict) else str(page)
            number = page.get('page', index + 1) if isinstance(page, dict) else index + 1
            if text.strip():
                result.append({'id': f"doc:{doc['id']}:p{number}:0", 'document_id': doc['id'],
                    'kind': 'case_document', 'text': text, 'page': number, 'version': doc.get('version', 1)})
    return result


def _review_gaps(case, values):
    """Requested files complete != every form fact known. Never auto-request.

    Recorded interview statements provide review context only. They cannot fill
    verified form values, calculation inputs, or documentary evidence rows.
    """
    record = case.get('consultation') or {}
    consultation_sources = []
    allowed = record.get('status') != 'quarantined' and (
        not case.get('intake') or case['intake'].get('status') == 'completed')
    if allowed:
        texts = [('notes', record.get('notes'))]
        if isinstance(record.get('answers'), dict):
            texts.extend(('answer:' + str(key), value) for key, value in record['answers'].items())
        for key, text in texts:
            if isinstance(text, str) and text.strip():
                consultation_sources.append({'id': f"consultation:{record.get('version', 1)}:{key}",
                    'kind': 'party_statement', 'text': text, 'version': record.get('version', 1)})
    reported = document_facts.extract(consultation_sources)
    groups = [
        ('HOUSING_EVIDENCE_REQUIRED', '거주 형태·보증금·월 주거비 확인',
         ['housing_type', 'housing_deposit', 'housing_cost'], ['D33'],
         ['무상거주 확인서 또는 임대차계약서 등 거주 형태에 맞는 자료'],
         '현재 원문에서 거주 형태·보증금·월 부담액을 모두 확인하지 못했습니다. 상담 진술과 실제 거주 조건을 대조해 주세요.'),
        ('LIVING_EXPENSES_EVIDENCE_REQUIRED', '실제 월 생활지출 확인',
         ['living_expenses'], ['D33', 'D36', 'D42'],
         ['월 생활지출 내역과 해당 지출 근거'],
         '실제 월 생활지출 합계를 확인해 주세요. 계좌의 생활비 출금과 법률상 인정 생계비는 전체 생활지출과 별개입니다.'),
        ('INSURANCE_EVIDENCE_REQUIRED', '보험 보유·해약환급금 확인',
         ['insurance_surrender'], ['D39'],
         ['보험가입 조회 결과 또는 해당 보험의 해약환급금 확인자료'],
         '보험 보유 여부와 해약환급금을 확인할 원문이 부족합니다. 상담의 보험 없음 진술은 별도 확인 전까지 0원으로 확정하지 않습니다.'),
    ]
    result = []
    for code, title, fields, catalogs, suggested, reason in groups:
        missing = [key for key in fields if key not in values]
        if not missing:
            continue
        reported_keys = set(missing) | ({'insurance_contracts'} if code == 'INSURANCE_EVIDENCE_REQUIRED' else set())
        rows = [row for row in reported if row['key'] in reported_keys]
        reported_value = {}
        conflicts = []
        for key in sorted(reported_keys):
            selected = [row for row in rows if row['key'] == key]
            choices = {store.dumps(row['value']) for row in selected}
            if len(choices) == 1:
                reported_value[key] = selected[0]['value']
            elif len(choices) > 1:
                conflicts.append(key)
        requests = [request for request in case.get('requests', []) if request.get('catalog_id') in catalogs
                    and request.get('status') not in {'withdrawn', 'cancelled', 'superseded'}
                    and not request.get('no_longer_required')]
        status = ('fulfilled' if all(request.get('status') == 'fulfilled' for request in requests)
                  else 'requested') if requests else 'not_requested'
        result.append({'code': code, 'stage': 'analysis', 'title': title, 'label': title,
            'reason': reason, 'fields': missing, 'status': 'needs_review',
            'reported_value': reported_value, 'reported_conflicts': conflicts,
            'reported_sources': [{key: row.get(key) for key in ('source_id', 'source_version', 'quote', 'line_start', 'line_end')}
                                 | {'source_kind': 'party_statement', 'key': row['key'], 'value': row['value']} for row in rows],
            'suggested_documents': suggested, 'request_status': status,
            'request_ids': [request['id'] for request in requests if request.get('id')],
            'auto_request': False, 'blocks_first_draft': False, 'requires_staff_confirmation': True})
    return result


def build(case):
    original_sources = sources(case)
    facts = document_facts.extract(original_sources)
    source_map = {source['id']: source for source in original_sources}
    # The extractor supplies typed context and exact source lines. Re-running it
    # prevents a forged typed_fact_id/value on a stored candidate from authorizing a field.
    facts = [row for row in facts if row.get('quote') and row['quote'] in source_map[row['source_id']]['text']]
    observed_facts = copy.deepcopy(facts)
    reviewed = human_review_evidence.apply(case, facts)
    facts = reviewed['facts']
    by_key = defaultdict(list)
    for row in facts:
        by_key[row['key']].append(row)
    values, origins, errors, derivations = {}, {}, list(reviewed['errors']), []

    def put(key, value, rows, operation='source_observation'):
        if value is None:
            return
        values[key] = value
        origins[key] = {'type': 'deterministic_evidence', 'status': 'source_checked',
            'source_ids': sorted({row['document_id'] for row in rows}),
            'fact_ids': [row['id'] for row in rows], 'operation': operation,
            'semantic_review_required': True}
        review_ids = sorted({source_id for row in rows for source_id in row.get('review_source_ids', [])})
        if review_ids:
            origins[key].update(type='human_review', status='human_reviewed', review_source_ids=review_ids,
                                ai_verified=False)
        if operation != 'source_observation':
            derivations.append({'key': key, 'value': value, 'operation': operation,
                                'operand_fact_ids': [row['id'] for row in rows]})

    def unique(key, rows=None):
        rows = by_key[key] if rows is None else rows
        choices = {store.dumps(row['value']) for row in rows}
        if len(choices) == 1:
            return rows[0]['value'], rows
        if len(choices) > 1:
            errors.append({'code': 'CONFLICTING_TYPED_VALUES', 'key': key, 'fact_ids': [row['id'] for row in rows]})
        return None, rows

    for key in DIRECT:
        if key == 'job_title':
            continue
        value, rows = unique(key)
        put(key, value, rows)
    titles = by_key['job_title']
    choices = sorted({row['value'] for row in titles}, key=len)
    employers = {row.get('employer') for row in titles}
    # A department-prefixed rank and the same bare rank at one employer are
    # compatible text variants, not different jobs. Unrelated ranks still hold.
    compatible = bool(choices) and len(employers) == 1 and None not in employers and all(
        choices[-1] == value or choices[-1].endswith(' ' + value) for value in choices)
    if compatible:
        put('job_title', choices[-1], titles, 'same_employer_exact_rank_suffix')
    elif titles:
        value, rows = unique('job_title')
        put('job_title', value, rows)
    else:
        value, rows = unique('job_duties')
        put('job_title', value, rows, 'explicit_duties_as_job_description')
    registered=[row for row in by_key['address'] if re.search(r'주민\s*등록.*등본',source_map[row.get('original_source_id', row['source_id'])]['text'][:300])]
    value,rows=unique('registered_address',registered)
    put('registered_address',value,rows)
    if 'employment_period' not in values:
        active_starts=[row for row in by_key['employment_start'] if any(
            job['source_id']==row['source_id'] and job['value']=='급여소득자' and re.search(
                r'현재\s*(?:재직|근무)|재직\s*중|재직(?:하고)?\s*있',job['quote'])
            for job in by_key['employment_type'])]
        start,rows=unique('employment_start',active_starts)
        if start:
            put('employment_period',str(start)+' ~ 현재',rows,'start_date_plus_explicit_current_employment')
    tenant, rows = unique('housing_tenant')
    put('tenant_name', tenant, rows)

    # Prefer explicitly monthly net earnings. Gross, annual taxable income,
    # pension assessment and bank transfers are never silently monthly net pay.
    monthly = [row for row in by_key['income_net'] if row.get('frequency') == 'monthly']
    if not monthly:
        monthly = [row for row in by_key['income_gross'] if row.get('frequency') == 'monthly']
    income_value, income_rows = unique('monthly_income', monthly)
    basis = income_rows[0].get('basis') if income_rows and income_value is not None else None
    if income_value is not None:
        put('monthly_income', income_value, income_rows)
        put('income_manwon', income_value / 10000, income_rows, 'KRW_divide_10000')
        put('annual_income', income_value * 12, income_rows, 'monthly_times_12_projection')
        put('income_label', '급여', income_rows, 'typed_payroll_label')
        put('income_period_label', '월평균', income_rows, 'explicit_monthly_period')
        dated_income = [row for row in income_rows if row.get('period_start') and row.get('period_end')]
        if dated_income:
            # This certificate asserts a period of the stated income, not merely
            # employment. Never substitute the hire date for the pay period.
            start = min(row['period_start'] for row in dated_income)
            try:
                checked_start = date.fromisoformat(start)
            except ValueError:
                checked_start = None
            if checked_start:
                start_rows = [row for row in dated_income if row['period_start'] == start]
                for suffix, value in [('year', checked_start.year), ('month', checked_start.month), ('day', checked_start.day)]:
                    put('income_start_' + suffix, value, start_rows, 'observed_income_period_start_' + suffix)

    # An account/loan identity is never its row index or just a similar amount.
    # Duplicated documents for the same dated balance do not double count money.
    balances = defaultdict(list)
    unscoped_balances = []
    for row in by_key['cash_balance']:
        if row.get('balance_kind') == 'opening':
            continue
        identity = (row.get('institution'), row.get('account_key'))
        if not all(identity):
            unscoped_balances.append(row)
            continue
        balances[identity].append(row)
    balance_rows = []
    for identity, rows in balances.items():
        dated = [row for row in rows if row.get('as_of')]
        if dated:
            latest = max(row['as_of'] for row in dated)
            rows = [row for row in dated if row['as_of'] == latest]
        value, rows = unique('cash_balance', rows)
        if value is not None:
            balance_rows.append(rows[0])
    if balance_rows and len(balance_rows) == len(balances):
        put('bank_balance', sum(row['value'] for row in balance_rows), balance_rows, 'distinct_latest_accounts_sum')
        for index, row in enumerate(balance_rows[:2]):
            suffix = '' if index == 0 else '_2'
            put('bank_name' + suffix, row.get('institution'), [row], 'source_account_metadata')
            put('bank_account' + suffix, row.get('account_key'), [row], 'source_account_metadata')
            put('bank_balance_' + str(index + 1), row['value'], [row], 'source_account_metadata')
        if any(row['value'] != values['bank_balance'] for row in unscoped_balances):
            errors.append({'code': 'UNSCOPED_BALANCE_RECONCILIATION', 'key': 'bank_balance'})
    elif not balances and unscoped_balances:
        # A narrative total is a single total, never an additional account. It
        # can populate the observed-total field, but cannot identify each account.
        value, rows = unique('cash_balance', unscoped_balances)
        put('bank_balance', value, rows, 'unscoped_total_requires_account_reconciliation')

    debt_docs = defaultdict(list)
    for row in facts:
        if row['key'].startswith('creditor_'):
            # A PDF can contain several creditors or several loans at one bank.
            # Page-local issuer/loan metadata identifies the entity, never names[0].
            debt_docs[(row['document_id'], row.get('institution'), row.get('loan_key'), row.get('page'))].append(row)
    entities = defaultdict(list)
    for (doc_id, institution, loan, page), rows in debt_docs.items():
        names = [row for row in rows if row['key'] == 'creditor_name']
        name = names[0]['value'] if names else institution
        if not name:
            continue
        entities[(name, loan)].append((doc_id + ':page:' + str(page), rows))
    creditors, creditor_fact_rows = [], []
    for (name, loan), documents in entities.items():
        if len(documents) > 1 and not loan:
            errors.append({'code': 'LOAN_IDENTITY_REQUIRED', 'key': 'creditors'})
            continue
        rows = [row for _, grouped in documents for row in grouped]
        row = {'id': 'creditor-' + store.digest([name, loan, documents[0][0]])[:12],
               'name': name, 'kind': None, 'principal': None, 'interest': None,
               'evidence_ids': sorted({r['document_id'] for r in rows})}
        for suffix, target in [('principal', 'principal'), ('interest', 'interest'), ('cause', 'cause'), ('address', 'address')]:
            row[target], _ = unique('creditor_' + suffix, [r for r in rows if r['key'] == 'creditor_' + suffix])
        kind, _ = unique('creditor_kind', [r for r in rows if r['key'] == 'creditor_kind'])
        row['kind'] = {'무담보': 'unsecured', '담보': 'secured', '우선권': 'priority'}.get(kind)
        index = len(creditors)
        creditors.append(row)
        creditor_fact_rows.extend(rows)
        for key, value in row.items():
            if key not in {'id', 'evidence_ids'}:
                put(f'creditors.{index}.{key}', value, rows, 'document_creditor_binding')
        put(f'creditors.{index}.number', index + 1, rows, 'list_order')
        put(f'creditors.{index}.basis', '원문에 기재된 원금·이자 및 기준일 대조', rows, 'evidence_description')
    unresolved_creditors=any(error['code'] in {'LOAN_IDENTITY_REQUIRED','CONFLICTING_TYPED_VALUES',
                            'HUMAN_REVIEW_REJECTED','HUMAN_REVIEW_CONFLICT'} and
                            error.get('key','').startswith('creditor') for error in errors)
    if creditors and not unresolved_creditors:
        for field, target in [('principal', 'principal_total'), ('interest', 'interest_total')]:
            if all(type(row[field]) is int for row in creditors):
                put(target, sum(row[field] for row in creditors), creditor_fact_rows, 'distinct_creditors_sum')
        if all(type(row[field]) is int for row in creditors for field in ('principal', 'interest')):
            put('total_debt', sum(row['principal'] + row['interest'] for row in creditors), creditor_fact_rows, 'principal_plus_interest')
            if all(row['kind'] for row in creditors):
                for secured, key in [(True, 'secured_debt'), (False, 'unsecured_debt')]:
                    put(key, sum(row['principal'] + row['interest'] for row in creditors if (row['kind'] == 'secured') == secured),
                        creditor_fact_rows, 'classified_creditors_sum')
    if creditors:
        def amount(value):
            return f'{value:,}원' if type(value) is int else '미확인'
        readable=[]
        for row in creditors:
            category={'unsecured':'무담보','secured':'담보부','priority':'우선권'}.get(row['kind'],'분류 미확인')
            readable.append(f"{row['name']} · 원금 {amount(row['principal'])} · 이자 {amount(row['interest'])} · {category}")
        put('creditors','\n'.join(readable),creditor_fact_rows,'readable_creditor_summary')

    # Ordinary severance and DB/DC/IRP benefits have different legal treatment.
    # Only an explicitly identified ordinary scheme can contribute its gross
    # estimate to the asset input; legal deductions remain unknown below.
    retirement_review = []
    ordinary_retirement = defaultdict(list)
    retirement_rows = by_key['retirement_expected'] + by_key['retirement_pension_balance']
    for row in retirement_rows:
        kinds = [kind for kind in by_key['retirement_kind'] if kind['document_id'] == row['document_id']]
        ordinary = {kind['value'] for kind in kinds} == {'일반 퇴직금'}
        if row['key'] == 'retirement_expected' and ordinary and row.get('employer'):
            ordinary_retirement[row['employer']].append(row)
        else:
            reason = '퇴직급여 제도·근무처 또는 퇴직연금의 법률상 처리 확인 전에는 일반 퇴직금 자산으로 합산하지 않습니다.'
            errors.append({'code': 'RETIREMENT_TREATMENT_REVIEW', 'key': row['key'],
                           'document_id': row['document_id'], 'reason': reason})
            retirement_review.append({'code': 'RETIREMENT_TREATMENT_REVIEW', 'stage': 'analysis',
                'title': '퇴직급여 종류·공제 확인', 'label': '퇴직급여 종류·공제 확인', 'reason': reason,
                'fields': [row['key']], 'status': 'needs_review', 'blocks_first_draft': False,
                'requires_staff_confirmation': True, 'auto_request': False, 'document_ids': [row['document_id']]})
    retirement_values, retirement_evidence = [], []
    for employer, rows in ordinary_retirement.items():
        value, amount_rows = unique('retirement_expected', rows)
        if value is not None:
            retirement_values.append(value)
            retirement_evidence.extend(amount_rows)
            retirement_evidence.extend(kind for kind in by_key['retirement_kind']
                if kind['document_id'] in {row['document_id'] for row in rows})
    if retirement_values and len(retirement_values) == len(ordinary_retirement) and not retirement_review:
        put('retirement_expected', sum(retirement_values), retirement_evidence,
            'ordinary_severance_gross_deduplicated_by_employer')

    assets = []
    for key, label in [('bank_balance', '예금'), ('housing_deposit', '임차보증금'), ('insurance_surrender', '보험해약환급금')]:
        if type(values.get(key)) is int:
            assets.append({'id': key, 'label': label, 'owned_value': values[key], 'secured_deduction': None,
                'exempt_deduction': None, 'disposal_cost': None, 'evidence_ids': origins[key]['source_ids']})
    if type(values.get('retirement_expected')) is int:
        assets.append({'id': 'retirement_expected', 'label': '일반 퇴직금 예상총액(공제 전)',
            'owned_value': values['retirement_expected'], 'secured_deduction': None,
            'exempt_deduction': None, 'disposal_cost': None,
            'evidence_ids': origins['retirement_expected']['source_ids']})
    for key, target in [('vehicle_ownership', 'vehicle_value'), ('real_estate_ownership', 'real_estate_value')]:
        owned, rows = unique(key)
        if owned is False:
            put(target, 0, rows, 'explicit_no_ownership')
    contracts, rows = unique('insurance_contracts')
    if contracts is False and 'insurance_surrender' not in values:
        put('insurance_surrender', 0, rows, 'explicit_no_contract')
    if assets:
        # This is the sum of observed assets, not a declaration of exhaustive ownership.
        asset_rows = [row for row in facts if row['id'] in {fid for asset in assets for fid in origins[asset['id']]['fact_ids']}]
        put('assets_total', sum(row['owned_value'] for row in assets), asset_rows, 'observed_assets_sum')

    rules = legal_calculator.policy()
    inputs = {key: None for key in ('recognized_household_size', 'additional_living_cost', 'base_living_cost',
        'months', 'prepaid_months', 'monthly_trustee_fee', 'preapproval_costs_paid', 'objection', 'annual_discount_rate', 'living_cost_mode')}
    period_rows = by_key['income_period']
    period = ' / '.join(dict.fromkeys(row['value'] for row in period_rows)) or next((
        f"{row['period_start']}~{row['period_end']}" for row in income_rows if row.get('period_start') and row.get('period_end')), None)
    put('income_period',period,period_rows or income_rows,'explicit_source_periods')
    inputs.update(policy_id=rules['id'], as_of=date.today().isoformat(), assets=assets, creditors=creditors, decisions={},
        income={'kind': 'wage' if monthly and (values.get('employment_type') == '급여소득자' or values.get('employer')) else None,
            'basis': basis, 'monthly_amount': income_value, 'period': period,
            # Net inputs must not deduct tax twice. This zero is algebra, not absence of tax.
            'taxes_and_social_insurance': 0 if basis == 'net' else unique('income_deductions', [r for r in by_key['income_deductions'] if r.get('frequency') == 'monthly'])[0],
            'business_expenses': 0 if monthly and values.get('employment_type') == '급여소득자' else None,
            'evidence_ids': sorted({row['document_id'] for row in income_rows})})
    proposed = [{'key': 'months', 'value': rules['normal_max_months'], 'status': 'proposed',
        'source_ids': ['statute-611'], 'reason': '일반 변제기간 상한을 적용한 검토안이며 기간 특례·수행 가능성 검토가 필요합니다.'}]
    if case.get('court_id') == 'CT01' and values.get('household_size') in range(1, 8):
        people = values['household_size']
        median = rules['median_income_2026'][str(people)]
        base = int((Decimal(median) * Decimal(rules['seoul_base_living_ratio'])).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        proposed.extend([
            {'key': 'recognized_household_size', 'value': people, 'status': 'proposed',
             'source_ids': origins['household_size']['source_ids'], 'reason': '서류상 가구원 수를 출발점으로 한 안이며 법률상 부양인원 인정은 미확정입니다.'},
            {'key': 'base_living_cost', 'value': base, 'status': 'proposed', 'source_ids': ['median-2026', 'seoul-living-2026'],
             'reason': '위 인정인원 제안에 따른 서울 기준 생계비. 실제 지출·추가생계비 판단과 구별합니다.'}])
    review_gaps = _review_gaps(case, values) + retirement_review + [
        {'code': error['code'], 'stage': 'analysis', 'title': '담당자 수정·반려 항목 확인',
         'label': '담당자 수정·반려 항목 확인', 'reason': error['reason'], 'fields': [error.get('key')],
         'status': 'needs_review', 'blocks_first_draft': False, 'requires_staff_confirmation': True,
         'auto_request': False, 'document_ids': [error['document_id']] if error.get('document_id') else []}
        for error in reviewed['errors']]
    return {'version': VERSION, 'sources': original_sources, 'facts': facts, 'observed_facts': observed_facts,
        'human_review_sources': reviewed['sources'], 'human_review_edits': reviewed['edits'],
        'form_values': values, 'origins': origins,
        'inputs': inputs, 'derivations': derivations, 'errors': errors, 'proposed_decisions': proposed,
        'review_gaps': review_gaps,
        'source_signature': store.digest([original_sources, reviewed['edits'], reviewed['errors']])
            if reviewed['edits'] or reviewed['errors'] else store.digest(original_sources),
        'input_revision': case.get('input_revision'),
        'semantic_verification_required': True, 'legal_approval': False}


def candidate_rows(packet):
    """Compatibility projections are code-derived, not new source observations."""
    rows = []
    by_id = {row['id']: row for row in packet['facts']}
    for key, value in packet['form_values'].items():
        if '.' in key:
            continue
        origin = packet['origins'][key]
        evidence = [by_id[fid] for fid in origin['fact_ids'] if fid in by_id]
        if not evidence:
            continue
        rows.append({'id': 'mapped-' + store.digest([key, value, origin])[:16], 'key': key,
            'label': document_facts.LABELS.get(key, key), 'value': value, 'status': 'source_checked',
            'document_id': evidence[0]['document_id'], 'source_ids': origin['source_ids'] + origin.get('review_source_ids', []),
            'quote': evidence[0]['quote'], 'origin': VERSION, 'derivation': origin,
            'typed_fact_ids': origin['fact_ids'], 'source_type': 'human_review' if origin['type'] == 'human_review' else 'case_document'})
    return rows


def form_facts(packet, template_id):
    """Preserve every supported observation in the relevant form's review annex."""
    def target(row):
        key = row['key']
        if key.startswith(('creditor_', 'loan_')) or key == 'tax_arrears':
            return 'D5106'
        if key.startswith(('bank_', 'retirement_')) or key in {'cash_balance', 'account_key', 'account_scope', 'institution',
                'insurance_contracts', 'insurance_surrender', 'insurance_name', 'insurance_policy',
                'real_estate_ownership', 'vehicle_ownership', 'housing_ownership'}:
            return 'D5101'
        if key.startswith('income_') or key in {'living_expenses', 'pension_assessed_income'}:
            return 'D5103'
        if key.startswith(('employment_', 'employer', 'job_')) or key in {'health_qualification', 'pension_membership'}:
            return 'D5115'
        if key in {'client_name', 'resident_id', 'address', 'postal_code', 'phone'}:
            return 'D5100'
        return 'D5105'
    return [copy.deepcopy(row) for row in packet['facts'] if target(row) == template_id]


def mapping(sources_input, context=None):
    """Calculator interface using the same parser; no model call or legal defaults."""
    case = {'documents': [{'id': row.get('document_id') or row['id'], 'status': 'verified', 'text': row['text'],
        'version': row.get('version', 1)} for row in sources_input], **(context or {})}
    packet = build(case)
    if not packet['facts']:
        return None
    inputs = packet['inputs']
    inputs.update({key: context[key] for key in ('as_of', 'policy_id') if (context or {}).get(key)})
    return {'status': 'extracted' if not packet['errors'] else 'needs_review', 'inputs': inputs,
        'evidence': [], 'errors': packet['errors'], 'typed_facts': packet['facts'],
        'deterministic_verification': {'passed': True, 'scope': 'typed_original_pages_units_and_arithmetic'},
        'semantic_verification_required': True, 'semantic_review_nonblocking_for_first_draft': True,
        'proposed_decisions': packet['proposed_decisions'], 'input_sha256': store.digest(sources_input),
        'external_processing': False, 'version': VERSION}
