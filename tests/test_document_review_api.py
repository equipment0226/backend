"""Corrected PDF review authorization, evidence freshness and audit persistence."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import pymupdf
from fastapi.testclient import TestClient

os.environ.setdefault('DEBTOFF_DEMO_MODE', '1')
from apps.api import domain, store
from apps.api.main import app


class DocumentReviewApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.patches = [patch.object(store, 'DATA_DIR', Path(cls.temp.name)),
                       patch('apps.api.ax_service.maybe_schedule', return_value=None),
                       patch.dict(os.environ, {'DEBTOFF_DEMO_MODE': '1', 'DEBTOFF_AUTO_AX': '0',
                                               'DEBTOFF_LEGAL_WATCH': '0'})]
        for item in cls.patches:
            item.start()
        cls.context = TestClient(app, raise_server_exceptions=False)
        cls.client = cls.context.__enter__()
        cls.headers = {}
        for role in ('staff', 'lawyer', 'client'):
            login = cls.client.post('/api/auth/login', json={'username': 'demo-' + role, 'password': 'debtoff-demo'})
            cls.headers[role] = {'Authorization': 'Bearer ' + login.json()['token']}

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)
        for item in reversed(cls.patches):
            item.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.case = domain.new_case('synthetic', 'CT01', '서울회생법원', 'completed interview', True)
        self.case['client_user_id'] = 'client'
        pdf = pymupdf.open()
        pdf.new_page().insert_text((30, 40), 'synthetic')
        raw = pdf.tobytes()
        pdf.close()
        self.folder = store.DATA_DIR / 'generated' / self.case['id']
        self.folder.mkdir(parents=True)
        (self.folder / 'review-doc.pdf').write_bytes(raw)
        self.case['court_documents'] = [{'id': 'review-doc', 'input_revision': self.case['input_revision'],
            'status': 'draft', 'sha256': hashlib.sha256(raw).hexdigest(), 'approval': None,
            'ai_review': {'passed': False, 'status': 'pending'},
            'preview': {'template': {'pages': [0]}, 'fields': [
                {'key': 'client_name', 'value': 'synthetic', 'page': 0, 'rect': [20, 20, 200, 65],
                 'source': {'type': 'staff_input'}}], 'missing_fields': [], 'overflow_fields': []}}]
        store.insert_case(self.case)
        self.path = '/api/cases/' + self.case['id'] + '/court-documents/review-doc/verify'
        self.run = AsyncMock(return_value={'status': 'passed', 'passed': True, 'findings': []})
        self.mock = patch('apps.api.verification.run_local_verification_batched', new=self.run)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def post(self, role='staff', version=None):
        return self.client.post(self.path, headers=self.headers[role], json={
            'expected_version': version or store.get_case(self.case['id'])['version'],
            'reason': '수정 문서와 원문을 다시 대조합니다.'})

    def test_staff_recheck_persists_history_but_does_not_grant_human_approval(self):
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        doc = response.json()['court_documents'][0]
        self.assertEqual(doc['status'], 'automatically_verified')
        self.assertTrue(doc['ai_review']['passed'])
        self.assertFalse(doc['ai_review']['submission_ready'])
        self.assertIsNone(doc['approval'])
        self.assertEqual(doc['review_history'][0]['previous_review']['status'], 'pending')
        self.assertEqual(doc['review_history'][0]['actor'], 'staff')
        self.assertEqual(response.json()['input_revision'], self.case['input_revision'])

    def test_customer_cannot_run_internal_review_and_stale_version_does_not_call_model(self):
        self.assertEqual(self.post('client').status_code, 403)
        self.assertEqual(self.post(version=100).status_code, 409)
        self.run.assert_not_awaited()

    def test_changed_pdf_is_rejected_before_ai(self):
        (self.folder / 'review-doc.pdf').write_bytes(b'changed')
        response = self.post()
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()['code'], 'ARTIFACT_CHANGED')
        self.run.assert_not_awaited()

    def test_approved_document_requires_a_new_version(self):
        current = store.get_case(self.case['id'])
        store.mutate(current['id'], current['version'], {'name': '시험', 'role': 'lawyer'}, 'test.approve',
                     lambda case: case['court_documents'][0].update(approval={'actor': 'lawyer'}))
        response = self.post()
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()['code'], 'FORM_ALREADY_APPROVED')
        self.run.assert_not_awaited()

    def test_source_changed_during_ai_does_not_publish_review(self):
        async def change_source(*args, **kwargs):
            current = store.get_case(self.case['id'])
            store.mutate(current['id'], current['version'], {'name': '시험', 'role': 'staff'}, 'test.change',
                         lambda case: case.update(input_revision=case['input_revision'] + 1))
            return {'passed': True, 'status': 'passed', 'findings': []}
        self.run.side_effect = change_source
        response = self.post()
        self.assertEqual(response.status_code, 409, response.text)
        doc = store.get_case(self.case['id'])['court_documents'][0]
        self.assertEqual(doc['ai_review']['status'], 'pending')
        self.assertFalse(doc.get('review_history'))


if __name__ == '__main__':
    unittest.main()
