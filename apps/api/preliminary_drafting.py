"""Build reviewable first documents despite unresolved analysis, never a filing approval.

Unknown or disputed observations remain blank. A separately reproduced legal
scenario can fill review-only calculations with visible assumptions. Only an
evidence-filtered copy reaches the form renderer. AI outages stay explicit and
must not cause repeated calls to the same unavailable local service in one run.
"""
from __future__ import annotations

import asyncio
import copy
from decimal import Decimal

from . import drafting, store

VERSION = 'preliminary-evidence-draft-v6-current-source-gaps'


def attach_provisional_calculation(draft, calculation):
    """Add a labelled arithmetic section; this does not approve any input."""
    if not calculation or calculation.get('status') != 'provisional':
        return
    from .legal_calculator import provisional_value_text
    draft.update(provisional_calculation_id=calculation['id'],
        calculation_assumptions=copy.deepcopy(calculation['assumptions']),
        provisional_calculation_summary=copy.deepcopy(calculation['summary']),
        human_review_required=True, submission_ready=False)
    summary = calculation['summary']
    rows = [('months', '변제기간'), ('net_monthly_income', '서류 기준 월 소득'),
        ('base_living_cost', '가정한 기본생계비'), ('monthly_deposit', '검토용 월 변제금'),
        ('total_creditor_payment', '기간 전체 변제액'), ('liquidation_value', '공제 검토 전 재산 평가액'),
        ('present_value', '변제액의 현재가치')]
    source_id = 'calculation:' + calculation['id']
    section = {'id': 'repayment_projection', 'title': '변제계획 검토안',
        'content': '서류에서 확인한 금액과 아래 가정으로 계산했습니다. 인정 생계비·면제재산·비용 등을 확정한 결과가 아니며, 법원 제출 전 검토가 필요합니다.\n\n' +
            '\n'.join(row['label'] + ': ' + provisional_value_text(row) + ' — ' + row['reason']
                      for row in calculation['assumptions']),
        'fields': [{'key': key, 'label': label, 'value': summary.get(key), 'status': '검토 전 계산',
                    'source_ids': [source_id]} for key, label in rows]}
    draft['sections'] = [section for section in draft['sections'] if section['id'] != 'repayment_projection'] + [section]


def attach_source_reading_history(draft, safe_case):
    """Separate old readings/scope differences from genuinely missing facts."""
    history = copy.deepcopy(safe_case.get('source_reading_exclusions', []))
    draft['source_reading_exclusions'] = history
    summaries = list(dict.fromkeys(row['label'] + ': ' + row['reason'] for row in history))
    draft['sections'] = [section for section in draft['sections'] if section['id'] != 'source_reading_history']
    if summaries:
        draft['sections'].append({'id': 'source_reading_history', 'title': '이전 판독과 현재 작성 기준',
            'content': '아래 내용은 이전 후보를 그대로 사용하지 않은 이유입니다. 현재 원문에서 확인한 값은 본문과 서식에 반영했습니다. '
                '기간·범위가 다른 후보나 근거 불일치는 검토 이력에 남기며, 해당 후보가 검증을 통과했다는 뜻은 아닙니다.\n\n' +
                '\n'.join(summaries), 'fields': []})


def render_case(case, ocr_check=None):
    from .verification import _numbers
    from . import evidence_mapping, extraction_readiness
    packet = evidence_mapping.build(case)
    safe = copy.deepcopy(case)
    active_ids = {doc['id'] for doc in extraction_readiness.active_documents(case)}
    # Keep historical originals in the actual case, but remove them from the
    # isolated renderer input as well as the deterministic evidence packet.
    safe['documents'] = [doc for doc in safe.get('documents', []) if doc['id'] in active_ids]
    # Keep bound review records available for an independent PDF reparse, away
    # from the display candidates selected below. No value bypasses the mapper's
    # original-file, fact and scope checks through this provenance collection.
    safe['source_review_candidates'] = copy.deepcopy([
        candidate for candidate in case.get('extraction_candidates', [])
        if candidate.get('source_edit') and candidate.get('document_id') in active_ids])
    documents = {doc['id']: doc for doc in safe['documents'] if doc.get('status') == 'verified'
                 and doc.get('text', '').strip() and doc.get('automated_check', {}).get('coverage_status') != 'identity_conflict'}
    checked = set((ocr_check or {}).get('checked_item_ids', []))
    supported = {row.get('item_id') for row in (ocr_check or {}).get('findings', []) if row.get('status') == 'supported'}
    if (ocr_check or {}).get('passed'):
        supported |= checked
    retained, omitted_candidates = [], []
    for candidate in case.get('extraction_candidates', []):
        if candidate.get('status') in {'rejected', 'quarantined', 'superseded'}:
            continue
        value, quote = candidate.get('value'), candidate.get('quote', '')
        source = documents.get(candidate.get('document_id'), {})
        grounded = bool(quote and quote in source.get('text', '') and value is not None)
        if type(value) in {int, float}:
            grounded = grounded and Decimal(str(value)) in _numbers(quote)
        elif isinstance(value, str):
            grounded = grounded and value in quote
        else:
            grounded = False  # Nested or legal-judgment values require explicit review.
        if grounded and candidate.get('id') in supported and candidate.get('id') in checked:
            retained.append(copy.deepcopy(candidate))
        else:
            omitted_candidates.append(candidate)
    values = {}
    for row in retained:
        values.setdefault(row['key'], set()).add(store.dumps(row['value']))
    conflicts = {key for key, choices in values.items() if len(choices) > 1}
    safe['extraction_candidates'] = [row for row in retained if row['key'] not in conflicts]
    # A failed semantic review is retained as a review issue; it does not erase
    # original-page observations whose label, unit and number are established by
    # the deterministic typed parser. Never trust a caller's claimed typed ID.
    mapped = evidence_mapping.candidate_rows(packet)
    mapped_keys = {row['key'] for row in mapped}
    safe['extraction_candidates'] = [row for row in safe['extraction_candidates'] if row['key'] not in mapped_keys] + mapped
    safe['evidence_mapping'] = packet
    # A discarded historical reading is not a missing report field when the
    # current source mapper has recovered that field. Compare semantic keys,
    # never Korean labels against English keys. Detailed observations may live
    # in source annexes rather than a single form_value (e.g. monthly vs annual
    # gross pay); only an exact source/meaning/value match clears those gaps.
    def representation(candidate):
        if candidate.get('key') in mapped_keys:
            return 'recovered'
        matching_observation = False
        for fact in packet['facts']:
            if (fact['key'] != candidate.get('key') or fact['document_id'] != candidate.get('document_id')
                    or store.dumps(fact['value']) != store.dumps(candidate.get('value'))):
                continue
            quote = candidate.get('quote') or ''
            if not quote or not (fact['quote'] in quote or quote in fact['quote']):
                continue
            matching_observation = True
            # Legacy candidates use amount_basis; current typed rows use basis.
            qualifiers = {'page': candidate.get('page'), 'basis': candidate.get('basis', candidate.get('amount_basis')),
                **{key: candidate.get(key) for key in ('frequency', 'period_start', 'period_end', 'account_key', 'loan_key')}}
            if all(value is None or value == fact.get(key) for key, value in qualifiers.items()):
                return 'recovered'
        if matching_observation:
            return 'changed_scope'
        return 'unresolved' if candidate.get('document_id') in active_ids else 'historical_source'
    omitted, history = [], []
    reasons = {'changed_scope': '현재 원문에서 금액과 기재 내용은 확인했습니다. 이전 후보의 기간·쪽수·범위가 달라 현재 원문 기준으로 작성했으며, 그 차이는 별도 대조가 필요합니다.',
        'historical_source': '상담 또는 이전 자료의 후보입니다. 현재 제출서류의 확인값과 구분해 보존하며, 이 후보가 빠졌다는 이유로 자료 누락으로 표시하지 않습니다.',
        'unresolved': '현재 제출자료와 이전 판독값을 대조하지 못했습니다. 원문과 판독 내용을 확인해야 합니다.'}
    for row in omitted_candidates:
        state = representation(row)
        if state == 'recovered':
            continue
        label = row.get('label') or row.get('key') or '추출 항목'
        history.append({'candidate_id': row.get('id'), 'key': row.get('key'), 'label': label,
            'document_id': row.get('document_id'), 'status': state,
            'requires_review': state in {'changed_scope', 'unresolved'}, 'reason': reasons[state]})
        if state == 'unresolved':
            omitted.append(label)
    safe['source_reading_exclusions'] = history
    conflicts -= mapped_keys
    safe['facts'] = []
    for fact in case.get('facts', []):
        refs = fact.get('evidence_ids', [])
        evidence = [documents[ref]['text'] for ref in refs if ref in documents]
        value = fact.get('value')
        grounded = (bool(refs) and len(evidence) == len(refs) and value is not None
                    and fact.get('status') == 'confirmed' and not fact.get('stale'))
        if type(value) in {int, float}:
            grounded = grounded and any(Decimal(str(value)) in _numbers(text) for text in evidence)
        elif isinstance(value, str):
            grounded = grounded and any(value in text for text in evidence)
        else:
            grounded = False
        if grounded:
            safe['facts'].append(copy.deepcopy(fact))
    # Old statement runs were based on a different evidence selection. They cannot
    # fill the new preliminary PDF without a fresh source-bound narrative review.
    safe['statement_runs'] = []
    return safe, list(dict.fromkeys(omitted + [f'{key}: 출처 간 값이 달라 확인 필요' for key in sorted(conflicts)]))


def _unavailable(prior, kind):
    return {'status': 'unavailable', 'passed': False, 'kind': kind, 'external_processing': False,
            'findings': [], 'checked_item_ids': [], 'error': {
                'code': 'PRIOR_LOCAL_VERIFICATION_UNAVAILABLE',
                'message': '앞선 원문 검증 연결이 완료되지 않아 같은 실행의 추가 AI 검토를 보류했습니다.'},
            'prior_error_code': (prior.get('error') or {}).get('code') or prior.get('status'),
            'attempted': False}


async def create(case, pending_issues, review_required_stages, *, calculation=None,
                 ocr_check=None, local_failure=None, narrative=None, progress=None, strategy_review=None, strategy=None):
    from . import automation, auto_documents, grounded_drafting, intake_workflow
    from .verification import run_local_verification_batched
    safe, omitted = render_case(case, ocr_check)
    case['evidence_mapping'] = copy.deepcopy(safe['evidence_mapping'])
    issues = [copy.deepcopy(issue) for issue in pending_issues]
    stages = set(review_required_stages)
    legal_sources = None
    projection = None
    def report(stage, index, message, batch_progress=None):
        if progress:
            progress(stage, index, message, **({'batch_progress': batch_progress} if batch_progress else {}))

    if safe['evidence_mapping']['facts']:
        from . import legal_calculator
        # Preserve a reproducible arithmetic/unknown-input report even when an
        # earlier semantic check was inconclusive. Missing judgments stay null.
        if calculation is None:
            report('legal_analysis', 3, '보관된 추출값으로 계산·근거 검색을 진행하고 미확정 항목을 함께 표시합니다.')
            calculation, legal_sources = await asyncio.gather(
                asyncio.to_thread(legal_calculator.calculate_legal, case, safe['evidence_mapping']['inputs']),
                asyncio.to_thread(automation._retrieve, safe))
            calculation.update(created_by='automation', created_at=store.now(), stale=False)
            case.setdefault('legal_calculations', []).append(calculation)
        projection = await asyncio.to_thread(legal_calculator.calculate_provisional, case, calculation.get('inputs'))
        if projection['status'] == 'provisional':
            projection = legal_calculator.record_provisional(case, projection,
                created_by='automation', created_at=store.now(), analysis_calculation_id=calculation['id'])
            issues.append({'code': 'PROVISIONAL_CALCULATION', 'stage': 'analysis',
                'reason': '기본 변제기간과 명시한 가정으로 변제계획 검토안을 계산했습니다. 인정인원·공제·비용 등 가정을 확인한 뒤 확정해야 합니다.'})
        else:
            projection = None
        structured = {'id': store.uid('structured'),
            'source_hash': safe['evidence_mapping']['source_signature'], 'input_revision': case['input_revision'],
            'created_at': store.now(), 'facts': safe['evidence_mapping']['facts'],
            'calculation_inputs': safe['evidence_mapping']['inputs'], 'mapping': safe['evidence_mapping'],
            'verification_id': (ocr_check or {}).get('id'),
            'semantic_verified': bool((ocr_check or {}).get('passed'))}
        case.setdefault('structured_data', []).append(structured)
        issues.extend({'code': error['code'], 'stage': 'ocr', 'reason': '원문에 연결된 항목의 기간·계좌·금액을 대조해야 합니다.',
                       'field': error.get('key')} for error in safe['evidence_mapping']['errors'])
        issues.append({'code': 'PROPOSED_LEGAL_INPUTS', 'stage': 'analysis',
                       'reason': '서류 사실과 별도로 생계비 인정인원·기간·비용·면제재산 등 미확정 법률 입력을 검토해야 합니다.'})
        gaps = safe['evidence_mapping'].get('review_gaps', [])
        issues.extend(copy.deepcopy(gaps))
        stages.add('analysis')
        # A semantic outage blocks approval, not deterministic arithmetic or
        # numeric-only issue analysis. Never send private prose to this callback.
        # Only an analysis explicitly produced by this execution may be reused.
        # Historical failures and prior law snapshots must not defeat a retry.
        if strategy and strategy.get('calculation_id') != calculation['id']:
            strategy = None
        if strategy is None:
            if legal_sources is None:
                legal_sources = await asyncio.to_thread(automation._retrieve, safe)
            strategy = automation._strategy(calculation, legal_sources, safe)
            strategy.update(id=store.uid('strategy'), calculation_id=calculation['id'],
                structured_data_id=structured['id'], created_at=store.now(), input_revision=case['input_revision'],
                decision='review_required', evidence_status='verified' if (ocr_check or {}).get('passed') else 'semantic_review_pending',
                review_gaps=copy.deepcopy(gaps), submission_ready=False)
            for gap in gaps:
                strategy['strategies'].append({'code':gap['code'], 'title':gap.get('title', '자료 보완'),
                    'description':gap['reason'], 'required_evidence':gap.get('suggested_documents', []), 'source_refs':[]})
            report('legal_analysis', 3, '확인된 금액과 미확정 계산항목을 전달해 쟁점·보완 방향을 검토합니다.')
            reasoning = {'status':'not_attempted', 'passed':False, 'findings':[]}
            if strategy_review:
                try:
                    from . import strategy_context
                    reasoning = await strategy_review(
                        strategy_context.build(safe, calculation, safe['evidence_mapping']),
                        calculation, legal_sources or strategy['source_refs'])
                except Exception:
                    # Supplementary inference must not discard the detached
                    # extraction/calculation report or prevent a review draft.
                    # Cancellation (BaseException) still aborts stale work.
                    reasoning = {'status':'unavailable', 'passed':False, 'findings':[],
                        'error':{'code':'STRATEGY_UNAVAILABLE', 'message':'전략 검증 연결을 완료하지 못했습니다. 계산 결과와 확인된 자료는 보존했습니다.'}}
            strategy['verification'] = reasoning
            strategy['reasons'].extend(copy.deepcopy(issues))
            from . import strategy_context
            strategy['strategies'].extend(strategy_context.finding_cards(reasoning))
            if not reasoning.get('passed'):
                reason = {'code':'STRATEGY_VERIFICATION', 'stage':'analysis',
                          'reason':'법률 쟁점·전략의 독립 검증이 완료되지 않았습니다.'}
                strategy['reasons'].append(reason)
                issues.append(reason)
            case.setdefault('strategy_analyses', []).append(strategy)

    def draft_progress(value):
        report('document_verification', 5, '1차 초안의 작성 항목과 원문 근거를 대조하고 있습니다.',
               {'completed': value['completed'], 'total': value['total'], 'label': '1차 초안 원문 대조'})

    def artifact_progress(value):
        report('document_verification', 5, '생성한 공식 서식의 실제 출력값과 누락 항목을 확인합니다.',
               {key: value[key] for key in ('completed', 'total', 'label')})

    report('drafting', 4, '확인된 자료와 계산 가정을 구분해 1차 검토 문서를 작성합니다.')
    sources = automation._sources(safe, verified_only=True, packet=safe['evidence_mapping'])
    items = automation._verification_items(safe, sources, packet=safe['evidence_mapping'])
    # A strict blocked calculation is never used for form fill. Only the separate
    # source-bound projection may fill a visibly labelled, unapproved scenario.
    if narrative is None and not local_failure:
        if legal_sources is None:
            legal_sources = await asyncio.to_thread(automation._retrieve, safe)
        consultation = safe.get('consultation', {})
        narrative_args = (items, legal_sources, automation.approved_examples(safe), {})
        narrative_scope = {'court_id':safe.get('court_id'), 'org_id':safe.get('org_id'), 'section_ids':['statement'],
            'consultation':{'text':intake_workflow.record_text(safe), 'source_id':'consultation:notes'}
                if consultation.get('status') != 'quarantined' else None}
        narrative_signature = grounded_drafting.signature(*narrative_args, **narrative_scope)
        narrative = next((entry for entry in reversed(case.get('narrative_runs', []))
            if entry.get('input_signature') == narrative_signature and entry.get('verification', {}).get('passed')
            and entry.get('status') == 'completed' and entry.get('sections')), None)
        if narrative is None:
            narrative = await grounded_drafting.compose(*narrative_args, **narrative_scope)
            narrative.update(id=store.uid('narrative'), created_at=store.now())
            case.setdefault('narrative_runs', []).append(narrative)
    if narrative and narrative.get('status') == 'unavailable':
        local_failure = narrative
    if not narrative or not narrative.get('verification',{}).get('passed'):
        if legal_sources is None:
            legal_sources = await asyncio.to_thread(automation._retrieve, safe)
        fallback = grounded_drafting.review_fallback(items,legal_sources,automation.approved_examples(safe),{},
            court_id=safe.get('court_id'),org_id=safe.get('org_id'),section_ids=['statement'],
            consultation={'text':intake_workflow.record_text(safe),'source_id':'consultation:notes'}
                if safe.get('consultation',{}).get('status')!='quarantined' else None,
            failure=narrative)
        if fallback.get('sections'):
            fallback.update(id=store.uid('narrative'),created_at=store.now())
            case.setdefault('narrative_runs',[]).append(fallback)
            narrative=fallback
    safe['narrative_runs'] = copy.deepcopy(case.get('narrative_runs', []))
    draft = drafting.generate(safe, '자료 제출·검토 완료 후 1차 문서 자동 작성')
    draft.update(stage_status='preliminary', human_review_required=True, submission_ready=False,
                 preliminary_version=VERSION, calculation_id=None,
                 analysis_calculation_id=(calculation or {}).get('id'),
                 review_pending=issues, scope='담당자 보완·승인이 필요한 1차 검토용 초안 · 법원 제출 승인 전')
    draft['evidence_mapping'] = copy.deepcopy(safe['evidence_mapping'])
    attach_source_reading_history(draft, safe)
    attach_provisional_calculation(draft, projection)
    if strategy:
        draft.update(strategy_id=strategy['id'], structured_data_id=strategy['structured_data_id'],
                     strategies=copy.deepcopy(strategy['strategies']))
    draft['proposed_decisions'] = copy.deepcopy(safe['evidence_mapping']['proposed_decisions'])
    draft['narrative_fact_ids'] = [item['id'] for item in items]
    draft['narrative_source_signature'] = safe['evidence_mapping']['source_signature']
    draft['missing_fields'] = list(dict.fromkeys(draft['missing_fields'] + omitted + [i.get('reason', i.get('code', '확인 필요')) for i in issues]))
    source_summary=bool(narrative and narrative.get('authoring_method')=='source_summary'
                        and narrative.get('verification',{}).get('source_binding_passed'))
    if narrative and (narrative.get('verification', {}).get('passed') or source_summary) and narrative.get('sections'):
        for written in narrative['sections']:
            section = next((section for section in draft['sections'] if section['id'] == written['id']), None)
            if section:
                section.update(content='\n\n'.join(p['text'] for p in written['paragraphs']),
                               grounded_paragraphs=copy.deepcopy(written['paragraphs']))
        draft.update(narrative_id=narrative['id'], narrative_sources=copy.deepcopy(narrative.get('source_refs', [])))
        draft['narrative_section_ids'] = ['statement']
        if source_summary:
            issues.append({'code':'GROUNDED_WRITING_REQUIRED','stage':'draft',
                'reason':narrative['review_reason']})
            stages.add('draft')
    else:
        issues.append({'code': 'GROUNDED_WRITING_REQUIRED', 'stage': 'draft',
                       'reason': '진술 본문 작성·근거 검증이 완료되지 않았습니다. 확인된 항목과 원문을 바탕으로 보완해 주세요.'})
        stages.add('draft')
    sources.extend(draft.get('narrative_sources', []))
    if projection:
        sources.append({'id': 'calculation:' + projection['id'], 'kind': 'code_calculation',
            'text': store.dumps({'summary': projection['summary'], 'assumptions': projection['assumptions'],
                                 'scope': projection['scope']})})
    source_ids = {source['id'] for source in sources}
    identity_id = 'case:identity'
    sources.append({'id': identity_id, 'kind': 'case_record',
                    'text': f"성명: {case.get('client_name', '')}\n법원: {case.get('court_name', '')}"})
    source_ids.add(identity_id)
    def source_refs(refs):
        resolved = []
        for ref in refs:
            if ref in source_ids:
                resolved.append(ref)
            elif isinstance(ref, str) and ref.startswith('doc:'):
                match = next((source_id for source_id in sorted(source_ids, key=len, reverse=True)
                              if ref[4:] == source_id or ref[4:].startswith(source_id + ':')), None)
                if match:
                    resolved.append(match)
        return list(dict.fromkeys(resolved))
    review_items = [{'id': 'identity:name', 'key': 'client_name', 'value': case.get('client_name'), 'source_ids': [identity_id]}]
    for section in draft['sections']:
        for index, field in enumerate(section['fields']):
            if field.get('value') is None:
                continue
            refs = source_refs(field.get('source_ids', []))
            if not refs:
                field.update(value=None, status='unknown')
                draft['missing_fields'].append(field['label'] + ': 원문 연결 확인 필요')
                continue
            field['source_ids'] = refs
            review_items.append({'id': f"field:{section['id']}:{index}", 'key': field['key'],
                                 'value': field['value'], 'source_ids': refs})
        for index, paragraph in enumerate(section.get('grounded_paragraphs', [])):
            review_items.append({'id': f"paragraph:{section['id']}:{index}", 'key': 'grounded_narrative',
                                 'value': paragraph['text'], 'source_ids': paragraph['source_ids']})
    report('document_verification', 5, '1차 초안의 작성 항목·근거·빈칸을 검토하고 보완 목록을 정리합니다.')
    review = _unavailable(local_failure, 'document') if local_failure else await run_local_verification_batched(
        'document', {'sources': sources, 'items': review_items,
                     'context': {'scope': '1차 검토 초안에 실제 채워진 값과 인용 문단만 대조. 빈칸과 미확정 법률 판단은 검증 통과를 뜻하지 않는다.'}},
        **({'progress': draft_progress} if progress else {}))
    review.update(id=store.uid('verify'), kind='document', created_at=store.now(),
                  review_scope={'available_fields_and_sourced_paragraphs': True, 'completeness_verified': False})
    case.setdefault('verification_runs', []).append(review)
    draft['ai_review'] = review
    draft['status'] = 'verification_required'
    if not review.get('passed'):
        stages.add('review')
        issues.append({'code': 'DRAFT_VERIFICATION', 'stage': 'review', 'reason': '1차 초안의 AI 문서 검토가 완료되지 않아 담당자 보완이 필요합니다.'})
    draft['content_hash'] = store.digest({'sections': draft['sections'], 'source_refs': draft['source_refs'], 'input_revision': draft['input_revision']})
    for old in case.setdefault('drafts', []):
        old.update(stale=True, stale_reason='새 자료 기준 1차 검토 초안 작성')
    case['drafts'].append(draft)
    report('drafting', 4, '확인한 자료와 가정을 명시한 변제계획 검토안을 공식 서식에 반영합니다.')
    artifacts, artifact_issues = await asyncio.to_thread(auto_documents.prepare, case, draft, projection, render_case=safe)
    issues.extend({**issue, 'stage': 'review'} for issue in artifact_issues)
    if review.get('status') == 'unavailable':
        rendered = _unavailable(review, 'artifact')
        for record in artifacts:
            record['ai_review'] = copy.deepcopy(rendered)
    else:
        report('document_verification', 5, '생성한 공식 서식의 실제 출력값과 누락 항목을 확인합니다.')
        rendered = await auto_documents.verify_artifacts(case, artifacts, projection,
            **({'progress': artifact_progress} if progress else {}))
    draft['rendered_review'] = rendered
    if not rendered.get('passed'):
        stages.add('review')
        issues.append({'code': 'RENDERED_VERIFICATION', 'stage': 'review', 'reason': '공식 서식의 빈칸·출력값 검토와 보완이 필요합니다.'})
    if omitted:
        stages.add('ocr')
    draft['review_pending'] = issues
    draft['missing_fields'] = list(dict.fromkeys(draft['missing_fields'] + [i.get('reason', i.get('code', '확인 필요')) for i in issues]))
    draft['review_required_stages'] = sorted(stages)
    for artifact in artifacts:
        artifact.update(stage_status='preliminary', human_review_required=True, submission_ready=False,
                        review_pending=copy.deepcopy(issues))
    # Refresh the editable package after its final review findings are available.
    folder = store.DATA_DIR / 'generated' / case['id']
    if folder.exists():
        (folder / (draft['id'] + '.docx')).write_bytes(drafting.to_docx(case, draft))
        (folder / (draft['id'] + '.html')).write_text(drafting.to_html(case, draft), encoding='utf-8')
    return draft
