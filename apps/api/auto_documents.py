"""Automatic versioned document artifacts. No filing or human-approval impersonation."""
import hashlib
import inspect
import re

import pymupdf

from . import court_forms, drafting, store

VERIFICATION_VERSION = 'rendered-fields-original-context-v4'
VERIFICATION_SCOPE = 'mapped_fields_and_original_context'

# Pin the meaning of the only preselected factual alternatives in this original.
# D5110 is the income-only plan variant. These boxes describe plan funding, not
# whether the debtor owns assets; liquidation value and disposal_cost are not
# property-sale contributions. Page indices are those of the public original.
_SELECTED_CLAUSES = {'D5110': [
    {'page': 1, 'key': 'income_only_funding',
     'text': '나. 재산 : [ 해당 사항 있음 □ / 해당 사항 없음 ☑]'},
    {'page': 3, 'key': 'no_property_sale_funding',
     'text': '나. 재산의 처분에 의한 변제 [ 해당 사항 있음 □/ 해당 사항 없음 ☑]'},
]}
_CHECKMARK_INSTRUCTIONS = {
    'D5103': [{'page': 0, 'text': '해당란에 ☑표시'}],
    'D5105': [{'page': 2, 'text': '해당란에 ☑표시 후 기재합니다.'}],
}


def _compact(value):
    return re.sub(r'\s+', '', str(value))


def _funding_evidence(case, calculation):
    """Describe the actual code schedule, without inferring absent client facts."""
    issues = []
    contribution_keys = {'planned_asset_sale', 'asset_sale_proceeds', 'sale_proceeds',
                         'property_contribution', 'asset_contribution', 'lump_sum_repayment'}

    def contributions(value, prefix=''):
        found = []
        if isinstance(value, dict):
            for key, item in value.items():
                path = prefix + '.' + key if prefix else key
                if key in contribution_keys and item not in (None, False, 0, '', '0'):
                    found.append(path)
                found.extend(contributions(item, path))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                found.extend(contributions(item, f'{prefix}.{index}'))
        return found

    unsupported = contributions(calculation.get('inputs', {}))
    unsupported += [f"facts.{fact.get('id', index)}" for index, fact in enumerate(case.get('facts', []))
                    if fact.get('status') == 'confirmed' and fact.get('key') in contribution_keys
                    and fact.get('value') not in (None, False, 0, '', '0')]
    if unsupported:
        issues.append({'code': 'FORM_FUNDING_VARIANT_UNSUPPORTED', 'fields': unsupported,
                       'reason': '재산 처분대금·일시납 투입 계획은 가용소득만 변제 원본의 해당없음 표시와 함께 자동 검증할 수 없습니다.'})
    summary, schedule = calculation.get('summary', {}), calculation.get('schedule', [])
    capacity, months, total = (summary.get(key) for key in ('monthly_creditor_capacity', 'months', 'total_creditor_payment'))
    known = (type(capacity) is int and capacity > 0 and type(months) is int and months > 0
             and type(total) is int and isinstance(schedule, list) and len(schedule) == months
             and all(isinstance(row, dict) and type(row.get('creditor_payment')) is int
                     and 0 <= row['creditor_payment'] <= capacity for row in schedule))
    known = known and sum(row['creditor_payment'] for row in schedule) == total
    if not known:
        issues.append({'code': 'FORM_FUNDING_BASIS_MISSING',
                       'reason': '변제회차별 계산과 월 가용소득 한도로 원본의 변제재원 유형을 확인해야 합니다.'})
    return {'funding_kind': 'monthly_disposable_income_only' if known and not unsupported else 'unverified',
            'statement': '현재 계산안은 각 회차의 월 실제 가용소득 한도 안에서만 변제액을 배정합니다. 재산의 소유 여부를 뜻하지 않습니다.',
            'monthly_creditor_capacity': capacity, 'months': months,
            'total_creditor_payment': total,
            'schedule_conservation_verified': bool(known)}, issues


def _original_context(record, preview, pdf):
    """Read hash-checked public originals, never a generated page as evidence."""
    template_id = record.get('template_id')
    if not template_id:
        return [], [], [], []  # Minimal synthetic artifacts have no public template.
    registered = court_forms.TEMPLATES.get(template_id)
    if not registered or preview['template']['pages'] != registered['pages']:
        raise ValueError('ORIGINAL_TEMPLATE_MISMATCH')
    expected_hash = registered['source']['sha256']
    if record.get('original_sha256') != expected_hash or preview.get('original_sha256') != expected_hash:
        raise ValueError('ORIGINAL_TEMPLATE_HASH_MISMATCH')
    sources, pages, clauses, issues = [], [], [], []
    with pymupdf.open(court_forms.original_path(template_id)) as original:
        for target_page, source_page in enumerate(registered['pages']):
            text, rendered = original[source_page].get_text(), pdf[target_page].get_text()
            source_id = f'court-original:{template_id}:p{source_page}'
            sources.append({'id': source_id, 'text': text, 'sha256': expected_hash,
                            'url': registered['source']['url'], 'role': 'public_form_not_customer_evidence'})
            pages.append({'page': source_page, 'source_id': source_id,
                          'original_text': text, 'rendered_text': rendered})
            # Rendered text must not silently acquire or lose preselected choices.
            if text.count('☑') != rendered.count('☑'):
                issues.append({'code': 'ORIGINAL_SELECTION_CHANGED', 'page': source_page})
        by_page = {page['page']: page for page in pages}
        expected_selections = _SELECTED_CLAUSES.get(template_id, [])
        instructions = _CHECKMARK_INSTRUCTIONS.get(template_id, [])
        # A checkmark printed in "해당란에 ☑표시" is an instruction example,
        # not a preselected factual answer. Its original page is still reviewed.
        for instruction in instructions:
            if _compact(instruction['text']) not in _compact(by_page[instruction['page']]['original_text']):
                issues.append({'code': 'ORIGINAL_INSTRUCTION_MAPPING_CHANGED', 'page': instruction['page']})
        if sum(page['original_text'].count('☑') for page in pages) != len(expected_selections) + len(instructions):
            issues.append({'code': 'ORIGINAL_SELECTION_REVIEW_REQUIRED',
                           'reason': '등록된 적용성 검증 범위 밖의 원본 선택 표시가 있습니다.'})
        for clause in expected_selections:
            page = by_page[clause['page']]
            if _compact(clause['text']) not in _compact(page['original_text']):
                issues.append({'code': 'ORIGINAL_CLAUSE_MAPPING_CHANGED', 'key': clause['key']})
            if _compact(clause['text']) not in _compact(page['rendered_text']):
                issues.append({'code': 'ORIGINAL_CLAUSE_CHANGED', 'key': clause['key'], 'page': clause['page']})
            clauses.append({**clause, 'source_id': page['source_id'],
                            'rendered_text': page['rendered_text']})
    return sources, pages, clauses, issues


def prepare(case, draft, calculation=None, *, render_case=None):
    preparation_case = render_case if render_case is not None else case
    calculation_id = calculation.get('id') if calculation else None
    folder = store.DATA_DIR / 'generated' / case['id']
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (draft['id'] + '.docx')).write_bytes(drafting.to_docx(case, draft))
    (folder / (draft['id'] + '.html')).write_text(drafting.to_html(case, draft), encoding='utf-8')
    results, issues = [], []
    for template in court_forms.catalog(case.get('court_id'))['templates']:
        # A court's blank checklist is a reference, not a generated case document.
        if not template.get('fields'):
            continue
        template_id = template['id']
        try:
            preview = court_forms.preview(preparation_case, template_id, calculation=calculation)
            pdf = court_forms.render_pdf(template_id, preparation_case, calculation=calculation)
        except (OSError, ValueError, KeyError) as exc:
            issues.append({'template_id': template_id, 'code': 'FORM_GENERATION_FAILED', 'reason': type(exc).__name__})
            continue
        document_id = store.uid('court-doc')
        (folder / (document_id + '.pdf')).write_bytes(pdf)
        record = {'id': document_id, 'template_id': template_id, 'created_at': store.now(),
                  'created_by': 'automation', 'input_revision': case['input_revision'], 'status': 'draft',
                  'stale': False, 'fields': {}, 'calculation_id': calculation_id,
                  'draft_id': draft['id'], 'preview': preview, 'original_sha256': preview['original_sha256'],
                  'sha256': hashlib.sha256(pdf).hexdigest(), 'approval': None,
                  'ai_review': {'status': 'pending', 'passed': False, 'scope': VERIFICATION_SCOPE},
                  'stage_status': 'preliminary', 'human_review_required': True, 'submission_ready': False,
                  'scope': '담당자 검토용 1차 자동 작성본 · 미확정 값 보완·서명·제출·인가 전'}
        record['content_hash'] = store.digest({'fields': {}, 'preview': preview,
            'input_revision': case['input_revision'], 'calculation_id': calculation_id})
        results.append(record)
        if preview['missing_fields'] or preview['overflow_fields']:
            issues.append({'template_id': template_id, 'code': 'FORM_FIELDS_REQUIRED',
                           'reason': '공식 서식의 누락·배치 확인',
                           'missing_fields': preview['missing_fields'], 'overflow_fields': preview['overflow_fields']})
    case.setdefault('court_documents', []).extend(results)
    draft['court_document_ids'] = [r['id'] for r in results]
    draft['artifact_status'] = 'created' if results else 'unavailable'
    draft['form_issues'] = issues
    return results, issues


async def verify_artifacts(case, records, calculation, progress=None):
    """Independently check the rendered PDF text against evidence and code results.

    A draft's pass is never copied to its PDF. Generated pages are comparison
    targets only, not evidence sources that can prove their own correctness.
    """
    import copy
    from .verification import run_local_verification_batched
    from .legal_watch import case_policy_status
    from . import evidence_mapping, statement_authoring
    results, issues = [], []
    legal_signature = case_policy_status(case)['signature']
    verification_unavailable = None
    calculation = calculation or {}
    documents = [d for d in case.get('documents', []) if d.get('status') not in {'rejected', 'quarantined', 'superseded'} and d.get('text')]
    sources = [{'id': d['id'], 'text': d['text'], 'version': d.get('version', 1)} for d in documents]
    # Recompute from the independently submitted originals, never from a preview
    # or from generated PDF text. Arithmetic displays have their own code source.
    packet = evidence_mapping.build(case)
    sources.extend(copy.deepcopy(packet.get('human_review_sources', [])))
    typed_by_id = {row['id']: row for row in packet['facts']}
    typed_code_sources = {}
    for key, origin in packet['origins'].items():
        if origin['operation'] == 'source_observation':
            continue
        operands = [typed_by_id[fid] for fid in origin['fact_ids'] if fid in typed_by_id]
        source_id = 'arithmetic:' + store.digest([key, packet['source_signature']])[:20]
        typed_code_sources[key] = source_id
        sources.append({'id': source_id, 'kind': 'code_calculation', 'version': packet['version'],
            'text': store.dumps({'field': key, 'value': packet['form_values'][key], 'operation': origin['operation'],
                'operands': [{'key': row['key'], 'value': row['value'], 'quote': row['quote'],
                              'source_id': row['source_id'] if row.get('source_type') == 'human_review' else row['document_id'],
                              'source_type': row.get('source_type'), 'document_id': row['document_id'],
                              'page': row['page']} for row in operands]}),
            'source_signature': packet['source_signature'], 'original_source_ids': origin['source_ids']})
    calc_id = 'calculation:' + calculation['id'] if calculation.get('id') else None
    summary = calculation.get('summary', {})
    # These are independently derived display conversions, still attributed to
    # the calculator output rather than to preview values or generated pages.
    derived = {}
    monthly = summary.get('net_monthly_income')
    if type(monthly) is int:
        derived.update(annual_income=monthly * 12, income_manwon=monthly / 10000)
    if type(summary.get('base_living_cost')) is int and type(summary.get('additional_living_cost')) is int:
        derived['recognized_living_cost'] = summary['base_living_cost'] + summary['additional_living_cost']
    if type(summary.get('unsecured_debt')) is int and type(summary.get('secured_debt')) is int:
        derived['total_debt'] = summary['unsecured_debt'] + summary['secured_debt']
    retirement = next((row for row in calculation.get('asset_calculations', [])
                       if row.get('id') == 'retirement_expected'), None)
    assets = calculation.get('inputs', {}).get('assets', [])
    if retirement:
        derived['retirement_value'] = retirement.get('liquidation_value')
        retirement_input = next((row for row in assets if row.get('id') == 'retirement_expected'), None)
        if retirement_input and all(row.get('secured_deduction') == 0 and row.get('disposal_cost') == 0
                                    and type(row.get('exempt_deduction')) is int for row in assets):
            derived['assets_total'] = sum(row['owned_value'] for row in assets) - retirement_input['exempt_deduction']
            derived['exempt_value'] = sum(row['exempt_deduction'] for row in assets if row['id'] != 'retirement_expected')
    if calc_id:
        sources.append({'id': calc_id, 'text': store.dumps({'summary': summary, 'display_conversions': derived,
            'asset_calculations': calculation.get('asset_calculations', []), 'asset_inputs': assets,
            'recognized_household_size': calculation.get('inputs', {}).get('recognized_household_size'),
            'living_cost_mode': calculation.get('inputs', {}).get('living_cost_mode'),
            'median_percent': 60 if calculation.get('inputs', {}).get('living_cost_mode') == 'seoul_median_60' else None})})
    funding, funding_issues = _funding_evidence(case, calculation)
    funding_id = (calc_id or 'calculation:unavailable') + ':funding'
    sources.append({'id': funding_id, 'text': store.dumps(funding)})
    for index, row in enumerate(calculation.get('inputs', {}).get('creditors', [])):
        sources.append({'id': f'{calc_id}:creditors:{index}', 'text': store.dumps({'number': index + 1, **row})})
    for index, row in enumerate(calculation.get('creditor_allocations', [])):
        payments = [next((value.get('total') for value in step.get('allocations', []) if value.get('creditor_id') == row.get('creditor_id')), None)
                    for step in calculation.get('schedule', [])]
        sources.append({'id': f'{calc_id}:allocations:{index}', 'text': store.dumps({'number': index + 1, **row,
            'payments': payments, 'periodic_payment': '회차별 별지' if len(set(payments)) > 1 else payments[0] if payments else None})})
    workflow_id = 'case:identity'
    sources.append({'id': workflow_id, 'text': store.dumps({key: case.get(key) for key in ('client_name', 'court_name', 'court_id')})})
    from .intake_workflow import record_text
    notes = record_text(case)
    if notes and case.get('consultation', {}).get('status') != 'quarantined':
        sources.append({'id': 'consultation:notes', 'text': notes})
    current_narrative = statement_authoring.current_verified_draft(case, require_semantic=False)
    if current_narrative:
        sources.extend(current_narrative.get('narrative_sources', []))
    # A single source can serve several identical fields. IDs must still be
    # unique for strict source binding in the local verification boundary.
    sources = list({source['id']: source for source in sources}.values())
    allowed = {source['id'] for source in sources}
    # Free-form document edits are comparison targets, never their own proof.
    # Bound extraction corrections above remain explicit staff audit records;
    # they do not relabel the original OCR as correct or bypass an AI check.
    correction_evidence = [d['id'] for d in documents if d.get('status') == 'verified']
    if 'consultation:notes' in allowed:
        correction_evidence.append('consultation:notes')
    folder = (store.DATA_DIR / 'generated' / case['id']).resolve()
    for record_index, record in enumerate(records):
        async def report_batch(batch=None, finished=False):
            if progress:
                title = court_forms.TEMPLATES.get(record.get('template_id'), {}).get('title') or '작성 문서'
                detail = (f" · 원문 대조 {batch['completed']}/{batch['total']}" if batch else '')
                emitted = progress({'completed': record_index + int(finished), 'total': len(records),
                                    'label': f'{title}{detail}'})
                if inspect.isawaitable(emitted):
                    await emitted
        await report_batch()
        record_issues, items = [], []
        public_sources, original_pages, selected_clauses = [], [], []
        field_count, appendix_pages = 0, 0
        path = (folder / (record['id'] + '.pdf')).resolve()
        result = None
        try:
            if path.parent != folder:
                raise ValueError('INVALID_ARTIFACT_PATH')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != record.get('sha256'):
                raise ValueError('ARTIFACT_HASH_MISMATCH')
            preview = record['preview']
            template = preview['template']
            page_map = {source_page: index for index, source_page in enumerate(template['pages'])}
            if preview.get('missing_fields') or preview.get('overflow_fields'):
                record_issues.append({'code': 'FORM_FIELDS_REQUIRED', 'missing_fields': preview.get('missing_fields'), 'overflow_fields': preview.get('overflow_fields')})
            with pymupdf.open(stream=raw, filetype='pdf') as pdf:
                public_sources, original_pages, selected_clauses, original_issues = _original_context(record, preview, pdf)
                record_issues.extend(original_issues)
                appendix_pages = max(0, len(pdf) - len(template['pages']))
                if selected_clauses:
                    record_issues.extend(funding_issues)
                page_refs = {}
                for field_index, field in enumerate(preview.get('fields', [])):
                    value = field.get('value')
                    if value is None or value == '':
                        continue
                    page = pdf[page_map[field['page']]]
                    rendered = page.get_text('text', clip=pymupdf.Rect(field['rect']))
                    expected = court_forms._display(value, field['key'])
                    normalized = lambda text: re.sub(r'\s+', '', str(text)).replace(',', '') if type(value) in {int, float} else re.sub(r'\s+', '', str(text))
                    matches = bool(re.search(r'(?<![\d.])' + re.escape(normalized(expected)) + r'(?![\d.])', normalized(rendered))) if type(value) in {int, float} else normalized(expected) in normalized(rendered)
                    if not matches:
                        record_issues.append({'code': 'RENDERED_FIELD_MISMATCH', 'key': field['key'], 'page': field['page']})
                    origin = field.get('source', {})
                    refs = []
                    if origin.get('type') == 'proposed_legal_input':
                        record_issues.append({'code': 'PROPOSED_LEGAL_INPUT_REVIEW', 'key': field['key'],
                            'reason': '법률 판단 제안은 계산·변호사 승인 전이며 원문 사실과 구별합니다.'})
                        continue
                    if origin.get('type') in {'deterministic_evidence', 'human_review'}:
                        fresh_origin = packet['origins'].get(field['key'], {})
                        if (packet['form_values'].get(field['key']) != value or fresh_origin != origin
                                or preview.get('evidence_source_signature') != packet['source_signature']):
                            record_issues.append({'code': 'DETERMINISTIC_FIELD_CHANGED', 'key': field['key']})
                            continue
                        refs = list(origin['source_ids'])
                        if origin.get('type') == 'human_review':
                            refs.extend(origin.get('review_source_ids', []))
                            if not origin.get('review_source_ids'):
                                record_issues.append({'code': 'ARTIFACT_FIELD_EVIDENCE_MISSING', 'key': field['key']})
                                continue
                        if field['key'] in typed_code_sources:
                            refs.insert(0, typed_code_sources[field['key']])
                    elif origin.get('type') in {'legal_calculation', 'legal_calculation_input', 'creditor_list_order'}:
                        refs = [calc_id]
                        match = re.match(r'(creditors|allocations)\.(\d+)\.', field['key'])
                        if match:
                            refs = [f'{calc_id}:{match[1]}:{match[2]}']
                    elif origin.get('document_id') in allowed:
                        refs = [origin['document_id']]
                    elif origin.get('type') == 'consultation' and 'consultation:notes' in allowed:
                        refs = ['consultation:notes']
                    elif origin.get('type') == 'grounded_narrative':
                        refs = [source_id for source_id in origin.get('source_ids', []) if source_id in allowed]
                    elif field['key'] in {'client_name', 'court_prefix'}:
                        refs = [workflow_id]
                    elif field['key'] in derived:
                        refs = [calc_id]
                    elif origin.get('type') == 'confirmed_fact':
                        fact = next((fact for fact in case.get('facts', []) if fact.get('id') == origin.get('id')), {})
                        refs = [source_id for source_id in fact.get('evidence_ids', []) if source_id in allowed]
                    elif origin.get('type') in {'staff_input', 'address_candidate'}:
                        refs = list(correction_evidence)
                    if not refs or any(ref not in allowed for ref in refs):
                        record_issues.append({'code': 'ARTIFACT_FIELD_EVIDENCE_MISSING', 'key': field['key']})
                        continue
                    field_count += 1
                    page_refs.setdefault(field['page'], set()).update(refs)
                    # A semantic key can appear repeatedly, even twice on the
                    # same page. Verification IDs identify the physical field.
                    items.append({'id': f"{record['id']}:field:{field_index}:p{field['page']}:{field['key']}", 'key': field['key'],
                        'value': value, 'source_ids': refs, 'rendered_text': rendered,
                        'comparison': '실제 PDF 필드의 판독문을 값 및 근거자료와 대조. 판독문에 값이 없으면 supported 금지.'})
                for page in original_pages:
                    items.append({'id': f"{record['id']}:original-page:{page['page']}",
                        'key': 'original_form_context', 'value': '원본 문구와 작성값의 문맥·배치 대조',
                        'source_ids': [page['source_id'], *sorted(page_refs.get(page['page'], set()))],
                        'rendered_text': page['rendered_text'],
                        'comparison': '공식 원본과 실제 작성 페이지를 함께 비교합니다. 작성값이 다른 항목의 답변으로 읽히거나 기존 문구·표시와 모순되면 mismatch입니다. 원본에 있던 빈칸·미선택 동의·서명은 고객 사실을 뜻하지 않으며 제출 준비 완료를 판단하지 않습니다. 원본은 서식 문구의 근거일 뿐 고객 사실의 증거가 아닙니다.'})
                for clause in selected_clauses:
                    items.append({'id': f"{record['id']}:original-clause:{clause['key']}",
                        'key': 'original_clause_applicability',
                        'value': '현재 작성된 계산안의 변제재원은 월 가용소득이며 재산 처분대금 투입은 계산되어 있지 않습니다.',
                        # Only independent schedule evidence may support the
                        # clause's applicability; original wording cannot.
                        'source_ids': [funding_id], 'rendered_text': clause['text'],
                        'original_page': clause['page'], 'original_source_id': clause['source_id'],
                        'comparison': '해당없음 표시는 계산안의 변제재원 유형만 뜻합니다. 재산이 없다는 진술로 해석하면 안 됩니다. funding_kind가 unverified이거나 별도 재산 처분대금 투입 계획이 있으면 supported 금지.'})
            if not field_count:
                record_issues.append({'code': 'NO_RENDERED_FIELDS',
                                      'reason': '원본 문맥만 대조한 빈 서식을 작성 항목 검증 완료로 처리하지 않습니다.'})
                # There is no authored value for an AI to compare. The hash and
                # original selection checks above still ran, and this artifact
                # remains incomplete. Do not spend one call per blank page.
                result = {'status': 'needs_review', 'passed': False, 'findings': [], 'attempted': False,
                          'error': {'code': 'NO_RENDERED_FIELDS', 'message': '작성할 항목을 보완한 뒤 AI로 재검토하세요.'}}
            elif items and verification_unavailable:
                result = {'status': 'unavailable', 'passed': False, 'findings': [],
                          'attempted': False, 'error': verification_unavailable}
            elif items:
                patterns = case.get('strategy_analyses', [])[-1].get('outcome_patterns', []) if case.get('strategy_analyses') else []
                result = await run_local_verification_batched('document', {'sources': sources + public_sources, 'items': items,
                    'context': {'prior_outcome_checks': [p for p in patterns if p.get('template_id') == record.get('template_id')],
                                'scope': '과거 결과는 누락·증빙 대조 우선순위로만 사용. 현재 원문 근거를 대체하지 않는다. human_review는 현재 원문에 연결된 담당자 교정 기록이며, 수정 후 값의 출력 일치와 기존 OCR과의 차이를 구분해 확인한다. 교정 기록을 원문 자체나 AI 통과로 간주하지 않는다.'}},
                    **({'progress': report_batch} if progress else {}))
                if result.get('status') == 'unavailable':
                    verification_unavailable = result.get('error') or {
                        'code': 'LOCAL_REVIEW_UNAVAILABLE', 'message': 'AI 검토에 연결하지 못했습니다. 문서는 보존되며 재검토할 수 있습니다.'}
            else:
                result = {'status': 'needs_review', 'passed': False, 'findings': [],
                          'error': {'code': 'NO_RENDERED_FIELDS', 'message': '자동 대조할 작성 필드가 없습니다.'}}
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            record_issues.append({'code': 'ARTIFACT_VERIFICATION_UNAVAILABLE', 'error_type': type(exc).__name__})
            result = {'status': 'unavailable', 'passed': False, 'findings': []}
        if record_issues:
            result.update(passed=False, status='needs_review' if result.get('status') != 'unavailable' else 'unavailable')
        result.update(scope=VERIFICATION_SCOPE, artifact_sha256=record.get('sha256'),
                      input_revision=case['input_revision'], deterministic_issues=record_issues,
                      legal_dependency_signature=legal_signature,
                      at=store.now(), self_source_used=False, verification_version=VERIFICATION_VERSION,
                      scope_label='작성 필드·원본 문맥 및 등록된 선택 조항 대조',
                      coverage={'mapped_field_occurrences': field_count, 'original_pages': len(original_pages),
                                'selected_original_clauses': len(selected_clauses),
                                'appendix_pages_not_independently_reviewed': appendix_pages},
                      whole_document_verified=False, submission_ready=False,
                      deferred_checks=['미매핑 빈칸 및 별지 전체 행', '서명·날인 및 법률상 진술·동의', '법원 제출요건 최종 확인'])
        record['ai_review'] = result
        record['status'] = 'automatically_verified' if result['passed'] else 'verification_required'
        results.append({'document_id': record['id'], **result})
        issues.extend({'document_id': record['id'], **issue} for issue in record_issues)
        await report_batch(finished=True)
    passed = bool(records) and all(result.get('passed') for result in results)
    return {'passed': passed, 'status': 'passed' if passed else 'unavailable' if any(result['status'] == 'unavailable' for result in results) else 'needs_review',
            'records': results, 'issues': issues, 'scope': VERIFICATION_SCOPE,
            'whole_document_verified': False, 'submission_ready': False, 'external_processing': False}
