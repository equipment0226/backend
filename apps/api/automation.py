"""Evidence-gated AX orchestration. All private text stays on the local boundary.

The gate authorizes document preparation, never predicts or records court approval.
Expensive work runs on a detached case; ax_service commits it with optimistic locking.
"""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timezone, timedelta

from . import domain, drafting, legal_calculator, store, workflow_contract

VERSION = 'ax-loop-2026-10-04.8-evidence-dashboard-graph'
STEPS = [(stage['id'], stage['title']) for stage in workflow_contract.STAGES]
INACTIVE = {'withdrawn', 'superseded', 'cancelled'}


def notify(case, key, title, message, audience='staff', kind='review_request', request_id=None):
    rows = case.setdefault('notifications', [])
    existing = next((n for n in rows if n.get('key') == key and not n.get('resolved_at')), None)
    if existing:
        return existing
    row = {'id': store.uid('notice'), 'key': key, 'title': title, 'message': message,
           'audience': audience, 'kind': kind, 'request_id': request_id,
           'created_at': store.now(), 'read_at': None, 'resolved_at': None}
    rows.append(row)
    return row


def _state(case, stage, index, reasons=None, strategies=None):
    from . import legal_watch
    legal_update = legal_watch.case_policy_status(case)
    status = 'completed' if stage == 'completed' else 'waiting' if stage in ('collecting', 'verification_waiting', 'human_review') else 'blocked'
    steps = workflow_contract.project_steps(index, status)
    case['ax_pipeline'] = {'version': VERSION, 'contract_version': workflow_contract.VERSION, 'stage': stage, 'status': status,
                           'steps': steps, 'reasons': reasons or [], 'strategies': strategies or [],
                           'updated_at': store.now(), 'input_revision': case['input_revision'],
                           'legal_update': {**legal_update, 'version': legal_update.get('active_version'),
                               'summary': '적용 근거 변경 검토 필요' if legal_update.get('pending_changes') else '현재 적용 근거 반영'}}
    checkpoint = workflow_contract.checkpoint(case['ax_pipeline'])
    history = case.setdefault('workflow_checkpoints', [])
    if not history or any(history[-1].get(key) != checkpoint.get(key) for key in
                          ('stage', 'status', 'input_revision', 'legal_version', 'reason_codes')):
        history.append(checkpoint)
    case['stage'] = {'collecting': '자료 수집', 'lawyer_review': '검토 보류',
                     'human_review': '1차 문서 검토', 'completed': '문서 작성 완료'}.get(stage, '검증 중')
    case['automation'] = {'stage': stage, 'label': case['stage'], 'generated_at': store.now()}
    if stage in ('lawyer_review', 'human_review'):
        message = '\n'.join(str(r.get('reason', r.get('message', ''))) if isinstance(r, dict) else str(r) for r in reasons or [])
        if stage == 'human_review' and not message:
            message = '1차 문서 작성과 AI 검토를 마쳤습니다. 문서 내용을 확인하고 보완·최종 승인을 진행해 주세요.'
        notify(case, 'review:' + store.digest([case['input_revision'], reasons]), '사건 검토 요청', message)
    elif stage == 'completed':
        for n in case.get('notifications', []):
            if n.get('kind') == 'review_request' and not n.get('resolved_at'):
                n['resolved_at'] = store.now()
    return case['ax_pipeline']


def _human_review(case, draft, reasons=None, strategies=None, review_required_stages=()):
    """Finish autonomous preparation, without silently completing failed gates."""
    pending = copy.deepcopy(reasons or draft.get('review_pending', []))
    draft.update(stage_status='preliminary', human_review_required=True, submission_ready=False,
                 review_pending=pending)
    required = set(review_required_stages) | set(draft.get('review_required_stages', []))
    pipeline = _state(case, 'human_review', 6, pending, strategies)
    pipeline.update(draft_id=draft['id'], human_review_required=True, submission_ready=False,
                    review_required_stages=sorted(required))
    for step in pipeline['steps']:
        if step['id'] in required:
            step['status'] = 'review_required'
            step['detail'] = '자동 검증 미완료 항목을 1차 초안과 함께 담당자가 보완해야 합니다.'
    case['workflow_checkpoints'][-1] = workflow_contract.checkpoint(pipeline)
    case['automation'].update(draft_id=draft['id'], label='1차 문서 작성·AI 검토 후 담당자 보완·승인 대기')
    return pipeline


def sync_request_notifications(case):
    for req in case.get('requests', []):
        if req.get('status') in INACTIVE | {'fulfilled'}:
            for notice in case.get('notifications', []):
                if notice.get('request_id') == req['id'] and not notice.get('resolved_at'):
                    notice['resolved_at'] = store.now()
        elif req.get('status') in ('requested', 'needs_more'):
            message = req.get('public_review_note') or f"{req['title']} · {req.get('period', '')} 자료를 제출해주세요."
            notify(case, 'request:' + store.digest([req['id'], req.get('version'), req['status'], message]),
                   '서류 보완 요청' if req['status'] == 'needs_more' else '서류 제출 요청',
                   message, audience='client', kind='document_request', request_id=req['id'])


def _sources(case, verified_only=False, packet=None):
    from .extraction_readiness import active_documents
    def page_ranges(document):
        ranges, cursor = [], 0
        text = document.get('text', '')
        for index, page in enumerate(document.get('page_texts', []), 1):
            page_text = page.get('text')
            number = page.get('page', index)
            if not isinstance(page_text, str) or not page_text or type(number) is not int:
                continue
            start = text.find(page_text, cursor)
            if start < 0:
                continue
            end = start + len(page_text)
            ranges.append({'page':number, 'start':start, 'end':end})
            cursor = end
        return ranges
    sources = [{'id': d['id'], 'text': d.get('text', ''), 'version': d.get('version', 1),
             'kind': 'reviewed_decision' if d.get('source_type') == 'reviewed_decision' and d.get('verified_by') else 'case_document',
             'page_ranges': page_ranges(d)}
            for d in active_documents(case) if d.get('status') not in
            {'rejected', 'quarantined', 'superseded'} and (not verified_only or d.get('status') == 'verified')
            and d.get('source_type') != 'meeting' and d.get('text', '').strip()]
    from . import evidence_mapping
    allowed = {source['id'] for source in sources}
    sources.extend(copy.deepcopy(source) for source in (packet if packet is not None else evidence_mapping.build(case)).get('human_review_sources', [])
                   if source.get('document_id') in allowed)
    return sources


def _verification_items(case, sources, packet=None):
    allowed = {s['id'] for s in sources}
    if packet is not None or case.get('evidence_mapping'):
        from . import evidence_mapping
        packet = packet if packet is not None else evidence_mapping.build(case)
        # Repeated identical observations share semantic review while every
        # original typed row keeps its independent page/unit check and provenance.
        result = {}
        for row in packet['facts']:
            if row['document_id'] not in allowed or row.get('value') is None:
                continue
            source_id = row['source_id'] if row.get('source_type') == 'human_review' else row['document_id']
            if source_id not in allowed:
                continue
            key = store.dumps([row['key'], row['value'], row.get('basis'), row.get('frequency'),
                               row.get('period_start'), row.get('period_end'), row.get('account_key'),
                               source_id if row.get('source_type') == 'human_review' else None])
            item = result.setdefault(key, {'id': row['id'], 'key': row['key'], 'value': row['value'],
                'source_ids': [], 'quote': row['quote'], 'source_quotes': {}, 'source_evidence': {}, 'covered_fact_ids': [],
                'basis': row.get('basis'), 'frequency': row.get('frequency'), 'unit': row.get('unit'),
                'period_start': row.get('period_start'), 'period_end': row.get('period_end'),
                'period_quote': row.get('period_quote'),
                'source_type': row.get('source_type', 'case_document'),
                'original_source_id': row.get('original_source_id'),
                'source_unit_quote': row.get('source_unit_quote'), 'unit_multiplier':row.get('unit_multiplier',1)})
            if source_id not in item['source_ids']:
                item['source_ids'].append(source_id)
            # Merged equal values can have different wording in each original.
            # Keep every exact quotation for bounded per-source AI checks.
            quotes = item['source_quotes'].setdefault(source_id, [])
            if row['quote'] not in quotes:
                quotes.append(row['quote'])
            evidence = item['source_evidence'].setdefault(source_id, [])
            position = {name:row[name] for name in ('quote','page','line_start','line_end',
                'source_unit_quote','unit_multiplier','unit','basis','frequency',
                'period_start','period_end','period_quote') if name in row}
            if position not in evidence:
                evidence.append(position)
            item['covered_fact_ids'].append(row['id'])
        if result:
            return list(result.values())
    return [{'id': c['id'], 'key': c['key'], 'value': c.get('value'),
             'source_ids': [c['document_id']], 'quote': c.get('quote', '')}
            for c in case.get('extraction_candidates', []) if c.get('document_id') in allowed
            and c.get('status') not in {'rejected', 'quarantined', 'superseded'} and c.get('value') is not None]


def _need_more(case, req, reason):
    req.update(status='needs_more', public_review_note=reason)
    history = req.setdefault('validation_history', [])
    if not history or history[-1].get('reason') != reason:
        history.append({'at': store.now(), 'status': 'needs_more', 'reason': reason})


def document_review_signature(case, document):
    """Bind a staff scope review to this immutable file, OCR and request scope."""
    request = next((r for r in case.get('requests', []) if r.get('id') == document.get('request_id')), {})
    account = next((a for a in case.get('financial_accounts', case.get('accounts', []))
                    if isinstance(a, dict) and str(a.get('id') or a.get('account_key') or '') == str(request.get('account_key'))), {})
    return store.digest({'version': 'manual-source-scope-v1', 'document': {
        key: document.get(key) for key in ('id', 'sha256', 'version', 'text', 'page_texts', 'document_metadata', 'request_id')},
        'request': {key: request.get(key) for key in ('id', 'catalog_id', 'period', 'period_start', 'period_end',
            'institution', 'account_key', 'issuance_options', 'scope_unresolved')},
        'person': case.get('client_name'), 'account': account.get('account_number', account.get('number'))})


def manual_document_review_current(case, document):
    """An audited human review can settle scope, but never replace OCR fact checks.

    Legacy uploads are immutable version-one records. Accept only an explicit
    complete review with no later metadata/file/request change; subsequent reviews
    carry the exact evidence signature and no longer rely on this compatibility.
    """
    if (document.get('status') != 'verified' or not document.get('verified_by') or not document.get('verified_at')
            or not all(document.get(key) is True for key in ('scope_confirmed', 'content_confirmed', 'person_confirmed'))):
        return False
    review = document.get('manual_verification')
    if review:
        return (review.get('signature') == document_review_signature(case, document)
                and review.get('verified_by') == document.get('verified_by')
                and review.get('verified_at') == document.get('verified_at'))
    if document.get('version', 1) != 1 or not document.get('sha256'):
        return False
    request = next((r for r in case.get('requests', []) if r.get('id') == document.get('request_id')), {})
    modified = [document.get('updated_at'), document.get('metadata_review', {}).get('at'), request.get('updated_at')]
    return all(not at or str(at) <= str(document['verified_at']) for at in modified)


def _request_metadata(case, request, document):
    """Parse only the existing request scope from local OCR, retaining exact quotes.

    Do not infer complete statement coverage from the first/last transaction: an
    explicit issuer date-range header is necessary. Opaque account IDs are bound
    to numbers already recorded on this case; they are never model inventions.
    """
    import re
    from datetime import date
    text = document.get('text', '')
    metadata = document.setdefault('document_metadata', dict(document.get('metadata', {})))
    quotes = document.setdefault('metadata_source_quotes', {})
    institution = request.get('institution')
    if institution and institution in text:
        metadata['institution'] = institution
        quotes['institution'] = institution
    account = next((a for a in case.get('financial_accounts', case.get('accounts', []))
                    if isinstance(a, dict) and str(a.get('id') or a.get('account_key') or '') == str(request.get('account_key'))), {})
    number = re.sub(r'\D', '', str(account.get('account_number', account.get('number', ''))))
    if number:
        expression = r'(?<!\d)' + r'[\s-]*'.join(number) + r'(?!\d)'
        match = re.search(expression, text)
        if match:
            metadata['account_key'] = request['account_key']
            quotes['account_key'] = match[0]
    day = r'(20\d{2})\s*[.년/-]\s*(\d{1,2})\s*[.월/-]\s*(\d{1,2})\s*일?'
    issued = re.search(r'(?:발급일자?|발행일자?|출력일자?)\s*[:：]?\s*' + day, text)
    def iso(groups):
        try:
            return date(*(int(g) for g in groups)).isoformat()
        except ValueError:
            return None
    if issued:
        value = iso(issued.groups())
        if value:
            metadata['issued_at'], quotes['issued_at'] = value, issued[0]
    coverage = re.search(r'(?:조회|거래|증명|대상|급여|발급대상)\s*기간\s*[:：]?\s*' + day +
                         r'\s*[.\s]*\s*(?:~|～|〜|부터|–|-)\s*' + day, text)
    if coverage:
        start, end = iso(coverage.groups()[:3]), iso(coverage.groups()[3:])
        if start and end:
            metadata.update(period_start=start, period_end=end)
            quotes.update(period_start=coverage[0], period_end=coverage[0])
    options = request.get('issuance_options', {})
    for key, aliases in {'certificate_type': ['상세'], 'tax_scope': ['모든 세목', '전체 세목'],
                         'jurisdiction_scope': ['전국'], 'address_history': ['과거 주소 전체', '주소변동 전체']}.items():
        if options.get(key):
            found = next((alias for alias in aliases if alias in text), None)
            if found:
                metadata[key], quotes[key] = options[key], found
    if options.get('person_number_display'):
        # Main person's full identifier and third-person suppression are checked
        # together; a globally unmasked family certificate cannot pass this test.
        person = case.get('client_name', '')
        full = r'\d{6}\s*-?\s*[1-4]\d{6}'
        lines = text.splitlines()
        own = next((line for line in lines if person and person in line and re.search(full, line)), None)
        other_full = [line for line in lines if re.search(full, line) and line != own]
        if own and not other_full:
            metadata['person_number_display'] = options['person_number_display']
            quotes['person_number_display'] = own
    return metadata


async def _validate_requests(case, checks, progress=None):
    from . import court_rules, extraction_readiness, request_collection
    from .verification import run_local_verification_batched as run_local_verification
    reqs = [r for r in case.get('requests', []) if r.get('status') not in INACTIVE and not r.get('no_longer_required')]
    check_by_doc = {c['document_id']: c for c in checks}
    sources = _sources(case)
    source_ids = {s['id'] for s in sources}
    pending, unavailable = [], False
    for req in reqs:
        linked = request_collection.active_documents(case, req)
        if not linked:
            if req.get('status') == 'fulfilled':
                _need_more(case, req, '현재 요청 범위를 증명하는 유효한 원본이 없습니다. 해당 자료를 제출해주세요.')
            continue
        identity_conflicts = [doc for doc in linked if check_by_doc.get(doc['id'], {}).get('coverage_status') == 'identity_conflict'
                              or doc.get('automated_check', {}).get('coverage_status') == 'identity_conflict']
        if identity_conflicts:
            for doc in identity_conflicts:
                doc.update(status='quarantined', public_status='명의 확인 후 다시 제출 필요')
            _need_more(case, req, '요청 대상자의 서류인지 확인하고 다시 제출해주세요.')
            continue
        # Every original belongs to the collection until explicitly replaced.
        # Human review of one monthly certificate cannot approve other months.
        if all(manual_document_review_current(case, doc) for doc in linked):
            req['status'] = 'fulfilled'
            continue
        if any(not extraction_readiness.document_state(case, doc)['can_review'] for doc in linked):
            req.update(status='received', public_review_note='제출한 모든 파일의 추출 완료를 기다리고 있습니다.')
            unavailable = True
            continue
        accepted_types = {req['catalog_id'], *req.get('accepted_catalog_ids', [])}
        wrong_types = [doc for doc in linked if not manual_document_review_current(case, doc)
                       and not req['catalog_id'].startswith('SUPPORT-')
                       and check_by_doc.get(doc['id'], {}).get('catalog_id')
                       and check_by_doc[doc['id']]['catalog_id'] not in accepted_types]
        if wrong_types:
            for doc in wrong_types:
                doc.update(status='needs_more', public_status='요청한 종류의 서류가 필요합니다')
            _need_more(case, req, f"요청한 {req['title']}와 다른 서류가 제출되었습니다. 해당 서류로 다시 제출해주세요.")
            continue
        if any(doc['id'] not in source_ids or doc.get('ocr_summary', {}).get('unreadable_pages') for doc in linked):
            _need_more(case, req, '일부 내용을 읽을 수 없습니다. 빠진 쪽 없이 선명한 원본을 다시 제출해주세요.')
            continue
        # Scope is a local evidence constraint, not a conclusion delegated to AI.
        # Account numbers below are used only by the loopback verification call.
        managed = req.get('managed_by') == court_rules.MANAGED_BY
        if managed and req.get('scope_unresolved'):
            req.update(status='received', public_review_note='기관·계좌의 요청 범위를 확인 중입니다.')
            req['metadata_validation'] = {'status': 'metadata_required', 'unknown_fields': ['request_scope']}
            unavailable = True
            continue
        if managed:
            for doc in linked:
                _request_metadata(case, req, doc)
            aggregate = request_collection.combined_metadata(req, linked)
            metadata_check = court_rules.validate_metadata(req, {'document_metadata': aggregate})
            req['metadata_validation'] = metadata_check
            req['collection_metadata'] = aggregate
            if metadata_check['failures']:
                reason = ' '.join(dict.fromkeys(f['message'] for f in metadata_check['failures']))
                _need_more(case, req, reason)
                for doc in linked:
                    if doc.get('status') != 'verified':
                        doc.update(status='needs_more', public_status='요청 기간·기관·발급정보 보완 필요')
                continue
        scope = {k: req.get(k) for k in ('catalog_id', 'title', 'institution', 'account_key',
                  'period', 'period_start', 'period_end', 'issuance_options', 'source_refs')}
        if managed:
            scope['unknown_metadata_fields'] = req.get('metadata_validation', {}).get('unknown_fields', [])
            account = next((a for a in case.get('financial_accounts', case.get('accounts', []))
                            if isinstance(a, dict) and str(a.get('id') or a.get('account_key') or '') == str(req.get('account_key'))), {})
            scope['expected_account_number'] = account.get('account_number', account.get('number'))
        signature = store.digest([[{key: doc.get(key) for key in ('id','sha256','version','text','document_metadata')}
                                   for doc in linked], scope, case.get('client_name'), VERSION])
        prior = req.get('automatic_validation', {})
        if prior.get('signature') == signature and prior.get('status') == 'passed':
            req['status'] = 'fulfilled'
            continue
        pending.append((req, linked, signature, scope))
    if pending:
        payload = {'sources': [s for s in sources if s['id'] in {doc['id'] for _, linked, _, _ in pending for doc in linked}],
                   'items': [{'id': req['id'], 'key': 'document_scope',
                              'value': {'expected_person': case.get('client_name'), **scope},
                              'source_ids': [doc['id'] for doc in linked]} for req, linked, _, scope in pending],
                   'context': {'instruction': '각 요청에 연결된 모든 파일을 하나의 제출묶음으로 대조한다. 종류·명의·기관·계좌·시작/종료기간·발급옵션을 모두 원문 확인한다. 월별 파일은 합쳐 요청기간 전체를 충족해야 하며 빠진 월·기관이 있으면 통과할 수 없다. 마지막 파일만 보고 판단하지 않는다. 가족서류는 요구된 관계를 대조.'}}
        result = await run_local_verification('document_selection', payload, **({'progress': progress} if progress else {}))
        checked = set(result.get('checked_item_ids', []))
        for req, linked, signature, _ in pending:
            req['automatic_validation'] = {**result, 'signature': signature, 'at': store.now()}
            # Batch pass requires all requested scopes; missing one must never authorize others.
            individual_supported = any(f.get('item_id') == req['id'] and f.get('status') == 'supported'
                                       for f in result.get('findings', []) if isinstance(f, dict))
            if (result.get('passed') or individual_supported) and req['id'] in checked:
                if req.get('managed_by') == court_rules.MANAGED_BY and req.get('metadata_validation', {}).get('status') != 'matched':
                    req.update(status='received', public_review_note='제출 완료 · 발급정보와 요청 범위의 원문 확인 대기')
                    req['automatic_validation']['status'] = 'metadata_required'
                    req['automatic_validation']['passed'] = False
                    unavailable = True
                    continue
                req.update(status='fulfilled', public_review_note='요청한 서류의 범위 확인이 완료되었습니다.')
                # Another document's failed batch must not erase this completed
                # scope check and force its successful local inference to repeat.
                req['automatic_validation'].update(status='passed', passed=True,
                    checked_item_ids=[req['id']], findings=[f for f in result.get('findings', [])
                        if isinstance(f, dict) and f.get('item_id') == req['id']])
                for doc in linked:
                    if not manual_document_review_current(case, doc):
                        doc.update(status='verified', auto_verified=True, verified_at=store.now(),
                                   public_status='서류 범위 확인 완료', scope_confirmed=True, person_confirmed=True)
                request_collection.sync_review_status(case, req)
            elif result.get('status') == 'unavailable':
                unavailable = True
                req.update(status='received', public_review_note='제출 완료 · 자동 검증 재시도 대기')
            else:
                findings = result.get('findings', [])
                note = next((f.get('message') or f.get('reason') for f in findings if isinstance(f, dict)
                             and f.get('item_id') == req['id']), None)
                # Customer sees bounded scope instructions, not local-model free text or other-party details.
                _need_more(case, req, f"{req['title']}의 요청 범위({req.get('period', '')})·기관·계좌·발급옵션을 확인해 다시 제출해주세요.")
                for doc in linked:
                    if doc.get('status') != 'verified':
                        doc.update(status='needs_more', public_status='서류 범위 보완 필요')
    sync_request_notifications(case)
    return bool(reqs) and all(r.get('status') == 'fulfilled' for r in reqs), unavailable


def _retrieve(case):
    import re
    from . import corpus
    core_articles = {'LW579': 579, 'LW614': 614, 'LW611': 611}

    def article_body(chunk, article):
        # The first chunk can contain only the act title and effective date.
        # Keep the original chunk intact: the external boundary reloads its ID.
        heading = re.compile(rf'^\s*제\s*{article}\s*조\s*\([^)]+\)')
        text = (chunk.get('text') or '').strip()
        match = heading.match(text)
        if match:
            return bool(text[match.end():].strip())
        # Long articles may continue in another chunk with the same locator.
        return bool(text and heading.match(chunk.get('locator') or ''))

    try:
        found = corpus.search(query='개인회생 인가 기각 청산가치 가용소득 재산 처분 채무한도 보정 변제계획',
                              court_id=case.get('court_id'), limit=8)
        # Current core provisions enter the small writing context before historical
        # examples. A long archived act must not displace an amended operative rule.
        current = []
        today = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
        for source_id, article in core_articles.items():
            source = corpus.source_detail(source_id)
            if (source and source.get('status') == 'collected' and source.get('chunks')
                    and source.get('applicability_status') in ('baseline_unchanged', 'watch_scalar_validated')
                    and (source.get('effective_date') or '') <= today):
                body = next((chunk for chunk in source['chunks']
                             if chunk.get('source_id') == source_id and article_body(chunk, article)), None)
                if body:
                    current.append(body)
        seen = {chunk['id'] for chunk in current}
        return (current + [chunk for chunk in found if chunk['id'] not in seen
                           and (chunk.get('source_id') not in core_articles
                                or article_body(chunk, core_articles[chunk['source_id']]))])[:8]
    except (OSError, ValueError, KeyError):
        return []


def _strategy(calculation, sources, case=None):
    reasons, strategies = [], []
    refs = legal_calculator.policy()['sources']
    for b in calculation.get('blockers', []):
        reasons.append({'code': b['code'], 'reason': b.get('message', b.get('reason', '확인 필요')), 'field': b.get('field')})
    codes = {r['code'] for r in reasons}
    summary = calculation.get('summary', {})
    def add(code, title, description, ref_id):
        strategies.append({'code': code, 'title': title, 'description': description,
                           'source_refs': [r for r in refs if r['id'] == ref_id]})
    if summary.get('liquidation_shortfall', 0) > 0:
        add('LIQUIDATION', '청산가치 부족분 보완',
            f"현재 현가 기준 부족액은 {summary['liquidation_shortfall']:,}원입니다. 평가액·담보·면제범위 증빙을 대조하고 적법한 추가변제 또는 특별사정에 따른 기간 조정안을 재계산합니다. 자산을 매각해도 대금은 재산에 포함되므로 매각만으로 부족액이 사라지지 않습니다.", 'statute-614')
    if 'DEBT_LIMIT_EXCEEDED' in codes:
        add('DEBT_LIMIT', '채무한도 초과 및 절차 선택 검토', '원금·이자·담보 구분과 중복 채권을 부채증명서로 확인합니다. 실제 한도 초과가 유지되면 일반회생·파산 등 다른 절차를 검토합니다. 임의 누락·명목상 이전으로 한도를 맞추지 않습니다.', 'statute-579')
    if 'NO_POSITIVE_REPAYMENT_CAPACITY' in codes or summary.get('monthly_creditor_capacity', 1) <= 0:
        add('CAPACITY', '지속 가능한 소득과 지출 증빙 보완', '실수령액·지속근무·실제 부양과 추가지출 근거를 확인해 재산정하고 변제수행 가능성을 검토합니다.', 'statute-614')
    if any(r['code'] in {'EVIDENCE_REQUIRED', 'EVIDENCE_NOT_VERIFIED', 'LEGAL_INPUT_REASON_REQUIRED', 'INVALID_MONEY', 'BOOLEAN_REQUIRED'} for r in reasons):
        add('MISSING_INPUT', '미확인 계산 입력·근거 보완', '미확인 금액을 0원으로 처리하지 않습니다. 아래 항목의 해당 증빙 또는 사건별 판단을 추가하면 계산과 전략분석이 다시 진행됩니다.', 'statute-614')
    from . import legal_knowledge_graph, strategy_context, dashboard_insights
    features = strategy_context.graph_features(strategy_context.build(case or {}, calculation), calculation)
    knowledge = legal_knowledge_graph.retrieve(features, dashboard_insights.knowledge_policy(case or {}))
    for match in knowledge['matches']:
        if match['kind'] in {'case', 'court_rule'}:
            strategies.append({'code': match['id'], 'title': match['title'],
                'description': match['summary'], 'actions': match['actions'],
                'required_evidence': match['required_evidence'], 'source_refs': [match['source']],
                'limits': match['limits'], 'precedent_outcome': match.get('outcome'),
                'origin': 'public_issue_match'})
    return {'reasons': reasons, 'strategies': strategies, 'source_refs': refs,
            'knowledge': knowledge, 'knowledge_signature': knowledge['signature'],
            'retrieved_cases': sources, 'decision': 'review_required' if reasons else 'prepare_documents',
            'scope': '문서 작성 진행 판단이며 법원 인가 확률이 아님'}


def _features(case, calc=None, generated=None):
    from . import approval_estimator
    summary = (calc or {}).get('summary', {})
    features = {'court_id': case.get('court_id'), 'case_type': case.get('case_type', 'personal_rehabilitation'),
        'rule_version': VERSION, 'document_types': sorted({r['catalog_id'] for r in case.get('requests', []) if r.get('status') not in INACTIVE and r.get('catalog_id')}),
            'missing_request_count': sum(r.get('status') not in INACTIVE | {'fulfilled'} for r in case.get('requests', [])),
            'risk_codes': sorted({r.get('code') for r in case.get('ax_pipeline', {}).get('reasons', []) if isinstance(r, dict) and r.get('code')}),
            'liquidation_shortfall': summary.get('liquidation_shortfall'),
            'has_positive_capacity': summary.get('monthly_creditor_capacity', 0) > 0}
    generated = generated or {}
    fields = generated.get('preview', {}).get('fields', []) or [f for s in generated.get('sections', []) for f in s.get('fields', [])]
    features['template_id'] = generated.get('template_id', 'draft_package')
    features['field_presence'] = {f['key']: f.get('value') is not None and f.get('value') != '' for f in fields if f.get('key')}
    features['field_evidence'] = {f['key']: bool(f.get('source_ids') or f.get('source')) for f in fields if f.get('key')}
    features['approval_profile'] = approval_estimator.profile(case, calc)
    return features


def learned_patterns(case):
    """Aggregate evidence-backed local outcomes in the same organization/court only."""
    try:
        with store.db() as con:
            rows = con.execute('SELECT body FROM court_outcomes WHERE org_id=? AND court_id=?',
                               (case['org_id'], case.get('court_id'))).fetchall()
    except Exception:
        return []
    groups = {}
    for row in rows:
        record = json.loads(row['body'])
        if record.get('synthetic') or not record.get('evidence_verified'):
            continue
        for dtype in record.get('features', {}).get('document_types', []):
            g = groups.setdefault(dtype, {'catalog_id': dtype, 'approved': 0, 'correction': 0, 'rejected': 0})
            g[record['outcome']] += 1
        features = record.get('features', {})
        for key, present in features.get('field_presence', {}).items():
            if type(present) is not bool:
                continue
            identity = (features.get('template_id', 'draft_package'), key, present)
            g = groups.setdefault(identity, {'template_id': identity[0], 'field_key': key,
                'present_in_document': present, 'approved': 0, 'correction': 0, 'rejected': 0})
            g[record['outcome']] += 1
    return [{**g, 'scope': '관찰된 동시출현 빈도. 법원 결정 사유·인과관계·인가 확률로 사용하지 않음'} for g in groups.values()]


def approved_examples(case):
    """Return bounded verified generation structures, never another client's prose."""
    try:
        with store.db() as con:
            rows = con.execute('SELECT body FROM court_outcomes WHERE org_id=? AND court_id=? ORDER BY rowid DESC LIMIT 100',
                               (case['org_id'], case.get('court_id'))).fetchall()
    except Exception:
        return []
    examples = []
    for row in rows:
        record = json.loads(row['body'])
        if record.get('outcome') != 'approved' or record.get('synthetic') or not record.get('evidence_verified'):
            continue
        examples.append({**{key: record.get(key) for key in ('id', 'document_hash', 'outcome', 'synthetic', 'evidence_verified', 'features')},
            'org_id': case['org_id'], 'court_id': case['court_id'],
            'sections': [{'id': section['id']} for section in record.get('generated_snapshot', {}).get('sections', []) if section.get('id')]})
        if len(examples) == 2:
            break
    return examples


async def advance(case, document_checks=None, progress=None):
    from . import intake_workflow
    if intake_workflow.pending(case):
        intake_workflow.waiting_state(case)
        return
    from .verification import run_local_verification_batched as run_local_verification, run_strategy_verification, extract_calculation_inputs
    from . import legal_watch
    policy_state = legal_watch.case_policy_status(case)
    case['legal_update'] = policy_state
    pending_review_stages = set()
    def report(stage, index, detail, batch_progress=None):
        if progress:
            pipeline = {'version': VERSION, 'contract_version': workflow_contract.VERSION,
                'stage': stage, 'status': 'running', 'updated_at': store.now(), 'input_revision': case['input_revision'],
                'legal_update': {**policy_state, 'version': policy_state.get('active_version')},
                'steps': workflow_contract.project_steps(index, 'running', detail), 'reasons': []}
            for step in pipeline['steps']:
                if step['id'] in pending_review_stages and step['status'] == 'completed':
                    step.update(status='review_required', detail='확인이 끝나지 않은 항목은 보완 대상으로 유지하며 다음 작업을 진행합니다.')
            if batch_progress:
                pipeline['progress'] = {key: batch_progress[key] for key in ('completed', 'total', 'label')}
            progress(pipeline)
    def batch_report(stage, index, label):
        def update(value):
            report(stage, index, f"{label} · {value['completed']}/{value['total']}묶음 확인",
                   {**value, 'label': label})
        return update
    async def preliminary(reasons, stages, *, calculation=None, check=None, local_failure=None, narrative=None, strategies=None, strategy_record=None):
        from . import preliminary_drafting
        pending_review_stages.update(stages)
        draft = await preliminary_drafting.create(case, reasons, stages, calculation=calculation,
            ocr_check=check, local_failure=local_failure, narrative=narrative, progress=report if progress else None,
            strategy_review=run_strategy_verification, strategy=strategy_record)
        draft['legal_dependency_signature'] = policy_state['signature']
        _human_review(case, draft, strategies=strategies or draft.get('strategies'))
        linked_strategy = next((entry for entry in case.get('strategy_analyses', [])
                                if entry.get('id') == draft.get('strategy_id')), {})
        case['ax_pipeline']['retryable_verification'] = any(
            (review or {}).get('status') == 'unavailable' for review in (
                local_failure, draft.get('ai_review'), draft.get('rendered_review'), linked_strategy.get('verification')))
        return draft
    report('validation', 1, '제출 원문과 요청 기관·계좌·기간·발급옵션을 대조하고 있습니다.')
    complete, unavailable = await _validate_requests(case, document_checks or [],
        progress=batch_report('validation', 1, '서류 범위 대조') if progress else None)
    if not complete:
        _state(case, 'verification_waiting' if unavailable else 'collecting', 1 if unavailable else 0)
        return
    from . import extraction_readiness
    incomplete = []
    for document in extraction_readiness.active_documents(case):
        state = extraction_readiness.document_state(case, document)
        if not state['can_review']:
            incomplete.append({'code': 'DOCUMENT_EXTRACTION_INCOMPLETE', 'stage': 'ocr',
                'document_id': document['id'], 'reason': state['message'], 'extraction_state': state})
        elif document.get('status') != 'verified':
            incomplete.append({'code': 'DOCUMENT_REVIEW_PENDING', 'stage': 'ocr', 'document_id': document['id'],
                'reason': '추가로 받은 자료의 원문과 추출값을 함께 검토 완료해 주세요.', 'extraction_state': state})
    if incomplete:
        _state(case, 'verification_waiting', 2, incomplete)
        return
    from . import evidence_mapping
    case['evidence_mapping'] = evidence_mapping.build(case)
    sources = _sources(case, verified_only=True, packet=case['evidence_mapping'])
    report('ocr_verification', 2, '서류가 모두 준비되어 추출값을 원문과 다시 대조하고 있습니다.')
    items = _verification_items(case, sources, packet=case['evidence_mapping'])
    if not sources or not items:
        _state(case, 'verification_waiting', 2, [{'code': 'NO_STRUCTURED_EVIDENCE', 'stage': 'ocr',
            'reason': '서류에서 계산에 사용할 원문 연결 추출값이 확보되지 않았습니다.'}])
        return
    corrected = [{key: row.get(key) for key in ('id','key','value','status','origin','document_id')}
                 for row in case.get('extraction_candidates', [])
                 if row.get('origin') == 'human_correction' or row.get('status') == 'rejected']
    signature = store.digest([sources, items, VERSION] + ([corrected] if corrected else []))
    expected_items = sorted(item['id'] for item in items)
    # Confirming an unchanged extraction must not purchase thirteen identical
    # checks again. Reuse a complete negative verdict as a negative verdict;
    # source/value/correction changes or incomplete outages still require work.
    from .verification import VERSION as VERIFICATION_VERSION
    check = next((v for v in reversed(case.get('verification_runs', []))
                  if v.get('signature') == signature and v.get('kind') == 'ocr'
                  and v.get('version') == VERIFICATION_VERSION
                  and not v.get('unattempted_batch_count', 0)
                  and sorted(v.get('checked_item_ids', [])) == expected_items
                  and (v.get('passed') or (
                      v.get('status') == 'needs_review' and not v.get('error')
                      and sorted(f.get('item_id', '') for f in v.get('findings', [])) == expected_items))), None)
    if check is None:
        check = await run_local_verification('ocr', {'sources': sources, 'items': items},
            **({'progress': batch_report('ocr_verification', 2, '서류 원문 대조')} if progress else {}))
        check.update(id=store.uid('verify'), kind='ocr', signature=signature, created_at=store.now())
        case.setdefault('verification_runs', []).append(check)
    coverage_complete = (check.get('status') in {'passed', 'needs_review'} and not check.get('error')
        and not check.get('unattempted_batch_count', 0)
        and sorted(check.get('checked_item_ids', [])) == expected_items
        and (check.get('passed') or sorted(f.get('item_id', '') for f in check.get('findings', [])) == expected_items))
    if not coverage_complete:
        _state(case, 'verification_waiting', 2, [{'code': 'OCR_VERIFICATION_INCOMPLETE', 'stage': 'ocr',
            'reason': '추출값의 전체 원문 대조가 끝나지 않았습니다. 완료 전에는 계산과 문서를 생성하지 않습니다.',
            'checked_count': len(check.get('checked_item_ids', [])), 'total_count': len(items),
            'error': check.get('error')}])
        case['ax_pipeline']['retryable_verification'] = True
        return
    semantic_issues = [] if check.get('passed') else [{'code': 'OCR_VERIFICATION', 'stage': 'ocr',
        'reason': '전체 추출값 대조는 끝났지만 원문과의 의미 일치에 확인이 필요한 항목이 있습니다.',
        'findings': check.get('findings', [])}]
    semantic_stages = {'ocr'} if semantic_issues else set()
    if policy_state.get('pending_changes'):
        await preliminary([{'code': 'LEGAL_CHANGE_REVIEW', 'stage': 'analysis',
            'reason': '적용 근거 변경의 의미·시행일·경과규정을 확인해 법률 판단과 문서 내용을 보완해야 합니다.',
            'changes': policy_state['pending_changes']}] + semantic_issues, {'analysis'} | semantic_stages, check=check,
            narrative={'status': 'needs_review', 'verification': {'passed': False}, 'sections': []})
        return
    if case.get('court_request_plan', {}).get('coverage') == 'unverified':
        await preliminary([{'code': 'COURT_RULE_UNVERIFIED', 'stage': 'analysis',
                'reason': '이 관할의 세부 서류기준 원문이 확보되지 않았습니다. 공통기준에 더해 개별 요구를 확인해야 합니다.'}] + semantic_issues, {'analysis'} | semantic_stages, check=check,
            narrative={'status': 'needs_review', 'verification': {'passed': False}, 'sections': []})
        return
    if not check.get('passed'):
        await preliminary([{'code': 'OCR_VERIFICATION', 'stage': 'ocr',
            'reason': (check.get('error') or {}).get('message') or '추출값과 원문의 일치 검증이 완료되지 않았습니다.',
            'findings': check.get('findings', [])}], {'ocr', 'analysis'}, check=check,
            local_failure=check if check.get('status') == 'unavailable' else None)
        return
    report('legal_analysis', 3, '구조화 데이터를 저장하고 법률계산과 근거 검색을 병렬로 진행합니다.')
    source_digest = store.digest([sources, case['evidence_mapping']['source_signature'], case.get('court_id'), legal_calculator.policy()['policy_hash']])
    previous = next((x for x in reversed(case.get('structured_data', [])) if x.get('source_hash') == source_digest), None)
    # An explicit calculation input is an audited human intervention. Reuse only its current revision.
    manual = next((x for x in reversed(case.get('legal_calculations', []))
                   if x.get('created_by') not in (None, 'automation') and x.get('input_revision') == case['input_revision'] and not x.get('stale')), None)
    if manual:
        inputs = copy.deepcopy(manual['inputs'])
        mapped = {'status': 'human_input', 'evidence': [], 'inputs': inputs}
    elif previous:
        mapped = previous['mapping']; inputs = copy.deepcopy(previous['calculation_inputs'])
    elif case['evidence_mapping'].get('human_review_edits'):
        # A source-only reparse would discard the audited correction. Use the
        # current effective packet and keep its separate human provenance.
        inputs = copy.deepcopy(case['evidence_mapping']['inputs'])
        mapped = {**copy.deepcopy(case['evidence_mapping']), 'status': 'human_reviewed',
            'semantic_review_nonblocking_for_first_draft': True}
    else:
        mapped = await extract_calculation_inputs(sources, {'court_id': case.get('court_id'),
            'as_of': datetime.now(timezone(timedelta(hours=9))).date().isoformat(), 'policy_id': legal_calculator.policy()['id']})
        inputs = mapped.get('inputs') or {}
    if mapped.get('semantic_review_nonblocking_for_first_draft'):
        # Re-parsed typed fields and arithmetic are deterministic. The preceding
        # OCR semantic review remains visible; do not purchase a duplicate pass.
        mapped['semantic_verification'] = check
    if mapped.get('semantic_verification_required') and not mapped.get('semantic_review_nonblocking_for_first_draft') and not mapped.get('semantic_verification', {}).get('passed'):
        mapping_items = [{'id': 'map:' + e['path'], 'key': e['path'], 'value': e['value'],
                          'source_ids': [e['source_id']], 'quote': e['quote']}
                         for e in mapped.get('evidence', []) if e.get('path') and e.get('source_id')]
        mapping_check = await run_local_verification('ocr', {'sources': sources, 'items': mapping_items},
            **({'progress': batch_report('ocr_verification', 2, '계산 항목 원문 대조')} if progress else {}))
        mapped['semantic_verification'] = mapping_check
        if not mapping_check.get('passed'):
            case.setdefault('verification_runs', []).append({**mapping_check, 'id': store.uid('verify'), 'kind': 'calculation_mapping', 'created_at': store.now()})
            await preliminary([{'code': 'CALCULATION_MAPPING_VERIFICATION', 'stage': 'analysis',
                'reason': '계산 항목에 연결된 원문·단위·의미의 독립 검증이 완료되지 않았습니다.'}], {'analysis'}, check=check,
                local_failure=mapping_check if mapping_check.get('status') == 'unavailable' else None)
            return
    structured = {'id': store.uid('structured'), 'source_hash': source_digest, 'input_revision': case['input_revision'],
                  'created_at': store.now(), 'facts': items, 'calculation_inputs': inputs, 'mapping': mapped,
                  'source_versions': [{k: d.get(k) for k in ('id', 'sha256', 'version')} for d in case['documents']],
                  'verification_id': check['id']}
    case.setdefault('structured_data', []).append(structured)
    # Arithmetic and official-source retrieval do not depend on one another.
    calculation, sources_legal = await asyncio.gather(asyncio.to_thread(legal_calculator.calculate_legal, case, inputs), asyncio.to_thread(_retrieve, case))
    calculation.update(created_by='automation', created_at=store.now(), stale=False)
    case.setdefault('legal_calculations', []).append(calculation)
    strategy = _strategy(calculation, sources_legal, case)
    strategy['outcome_patterns'] = learned_patterns(case)
    from . import approval_estimator
    strategy['approval_estimate'] = await asyncio.to_thread(approval_estimator.estimate, case, calculation)
    case['approval_estimate'] = strategy['approval_estimate']
    report('legal_analysis', 3, '계산 결과와 법률 근거를 바탕으로 쟁점·전략을 독립 검증하고 있습니다.')
    from . import strategy_context
    reasoning = await run_strategy_verification(strategy_context.build(case, calculation),
        calculation, sources_legal or strategy['source_refs'])
    strategy.update(id=store.uid('strategy'), verification=reasoning, calculation_id=calculation['id'], input_revision=case['input_revision'],
                    structured_data_id=structured['id'], created_at=store.now())
    case.setdefault('strategy_analyses', []).append(strategy)
    strategy['strategies'].extend(strategy_context.finding_cards(reasoning))
    if not reasoning.get('passed'):
        strategy['reasons'].append({'code': 'STRATEGY_VERIFICATION', 'reason': '법률 쟁점·전략의 독립 검증이 완료되지 않았습니다.'})
    if strategy['reasons']:
        await preliminary([{**reason, 'stage': 'analysis'} for reason in strategy['reasons']], {'analysis'},
            check=check, calculation=calculation, strategies=strategy['strategies'], strategy_record=strategy)
        return
    report('drafting', 4, '현재 자료·관련 법령과 확인된 문서 구성 사례를 바탕으로 본문을 작성합니다.')
    from . import grounded_drafting
    examples = approved_examples(case)
    narrative_args = (items, sources_legal, examples, calculation)
    consultation = case.get('consultation', {})
    narrative_scope = {'court_id': case.get('court_id'), 'org_id': case.get('org_id'),
        'consultation': {'text': intake_workflow.record_text(case),
                         'source_id': 'consultation:notes'} if consultation.get('status') != 'quarantined' else None}
    narrative_signature = grounded_drafting.signature(*narrative_args, **narrative_scope)
    narrative = next((entry for entry in reversed(case.get('narrative_runs', []))
        if entry.get('input_signature') == narrative_signature and entry.get('verification', {}).get('passed')), None)
    if narrative is None:
        narrative = await grounded_drafting.compose(*narrative_args, **narrative_scope)
        narrative.update(id=store.uid('narrative'), created_at=store.now())
        case.setdefault('narrative_runs', []).append(narrative)
    if not narrative.get('verification', {}).get('passed') or not narrative.get('sections'):
        if narrative.get('verification', {}).get('code') == 'STATEMENT_INFORMATION_REQUIRED':
            request = next((r for r in case.get('requests', [])
                            if r.get('managed_by') == 'statement_evidence' and r.get('status') not in INACTIVE), None)
            if request is None:
                request = {'id': store.uid('req'), 'title': '채무 발생 및 상환 곤란 경위서',
                    'period': '최초 차입부터 현재까지', 'status': 'requested', 'due_date': None,
                    'managed_by': 'statement_evidence', 'created_at': store.now(), 'version': 1,
                    'document_ids': [], 'public_review_note':
                        '처음 돈을 빌린 시기와 사용처, 채무가 늘어난 과정, 현재 상환이 어려운 이유를 작성해주세요. 기억나지 않는 내용은 추측하지 않아도 됩니다.'}
                case.setdefault('requests', []).append(request)
            sync_request_notifications(case)
        await preliminary([{'code': 'GROUNDED_WRITING_REQUIRED', 'stage': 'draft',
            'reason': '관련 법령과 현재 증빙을 인용하는 본문 작성·근거 검증이 완료되지 않았습니다.',
            'details': narrative.get('verification', {})}], {'draft'}, check=check, calculation=calculation,
            narrative=narrative, local_failure=narrative if narrative.get('status') == 'unavailable' else None,
            strategy_record=strategy)
        return
    draft = drafting.generate(case, '자료 완비·원문 대조·법률계산·전략검증 완료')
    draft.update(calculation_id=calculation['id'], strategy_id=strategy['id'], structured_data_id=structured['id'],
                 narrative_id=narrative['id'], legal_dependency_signature=policy_state['signature'])
    draft['outcome_guidance'] = strategy['outcome_patterns']
    draft['review_priorities'] = [p for p in strategy['outcome_patterns'] if p.get('field_key')
        and p.get('present_in_document') is False and p.get('correction', 0) + p.get('rejected', 0) > 0]
    plan = next((s for s in draft['sections'] if s['id'] == 'repayment_plan'), None)
    if plan:
        plan['content'] = '현재 증빙에 따른 법률계산 결과입니다. 계산 기준과 근거는 연결된 계산 이력에서 확인할 수 있습니다.'
        plan['fields'] = [{'key': k, 'label': k, 'value': v, 'status': 'automatically_verified',
                           'source_ids': ['calculation:' + calculation['id']]} for k, v in calculation['summary'].items()]
    for written in narrative['sections']:
        section = next((s for s in draft['sections'] if s['id'] == written['id']), None)
        if section is None:
            section = {'id': written['id'], 'title': '법률 쟁점 및 소명 방향', 'fields': []}
            draft['sections'].insert(-1, section)
        section['content'] = '\n\n'.join(p['text'] for p in written['paragraphs'])
        section['grounded_paragraphs'] = copy.deepcopy(written['paragraphs'])
    draft['narrative_sources'] = copy.deepcopy(narrative.get('source_refs', []))
    draft['content_hash'] = store.digest({'sections': draft['sections'], 'source_refs': draft['source_refs'], 'input_revision': draft['input_revision']})
    calculation_source = 'calculation:' + calculation['id']
    review_sources = sources + [{'id': calculation_source, 'kind': 'code_calculation',
        'text': store.dumps({'summary': calculation['summary'], 'formulas': calculation.get('formulas', [])})}]
    review_sources.extend(narrative.get('source_refs', []))
    consultation = case.get('consultation', {})
    consultation_text = intake_workflow.record_text(case)
    if consultation_text and consultation.get('status') != 'quarantined':
        review_sources.append({'id': 'consultation:notes', 'kind': 'party_statement', 'text': consultation_text})
    review_sources.append({'id': 'case:workflow', 'kind': 'workflow_record', 'text': store.dumps({
        'client_name': case.get('client_name'), 'region': case.get('region'), 'court_name': case.get('court_name'),
        'case_type_label': case.get('case_type_label'), 'case_type': case.get('case_type'),
        'requests': [{key: request.get(key) for key in ('title', 'period', 'status')} for request in case.get('requests', [])]})})
    allowed_sources = {source['id'] for source in review_sources}
    def review_ids(refs):
        found = []
        for ref in refs:
            if ref in allowed_sources:
                found.append(ref)
            elif isinstance(ref, str):
                found.extend(source['id'] for source in sources if ref == 'doc:' + source['id'] or ref.startswith('doc:' + source['id'] + ':'))
                if ref == 'consultation' or ref.startswith('consultation:'):
                    found.extend(['consultation:notes'] if 'consultation:notes' in allowed_sources else [])
        return list(dict.fromkeys(found))
    draft_items, instructional_sections = [], []
    for section in draft['sections']:
        for index, field in enumerate(section.get('fields', [])):
            if field.get('value') is None:
                continue
            refs = review_ids([ref for ref in field.get('source_ids', []) if ref])
            draft_items.append({'id': f"field:{section['id']}:{index}:{field['key']}", 'key': field['key'],
                'value': field['value'], 'source_ids': refs or [source['id'] for source in sources]})
        if section.get('grounded_paragraphs'):
            for index, paragraph in enumerate(section['grounded_paragraphs']):
                draft_items.append({'id': f"paragraph:{section['id']}:{index}", 'key': 'grounded_narrative',
                    'value': paragraph['text'], 'source_ids': paragraph['source_ids']})
        elif section['id'] in {'application', 'statement', 'attachments'}:
            refs = ['consultation:notes'] if section['id'] == 'statement' and 'consultation:notes' in allowed_sources else ['case:workflow']
            draft_items.append({'id': 'section:' + section['id'], 'key': 'draft_section',
                'value': section['content'], 'source_ids': refs})
        else:
            # These strings describe the preparation procedure, not case facts.
            # Their independently attributed field values are checked above.
            instructional_sections.append(section['id'])
    report('document_verification', 5, '작성한 문서의 수치·근거·누락·모순을 다시 확인하고 있습니다.')
    verification = await run_local_verification('document', {'sources': review_sources, 'items': draft_items,
        'context': {'prior_outcome_checks': draft['review_priorities'],
                    'scope': '동일 관할 과거 문서의 누락과 결과를 대조한 검토 우선순위. 법원 결정 사유·인가 확률로 단정하지 말고 현재 원문과 누락을 재검증.'}},
        **({'progress': batch_report('document_verification', 5, '작성 내용 원문 대조')} if progress else {}))
    paragraph_findings = [finding for finding in verification.get('findings', [])
        if str(finding.get('item_id', '')).startswith('paragraph:') and finding.get('status') != 'supported']
    if (not verification.get('passed') and verification.get('status') == 'needs_review'
            and not verification.get('error') and paragraph_findings):
        # One bounded semantic revision; network failures and bad numeric input
        # are not reasons to regenerate the same private prose repeatedly.
        prior = {**verification, 'id': store.uid('verify'), 'kind': 'document_before_revision', 'created_at': store.now()}
        case.setdefault('verification_runs', []).append(prior)
        report('drafting', 4, '본문 검증에서 지적된 부분을 원문 근거에 맞춰 한 번 보완합니다.')
        revised = await grounded_drafting.compose(*narrative_args, **narrative_scope, revision_feedback=paragraph_findings)
        revised.update(id=store.uid('narrative'), created_at=store.now(), revises=narrative['id'])
        case.setdefault('narrative_runs', []).append(revised)
        draft['revision_attempts'] = 1
        if revised.get('verification', {}).get('passed') and revised.get('sections'):
            for written in revised['sections']:
                section = next((section for section in draft['sections'] if section['id'] == written['id']), None)
                if section is None:
                    continue
                section['content'] = '\n\n'.join(p['text'] for p in written['paragraphs'])
                section['grounded_paragraphs'] = copy.deepcopy(written['paragraphs'])
            draft.update(narrative_id=revised['id'], narrative_sources=copy.deepcopy(revised.get('source_refs', [])))
            draft['content_hash'] = store.digest({'sections': draft['sections'], 'source_refs': draft['source_refs'], 'input_revision': draft['input_revision']})
            source_map = {source['id']: source for source in review_sources}
            source_map.update({source['id']: source for source in revised.get('source_refs', [])})
            draft_items = [item for item in draft_items if not item['id'].startswith('paragraph:')]
            draft_items.extend({'id': f"paragraph:{section['id']}:{index}", 'key': 'grounded_narrative',
                'value': paragraph['text'], 'source_ids': paragraph['source_ids']}
                for section in draft['sections'] for index, paragraph in enumerate(section.get('grounded_paragraphs', [])))
            report('document_verification', 5, '보완한 본문과 모든 작성 항목을 다시 대조하고 있습니다.')
            verification = await run_local_verification('document', {'sources': list(source_map.values()), 'items': draft_items,
                'context': {'revision_of': prior['id'], 'scope': '수정 문장을 포함한 전체 작성 항목의 사실·금액·인과관계 재검증. 없는 사실을 덧붙이지 않는다.'}},
                **({'progress': batch_report('document_verification', 5, '보완 내용 원문 대조')} if progress else {}))
    verification['review_scope'] = {'case_fields': True, 'factual_sections': ['application', 'statement', 'attachments'],
        'instructional_sections': instructional_sections, 'rendered_pdf_verified': False}
    verification.update(id=store.uid('verify'), kind='document', created_at=store.now())
    case.setdefault('verification_runs', []).append(verification)
    draft['ai_review'] = verification
    draft['status'] = 'automatically_verified' if verification.get('passed') else 'verification_required'
    draft.update(stage_status='preliminary', human_review_required=True, submission_ready=False,
                 scope='담당자 보완·승인이 필요한 1차 검토용 초안 · 법원 제출 승인 전')
    artifact_issues = []
    from .auto_documents import prepare, verify_artifacts
    from .preliminary_drafting import render_case, _unavailable
    rendering_calculation = None
    preparation_case = case
    if verification.get('passed'):
        calculation['auto_preparation'] = {'passed': True, 'scope': '자동 초안 작성용 계산 검증',
            'verification_id': verification['id'], 'strategy_id': strategy['id'],
            **{k: calculation.get(k) for k in ('input_hash', 'policy_hash', 'result_hash')}}
        rendering_calculation = calculation
    else:
        preparation_case, _ = render_case(case, check)
    report('drafting', 4, '공식 서식 PDF와 편집 가능한 1차 검토 문서를 생성하고 있습니다.')
    artifacts, artifact_issues = await asyncio.to_thread(prepare, case, draft, rendering_calculation,
        **({'render_case': preparation_case} if preparation_case is not case else {}))
    if verification.get('status') == 'unavailable':
        rendered = _unavailable(verification, 'artifact')
        for artifact in artifacts:
            artifact['ai_review'] = copy.deepcopy(rendered)
    else:
        report('document_verification', 5, '생성한 공식 서식의 실제 출력과 근거자료를 대조하고 있습니다.')
        rendered = await verify_artifacts(case, artifacts, rendering_calculation,
            **({'progress': lambda value: report('document_verification', 5,
                '생성한 공식 서식의 실제 출력과 근거자료를 대조하고 있습니다.', batch_progress=value)} if progress else {}))
    draft['rendered_review'] = rendered
    if not rendered.get('passed'):
        artifact_issues.append({'code': 'RENDERED_VERIFICATION', 'reason': '생성 서식의 출력값 재검증이 완료되지 않았습니다.'})
    if not verification.get('passed'):
        artifact_issues.append({'code': 'DRAFT_VERIFICATION', 'reason': '작성 문서의 원문·계산·누락 검증이 완료되지 않았습니다.'})
    draft['generation_features'] = _features(case, calculation, draft)
    for artifact in artifacts:
        artifact.update(generation_features=_features(case, calculation, artifact),
                        review_pending=copy.deepcopy(artifact_issues), stage_status='preliminary',
                        human_review_required=True, submission_ready=False)
    _human_review(case, draft, artifact_issues, strategy['strategies'], {'review'} if artifact_issues else set())


def record_outcome(case, data, user):
    domain.require(user['role'] == 'lawyer', 'LAWYER_ONLY', '법원 결과 등록은 담당 변호사가 확인합니다.')
    doc = domain.evidence(case, [data['source_document_id']])[0]
    bundle = next((d for key in ('drafts', 'court_documents', 'bundles') for d in case.get(key, []) if d['id'] == data['bundle_id']), None)
    domain.require(bundle is not None, 'BUNDLE_REQUIRED', '법원 결과에 대응하는 생성 문서 버전을 선택하세요.')
    from . import approval_estimator
    try:
        decision_verification = approval_estimator.verify_decision(data, doc)
    except (ValueError, TypeError) as exc:
        raise domain.DomainError(str(exc), '결정 유형·결정일과 주문의 정확한 원문 인용을 확인하세요.')
    fingerprint_data = [bundle['id'], doc.get('sha256'), data['outcome']]
    if data.get('decision_type'):
        # Classifying a legacy broad result is allowed. The estimator still
        # deduplicates court decisions and counts a case only once.
        fingerprint_data += [data['decision_type'], data.get('decision_date'), data.get('decision_quote')]
    fingerprint = store.digest(fingerprint_data)
    domain.require(not any(o.get('fingerprint') == fingerprint for o in case.get('court_outcomes', [])), 'DUPLICATE_OUTCOME', '이미 등록된 문서 버전의 결과입니다.')
    calc = next((c for c in case.get('legal_calculations', []) if c['id'] == bundle.get('calculation_id')), None)
    row = {**data, 'id': store.uid('outcome'), 'created_at': store.now(), 'recorded_by': user['id'],
           'fingerprint': fingerprint, 'evidence_verified': True, 'synthetic': bool(case.get('synthetic')),
           'decision_verified': decision_verification['verified'], 'decision_verification': decision_verification,
           'features': copy.deepcopy(bundle.get('generation_features', {})),
           'feature_status': 'generation_snapshot' if bundle.get('generation_features') else 'historical_features_unavailable',
           'document_hash': bundle.get('content_hash') or bundle.get('snapshot_hash'),
           'source_hash': doc.get('sha256'), 'source_version': doc.get('version'), 'generated_snapshot': copy.deepcopy(bundle)}
    case.setdefault('court_outcomes', []).append(row)
    if data['outcome'] != 'approved':
        domain.invalidate(case, '법원 보정·반려 결과 반영')
        _state(case, 'lawyer_review', 3, [{'code': 'COURT_' + data['outcome'].upper(), 'reason': data['reason']}],
               [{'title': '법원 요구별 추가자료 및 수정안', 'description': '보정 원문의 요구를 서류 요청에 연결하면 새 자료 제출 후 원문 검증·계산·전략·문서 작성이 재실행됩니다.'}])
    return row
