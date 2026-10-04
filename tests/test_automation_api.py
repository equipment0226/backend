"""HTTP authorization/version tests for AX scope, metadata, notices and outcomes.

All records live in a temporary SQLite directory; background/model/network work
is disabled. The tests exercise the real API and transaction boundary.
"""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('DEBTOFF_DEMO_MODE', '1')
from fastapi.testclient import TestClient
from apps.api.main import app
from apps.api import automation, domain, store


class AutomationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1] / '.work/tests'
        root.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix='automation-api-', dir=root)
        cls.data_patch = patch.object(store, 'DATA_DIR', Path(cls.temp.name))
        cls.schedule_patch = patch('apps.api.ax_service.maybe_schedule', return_value=None)
        cls.env_patch = patch.dict(os.environ, {'DEBTOFF_DEMO_MODE': '1', 'DEBTOFF_AUTO_AX': '0'})
        cls.data_patch.start(); cls.schedule_patch.start(); cls.env_patch.start()
        cls.context = TestClient(app, raise_server_exceptions=False)
        cls.client = cls.context.__enter__()
        cls.headers = {}
        for role in ('staff', 'lawyer', 'client'):
            response = cls.client.post('/api/auth/login', json={'username': 'demo-' + role, 'password': 'debtoff-demo'})
            if response.status_code != 200:
                raise AssertionError(response.text)
            cls.headers[role] = {'Authorization': 'Bearer ' + response.json()['token']}

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)
        cls.schedule_patch.stop(); cls.data_patch.stop(); cls.env_patch.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.case = domain.new_case('합성범위고객', 'CT03', '부산회생법원',
                                   '급여소득자. 본인 계좌 간 계좌이체 확인.', True)
        self.case.update(client_user_id='client', case_type='personal_rehabilitation')
        self.case['consultation'] = {'notes': '급여소득자이며 본인 계좌 간 계좌이체가 있습니다.',
                                     'answers': {}, 'status': 'party_statement'}
        self.case['requests'] = [{'id': 'req-one', 'version': 1, 'catalog_id': 'D36',
            'title': '은행 거래내역', 'period': '2025-10-03 ~ 2026-10-03',
            'status': 'fulfilled', 'document_ids': ['doc-one']}]
        self.case['documents'] = [{'id': 'doc-one', 'request_id': 'req-one', 'version': 1,
            'filename': 'synthetic.txt', 'sha256': 'synthetic-document-hash',
            'text': '합성범위고객의 합성 은행 거래내역입니다.', 'status': 'verified',
            'uploader': 'client', 'created_at': store.now(), 'public_status': '검증 완료'}]
        self.case['drafts'] = [{'id': 'draft-one', 'content_hash': 'generation-hash',
            'created_at': store.now(), 'input_revision': 1,
            'generated_features': {'court_id': 'CT03', 'document_types': ['D36'], 'risk_codes': []}}]
        self.case['notifications'] = [
            {'id': 'staff-only', 'audience': 'staff', 'kind': 'review_request', 'title': '내부 검토',
             'message': '직원용 검토 사유', 'read_at': None, 'resolved_at': None},
            {'id': 'customer', 'audience': 'client', 'kind': 'document_request', 'title': '자료 요청',
             'message': '요청 범위를 확인해주세요.', 'request_id': 'req-one', 'read_at': None, 'resolved_at': None}]
        store.insert_case(self.case)
        self.base = '/api/cases/' + self.case['id']

    def read(self, role='staff'):
        response = self.client.get(self.base, headers=self.headers[role])
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def post(self, path, payload=None, role='staff', version=None):
        return self.client.post(self.base + path, headers=self.headers[role], json={
            'expected_version': self.read()['version'] if version is None else version, **(payload or {})})

    def outcome(self, **changes):
        return {'outcome': 'approved', 'reason': '법원 원문과 해당 생성 문서 버전 확인',
                'bundle_id': 'draft-one', 'source_document_id': 'doc-one', **changes}

    def test_sensitive_read_endpoints_require_staff_access(self):
        for url in ('/api/court-request-rules', self.base + '/automation/evidence'):
            self.assertEqual(self.client.get(url).status_code, 401)
            self.assertEqual(self.client.get(url, headers=self.headers['client']).status_code, 403)
            self.assertEqual(self.client.get(url, headers=self.headers['staff']).status_code, 200)

    def test_metadata_is_staff_only_versioned_and_invalidates_outputs(self):
        payload = {'metadata': {'institution': '가상은행', 'issued_at': '2026-10-03'},
                   'reason': '원문 발급일과 기관을 대조하였습니다.'}
        original = self.read()
        self.assertEqual(self.post('/documents/doc-one/metadata', payload, role='client').status_code, 403)
        response = self.post('/documents/doc-one/metadata', payload, version=original['version'])
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['documents'][0]['document_metadata'], payload['metadata'])
        self.assertEqual(body['documents'][0]['metadata_review']['actor'], 'staff')
        self.assertEqual(body['requests'][0]['status'], 'received')
        self.assertTrue(body['drafts'][0]['stale'])
        self.assertGreater(body['input_revision'], original['input_revision'])
        self.assertEqual(self.post('/documents/doc-one/metadata', payload, version=original['version']).status_code, 409)
        self.assertNotIn('document_metadata', self.read('client')['documents'][0])

    def test_explicit_review_stamps_current_file_and_clears_old_public_warning(self):
        current = store.get_case(self.case['id'])
        def old_warning(case):
            case['documents'][0]['auto_verified'] = True
            case['requests'][0]['public_review_note'] = '과거 자동검증 보완 경고'
        store.mutate(current['id'], current['version'], {'name': '시험', 'role': 'staff'}, 'test.warning', old_warning)
        response = self.post('/documents/doc-one/verify', {
            'scope_confirmed': True, 'content_confirmed': True, 'person_confirmed': True,
            'reason': '원문과 인물 및 요청 범위를 대조했습니다.'})
        self.assertEqual(response.status_code, 200, response.text)
        case = response.json()
        doc = case['documents'][0]
        self.assertFalse(doc['auto_verified'])
        self.assertTrue(automation.manual_document_review_current(case, doc))
        self.assertEqual(doc['manual_verification']['verified_by_id'], 'staff')
        self.assertNotIn('public_review_note', case['requests'][0])
        doc['text'] += ' 변경된 OCR 원문'
        self.assertFalse(automation.manual_document_review_current(case, doc))

    def test_metadata_rejects_unknown_fields_invalid_dates_and_cross_case_document(self):
        for metadata in ({'prompt': '외부 지시'}, {'issued_at': '2026-99-99'}, {'institution': {'nested': True}}):
            response = self.post('/documents/doc-one/metadata', {'metadata': metadata, 'reason': '범위 검증 테스트입니다.'})
            self.assertEqual(response.status_code, 422, response.text)
        response = self.post('/documents/another-case-doc/metadata',
                             {'metadata': {'institution': '가상은행'}, 'reason': '다른 사건 자료 변경 시도'})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'ITEM_NOT_FOUND')

    def test_scopes_split_accounts_without_exposing_full_account_numbers(self):
        payload = {'financial_accounts': [
            {'id': 'acc-one', 'institution': '가상은행', 'account_number': '112233445566', 'salary_account': True},
            {'id': 'acc-two', 'institution': '가상은행', 'account_number': '998877665544'}]}
        self.assertEqual(self.post('/document-scopes', payload, role='client').status_code, 403)
        response = self.post('/document-scopes', payload)
        self.assertEqual(response.status_code, 200, response.text)
        requests = [r for r in response.json()['requests'] if r.get('managed_by') == 'court_request_rules'
                    and r['catalog_id'] == 'D36' and r['status'] != 'withdrawn']
        self.assertEqual({r['account_key'] for r in requests}, {'acc-one', 'acc-two'})
        self.assertTrue(any(r['purpose'] == 'salary_income' and r['period_start'][:4] == '2024' for r in requests))
        public = self.client.get(self.base, headers=self.headers['client'])
        self.assertNotIn('112233445566', public.text)
        self.assertNotIn('998877665544', public.text)
        self.assertNotIn('financial_accounts', public.json())
        self.assertTrue(all('account_key' not in r for r in public.json()['requests']))

    def test_scope_validation_rejects_bad_range_privilege_fields_and_stale_version(self):
        for row in ({'catalog_id': 'D36', 'period_start': '2026-10-03', 'period_end': '2026-01-01'},
                    {'catalog_id': 'D36', 'period_start': '2026-10-03'},
                    {'catalog_id': 'D36', 'managed_by': 'trusted'},
                    {'catalog_id': 'D36', 'institution': {'nested': 'invalid'}}):
            response = self.post('/document-scopes', {'document_scopes': [row]})
            self.assertEqual(response.status_code, 422, response.text)
        old_version = self.read()['version']
        self.assertEqual(self.post('/document-scopes', {'financial_accounts': []}, version=old_version).status_code, 200)
        self.assertEqual(self.post('/document-scopes', {'financial_accounts': []}, version=old_version).status_code, 409)

    def test_application_date_changes_requested_period_and_is_validated(self):
        response = self.post('/document-scopes', {'application_date': '2026-11-20',
            'financial_accounts': [{'id': 'acc-one', 'institution': '가상은행', 'account_number': '112233445566'}]})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['application_date'], '2026-11-20')
        active = [r for r in body['requests'] if r.get('managed_by') == 'court_request_rules' and r['status'] != 'withdrawn']
        self.assertTrue(all(r['reference_date'] == '2026-11-20' for r in active))
        self.assertEqual(self.post('/document-scopes', {'application_date': '2026-99-99'}).status_code, 422)

    def test_withdrawal_keeps_history_resolves_notice_and_rejects_duplicate(self):
        response = self.post('/requests/req-one/withdraw', {'reason': '현재 사건에서 불필요한 요청 확인'})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['requests'][0]['status'], 'withdrawn')
        self.assertTrue(body['requests'][0]['manual_override'])
        self.assertEqual(body['requests'][0]['document_ids'], ['doc-one'])
        self.assertTrue(body['request_history'][-1]['reason'])
        self.assertNotIn('customer', {n['id'] for n in body['notifications']})
        self.assertTrue(all(n['audience'] == 'staff' for n in body['notifications']))
        customer_notice = next(n for n in self.read('client')['notifications'] if n['id'] == 'customer')
        self.assertTrue(customer_notice['resolved_at'])
        self.assertFalse(customer_notice['unread'])
        stored_notice = next(n for n in store.get_case(self.case['id'])['notifications'] if n['id'] == 'customer')
        self.assertEqual(customer_notice['resolved_at'], stored_notice['resolved_at'])
        again = self.post('/requests/req-one/withdraw', {'reason': '중복 요청 철회를 시도합니다.'})
        self.assertEqual(again.status_code, 422)
        self.assertEqual(again.json()['code'], 'REQUEST_WITHDRAWN')

    def test_notification_visibility_read_authorization_and_version(self):
        self.assertEqual([n['id'] for n in self.read('client')['notifications']], ['customer'])
        denied = self.post('/notifications/staff-only/read', role='client')
        self.assertEqual(denied.status_code, 422)
        self.assertEqual(denied.json()['code'], 'NOTICE_ACCESS')
        version = self.read()['version']
        response = self.post('/notifications/customer/read', role='client', version=version)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['notifications'][0]['read_at'])
        self.assertEqual(self.post('/notifications/customer/read', role='client', version=version).status_code, 409)

    def test_cross_case_notification_and_scope_access_are_hidden(self):
        other = domain.new_case('다른합성고객', 'CT01', '서울회생법원', '다른 사건', True)
        other['client_user_id'] = 'not-this-client'
        other['notifications'] = [{'id': 'other-notice', 'audience': 'client'}]
        store.insert_case(other)
        response = self.client.post('/api/cases/' + other['id'] + '/notifications/other-notice/read',
            headers=self.headers['client'], json={'expected_version': 1})
        self.assertEqual(response.status_code, 404)

    def test_outcome_requires_lawyer_and_current_case_version(self):
        self.assertEqual(self.post('/court-outcomes', self.outcome(), role='client').status_code, 403)
        denied = self.post('/court-outcomes', self.outcome())
        self.assertEqual(denied.status_code, 422)
        self.assertEqual(denied.json()['code'], 'LAWYER_ONLY')
        version = self.read()['version']
        response = self.post('/court-outcomes', self.outcome(), role='lawyer', version=version)
        self.assertEqual(response.status_code, 200, response.text)
        outcome = response.json()['court_outcomes'][-1]
        self.assertEqual(outcome['document_hash'], 'generation-hash')
        self.assertEqual(outcome['source_hash'], 'synthetic-document-hash')
        self.assertTrue(outcome['synthetic'])
        self.assertEqual(outcome['generated_snapshot']['id'], 'draft-one')
        self.assertEqual(self.post('/court-outcomes', self.outcome(), role='lawyer', version=version).status_code, 409)
        duplicate = self.post('/court-outcomes', self.outcome(), role='lawyer')
        self.assertEqual(duplicate.status_code, 422)
        self.assertEqual(duplicate.json()['code'], 'DUPLICATE_OUTCOME')
        with store.db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM court_outcomes WHERE case_id=?', (self.case['id'],)).fetchone()[0], 1)

    def test_outcome_cannot_reference_unknown_generation_or_other_case_source(self):
        for payload, code in [(self.outcome(bundle_id='other-bundle'), 'BUNDLE_REQUIRED'),
                              (self.outcome(source_document_id='other-case-doc'), 'ITEM_NOT_FOUND')]:
            response = self.post('/court-outcomes', payload, role='lawyer')
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()['code'], code)

    def test_correction_outcome_invalidates_generation_and_alerts_staff(self):
        response = self.post('/court-outcomes', self.outcome(outcome='correction', reason='법원 청산가치 자료 보정 요구'), role='lawyer')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['ax_pipeline']['stage'], 'lawyer_review')
        self.assertTrue(body['drafts'][0]['stale'])
        self.assertTrue(any(n['kind'] == 'review_request' and n['audience'] == 'staff' for n in body['notifications']))


if __name__ == '__main__':
    unittest.main()
