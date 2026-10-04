"""Unknown analysis still produces grounded review documents, never filing approval."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pymupdf

from apps.api import automation, auto_documents, preliminary_drafting, grounded_drafting, store
from tests.test_automation import fixture, supported


class PreliminaryDraftTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.case, self.inputs = fixture()
        self.case['documents'][0]['status'] = 'verified'
        self.case['requests'][0]['status'] = 'fulfilled'
        self.check = {'passed': True, 'status': 'passed', 'checked_item_ids': ['income1'], 'findings': []}
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patch.object(store, 'DATA_DIR', Path(directory.name)).start()
        patch.object(automation, '_retrieve', return_value=[]).start()
        patch.object(automation, 'approved_examples', return_value=[]).start()
        patch.object(grounded_drafting, 'signature', return_value='bounded-evidence-signature').start()
        self.writer = patch.object(grounded_drafting, 'compose', new=AsyncMock(return_value={
            'status': 'completed', 'version': grounded_drafting.VERSION, 'input_signature': 'bounded-evidence-signature',
            'verification': {'passed': True}, 'sections': [{'id': 'statement', 'paragraphs': [{
                'text': '급여자료에 기재된 월 소득은 2800000원입니다.', 'source_ids': ['pay'],
                'quotes': [{'source_id': 'pay', 'quote': '월 소득 2800000원'}]}]}], 'source_refs': []})).start()
        self.review = patch('apps.api.verification.run_local_verification_batched', side_effect=supported).start()
        self.artifact_review = patch.object(auto_documents, 'verify_artifacts', new=AsyncMock(return_value={
            'status': 'needs_review', 'passed': False, 'issues': [{'code': 'FORM_FIELDS_REQUIRED'}]})).start()
        self.addCleanup(patch.stopall)

    async def test_progress_forwards_only_public_counts_and_stage_labels(self):
        updates = []
        async def checked(kind, payload, **kwargs):
            if kwargs.get('progress'):
                kwargs['progress']({'completed': 1, 'total': 2, 'item_ids': ['private-document-id'], 'raw_text': 'never-project'})
            return await supported(kind, payload)
        async def artifact_checked(case, records, calculation, **kwargs):
            if kwargs.get('progress'):
                kwargs['progress']({'completed': 1, 'total': 7, 'label': '진술서 · 원문 대조 1/3',
                                    'document_id': 'private-document-id'})
            return {'status': 'needs_review', 'passed': False}
        self.review.side_effect = checked
        self.artifact_review.side_effect = artifact_checked
        with patch.object(auto_documents, 'prepare', return_value=([], [])):
            await preliminary_drafting.create(self.case,
                [{'code': 'CALCULATION_REVIEW', 'reason': '계산 판단 확인 필요'}], {'analysis'}, ocr_check=self.check,
                progress=lambda stage, index, message, **kwargs: updates.append(kwargs.get('batch_progress')))
        batches = [update for update in updates if update]
        self.assertEqual(len(batches), 2)
        self.assertTrue(all(set(update) == {'completed', 'total', 'label'} for update in batches))
        self.assertEqual((batches[0]['completed'], batches[0]['total']), (1, 2))
        self.assertEqual((batches[1]['completed'], batches[1]['total']), (1, 7))
        self.assertNotIn('private-document-id', str(batches))
        self.assertNotIn('never-project', str(batches))

    async def test_blocked_calculation_still_generates_actual_forms_with_sourced_statement(self):
        original = copy.deepcopy(self.case['extraction_candidates'])
        draft = await preliminary_drafting.create(self.case,
            [{'code': 'CALCULATION_REVIEW', 'stage': 'analysis', 'reason': '인정 생계비 판단 확인 필요'}],
            {'analysis'}, calculation={'id': 'unfinished', 'status': 'blocked', 'summary': {'monthly_deposit': 9876543}},
            ocr_check=self.check)
        self.assertTrue(draft['human_review_required'])
        self.assertFalse(draft['submission_ready'])
        self.assertEqual(draft['analysis_calculation_id'], 'unfinished')
        self.assertIsNone(draft['calculation_id'])
        self.assertEqual(self.writer.call_args.args[3], {})
        self.assertEqual(self.writer.call_args.kwargs['section_ids'], ['statement'])
        self.assertEqual(self.case['extraction_candidates'], original)
        self.assertEqual(len(self.case['court_documents']), 7)
        self.assertTrue((store.DATA_DIR / 'generated' / self.case['id'] / (draft['id'] + '.docx')).exists())
        statement = next(record for record in self.case['court_documents'] if record['template_id'] == 'D5105')
        with pymupdf.open(store.DATA_DIR / 'generated' / self.case['id'] / (statement['id'] + '.pdf')) as pdf:
            self.assertIn('급여자료에 기재된 월 소득은 2800000원입니다.', ''.join(page.get_text() for page in pdf))
        plan = next(record for record in self.case['court_documents'] if record['template_id'] == 'D5110')
        self.assertTrue(plan['preview']['missing_fields'])
        self.assertFalse(plan['preview']['submission_ready'])
        self.assertNotIn(9876543, [field['value'] for field in plan['preview']['fields']])
        self.assertEqual(draft['status'], 'verification_required')
        self.assertIsNone(self.artifact_review.call_args.args[2])

    async def test_ocr_outage_outputs_only_safe_identity_and_leaves_numbers_blank(self):
        draft = await preliminary_drafting.create(self.case,
            [{'code': 'OCR_VERIFICATION', 'reason': '원문 대조 시간 초과', 'stage': 'ocr'}], {'ocr', 'analysis'},
            ocr_check={'passed': False, 'checked_item_ids': []},
            local_failure={'status': 'unavailable', 'error': {'code': 'MODEL_TIMEOUT'}})
        self.writer.assert_not_awaited()
        self.review.assert_not_called()
        self.artifact_review.assert_not_awaited()
        self.assertEqual(draft['ai_review']['status'], 'unavailable')
        self.assertFalse(draft['ai_review']['passed'])
        self.assertTrue(self.case['court_documents'])
        self.assertTrue(all(field['value'] is None for section in draft['sections'] for field in section['fields'] if field['key'] != 'client_name'))
        for record in self.case['court_documents']:
            self.assertFalse(record['ai_review']['passed'])
            self.assertTrue(record['human_review_required'])

    def test_only_exact_supported_values_survive_and_original_case_is_unchanged(self):
        self.case['extraction_candidates'].extend([
            {'id': 'wrong-number', 'key': 'assets_total', 'value': 9000000, 'document_id': 'pay', 'quote': '월 소득 2800000원', 'status': 'accepted'},
            {'id': 'unreviewed', 'key': 'total_debt', 'value': 2800000, 'document_id': 'pay', 'quote': '월 소득 2800000원', 'status': 'accepted'}])
        self.check['checked_item_ids'].append('wrong-number')
        before = copy.deepcopy(self.case)
        safe, omitted = preliminary_drafting.render_case(self.case, self.check)
        self.assertEqual([row['id'] for row in safe['extraction_candidates'] if row['key'] != 'client_name'], ['income1'])
        self.assertEqual(self.case, before)
        self.assertIn('assets_total', omitted)
        self.assertIn('total_debt', omitted)

    def test_conflicting_supported_values_are_left_unknown(self):
        self.case['documents'][0]['text'] += '\n다른 월 소득 2700000원'
        self.case['extraction_candidates'].append({'id': 'other', 'key': 'monthly_income', 'value': 2700000,
            'document_id': 'pay', 'quote': '다른 월 소득 2700000원', 'status': 'accepted'})
        self.check['checked_item_ids'].append('other')
        safe, omitted = preliminary_drafting.render_case(self.case, self.check)
        self.assertEqual([row for row in safe['extraction_candidates'] if row['key'] != 'client_name'], [])
        self.assertTrue(any('출처 간 값' in value for value in omitted))
