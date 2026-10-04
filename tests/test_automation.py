"""AX regression tests: authorization, full coverage, fail-closed gates and versioned outcomes."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from apps.api import automation, ax_service, domain, extraction_readiness, legal_calculator, store, grounded_drafting


def fixture():
    case = domain.new_case('합성고객', 'CT01', '서울회생법원', '합성 검증 사례', True)
    case.update(case_type='personal_rehabilitation', client_user_id='client')
    case['documents'] = [{'id': 'pay', 'request_id': 'req', 'filename': '합성.txt', 'text': '급여명세서 성명: 합성고객 월 소득 2800000원',
                          'sha256': 'test-hash', 'version': 1, 'status': 'received', 'uploader': 'client',
                          'created_at': store.now(), 'public_status': '제출 완료', 'page_texts': []}]
    case['requests'] = [{'id': 'req', 'catalog_id': 'D07', 'title': '급여명세서', 'period': '2026-07~2026-09',
                         'status': 'received', 'document_ids': ['pay'], 'version': 1}]
    case['extraction_candidates'] = [{'id': 'income1', 'key': 'monthly_income', 'value': 2800000,
                                      'document_id': 'pay', 'source_id': 'doc:pay', 'source_type': 'case_document',
                                      'quote': '월 소득 2800000원', 'status': 'candidate'}]
    extracted = extraction_readiness.candidates(case['documents'][0])
    ax_service.enrich_candidates(case, extracted, 'pay')
    extraction_readiness.capture(case, case['documents'][0], extracted)
    inputs = legal_calculator.example_payload()
    for row in [inputs['income']] + inputs['assets'] + inputs['creditors']:
        row['evidence_ids'] = ['pay']
    return case, inputs


async def supported(kind, payload, **kwargs):
    from apps.api.verification import VERSION
    return {'status': 'passed', 'passed': True, 'version': VERSION, 'error': None,
            'checked_item_ids': [i['id'] for i in payload['items']],
            'findings': [{'item_id': i['id'], 'status': 'supported'} for i in payload['items']]}


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.case, self.inputs = fixture()
        self.local = patch('apps.api.verification.run_local_verification_batched', side_effect=supported).start()
        self.mapping = patch('apps.api.verification.extract_calculation_inputs', new=AsyncMock(return_value={'status': 'mapped', 'inputs': self.inputs, 'evidence': []})).start()
        self.strategy = patch('apps.api.verification.run_strategy_verification', new=AsyncMock(return_value={'passed': True, 'status': 'passed'})).start()
        patch('apps.api.automation._retrieve', return_value=[]).start()
        patch('apps.api.automation.learned_patterns', return_value=[]).start()
        patch('apps.api.automation.approved_examples', return_value=[]).start()
        patch('apps.api.grounded_drafting.signature', return_value='synthetic-narrative').start()
        self.narrative = patch('apps.api.grounded_drafting.compose', new=AsyncMock(return_value={
            'status': 'completed', 'version': grounded_drafting.VERSION, 'input_signature': 'synthetic-narrative', 'verification': {'passed': True},
            'sections': [{'id': 'statement', 'paragraphs': [{'text': '월 소득은 2800000원입니다.',
                'source_ids': ['fact:income1'], 'quotes': [{'source_id': 'fact:income1', 'quote': '월 소득 2800000원'}]}]}],
            'source_refs': [{'id': 'fact:income1', 'kind': 'case_fact', 'text': '월 소득 2800000원', 'original_source_ids': ['pay']}]
        })).start()
        patch('apps.api.auto_documents.prepare', return_value=([], [])).start()
        patch('apps.api.auto_documents.verify_artifacts', create=True, new=AsyncMock(return_value={'passed': True, 'status': 'passed'})).start()
        self.addCleanup(patch.stopall)

    def run_pipeline(self, checks=None):
        asyncio.run(automation.advance(self.case, checks))

    def test_complete_evidence_automatically_calculates_and_drafts_without_approval(self):
        self.run_pipeline()
        self.assertEqual(self.case['ax_pipeline']['stage'], 'human_review')
        self.assertEqual(self.case['requests'][0]['status'], 'fulfilled')
        self.assertEqual(self.case['drafts'][-1]['status'], 'automatically_verified')
        self.assertEqual(self.case['legal_calculations'][-1]['summary']['monthly_creditor_capacity'], 1261457)
        self.assertEqual(self.case['approvals'], [])
        self.assertTrue(self.case['structured_data'][-1]['verification_id'])
        self.assertEqual([c.args[0] for c in self.local.call_args_list], ['document_selection', 'ocr', 'document'])
        self.assertTrue(self.case['drafts'][-1]['human_review_required'])
        self.assertFalse(self.case['drafts'][-1]['submission_ready'])
        self.assertEqual(self.case['ax_pipeline']['steps'][6]['status'], 'waiting')
        self.assertEqual(self.case['ax_pipeline']['steps'][7]['status'], 'waiting')

    def test_missing_request_prevents_ocr_calculation_and_draft(self):
        self.case['requests'].append({'id': 'missing', 'catalog_id': 'D38', 'title': '부채증명', 'status': 'requested', 'period': '기준일', 'document_ids': []})
        self.run_pipeline()
        self.assertEqual(self.case['ax_pipeline']['stage'], 'collecting')
        self.assertNotIn('drafts', self.case)
        self.mapping.assert_not_awaited()
        self.assertTrue(any(n['audience'] == 'client' for n in self.case['notifications']))

    def test_withdrawn_request_does_not_block(self):
        self.case['requests'].append({'id': 'old', 'catalog_id': 'D36', 'status': 'withdrawn'})
        self.run_pipeline()
        self.assertEqual(self.case['ax_pipeline']['stage'], 'human_review')

    def test_received_reference_document_waits_for_its_unified_review(self):
        from apps.api import ax_service, extraction_readiness
        reference = copy.deepcopy(self.case['documents'][0])
        reference.update(id='additional-source', request_id=None, status='received')
        reference.pop('extraction_manifest', None)
        self.case['documents'].append(reference)
        ax_service.enrich_candidates(self.case, extraction_readiness.candidates(reference))
        extraction_readiness.capture(self.case, reference)
        self.run_pipeline()
        self.assertEqual(self.case['ax_pipeline']['stage'], 'verification_waiting')
        self.assertIn('DOCUMENT_REVIEW_PENDING', {r['code'] for r in self.case['ax_pipeline']['reasons']})
        self.assertFalse(self.case.get('drafts'))
        self.mapping.assert_not_awaited()

    def test_wrong_kind_re_requests_customer_and_does_not_generate(self):
        self.run_pipeline([{'document_id': 'pay', 'catalog_id': 'D38'}])
        self.assertEqual(self.case['requests'][0]['status'], 'needs_more')
        self.assertIn('다른 서류', self.case['notifications'][0]['message'])
        self.local.assert_not_called()

    def test_wrong_person_quarantines_and_re_requests(self):
        self.run_pipeline([{'document_id': 'pay', 'coverage_status': 'identity_conflict'}])
        self.assertEqual(self.case['documents'][0]['status'], 'quarantined')
        self.assertEqual(self.case['requests'][0]['status'], 'needs_more')

    def test_verifier_outage_waits_without_false_rejection_or_pass(self):
        self.local.side_effect = AsyncMock(return_value={'status': 'unavailable', 'passed': False})
        self.run_pipeline()
        self.assertEqual(self.case['ax_pipeline']['stage'], 'verification_waiting')
        self.assertEqual(self.case['requests'][0]['status'], 'received')
        self.assertNotIn('drafts', self.case)

    def test_local_pass_without_request_coverage_cannot_fulfil(self):
        self.local.side_effect = AsyncMock(return_value={'status': 'passed', 'passed': True, 'checked_item_ids': []})
        self.run_pipeline()
        self.assertEqual(self.case['requests'][0]['status'], 'needs_more')
        self.assertNotIn('drafts', self.case)

    def test_ocr_failure_builds_review_draft_and_preserves_partial_analysis(self):
        async def result(kind, payload, **kwargs):
            checked = await supported(kind, payload)
            return {**checked, 'status': 'needs_review', 'passed': False,
                    'findings': [{'item_id': row['id'], 'status': 'uncertain'} for row in payload['items']]
                    } if kind == 'ocr' else checked
        self.local.side_effect = result
        progress = []
        asyncio.run(automation.advance(self.case, progress=progress.append))
        self.assertEqual(self.case['ax_pipeline']['stage'], 'human_review')
        self.strategy.assert_awaited_once()
        self.assertEqual(self.case['strategy_analyses'][-1]['decision'], 'review_required')
        self.assertEqual(self.case['strategy_analyses'][-1]['evidence_status'], 'semantic_review_pending')
        self.assertTrue(any(n['kind'] == 'review_request' for n in self.case['notifications']))
        self.assertTrue(self.case['drafts'])
        steps = {step['id']: step['status'] for step in self.case['ax_pipeline']['steps']}
        self.assertEqual(steps['ocr'], 'review_required')
        self.assertEqual(steps['analysis'], 'review_required')
        self.assertFalse(self.case['drafts'][-1]['submission_ready'])
        later = [row for row in progress if row['stage'] in {'legal_analysis','drafting','document_verification'}]
        self.assertTrue(later)
        self.assertTrue(all(next(step for step in row['steps'] if step['id']=='ocr')['status']=='review_required' for row in later))

    def test_ocr_outage_waits_without_new_analysis_or_documents_and_preserves_existing_versions(self):
        self.manual_review()
        original = self.existing_outputs()
        self.local.side_effect = AsyncMock(return_value={'status': 'unavailable', 'passed': False,
            'error': {'code': 'MODEL_TIMEOUT', 'message': '검증 시간 초과'}})
        self.run_pipeline()
        self.assertEqual(self.local.call_count, 1)
        self.assert_waiting_without_downstream(original)
        self.assertTrue(self.case['ax_pipeline']['retryable_verification'])

    def existing_outputs(self):
        """Prior generated/reviewed versions must survive an incomplete new check."""
        self.case['drafts'] = [{'id': 'old-draft', 'sections': [{'id': 'statement', 'content': '기존 근거 작성 본문'}],
                               'input_revision': 1, 'stale': False, 'ai_review': {'status': 'needs_review', 'passed': False}}]
        self.case['court_documents'] = [{'id': 'old-pdf', 'sha256': 'original-pdf-hash',
            'fields': {'statement': '기존 서식 본문'}, 'ai_review': {'status': 'passed', 'passed': True}, 'stale': False}]
        self.case['legal_calculations'] = [{'id': 'old-calculation', 'summary': {'monthly_income': 2800000}, 'stale': False}]
        self.case['strategy_analyses'] = [{'id': 'old-strategy', 'decision': 'review_required', 'stale': False}]
        return {key: copy.deepcopy(self.case[key]) for key in ('drafts', 'court_documents', 'legal_calculations', 'strategy_analyses')}

    def assert_waiting_without_downstream(self, previous=None):
        self.assertEqual(self.case['ax_pipeline']['stage'], 'verification_waiting')
        self.mapping.assert_not_awaited()
        self.strategy.assert_not_awaited()
        self.narrative.assert_not_awaited()
        for key in ('drafts', 'court_documents', 'legal_calculations', 'strategy_analyses'):
            self.assertEqual(self.case.get(key, []), (previous or {}).get(key, []))

    def test_false_ocr_pass_with_partial_coverage_cannot_start_downstream_work(self):
        self.manual_review()
        original = self.existing_outputs()
        async def partial(kind, payload, **kwargs):
            checked = await supported(kind, payload)
            checked['checked_item_ids'] = checked['checked_item_ids'][:-1]
            return checked
        self.local.side_effect = partial
        self.run_pipeline()
        self.assert_waiting_without_downstream(original)
        self.assertEqual(self.case['ax_pipeline']['reasons'][0]['code'], 'OCR_VERIFICATION_INCOMPLETE')

    def test_negative_ocr_missing_findings_is_incomplete_even_with_all_checked_ids(self):
        self.manual_review()
        async def partial(kind, payload, **kwargs):
            checked = await supported(kind, payload)
            return {**checked, 'status': 'needs_review', 'passed': False, 'findings': []}
        self.local.side_effect = partial
        self.run_pipeline()
        self.assert_waiting_without_downstream()

    def test_unattempted_ocr_batch_is_not_complete_despite_passed_flag(self):
        self.manual_review()
        async def partial(kind, payload, **kwargs):
            return {**await supported(kind, payload), 'unattempted_batch_count': 1}
        self.local.side_effect = partial
        self.run_pipeline()
        self.assert_waiting_without_downstream()

    def test_no_structured_items_waits_without_fabricating_calculation_or_document(self):
        document = self.case['documents'][0]
        document.update(text='추가 사정에 관한 별도 설명입니다.', page_texts=[])
        self.case['extraction_candidates'] = []
        extraction_readiness.capture(self.case, document)
        self.manual_review()
        original = self.existing_outputs()
        self.run_pipeline()
        self.assert_waiting_without_downstream(original)
        self.local.assert_not_called()
        self.assertEqual(self.case['ax_pipeline']['reasons'][0]['code'], 'NO_STRUCTURED_EVIDENCE')

    def test_incomplete_parser_receipt_does_not_start_ocr_or_analysis(self):
        self.manual_review()
        self.case['extraction_candidates'] = self.case['extraction_candidates'][:1]
        self.run_pipeline()
        self.assert_waiting_without_downstream()
        self.local.assert_not_called()
        self.assertEqual(self.case['ax_pipeline']['reasons'][0]['code'], 'DOCUMENT_EXTRACTION_INCOMPLETE')

    def test_complete_negative_ocr_is_reused_without_promoting_it_to_pass(self):
        async def result(kind, payload, **kwargs):
            from apps.api.verification import VERSION
            checked = await supported(kind, payload)
            return {**checked, 'status':'needs_review', 'passed':False, 'version':VERSION,
                    'findings':[{'item_id':item['id'],'status':'uncertain'} for item in payload['items']]} if kind=='ocr' else checked
        self.local.side_effect = result
        self.run_pipeline()
        self.case['extraction_candidates'][0]['status'] = 'accepted'
        self.case['input_revision'] += 1
        self.run_pipeline()
        self.assertEqual(sum(call.args[0]=='ocr' for call in self.local.call_args_list), 1)
        self.assertEqual(self.narrative.await_count, 1)
        self.assertEqual(self.case['ax_pipeline']['stage'], 'human_review')
        self.assertEqual(next(step for step in self.case['ax_pipeline']['steps'] if step['id']=='ocr')['status'], 'review_required')
        self.assertFalse(self.case['drafts'][-1]['submission_ready'])

    def test_incomplete_negative_ocr_is_not_reused(self):
        async def result(kind, payload, **kwargs):
            return {'status':'needs_review','passed':False,'checked_item_ids':[]} if kind=='ocr' else await supported(kind,payload)
        self.local.side_effect = result
        self.run_pipeline()
        self.run_pipeline()
        self.assertEqual(sum(call.args[0]=='ocr' for call in self.local.call_args_list), 2)
        self.assert_waiting_without_downstream()

    def test_human_correction_invalidates_negative_ocr_reuse(self):
        async def result(kind, payload, **kwargs):
            from apps.api.verification import VERSION
            checked = await supported(kind,payload)
            return {**checked,'status':'needs_review','passed':False,'version':VERSION,
                    'findings':[{'item_id':item['id'],'status':'uncertain'} for item in payload['items']]} if kind=='ocr' else checked
        self.local.side_effect = result
        self.run_pipeline()
        self.case['extraction_candidates'][0].update(status='accepted',origin='human_correction',value=3200000)
        self.case['input_revision'] += 1
        self.run_pipeline()
        self.assertEqual(sum(call.args[0]=='ocr' for call in self.local.call_args_list), 2)

    def manual_review(self, bind=True):
        doc = self.case['documents'][0]
        doc.update(status='verified', verified_by='담당자', verified_at=store.now(),
                   person_confirmed=True, scope_confirmed=True, content_confirmed=True)
        self.case['requests'][0]['status'] = 'fulfilled'
        if bind:
            doc['manual_verification'] = {'signature': automation.document_review_signature(self.case, doc),
                                         'verified_by': doc['verified_by'], 'verified_at': doc['verified_at']}
        return doc

    def test_manual_review_preserves_fulfilment_against_heuristic_misclassification(self):
        doc = self.manual_review()
        doc['auto_verified'] = True  # Legacy automatic marker must not override the explicit review.
        self.run_pipeline([{'document_id': 'pay', 'catalog_id': 'D38', 'coverage_status': 'needs_more'}])
        self.assertEqual(self.case['requests'][0]['status'], 'fulfilled')
        self.assertEqual(doc['status'], 'verified')
        self.assertEqual(self.case['ax_pipeline']['stage'], 'human_review')
        self.assertEqual([call.args[0] for call in self.local.call_args_list], ['ocr', 'document'])

    def test_new_identity_conflict_still_blocks_manual_review(self):
        doc = self.manual_review()
        self.run_pipeline([{'document_id': 'pay', 'coverage_status': 'identity_conflict'}])
        self.assertEqual(doc['status'], 'quarantined')
        self.assertEqual(self.case['requests'][0]['status'], 'needs_more')
        self.local.assert_not_called()

    def test_manual_scope_review_cannot_authorize_missing_machine_readable_pages(self):
        doc = self.manual_review()
        doc['ocr_summary'] = {'unreadable_pages': [2]}
        self.run_pipeline()
        self.assertEqual(doc['status'], 'verified')
        self.assertEqual(self.case['requests'][0]['status'], 'fulfilled')
        self.assertEqual(self.case['ax_pipeline']['reasons'][0]['code'], 'DOCUMENT_EXTRACTION_INCOMPLETE')
        self.assertFalse(any(n.get('audience') == 'client' for n in self.case.get('notifications', [])))
        self.assert_waiting_without_downstream()
        self.local.assert_not_called()

    def test_manual_review_invalidates_when_file_version_or_scope_changes(self):
        for target, field, changed in [('document', 'version', 2), ('document', 'text', '변경된 원문'),
                                        ('document', 'sha256', 'other-hash'), ('request', 'period', '새 기간')]:
            with self.subTest(field=field):
                self.case, self.inputs = fixture()
                doc = self.manual_review()
                self.assertTrue(automation.manual_document_review_current(self.case, doc))
                (doc if target == 'document' else self.case['requests'][0])[field] = changed
                self.assertFalse(automation.manual_document_review_current(self.case, doc))

    def test_legacy_manual_review_requires_complete_flags_and_unchanged_first_version(self):
        doc = self.manual_review(bind=False)
        self.assertTrue(automation.manual_document_review_current(self.case, doc))
        doc['version'] = 2
        self.assertFalse(automation.manual_document_review_current(self.case, doc))
        doc['version'] = 1
        doc['content_confirmed'] = False
        self.assertFalse(automation.manual_document_review_current(self.case, doc))

    def test_batch_progress_is_public_counts_without_source_ids(self):
        async def checked(kind, payload, **kwargs):
            if kwargs.get('progress'):
                kwargs['progress']({'completed': 0, 'total': 2, 'item_ids': ['private-source']})
                kwargs['progress']({'completed': 2, 'total': 2, 'item_ids': []})
            return await supported(kind, payload)
        self.local.side_effect = checked
        updates = []
        asyncio.run(automation.advance(self.case, progress=updates.append))
        batches = [entry['progress'] for entry in updates if entry.get('progress')]
        self.assertTrue(batches)
        self.assertEqual(batches[-1]['completed'], 2)
        self.assertTrue(all(set(entry) == {'completed', 'total', 'label'} for entry in batches))

    def test_liquidation_failure_routes_reason_and_strategy_to_lawyer(self):
        self.inputs['assets'][0]['owned_value'] = 80000000
        self.run_pipeline()
        self.assertEqual(self.case['ax_pipeline']['stage'], 'human_review')
        self.assertTrue(self.case['drafts'][-1]['human_review_required'])
        self.assertTrue(any(s['code'] == 'LIQUIDATION' for s in self.case['ax_pipeline']['strategies']))
        self.assertIn('매각만으로', self.case['ax_pipeline']['strategies'][0]['description'])

    def test_independent_strategy_failure_still_builds_preliminary_generation(self):
        self.strategy.return_value = {'passed': False, 'status': 'needs_review'}
        self.run_pipeline()
        self.assertTrue(self.case['drafts'][-1]['human_review_required'])
        self.assertIn('STRATEGY_VERIFICATION', {r['code'] for r in self.case['ax_pipeline']['reasons']})

    def test_pending_applicable_amendment_retains_legal_issue_in_first_draft(self):
        pending = {'signature': 'new-law', 'active_version': 'v2', 'as_of': '2026-10-03',
                   'pending_changes': [{'source_id': 'current-rule', 'reason': '경과규정 변경'}]}
        with patch('apps.api.legal_watch.case_policy_status', return_value=pending):
            self.run_pipeline()
        self.assertEqual([call.args[0] for call in self.local.call_args_list], ['document_selection', 'ocr', 'document'])
        self.assertEqual(self.case['ax_pipeline']['stage'], 'human_review')
        self.assertEqual(self.case['ax_pipeline']['reasons'][0]['code'], 'LEGAL_CHANGE_REVIEW')
        self.assertTrue(self.case['drafts'][-1]['human_review_required'])

    def test_pending_legal_change_cannot_bypass_incomplete_ocr(self):
        self.manual_review()
        original = self.existing_outputs()
        self.local.side_effect = AsyncMock(return_value={'status': 'unavailable', 'passed': False,
            'error': {'code': 'MODEL_TIMEOUT', 'message': '원문 대조 시간 초과'}})
        with patch('apps.api.legal_watch.case_policy_status', return_value={
                'signature': 'changed-law', 'pending_changes': [{'source_id': 'law', 'reason': '법령 변경'}]}):
            self.run_pipeline()
        self.assert_waiting_without_downstream(original)
        self.assertEqual(self.case['ax_pipeline']['reasons'][0]['code'], 'OCR_VERIFICATION_INCOMPLETE')

    def test_legal_policy_hold_preserves_completed_negative_ocr_as_review_required(self):
        async def negative(kind, payload, **kwargs):
            checked = await supported(kind, payload)
            return {**checked, 'status': 'needs_review', 'passed': False,
                    'findings': [{'item_id': item['id'], 'status': 'uncertain'} for item in payload['items']]
                    } if kind == 'ocr' else checked
        self.local.side_effect = negative
        with patch('apps.api.legal_watch.case_policy_status', return_value={
                'signature': 'changed-law', 'pending_changes': [{'source_id': 'law', 'reason': '법령 변경'}]}):
            self.run_pipeline()
        self.assertEqual(self.case['ax_pipeline']['stage'], 'human_review')
        stages = {step['id']: step['status'] for step in self.case['ax_pipeline']['steps']}
        self.assertEqual(stages['ocr'], 'review_required')
        self.assertEqual(stages['analysis'], 'review_required')
        self.assertTrue({'LEGAL_CHANGE_REVIEW', 'OCR_VERIFICATION'} <=
                        {reason['code'] for reason in self.case['ax_pipeline']['reasons']})

    def test_unverified_court_rule_preserves_completed_negative_ocr(self):
        self.case['court_request_plan'] = {'coverage': 'unverified'}
        async def negative(kind, payload, **kwargs):
            checked = await supported(kind, payload)
            return {**checked, 'status': 'needs_review', 'passed': False,
                    'findings': [{'item_id': item['id'], 'status': 'uncertain'} for item in payload['items']]
                    } if kind == 'ocr' else checked
        self.local.side_effect = negative
        self.run_pipeline()
        self.assertEqual(self.case['ax_pipeline']['stage'], 'human_review')
        self.assertEqual(next(step for step in self.case['ax_pipeline']['steps'] if step['id'] == 'ocr')['status'],
                         'review_required')
        self.assertTrue({'COURT_RULE_UNVERIFIED', 'OCR_VERIFICATION'} <=
                        {reason['code'] for reason in self.case['ax_pipeline']['reasons']})

    def test_grounded_writing_failure_preserves_available_artifacts_and_review_hold(self):
        self.narrative.return_value = {'status': 'unavailable', 'sections': [], 'verification': {'passed': False}}
        self.run_pipeline()
        self.assertEqual(self.case['ax_pipeline']['stage'], 'human_review')
        self.assertTrue(self.case['drafts'][-1]['human_review_required'])
        self.assertTrue(self.case['narrative_runs'])

    def test_missing_statement_history_creates_targeted_customer_request(self):
        self.narrative.return_value = {'status': 'needs_review', 'sections': [],
            'verification': {'passed': False, 'code': 'STATEMENT_INFORMATION_REQUIRED'}}
        self.run_pipeline()
        requests = [r for r in self.case['requests'] if r.get('managed_by') == 'statement_evidence']
        self.assertEqual(len(requests), 1)
        self.assertIn('처음 돈을 빌린 시기', requests[0]['public_review_note'])
        self.assertTrue(any(n.get('audience') == 'client' and n.get('request_id') == requests[0]['id']
                            for n in self.case['notifications']))

    def test_generated_statement_and_pdf_input_use_verified_narrative(self):
        self.run_pipeline()
        draft = self.case['drafts'][-1]
        from apps.api import court_forms
        values, origins = court_forms._values(self.case, None, None)
        self.assertEqual(values['statement'], '월 소득은 2800000원입니다.')
        self.assertEqual(origins['statement']['type'], 'grounded_narrative')
        review = [call.args[1] for call in self.local.call_args_list if call.args[0] == 'document'][0]
        paragraph = next(item for item in review['items'] if item['id'] == 'paragraph:statement:0')
        self.assertEqual(paragraph['source_ids'], ['fact:income1'])
        self.assertIn('fact:income1', {source['id'] for source in review['sources']})
        self.assertTrue(draft['legal_dependency_signature'])

    def test_post_generation_ai_failure_keeps_draft_and_review_history(self):
        async def result(kind, payload):
            return {'status': 'needs_review', 'passed': False} if kind == 'document' else await supported(kind, payload)
        self.local.side_effect = result
        self.run_pipeline()
        self.assertEqual(self.case['drafts'][-1]['status'], 'verification_required')
        self.assertEqual(self.case['ax_pipeline']['stage'], 'human_review')

    def test_semantic_paragraph_failure_gets_one_bounded_revision(self):
        attempts = []
        async def review(kind, payload):
            if kind == 'document':
                attempts.append(payload)
                if len(attempts) == 1:
                    return {'status': 'needs_review', 'passed': False, 'findings': [{
                        'item_id': 'paragraph:statement:0', 'status': 'uncertain',
                        'reason': '상담 진술을 증빙으로 단정하지 말고 근거에 맞게 보완'}]}
            return await supported(kind, payload)
        self.local.side_effect = review
        self.run_pipeline()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.narrative.await_count, 2)
        self.assertTrue(self.narrative.call_args.kwargs['revision_feedback'])
        self.assertEqual(self.case['drafts'][-1]['revision_attempts'], 1)
        self.assertEqual(self.case['ax_pipeline']['stage'], 'human_review')
        self.assertTrue(any(run['kind'] == 'document_before_revision' for run in self.case['verification_runs']))

    def test_input_change_invalidates_automation_calculation_and_draft(self):
        self.run_pipeline()
        domain.invalidate(self.case, '새 자료 도착')
        self.assertTrue(self.case['drafts'][-1]['stale'])
        self.assertTrue(self.case['legal_calculations'][-1]['stale'])
        self.assertTrue(self.case['ax_pipeline']['stale'])

    def test_progress_reports_real_stage_transitions(self):
        progress = []
        asyncio.run(automation.advance(self.case, progress=progress.append))
        self.assertEqual(progress[0]['stage'], 'validation')
        self.assertEqual(progress[-1]['stage'], 'document_verification')
        self.assertTrue(all(sum(s['status'] == 'running' for s in p['steps']) == 1 for p in progress))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = patch.object(store, 'DATA_DIR', Path(self.temp.name))
        self.patch.start(); self.addCleanup(self.patch.stop)
        store.initialize()
        self.case, self.inputs = fixture()
        self.user = {'id': 'lawyer', 'name': '변호사', 'role': 'lawyer'}
        self.case['documents'][0]['status'] = 'verified'
        self.case['drafts'] = [{'id': 'draft1', 'content_hash': 'hash1', 'created_at': store.now()}]
        self.case['structured_data'] = [{'id': 'data1', 'source_hash': 'source1', 'input_revision': 1, 'created_at': store.now(), 'facts': []}]
        store.insert_case(self.case)

    def test_immutable_separate_data_and_generated_versions(self):
        store.mutate(self.case['id'], 1, self.user, 'metadata', lambda c: c.update(summary='변경'))
        with store.db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM structured_case_data').fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT count(*) FROM generated_document_versions').fetchone()[0], 1)

    def test_outcome_requires_verified_source_and_known_generation(self):
        data = {'outcome': 'approved', 'reason': '원문 법원 인가 확인', 'bundle_id': 'draft1', 'source_document_id': 'pay'}
        with self.assertRaises(domain.DomainError):
            automation.record_outcome(self.case, data, {'role': 'staff'})
        self.case['documents'][0]['status'] = 'received'
        with self.assertRaises(domain.DomainError):
            automation.record_outcome(self.case, data, self.user)

    def test_recorded_correction_invalidates_outputs_and_notifies(self):
        data = {'outcome': 'correction', 'reason': '청산가치 산정 소명 필요', 'bundle_id': 'draft1', 'source_document_id': 'pay'}
        saved = store.mutate(self.case['id'], 1, self.user, 'court_outcome.recorded', lambda c: automation.record_outcome(c, data, self.user))
        self.assertTrue(saved['drafts'][0]['stale'])
        self.assertEqual(saved['ax_pipeline']['stage'], 'lawyer_review')
        with store.db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM court_outcomes').fetchone()[0], 1)
        with self.assertRaises(domain.DomainError):
            automation.record_outcome(saved, data, self.user)

    def test_client_projection_excludes_internal_checks_and_account_key(self):
        from apps.api.main import visible
        self.case['requests'][0].update(account_key='secret-account', account_masked='****12',
                                        automatic_validation={'private': 'internal reason'}, source_refs=[{'private': 'internal'}])
        self.case['notifications'] = [{'id': 'internal', 'audience': 'staff'}, {'id': 'customer', 'audience': 'client'}]
        public = visible(self.case, {'role': 'client', 'id': 'client'})
        self.assertNotIn('account_key', public['requests'][0])
        self.assertNotIn('automatic_validation', public['requests'][0])
        self.assertEqual([n['id'] for n in public['notifications']], ['customer'])
        self.assertNotIn('structured_data', public)


if __name__ == '__main__':
    unittest.main()
