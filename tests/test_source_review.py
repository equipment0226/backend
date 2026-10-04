"""One source review atomically confirms the original and all current candidates.

Uses temporary SQLite and a non-running executor. No live case or model calls.
"""
import copy
import hashlib
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from apps.api import automation, ax_service, domain, drafting, extraction_readiness, main, store

REAL_DOCUMENT_STATE = extraction_readiness.document_state


class SourceReviewTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(store, 'DATA_DIR', Path(folder)))
        self.stack.enter_context(patch.dict(os.environ, {'DEBTOFF_AUTO_AX': '1'}))
        self.stack.enter_context(patch.object(ax_service, 'knowledge_signature', return_value='test-law'))
        self.pool = self.stack.enter_context(patch.object(main, 'POOL', Mock()))
        self.generate = self.stack.enter_context(patch.object(drafting, 'generate', side_effect=AssertionError('No inline drafting')))
        # Parser completeness is exercised by extraction-readiness tests; these
        # transaction fixtures deliberately use a small hand-authored candidate set.
        self.readiness = self.stack.enter_context(patch('apps.api.extraction_readiness.document_state',
            return_value={'status': 'completed', 'parsed_pages': 1, 'total_pages': 1,
                          'extracted_count': 3, 'can_review': True, 'message': '추출 완료'}))
        store.initialize()
        self.user = {'id': 'staff', 'name': '담당 직원', 'role': 'staff', 'org_id': 'office-1'}
        self.stack.enter_context(patch.dict(main.app.dependency_overrides, {main.current_user: lambda: self.user}))
        self.client = TestClient(main.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.case = domain.new_case('합성 검토 고객', 'CT01', '서울회생법원', '통합 검토 시험', True)
        self.case['intake'] = {'status': 'completed'}
        self.case['requests'] = [{'id': 'req', 'title': '급여 명세', 'status': 'received',
                                  'document_ids': ['doc'], 'version': 1}]
        source = b'%PDF-synthetic-immutable-upload'
        self.original = store.DATA_DIR / 'uploads' / self.case['id'] / 'original.pdf'
        self.original.parent.mkdir(parents=True)
        self.original.write_bytes(source)
        self.source_hash = hashlib.sha256(source).hexdigest()
        self.case['documents'] = [{'id': 'doc', 'filename': '원본.pdf', 'storage_name': 'original.pdf',
            'sha256': self.source_hash, 'version': 1, 'request_id': 'req', 'status': 'received',
            'text': '월 실수령액 3,200,000원\n근무처 합성 사업장',
            'page_texts': [{'page': 1, 'text': '월 실수령액 3,200,000원\n근무처 합성 사업장'}],
            'scope_confirmed': False, 'content_confirmed': False, 'person_confirmed': False,
            'automated_check': {'coverage_status': 'needs_more'},
            'ai_review': {'status': 'needs_review', 'passed': False}},
            {'id': 'other', 'status': 'received', 'text': '다른 서류', 'version': 1}]
        self.case['extraction_candidates'] = [
            {'id': 'income', 'key': 'monthly_income', 'value': 3200000, 'status': 'candidate',
             'document_id': 'doc', 'quote': '월 실수령액 3,200,000원'},
            {'id': 'employer', 'key': 'employer', 'value': '합성 사업장', 'status': 'source_checked',
             'document_id': 'doc', 'quote': '근무처 합성 사업장'},
            {'id': 'ownership', 'key': 'vehicle_ownership', 'value': True, 'status': 'candidate', 'document_id': 'doc'},
            {'id': 'elsewhere', 'key': 'total_debt', 'value': 72000000, 'status': 'candidate', 'document_id': 'other'},
            *[{'id': status, 'key': 'ignored', 'value': status, 'status': status, 'document_id': 'doc'}
              for status in ('rejected', 'superseded', 'quarantined')]]
        self.case['drafts'] = [{'id': 'previous', 'input_revision': 1, 'stale': False,
            'sections': [{'content': '원문에 근거한 기존 작성본'}], 'ai_review': {'passed': False}}]
        self.case['court_documents'] = [{'id': 'previous-pdf', 'input_revision': 1, 'stale': False,
            'fields': {'statement': '기존 작성 내용'}, 'ai_review': {'status': 'needs_review', 'passed': False}}]
        store.insert_case(self.case)
        self.path = '/api/cases/' + self.case['id'] + '/documents/doc/review'

    def body(self, **extra):
        return {'expected_version': self.case['version'], 'scope_confirmed': True,
                'content_confirmed': True, 'person_confirmed': True,
                'reason': '원문과 추출된 내용을 함께 확인했습니다.', **extra}

    def assert_unchanged(self, before=None):
        self.assertEqual(store.get_case(self.case['id']), before or self.case)
        self.pool.submit.assert_not_called()

    def test_one_click_accepts_all_current_candidates_once_and_preserves_originals(self):
        response = self.client.post(self.path, json=self.body())
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['version'], 2)
        self.assertEqual(result['input_revision'], 2)
        self.assertEqual(len(result['audit']), 1)
        self.pool.submit.assert_called_once()
        self.assertEqual(len(result['ax_runs']), 1)
        doc = result['documents'][0]
        self.assertEqual(doc['status'], 'verified')
        self.assertFalse(doc['auto_verified'])
        self.assertFalse(doc['extraction_review']['ai_verified'])
        self.assertEqual(doc['ai_review'], self.case['documents'][0]['ai_review'])
        self.assertTrue(automation.manual_document_review_current(result, doc))
        for key in ('text', 'page_texts', 'sha256', 'storage_name', 'version'):
            self.assertEqual(doc[key], self.case['documents'][0][key])
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.source_hash)
        self.assertEqual(result['requests'][0]['status'], 'fulfilled')
        for candidate in result['extraction_candidates'][:3]:
            self.assertEqual(candidate['status'], 'accepted')
            self.assertEqual(candidate['review']['verification_type'], 'human_review')
        self.assertEqual(result['extraction_candidates'][3:], self.case['extraction_candidates'][3:])
        self.assertEqual(result['drafts'][0]['sections'], self.case['drafts'][0]['sections'])
        self.assertEqual(result['court_documents'][0]['fields'], self.case['court_documents'][0]['fields'])
        self.assertTrue(result['drafts'][0]['stale'])
        self.generate.assert_not_called()

    def test_delta_correction_and_rejection_retain_source_binding_and_previous_values(self):
        edits = [{'candidate_id': 'income', 'decision': 'correct', 'value': 3300000},
                 {'candidate_id': 'employer', 'decision': 'reject'},
                 {'candidate_id': 'ownership', 'decision': 'correct', 'value': False}]
        response = self.client.post(self.path, json=self.body(candidate_reviews=edits))
        self.assertEqual(response.status_code, 200, response.text)
        income, employer, ownership = response.json()['extraction_candidates'][:3]
        self.assertEqual(income['value'], 3300000)
        self.assertEqual(income['original_value'], 3200000)
        self.assertEqual(income['origin'], 'human_correction')
        self.assertEqual(income['quote'], self.case['extraction_candidates'][0]['quote'])
        self.assertEqual(income['review']['document_id'], 'doc')
        self.assertEqual(income['review']['source_sha256'], self.source_hash)
        self.assertEqual(income['review']['source_version'], 1)
        self.assertEqual(income['review']['actor_id'], 'staff')
        self.assertEqual(income['source_edit']['corrected_value'], 3300000)
        self.assertEqual(income['source_edit']['reviewed_basis']['original_value'], 3200000)
        self.assertEqual(income['source_edit']['source_document_signature'],
                         automation.document_review_signature(response.json(), response.json()['documents'][0]))
        self.assertEqual(income['review_history'][0]['previous']['value'], 3200000)
        self.assertEqual(employer['status'], 'rejected')
        self.assertIs(ownership['value'], False)
        self.assertEqual(response.json()['input_revision'], 2)
        self.pool.submit.assert_called_once()

    def test_later_unchanged_confirmation_preserves_explicit_correction_binding(self):
        first = self.client.post(self.path, json=self.body(candidate_reviews=[
            {'candidate_id': 'income', 'decision': 'correct', 'value': 3300000}]))
        self.assertEqual(first.status_code, 200, first.text)
        correction = first.json()['extraction_candidates'][0]['source_edit']
        second = self.client.post(self.path, json=self.body(expected_version=first.json()['version']))
        self.assertEqual(second.status_code, 200, second.text)
        candidate = second.json()['extraction_candidates'][0]
        self.assertEqual(candidate['source_edit'], correction)
        self.assertEqual(candidate['origin'], 'human_correction')
        self.assertEqual(candidate['review']['decision'], 'accept')
        self.assertEqual(candidate['value'], 3300000)
        self.assertEqual(len(candidate['review_history']), 2)

    def test_other_document_candidate_rolls_back_earlier_valid_delta(self):
        response = self.client.post(self.path, json=self.body(candidate_reviews=[
            {'candidate_id': 'income', 'decision': 'correct', 'value': 3300000},
            {'candidate_id': 'elsewhere', 'decision': 'accept'}]))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()['code'], 'CANDIDATE_DOCUMENT_MISMATCH')
        self.assert_unchanged()

    def test_foreign_case_candidate_is_not_found(self):
        foreign = domain.new_case('다른 합성 고객', 'CT01', '서울회생법원', '다른 사건', True)
        foreign['extraction_candidates'] = [{'id': 'foreign-only', 'document_id': 'doc', 'key': 'employer', 'value': '다름'}]
        store.insert_case(foreign)
        response = self.client.post(self.path, json=self.body(candidate_reviews=[
            {'candidate_id': 'foreign-only', 'decision': 'accept'}]))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()['code'], 'ITEM_NOT_FOUND')
        self.assert_unchanged()

    def test_duplicate_or_inactive_deltas_are_atomic_errors(self):
        for edits in ([{'candidate_id': 'income', 'decision': 'accept'}] * 2,
                      *[[{'candidate_id': status, 'decision': 'correct', 'value': '수정'}]
                        for status in ('rejected', 'superseded', 'quarantined')]):
            with self.subTest(edits=edits):
                response = self.client.post(self.path, json=self.body(candidate_reviews=edits))
                self.assertEqual(response.status_code, 422, response.text)
                self.assert_unchanged()

    def test_invalid_numeric_correction_rejects_whole_review(self):
        for value in (True, -1, 3200000.5, '3200000', None):
            with self.subTest(value=value):
                response = self.client.post(self.path, json=self.body(candidate_reviews=[
                    {'candidate_id': 'income', 'decision': 'correct', 'value': value}]))
                self.assertEqual(response.status_code, 422, response.text)
                self.assert_unchanged()

    def test_stale_version_and_duplicate_submit_never_schedule_twice(self):
        first = self.client.post(self.path, json=self.body())
        self.assertEqual(first.status_code, 200, first.text)
        before = store.get_case(self.case['id'])
        self.pool.submit.reset_mock()
        second = self.client.post(self.path, json=self.body())
        self.assertEqual(second.status_code, 409, second.text)
        self.assert_unchanged(before)

    def test_client_and_unassigned_staff_are_denied(self):
        for role, user_id, expected in (('client', 'client', 403), ('staff', 'unassigned', 404)):
            with self.subTest(role=role):
                self.user.update(role=role, id=user_id)
                response = self.client.post(self.path, json=self.body())
                self.assertEqual(response.status_code, expected, response.text)
                self.assert_unchanged()

    def test_missing_identity_confirmation_is_not_document_or_ai_pass(self):
        response = self.client.post(self.path, json=self.body(person_confirmed=False))
        self.assertEqual(response.status_code, 200, response.text)
        doc = response.json()['documents'][0]
        self.assertEqual(doc['status'], 'needs_more')
        self.assertNotIn('manual_verification', doc)
        self.assertFalse(doc['auto_verified'])
        self.assertEqual(response.json()['requests'][0]['status'], 'needs_more')

    def test_quarantined_source_and_explicit_identity_conflict_cannot_be_confirmed(self):
        for mutation in ({'status': 'quarantined'}, {'status': 'superseded'},
                         {'automated_check': {'coverage_status': 'identity_conflict'}}):
            with self.subTest(mutation=mutation):
                case = copy.deepcopy(self.case)
                case['documents'][0].update(mutation)
                with store.db() as con:
                    con.execute('UPDATE cases SET body=? WHERE id=?', (store.dumps(case), case['id']))
                response = self.client.post(self.path, json=self.body())
                self.assertEqual(response.status_code, 422, response.text)
                self.assert_unchanged(case)

    def test_original_text_cannot_be_overwritten_by_review_payload(self):
        response = self.client.post(self.path, json=self.body(text='바뀐 원문'))
        self.assertEqual(response.status_code, 422, response.text)
        self.assert_unchanged()

    def test_pending_or_failed_extraction_blocks_both_review_routes(self):
        for status in ('pending', 'running', 'failed'):
            self.readiness.return_value = {'status': status, 'can_review': False, 'message': '추출 완료 후 검토해 주세요.'}
            for suffix in ('review', 'verify'):
                with self.subTest(status=status, route=suffix):
                    response = self.client.post(self.path.rsplit('/', 1)[0] + '/' + suffix, json=self.body())
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(response.json()['code'], 'EXTRACTION_PENDING')
                    self.assert_unchanged()

    def test_actual_parser_completion_is_required_before_atomic_review(self):
        self.readiness.side_effect = REAL_DOCUMENT_STATE
        case = copy.deepcopy(self.case)
        case['extraction_candidates'] = []
        with store.db() as con:
            con.execute('UPDATE cases SET body=? WHERE id=?', (store.dumps(case), case['id']))
        blocked = self.client.post(self.path, json=self.body())
        self.assertEqual(blocked.status_code, 422, blocked.text)
        self.assertEqual(blocked.json()['code'], 'EXTRACTION_PENDING')
        self.assert_unchanged(case)
        document = case['documents'][0]
        rows = extraction_readiness.candidates(document)
        self.assertTrue(rows)
        ax_service.enrich_candidates(case, rows, document['id'])
        extraction_readiness.capture(case, document, rows)
        self.assertTrue(REAL_DOCUMENT_STATE(case, document)['can_review'])
        with store.db() as con:
            con.execute('UPDATE cases SET body=? WHERE id=?', (store.dumps(case), case['id']))
        response = self.client.post(self.path, json=self.body())
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result['documents'][0]['extraction_state']['can_review'])
        self.assertTrue(all(row['status'] == 'accepted' for row in result['extraction_candidates']))
        self.assertEqual(result['input_revision'], 2)
        self.pool.submit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
