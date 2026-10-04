"""Staff can resolve an unclassified original without uploading its bytes again."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from fastapi.testclient import TestClient
from apps.api import ax_service, domain, extraction_readiness, main, store


class DocumentAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(store, 'DATA_DIR', Path(self.stack.enter_context(tempfile.TemporaryDirectory()))))
        self.stack.enter_context(patch.dict(os.environ, {'DEBTOFF_AUTO_AX': '0'}))
        self.schedule = self.stack.enter_context(patch.object(ax_service, 'maybe_schedule'))
        self.user = {'id': 'staff', 'name': '검증직원', 'role': 'staff', 'org_id': 'office-1'}
        self.stack.enter_context(patch.dict(main.app.dependency_overrides, {main.current_user: lambda: self.user}))
        store.initialize()
        case = domain.new_case('검증고객', 'CT01', '서울회생법원', '미분류 자료 연결 검사', True)
        case['client_user_id'] = 'client'
        case['intake'] = {'status': 'completed'}
        case['requests'] = [{'id': 'request', 'catalog_id': 'D07', 'title': '급여명세서',
                             'status': 'requested', 'version': 1, 'document_ids': []}]
        store.insert_case(case)
        self.case_id = case['id']
        self.client = self.stack.enter_context(TestClient(main.app, raise_server_exceptions=False))
        self.original = '급여명세서\n월 실수령액: 2,800,000원'.encode('utf-8')
        response = self.client.post('/api/cases/' + case['id'] + '/documents', data={'expected_version': 1},
                                    files={'file': ('random.txt', self.original, 'text/plain')})
        self.assertEqual(response.status_code, 200, response.text)
        self.before = store.get_case(case['id'])
        self.doc_id = self.before['documents'][0]['id']
        self.path = '/api/cases/' + case['id'] + '/documents/' + self.doc_id + '/request'
        self.schedule.reset_mock()

    def assign(self, **changes):
        return self.client.post(self.path, json={'expected_version': self.before['version'], 'request_id': 'request',
            'reason': '원문을 보고 급여 자료임을 확인했습니다.', **changes})

    def change_fixture(self, fn):
        self.before = store.mutate(self.case_id, self.before['version'], self.user, 'test.setup', fn)

    def test_assignment_preserves_original_and_extracted_facts_and_requires_review(self):
        response = self.assign()
        self.assertEqual(response.status_code, 200, response.text)
        case = store.get_case(self.case_id)
        document = case['documents'][0]
        self.assertEqual(case['version'], self.before['version'] + 1)
        self.assertEqual(case['input_revision'], self.before['input_revision'] + 1)
        self.assertEqual(case['extraction_candidates'], self.before['extraction_candidates'])
        self.assertEqual(case['facts'], self.before['facts'])
        for field in ('text', 'page_texts', 'sha256', 'storage_name', 'extraction_manifest', 'version'):
            self.assertEqual(document[field], self.before['documents'][0][field])
        self.assertEqual((store.DATA_DIR / 'uploads' / self.case_id / document['storage_name']).read_bytes(), self.original)
        self.assertEqual(document['request_id'], 'request')
        self.assertEqual(document['status'], 'received')
        self.assertFalse(document['scope_confirmed'])
        self.assertFalse(document['classification_review_required'])
        self.assertTrue(document['scope_review_required'])
        self.assertEqual(case['requests'][0]['document_ids'], [self.doc_id])
        self.assertEqual(case['requests'][0]['status'], 'received')
        self.assertEqual(case['requests'][0]['collection_progress']['verified'], 0)
        self.assertEqual(document['request_assignment_history'][0]['actor_id'], 'staff')
        self.assertTrue(extraction_readiness.document_state(case, document)['can_review'])
        self.schedule.assert_called_once()

    def test_repeated_assignment_and_stale_submission_do_not_mutate(self):
        self.assertEqual(self.assign().status_code, 200)
        saved = store.get_case(self.case_id)
        self.assertEqual(self.assign().status_code, 409)
        response = self.assign(expected_version=saved['version'])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'DOCUMENT_ALREADY_ASSIGNED')
        self.assertEqual(store.get_case(self.case_id), saved)
        self.schedule.assert_called_once()

    def test_retired_request_and_foreign_request_are_not_assignable(self):
        self.change_fixture(lambda case: case['requests'][0].update(status='fulfilled', no_longer_required=True))
        response = self.assign()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'REQUEST_WITHDRAWN')
        self.assertEqual(store.get_case(self.case_id), self.before)
        other = domain.new_case('다른고객', 'CT01', '서울회생법원', '', True)
        other['requests'] = [{'id': 'foreign-request', 'status': 'requested', 'title': '다른 사건 요청'}]
        store.insert_case(other)
        response = self.assign(request_id='foreign-request')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'ITEM_NOT_FOUND')
        self.assertEqual(store.get_case(self.case_id), self.before)
        self.schedule.assert_not_called()

    def test_identity_conflict_is_not_bypassed_by_manual_assignment(self):
        self.change_fixture(lambda case: case['documents'][0].update(
            import_routing={'identity_status': 'conflict'}, classification_review_required=True))
        response = self.assign()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'DOCUMENT_IDENTITY_CONFLICT')
        self.assertEqual(store.get_case(self.case_id), self.before)
        self.schedule.assert_not_called()

    def test_quarantine_cannot_be_reactivated(self):
        self.change_fixture(lambda case: case['documents'][0].update(status='quarantined'))
        response = self.assign()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'INACTIVE_DOCUMENT')
        self.assertEqual(store.get_case(self.case_id), self.before)

    def test_unknown_subject_name_can_be_assigned_but_is_not_confirmed(self):
        self.change_fixture(lambda case: case['documents'][0].update(
            import_routing={'status': 'identity_review', 'identity_status': 'unresolved'}, classification_review_required=True))
        self.assertEqual(self.assign().status_code, 200)
        document = store.get_case(self.case_id)['documents'][0]
        self.assertFalse(document['person_confirmed'])
        self.assertEqual(document['import_routing']['identity_status'], 'unresolved')
        self.assertEqual(document['request_assignment_history'][0]['request_id'], 'request')

    def test_old_standalone_review_is_preserved_in_history_but_new_scope_reopens_review(self):
        def setup(case):
            case['documents'][0].update(status='verified', scope_confirmed=True,
                manual_verification={'signature': 'prior'}, extraction_review={'reason': '과거 검토'})
            case['court_documents'] = [{'id': 'old', 'stale': False}]
            case['legal_calculations'] = [{'id': 'old', 'stale': False}]
        self.change_fixture(setup)
        self.assertEqual(self.assign().status_code, 200)
        case = store.get_case(self.case_id)
        document = case['documents'][0]
        self.assertNotIn('manual_verification', document)
        self.assertEqual(document['request_assignment_history'][0]['previous']['manual_verification'], {'signature': 'prior'})
        self.assertTrue(case['court_documents'][0]['stale'])
        self.assertTrue(case['legal_calculations'][0]['stale'])

    def test_client_cannot_assign_but_assigned_lawyer_can(self):
        self.user = {'id': 'client', 'name': '검증고객', 'role': 'client', 'org_id': 'office-1'}
        self.assertEqual(self.assign().status_code, 403)
        self.assertEqual(store.get_case(self.case_id), self.before)
        self.user = {'id': 'lawyer', 'name': '담당변호사', 'role': 'lawyer', 'org_id': 'office-1'}
        self.assertEqual(self.assign().status_code, 200)


if __name__ == '__main__':
    unittest.main()
