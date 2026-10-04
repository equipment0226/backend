"""Real fresh-profile HTTP authentication, intake ownership, and notification delivery."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api import accounts, automation, corpus, domain, intake_workflow, main, store


class FreshStartTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(store, 'DATA_DIR', directory))
        self.stack.enter_context(patch.object(corpus, 'CORPUS_DIR', directory / 'corpus'))
        self.stack.enter_context(patch.dict(os.environ, {'DEBTOFF_START_PROFILE': 'fresh',
            'DEBTOFF_DEMO_MODE': '1', 'DEBTOFF_AUTO_AX': '0', 'DEBTOFF_LEGAL_WATCH': '0'}))
        self.stack.enter_context(patch.object(main, 'DEMO_MODE', True))
        self.stack.enter_context(patch.object(main, 'LOGIN_ATTEMPTS', {}))
        self.client = self.stack.enter_context(TestClient(main.app))
        self.headers = {}

    def login(self, name, password=None):
        result = self.client.post('/api/auth/login', json={'username': name, 'password': password or name})
        self.assertEqual(result.status_code, 200, result.text)
        self.headers[name] = {'Authorization': 'Bearer ' + result.json()['token']}
        return result.json()

    def auth(self, name):
        if name not in self.headers:
            self.login(name)
        return self.headers[name]

    def payload(self, **changes):
        return {'name': '김새봄', 'court_id': 'CT01', 'consultation': '매월 급여를 받고 있으며 채무 상환 상담을 신청합니다.',
                'consent': True, 'idempotency_key': 'intake-test-key-01', **changes}

    def apply(self, **changes):
        result = self.client.post('/api/portal/applications', json=self.payload(**changes), headers=self.auth('customer'))
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_fresh_accounts_have_hashes_and_zero_seed_cases_and_old_shortcuts_fail(self):
        self.assertEqual(self.client.get('/api/auth/config').json(), {
            'demo_mode': True, 'start_profile': 'fresh', 'allow_demo_shortcuts': False, 'local_test_accounts': True})
        self.assertEqual(self.client.get('/api/health').json()['start_profile'], 'fresh')
        for username, role in [('customer', 'client'), ('staff', 'staff'), ('lawyer', 'lawyer')]:
            body = self.login(username)
            self.assertEqual(body['user']['id'], username)
            self.assertEqual(body['user']['role'], role)
            self.assertNotIn('password', json.dumps(body))
            self.assertEqual(self.client.get('/api/cases', headers=self.auth(username)).json()['cases'], [])
        self.assertEqual(self.client.post('/api/auth/login', json={'username': 'demo-client', 'password': 'debtoff-demo'}).status_code, 401)
        with store.db() as con:
            rows = con.execute('SELECT * FROM accounts').fetchall()
            self.assertEqual(len(rows), 3)
            for row in rows:
                self.assertTrue(row['password_hash'].startswith('pbkdf2_sha256$600000$'))
                self.assertNotEqual(row['password_hash'], row['username'])
                self.assertTrue(accounts.verify_password(row['username'], row['password_hash']))
            original = rows[0]['password_hash']
        accounts.bootstrap(True)
        with store.db() as con:
            self.assertEqual(con.execute('SELECT password_hash FROM accounts WHERE id=?', (rows[0]['id'],)).fetchone()[0], original)

    def test_intake_is_owned_assigned_numbered_and_has_no_fabricated_completion(self):
        body = self.apply()
        case = store.get_case(body['id'])
        self.assertRegex(body['matter_number'], r'^\d{4}-000001$')
        self.assertEqual(case['client_user_id'], 'customer')
        self.assertEqual(set(case['members']), {'staff', 'lawyer'})
        self.assertEqual(case['consultation'], {})
        self.assertEqual(case['intake']['status'], 'received')
        self.assertEqual(case['intake']['application_notes'], self.payload()['consultation'])
        self.assertEqual(body['stage'], '상담 접수')
        self.assertNotIn('application_notes', body['intake'])
        self.assertTrue(case['synthetic'])
        self.assertEqual(body['requests'], [])
        self.assertEqual(body['documents'], [])
        self.assertEqual(case['approvals'], [])
        self.assertEqual(case['calculations'], [])
        self.assertEqual(case['facts'][0]['status'], 'unknown')
        self.assertEqual(body['notifications'], [])
        self.assertNotIn('members', body)
        for role in ('customer', 'staff', 'lawyer'):
            rows = self.client.get('/api/cases', headers=self.auth(role)).json()['cases']
            self.assertEqual(rows[0]['matter_number'], body['matter_number'])
            if role != 'customer':
                self.assertEqual(rows[0]['next_action'], '세부 상담 요청')
                self.assertIsNone(rows[0]['ax_status'])
        next_case = self.apply(idempotency_key='another-intake-key')
        self.assertTrue(next_case['matter_number'].endswith('000002'))

    def test_simultaneous_retries_create_one_case_without_early_analysis(self):
        auth = self.auth('customer')
        with patch.object(main.ax_service, 'maybe_schedule', return_value={'status': 'queued'}) as schedule:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.client.post('/api/portal/applications', json=self.payload(), headers=auth), range(2)))
            self.assertEqual([result.status_code for result in results], [200, 200])
            self.assertEqual(results[0].json()['id'], results[1].json()['id'])
            self.assertEqual(schedule.call_count, 0)
            self.apply()
            self.assertEqual(schedule.call_count, 0)
        changed = self.client.post('/api/portal/applications', json=self.payload(name='다른 이름'), headers=auth)
        self.assertEqual(changed.status_code, 409)
        with store.db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM cases').fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT count(*) FROM history').fetchone()[0], 1)

    def test_intake_does_not_need_an_analysis_service(self):
        with patch.object(main.ax_service, 'maybe_schedule', side_effect=RuntimeError('test failure')) as schedule:
            case = self.apply()
            schedule.assert_not_called()
        stored = store.get_case(case['id'])
        self.assertEqual(stored['intake']['application_notes'], self.payload()['consultation'])
        self.assertTrue(any(n['kind'] == 'application_received' for n in stored['notifications']))
        self.assertEqual(stored['approvals'], [])

    def test_detail_request_then_interview_starts_extraction_and_keeps_original(self):
        case = self.apply()
        base = '/api/cases/' + case['id']
        record = {'expected_version': case['version'], 'notes': '통화에서 급여소득만 있고 사업소득은 없음을 확인했습니다.',
                  'answers': {'income': '근로소득 월 실수령 280만원'}, 'consent': True}
        premature = self.client.post(base + '/consultation', json=record, headers=self.auth('staff'))
        self.assertEqual(premature.status_code, 422)
        self.assertEqual(premature.json()['code'], 'CONSULTATION_REQUEST_REQUIRED')
        message = {'expected_version': case['version'], 'message': '세부 상담을 진행하려고 합니다. 통화 가능한 시간과 현재 소득 형태를 알려 주세요.'}
        self.assertEqual(self.client.post(base + '/consultation/request', json=message, headers=self.auth('customer')).status_code, 403)
        requested = self.client.post(base + '/consultation/request', json=message, headers=self.auth('lawyer'))
        self.assertEqual(requested.status_code, 200, requested.text)
        self.assertEqual(requested.json()['intake']['status'], 'consultation_requested')
        self.assertEqual(requested.json()['requests'], [])
        client_case = self.client.get(base, headers=self.auth('customer')).json()
        self.assertEqual(client_case['messages'][-1]['text'], message['message'])
        self.assertEqual(client_case['notifications'][-1]['kind'], 'consultation_request')
        self.assertTrue(client_case['notifications'][-1]['unread'])
        record['expected_version'] = requested.json()['version']
        with patch.object(main.ax_service, 'maybe_schedule', return_value={'status': 'queued'}) as schedule:
            response = self.client.post(base + '/consultation', json=record, headers=self.auth('staff'))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(schedule.call_count, 1)
            scheduled = schedule.call_args.args[0]
            self.assertEqual(scheduled['intake']['status'], 'completed')
            self.assertEqual(scheduled['consultation']['notes'], record['notes'])
        saved = store.get_case(case['id'])
        self.assertEqual(saved['intake']['application_notes'], self.payload()['consultation'])
        record['expected_version'] = saved['version']
        self.assertEqual(self.client.post(base + '/consultation', json=record, headers=self.auth('lawyer')).status_code, 200)

    def test_pending_interview_cannot_be_bypassed_by_analysis_or_customer_reply(self):
        case = self.apply()
        base = '/api/cases/' + case['id']
        for route in ('/automation/run', '/ax-runs', '/agent-runs'):
            response = self.client.post(base + route, json={'expected_version': case['version'], **({'kind': 'case_review'} if route != '/automation/run' else {})}, headers=self.auth('staff'))
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()['code'], 'CONSULTATION_REQUIRED')
        with patch.dict(os.environ, {'DEBTOFF_AUTO_AX': '1'}), patch.object(main.ax_service, 'schedule') as schedule:
            response = self.client.post(base + '/messages', json={'expected_version': case['version'], 'text': '오늘 오후에 통화할 수 있습니다.'}, headers=self.auth('customer'))
            self.assertEqual(response.status_code, 200, response.text)
            schedule.assert_not_called()
        self.assertEqual(store.get_case(case['id'])['intake']['status'], 'received')

    def test_empty_interview_does_not_complete_consultation(self):
        case = self.apply()
        base = '/api/cases/' + case['id']
        requested = self.client.post(base + '/consultation/request', json={'expected_version': case['version'],
            'message': '현재 소득과 채무에 대해 세부 상담을 진행합니다.'}, headers=self.auth('staff')).json()
        response = self.client.post(base + '/consultation', json={'expected_version': requested['version'],
            'notes': '   ', 'answers': {'income': '   '}, 'consent': True}, headers=self.auth('staff'))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(store.get_case(case['id'])['intake']['status'], 'consultation_requested')

    def test_migration_preserves_uploads_manual_requests_and_original_history(self):
        case = domain.new_case('테스트 고객', 'CT01', '서울회생법원', '급여소득자 상담 신청 원문입니다.', True)
        case['audit'] = [{'action': 'application.created'}]
        case['consultation'] = {'notes': case['summary'], 'answers': {}, 'status': 'party_statement'}
        for rid, extra in [('auto', {}), ('manual', {'manual_override': True}),
                           ('order', {'court_order_id': 'court-1'}), ('uploaded', {'document_ids': ['doc-1']})]:
            case['requests'].append({'id': rid, 'title': '확인 자료', 'status': 'requested',
                'managed_by': 'court_request_rules', 'document_ids': [], **extra})
        case['documents'] = [{'id': 'doc-1', 'request_id': 'uploaded', 'text': '보존할 제출 원문'}]
        store.insert_case(case)
        intake_workflow.migrate_stored_applications()
        migrated = store.get_case(case['id'])
        self.assertEqual(migrated['requests'][0]['status'], 'withdrawn')
        self.assertEqual([r['status'] for r in migrated['requests'][1:]], ['requested'] * 3)
        self.assertEqual(migrated['documents'], case['documents'])
        self.assertEqual(migrated['intake']['application_notes'], case['summary'])
        self.assertEqual(migrated['consultation'], {})
        with store.db() as con:
            original = json.loads(con.execute('SELECT body FROM history WHERE case_id=? AND version=1', (case['id'],)).fetchone()[0])
            self.assertEqual(original['consultation'], case['consultation'])
        intake_workflow.migrate_stored_applications()
        self.assertEqual(store.get_case(case['id'])['version'], migrated['version'])

    def test_intake_requires_client_consent_and_rejects_identity_override(self):
        self.assertEqual(self.client.post('/api/portal/applications', json=self.payload()).status_code, 401)
        self.assertEqual(self.client.post('/api/portal/applications', json=self.payload(), headers=self.auth('staff')).status_code, 403)
        for patch_values in ({'consent': False}, {'consent': 'true'}, {'court_id': 'invented'},
                             {'client_user_id': 'someone-else'}, {'org_id': 'another-office'}, {'name': '  '}):
            response = self.client.post('/api/portal/applications', json=self.payload(**patch_values), headers=self.auth('customer'))
            self.assertEqual(response.status_code, 422, response.text)
        unknown = self.apply(court_id=None)
        self.assertEqual(unknown['court_id'], 'unknown')
        self.assertEqual(unknown['court_name'], '관할 확인 필요')
        self.assertEqual(len(self.client.get('/api/portal/public').json()['courts']), 15)

    def test_account_creation_is_persistent_hashed_and_cannot_escalate_or_reassign(self):
        payload = {'username': 'another-client', 'name': '다른 고객', 'role': 'client', 'password': 'unique-password-2026-test'}
        forbidden = self.client.post('/api/auth/accounts', json=payload, headers=self.auth('customer'))
        self.assertEqual(forbidden.status_code, 403)
        forbidden = self.client.post('/api/auth/accounts', json={**payload, 'role': 'lawyer'}, headers=self.auth('staff'))
        self.assertEqual(forbidden.status_code, 422)
        result = self.client.post('/api/auth/accounts', json=payload, headers=self.auth('staff'))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotIn('password', result.text)
        self.login(payload['username'], payload['password'])
        body = self.apply()
        self.assertEqual(self.client.get('/api/cases/' + body['id'], headers=self.headers['another-client']).status_code, 404)
        self.assertEqual(self.client.get('/api/notifications', headers=self.headers['another-client']).json()['notifications'], [])
        with store.db() as con:
            row = con.execute('SELECT * FROM accounts WHERE username=?', (payload['username'],)).fetchone()
            self.assertTrue(accounts.verify_password(payload['password'], row['password_hash']))
            self.assertEqual(row['org_id'], 'office-1')
        duplicate = self.client.post('/api/auth/accounts', json=payload, headers=self.auth('lawyer'))
        self.assertEqual(duplicate.status_code, 422)
        self.assertEqual(self.client.post('/api/auth/accounts', json={**payload, 'username': 'short-pass', 'password': 'staff'}, headers=self.auth('lawyer')).status_code, 422)

    def test_staff_lawyer_read_state_independent_and_no_cross_case_notice_leak(self):
        body = self.apply()
        staff = self.client.get('/api/notifications', headers=self.auth('staff')).json()
        self.assertEqual(staff['unread_count'], 1)
        notice = staff['notifications'][0]
        self.assertEqual(notice['kind'], 'application_received')
        self.assertEqual(notice['matter_number'], body['matter_number'])
        response = self.client.post(f"/api/cases/{body['id']}/notifications/{notice['id']}/read",
            json={'expected_version': body['version']}, headers=self.auth('staff'))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get('/api/notifications', headers=self.auth('staff')).json()['unread_count'], 0)
        lawyer = self.client.get('/api/notifications', headers=self.auth('lawyer')).json()
        self.assertEqual(lawyer['unread_count'], 1)
        self.assertIsNone(lawyer['notifications'][0]['read_at'])
        self.assertNotIn('read_by', lawyer['notifications'][0])
        self.assertEqual(self.client.get('/api/notifications', headers=self.auth('customer')).json()['notifications'], [])
        self.assertEqual(self.client.post(f"/api/cases/{body['id']}/notifications/{notice['id']}/read",
            json={'expected_version': response.json()['version']}, headers=self.auth('customer')).status_code, 422)
        other = domain.new_case('격리 고객', 'CT01', '서울회생법원', '노출 불가', False)
        other.update(members=['another-staff'], client_user_id='another-client')
        automation.notify(other, 'secret', '노출 금지', '남의 사건 알림')
        store.insert_case(other)
        foreign = domain.new_case('타사 고객', 'CT01', '서울회생법원', '노출 불가', False)
        foreign.update(org_id='office-2', members=['staff'], client_user_id='customer')
        automation.notify(foreign, 'foreign-secret', '타사 노출 금지', '다른 사무실 알림')
        store.insert_case(foreign)
        for role in ('staff', 'lawyer', 'customer'):
            text = self.client.get('/api/notifications', headers=self.auth(role)).text
            self.assertNotIn(other['id'], text)
            self.assertNotIn(foreign['id'], text)

    def test_messages_requests_and_customer_upload_deliver_correct_audience(self):
        body = self.apply()
        base = '/api/cases/' + body['id']
        response = self.client.post(base + '/messages', json={'expected_version': body['version'], 'text': '상담 내용을 추가로 보냅니다.'}, headers=self.auth('customer'))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['notifications'], [])
        response = self.client.post(base + '/messages', json={'expected_version': response.json()['version'], 'text': '자료 준비를 안내드립니다.'}, headers=self.auth('staff'))
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post(base + '/requests', json={'expected_version': response.json()['version'],
            'catalog_id': 'D01', 'period': '현재 발급본', 'reason': '본인 확인'}, headers=self.auth('staff'))
        self.assertEqual(response.status_code, 200, response.text)
        request = response.json()['requests'][0]
        response = self.client.post(base + '/documents', data={'expected_version': response.json()['version'], 'request_id': request['id']},
            files={'file': ('proof.txt', '본인 증명 자료 테스트 원문'.encode('utf-8'), 'text/plain')}, headers=self.auth('customer'))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['documents'][-1]['status'], 'received')
        staff_kinds = {n['kind'] for n in self.client.get('/api/notifications', headers=self.auth('staff')).json()['notifications']}
        client_kinds = {n['kind'] for n in self.client.get('/api/notifications', headers=self.auth('customer')).json()['notifications']}
        self.assertEqual(staff_kinds, {'application_received', 'client_message', 'document_received'})
        self.assertEqual(client_kinds, {'staff_message', 'document_request'})

    def test_testing_files_require_authenticated_fresh_customer(self):
        self.assertEqual(self.client.get('/api/portal/testing-materials').status_code, 401)
        self.assertEqual(self.client.get('/api/portal/testing-materials', headers=self.auth('staff')).status_code, 403)
        with patch.dict(os.environ, {'DEBTOFF_START_PROFILE': 'demo'}):
            # Config is public and reflects the actual running profile restriction.
            self.assertFalse(accounts.config()['local_test_accounts'])
            self.assertTrue(accounts.config()['allow_demo_shortcuts'])
        with patch.dict(os.environ, {'DEBTOFF_DEMO_MODE': '0'}):
            self.assertEqual(accounts.config()['start_profile'], 'production')
            self.assertEqual(self.client.get('/api/portal/testing-materials', headers=self.auth('customer')).status_code, 404)

    def test_case_testing_download_matches_person_and_preserves_case_authorization(self):
        import io
        import zipfile
        case = self.apply(name='이하늘')
        result = self.client.get('/api/portal/testing-materials', params={'case_id': case['id']},
                                 headers=self.auth('customer'))
        self.assertEqual(result.status_code, 200, result.text[:200] if result.status_code != 200 else '')
        with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
            self.assertEqual(len(archive.namelist()), 15)
            self.assertTrue(all(name.lower().endswith('.pdf') for name in archive.namelist()))
        result = self.client.get('/api/portal/testing-materials', params={'case_id': 'case-missing'},
                                 headers=self.auth('customer'))
        self.assertEqual(result.status_code, 404)
        stored = store.get_case(case['id'])
        stored['client_user_id'] = 'another-customer'
        with store.db() as con:
            con.execute('UPDATE cases SET body=? WHERE id=?', (json.dumps(stored), case['id']))
        self.assertEqual(self.client.get('/api/portal/testing-materials', params={'case_id': case['id']},
                         headers=self.auth('customer')).status_code, 404)

    def test_short_fresh_credentials_are_never_bootstrapped_in_production(self):
        new_directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        with patch.object(store, 'DATA_DIR', Path(new_directory)), patch.dict(os.environ,
            {'DEBTOFF_STAFF_PASSWORD': '', 'DEBTOFF_LAWYER_PASSWORD': '', 'DEBTOFF_START_PROFILE': 'fresh'}):
            store.initialize()
            with self.assertRaises(RuntimeError):
                accounts.bootstrap(False)
            with store.db() as con:
                self.assertEqual(con.execute('SELECT count(*) FROM accounts').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
