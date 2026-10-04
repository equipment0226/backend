"""Live ASGI/SQLite filing boundaries in an isolated synthetic test directory."""
import copy
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api import store
from tests import test_filing_gate


class FilingApiTests(unittest.TestCase):
    def setUp(self):
        fixture = test_filing_gate.FilingGateTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.case = fixture.case
        self.fixture = fixture
        environment = patch.dict(os.environ, {'DEBTOFF_DEMO_MODE': '1', 'DEBTOFF_START_PROFILE': 'demo',
                                               'DEBTOFF_AUTO_AX': '0', 'DEBTOFF_LEGAL_WATCH': '0'})
        environment.start(); self.addCleanup(environment.stop)
        schedule = patch('apps.api.ax_service.maybe_schedule', return_value=None)
        schedule.start(); self.addCleanup(schedule.stop)
        # Authentication throttles belong to each isolated ASGI fixture too;
        # preceding tests must not consume this fixture's login allowance.
        login_attempts = patch('apps.api.main.LOGIN_ATTEMPTS', {})
        login_attempts.start(); self.addCleanup(login_attempts.stop)
        from apps.api.main import app
        self.client = TestClient(app, raise_server_exceptions=False)
        self.client.__enter__(); self.addCleanup(lambda: self.client.__exit__(None, None, None))
        store.insert_case(self.case)
        self.headers = {}
        for role in ('staff', 'lawyer', 'client'):
            login = self.client.post('/api/auth/login', json={'username': 'demo-' + role, 'password': 'debtoff-demo'})
            self.assertEqual(login.status_code, 200, login.text)
            self.headers[role] = {'Authorization': 'Bearer ' + login.json()['token']}
        self.base = '/api/cases/' + self.case['id']

    def current(self):
        return store.get_case(self.case['id'])

    def post(self, path, body, role='staff'):
        return self.client.post(self.base + path, headers=self.headers[role],
                                json={'expected_version': self.current()['version'], **body})

    def approved(self):
        result = self.post('/filing-packages', {'document_ids': []})
        self.assertEqual(result.status_code, 200, result.text)
        package = result.json()['filing_packages'][-1]
        result = self.post('/filing-packages/' + package['id'] + '/approve',
                           {'reason': '서명 별지 계산 및 제출요건 확인 완료', 'final_checks_confirmed': True}, 'lawyer')
        self.assertEqual(result.status_code, 200, result.text)
        return package

    def upload_receipt(self, package, text='전자소송 접수증 가상 시험인 2026개회12345 접수완료', confirmed='true'):
        return self.client.post(self.base + '/filing-packages/' + package['id'] + '/receipts', headers=self.headers['staff'],
            data={'expected_version': self.current()['version'], 'court_case_number': '2026개회12345',
                  'reason': '실제 접수증 원문과 사건번호 명의 확인', 'person_confirmed': confirmed,
                  'case_number_confirmed': 'true', 'receipt_confirmed': 'true'},
            files={'file': ('receipt.txt', text.encode('utf-8'), 'text/plain')})

    def test_receipt_upload_preserves_approval_and_records_external_submission(self):
        ready = self.client.get(self.base + '/filing-readiness', headers=self.headers['staff'])
        self.assertEqual(ready.status_code, 200, ready.text)
        self.assertTrue(ready.json()['ready'])
        package = self.approved()
        original_revision = self.current()['input_revision']
        original_documents = copy.deepcopy(self.current()['documents'])
        response = self.upload_receipt(package)
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.current()
        receipt = saved['submission_receipts'][-1]
        self.assertEqual(saved['input_revision'], original_revision)
        self.assertEqual(saved['documents'], original_documents)
        self.assertFalse(saved['filing_packages'][-1]['stale'])
        self.assertEqual(saved['submissions'], [])
        result = self.post('/submissions', {'bundle_id': package['id'], 'receipt_document_id': receipt['id'],
                                             'court_case_number': '2026개회12345'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['ax_pipeline']['stage'], 'submitted')
        self.assertFalse(result.json()['submissions'][-1]['external_transmission'])
        downloaded = self.client.get(self.base + '/submission-receipts/' + receipt['id'] + '/download', headers=self.headers['staff'])
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn('접수증', downloaded.content.decode('utf-8'))

    def test_roles_confirmation_and_actual_receipt_text_are_enforced(self):
        blocked = self.client.get(self.base + '/filing-readiness', headers=self.headers['client'])
        self.assertEqual(blocked.status_code, 403)
        result = self.post('/filing-packages', {'document_ids': []})
        package = result.json()['filing_packages'][-1]
        result = self.post('/filing-packages/' + package['id'] + '/approve',
                           {'reason': '직원의 잘못된 최종 승인 시도', 'final_checks_confirmed': True})
        self.assertEqual(result.json()['code'], 'LAWYER_ONLY')
        self.assertEqual(self.post('/filing-packages/' + package['id'] + '/approve',
                                   {'reason': '서명 별지 최종 제출사항 확인', 'final_checks_confirmed': False}, 'lawyer').json()['code'],
                         'FILING_FINAL_CHECK_REQUIRED')
        package = self.approved()
        version = self.current()['version']
        self.assertEqual(self.upload_receipt(package, confirmed='false').json()['code'], 'RECEIPT_REVIEW_REQUIRED')
        self.assertEqual(self.upload_receipt(package, text='2026개회12345 일반 서류').json()['code'], 'RECEIPT_EVIDENCE_REQUIRED')
        self.assertEqual(self.current()['version'], version)
        self.assertFalse(self.current().get('submission_receipts'))

    def test_material_evidence_change_expires_package_but_does_not_remove_history(self):
        package = self.approved()
        result = self.post('/facts/income/confirm', {'value': 2800000, 'evidence_ids': ['evidence'], 'reason': '최신 보완 원문 근거 재확인'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['filing_packages'][-1]['stale'])
        self.assertEqual(self.upload_receipt(package).json()['code'], 'FILING_PACKAGE_STALE')
        self.assertEqual(len(self.current()['filing_packages']), 1)


if __name__ == '__main__':
    unittest.main()
