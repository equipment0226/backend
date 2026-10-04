import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import httpx

from apps.api import domain, store, portal_routes, portal_service, model_client, legal_watch
from apps.api.main import app as main_app, authorize, change, visible, notification_read, Payload


class PortalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        data_patch = patch.object(store, 'DATA_DIR', Path(directory.name))
        data_patch.start(); self.addCleanup(data_patch.stop)
        source_patch = patch.object(legal_watch, 'status', return_value={
            'status': 'ready', 'active_overlay_version': 'tested-law-version',
            'sources': [{'id': key, 'state': 'baseline', 'effective_date': '2026-10-02',
                         'checked_at': '2026-10-03T00:00:00+00:00'} for key in ('LW579', 'LW589', 'LW595', 'LW614')]})
        source_patch.start(); self.addCleanup(source_patch.stop)
        store.initialize()
        self.case = domain.new_case('김실고객', 'CT01', '서울회생법원', '본인 상담 요약', False)
        self.case.update(client_user_id='client-a', members=['staff-a', 'lawyer-a'])
        self.case['requests'] = [{'id': 'request-one', 'title': '급여명세서', 'status': 'requested',
                                 'period': '요청 기간 확인', 'document_ids': []}]
        store.insert_case(self.case)
        self.other = domain.new_case('다른고객', 'CT01', '서울회생법원', '다른 고객의 비밀', False)
        self.other.update(client_user_id='client-b', members=['staff-b'])
        store.insert_case(self.other)
        app = FastAPI()
        users = {key: {'id': key, 'name': key, 'role': key.split('-')[0] if not key.startswith('lawyer') else 'lawyer',
                       'org_id': 'office-1'} for key in ('client-a', 'client-b', 'staff-a', 'staff-b', 'lawyer-a')}

        def current_user(authorization: str = Header(default='')):
            if authorization not in users:
                raise HTTPException(401, '로그인 필요')
            return users[authorization]

        def staff(user=Depends(current_user)):
            if user['role'] not in ('staff', 'lawyer'):
                raise HTTPException(403, '직원 전용')
            return user

        @app.exception_handler(domain.DomainError)
        async def invalid(request, exc):
            return JSONResponse({'code': exc.code, 'detail': exc.message}, status_code=422)

        @app.exception_handler(store.VersionConflict)
        async def stale(request, exc):
            return JSONResponse({'detail': str(exc)}, status_code=409)

        portal_routes.register(app, current_user, staff, authorize, change, visible)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def post(self, path, body, role='client-a'):
        return self.client.post('/api/portal/' + path, json=body,
                                headers={'Authorization': role} if role else {})

    def review(self, **changes):
        return {'case_id': self.case['id'], 'title': '서류 안내가 도움이 됐어요',
                'content': '필요한 자료를 순서대로 확인할 수 있어 준비하는 데 도움이 됐습니다.',
                'alias': '새출발', 'public_consent': True, **changes}

    def model_reply(self, **changes):
        selection = {'faq_ids': ['documents'], 'request_ids': ['request-one'], 'requires_staff': False, **changes}
        return {'done': True, 'done_reason': 'stop', 'external_processing': False,
                'message': {'content': json.dumps(selection, ensure_ascii=False)}}

    def test_public_guide_is_available_without_login_and_has_no_seed_reviews_or_case_data(self):
        response = self.client.get('/api/portal/public')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['reviews'], [])
        self.assertTrue(body['guide']['steps'])
        self.assertTrue(body['faqs'][0]['sources'][0]['url'].startswith('https://www.law.go.kr/'))
        self.assertEqual(body['source_status']['source_version'], 'tested-law-version')
        self.assertNotIn('본인 상담', response.text)
        self.assertNotIn('다른 고객', response.text)

    def test_guide_distinguishes_pending_failure_and_future_amendments(self):
        for state, expected in [('pending', 'confirmation_required'), ('failed', 'confirmation_required'),
                                ('review_required', 'confirmation_required'), ('staged', 'future_change_scheduled')]:
            with self.subTest(state=state), patch.object(legal_watch, 'status', return_value={
                    'status': 'warning', 'sources': [{'id': 'LW579', 'state': state, 'effective_date': '2099-01-01'}]}):
                faq = next(row for row in portal_service.guide()['faqs'] if row['id'] == 'eligibility')
                self.assertEqual(faq['currentness'], expected)
                self.assertTrue(faq['currentness_label'])

    def test_unknown_watch_state_and_elapsed_staging_do_not_claim_current_guidance(self):
        for state, effective in [('unrecognized-state', None), ('staged', '2000-01-01'), ('staged', None)]:
            with self.subTest(state=state, effective=effective), patch.object(legal_watch, 'status', return_value={
                    'status': 'warning', 'sources': [{'id': 'LW579', 'state': state, 'effective_date': effective}]}):
                faq = next(row for row in portal_service.guide()['faqs'] if row['id'] == 'eligibility')
                self.assertEqual(faq['currentness'], 'confirmation_required')

    def test_real_api_cors_preflight_allows_portal_delete_patch_but_not_other_origin(self):
        real_client = TestClient(main_app)
        self.addCleanup(real_client.close)
        for method in ('DELETE', 'PATCH'):
            headers = {'Origin': 'http://localhost:5173', 'Access-Control-Request-Method': method,
                       'Access-Control-Request-Headers': 'Authorization,Content-Type'}
            response = real_client.options('/api/portal/reviews/example', headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers['access-control-allow-origin'], 'http://localhost:5173')
            self.assertIn(method, response.headers['access-control-allow-methods'])
            denied = real_client.options('/api/portal/reviews/example', headers={**headers, 'Origin': 'https://untrusted.example'})
            self.assertEqual(denied.status_code, 400)
            self.assertNotIn('access-control-allow-origin', denied.headers)

    def test_full_sqlite_backup_restores_reviews_usage_handoff_and_revokes_sessions(self):
        from scripts.backup import create_backup, restore_backup
        review = self.post('reviews', self.review()).json()
        self.post('handoff', {'case_id': self.case['id'], 'expected_version': 1, 'message': '담당자 연결 확인 요청입니다.'})
        portal_service._reserve_chat({'id': 'client-a'})
        with store.db() as con:
            con.execute('INSERT INTO sessions VALUES (?,?,?)', ('fixture-session', 'client-a', 9999999999))
        archive, restored = store.DATA_DIR / 'snapshot.zip', store.DATA_DIR / 'restored'
        create_backup(archive)
        result = restore_backup(archive, restored)
        self.assertEqual(result['integrity'], 'ok')
        with closing(sqlite3.connect(restored / 'debtoff.sqlite3')) as con:
            saved = json.loads(con.execute('SELECT body FROM portal_reviews WHERE id=?', (review['id'],)).fetchone()[0])
            self.assertEqual(saved['content'], self.review()['content'])
            self.assertTrue(saved['public_consent'])
            self.assertEqual(con.execute('SELECT count(*) FROM portal_chat_usage').fetchone()[0], 1)
            case = json.loads(con.execute('SELECT body FROM cases WHERE id=?', (self.case['id'],)).fetchone()[0])
            self.assertEqual(case['messages'][-1]['source'], 'portal_staff_handoff')
            self.assertEqual(case['notifications'][-1]['kind'], 'customer_handoff')
            self.assertEqual(con.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)

    def test_staff_handoff_alert_cannot_be_read_or_projected_by_customer(self):
        self.post('handoff', {'case_id': self.case['id'], 'expected_version': 1, 'message': '담당자에게 자료 문의드립니다.'})
        case = store.get_case(self.case['id'])
        notice_id = case['notifications'][-1]['id']
        client_user = {'id': 'client-a', 'role': 'client', 'org_id': 'office-1', 'name': '고객'}
        staff_user = {'id': 'staff-a', 'role': 'staff', 'org_id': 'office-1', 'name': '직원'}
        case['court_outcomes'] = [{'source_document_id': 'private-order', 'decision_quote': '직원 전용 결정문 판독'}]
        self.assertNotIn('private-order', json.dumps(visible(case, client_user)))
        self.assertEqual(visible(case, client_user)['notifications'], [])
        with self.assertRaises(domain.DomainError):
            notification_read(case['id'], notice_id, Payload(expected_version=case['version']), user=client_user)
        with self.assertRaises(HTTPException):
            notification_read(case['id'], notice_id, Payload(expected_version=case['version']),
                              user={**staff_user, 'id': 'staff-b'})
        response = notification_read(case['id'], notice_id, Payload(expected_version=case['version']), user=staff_user)
        self.assertTrue(response['notifications'][-1]['read_at'])

    def test_review_requires_login_client_own_case_and_explicit_public_consent(self):
        for role, code in [(None, 401), ('staff-a', 403), ('client-b', 404)]:
            self.assertEqual(self.post('reviews', self.review(), role=role).status_code, code)
        response = self.post('reviews', self.review(public_consent=False))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'PUBLIC_CONSENT_REQUIRED')
        self.assertEqual(self.post('reviews', self.review(public_consent='true')).status_code, 422)

    def test_review_is_published_with_masked_alias_without_private_ids_or_success_badge(self):
        response = self.post('reviews', self.review())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['alias'], '새**')
        self.assertIsNone(response.json()['outcome_evidence'])
        published = self.client.get('/api/portal/reviews').json()['reviews']
        self.assertEqual(len(published), 1)
        self.assertNotIn(self.case['id'], json.dumps(published))
        self.assertNotIn('client-a', json.dumps(published))
        self.assertNotIn('org_id', json.dumps(published))

    def test_review_blocks_personal_data_markup_guarantees_and_synthetic_success_stories(self):
        for content in ['연락은 010-1234-5678 번호로 해주세요.', '서류에 900101-1234567 이 기재되어 있어요.',
                        '김실고객이라는 이름으로 상담을 받았어요.', '<script>alert(1)</script> 이 안내입니다.',
                        '모든 고객에게 무조건 인가 보장을 약속합니다.']:
            with self.subTest(content=content):
                self.assertEqual(self.post('reviews', self.review(content=content)).status_code, 422)
        store.mutate(self.case['id'], 1, {'name': 'fixture', 'role': 'staff'}, 'fixture', lambda case: case.update(synthetic=True))
        response = self.post('reviews', self.review())
        self.assertEqual(response.json()['code'], 'SYNTHETIC_REVIEW_FORBIDDEN')

    def test_author_can_withdraw_publication_and_unassigned_staff_cannot_moderate(self):
        created = self.post('reviews', self.review()).json()
        path = '/api/portal/reviews/' + created['id']
        self.assertEqual(self.client.delete(path, headers={'Authorization': 'client-b'}).status_code, 404)
        self.assertEqual(self.client.patch(path, headers={'Authorization': 'staff-b'}, json={'status': 'hidden', 'reason': '개인정보 확인 필요'}).status_code, 404)
        self.assertEqual(self.client.get('/api/portal/reviews/manage', headers={'Authorization': 'staff-b'}).json()['reviews'], [])
        self.assertEqual(self.client.patch(path, headers={'Authorization': 'staff-a'}, json={'status': 'hidden', 'reason': '작성자 요청에 따른 숨김'}).status_code, 200)
        self.assertEqual(self.client.get('/api/portal/reviews').json()['reviews'], [])
        self.assertEqual(self.client.delete(path, headers={'Authorization': 'client-a'}).status_code, 200)
        record = json.loads(portal_service.review_record(created['id'])['body'])
        self.assertEqual(record['content'], '')
        self.assertFalse(record['public_consent'])

    def test_my_reviews_includes_hidden_own_review_but_never_other_authors(self):
        created = self.post('reviews', self.review()).json()
        self.client.patch('/api/portal/reviews/' + created['id'], headers={'Authorization': 'staff-a'},
                          json={'status': 'hidden', 'reason': '작성자 요청에 따른 숨김'})
        mine = self.client.get('/api/portal/reviews/mine', headers={'Authorization': 'client-a'})
        self.assertEqual(mine.status_code, 200)
        self.assertEqual(mine.json()['reviews'][0]['status'], 'hidden')
        self.assertNotIn('author_id', mine.text)
        self.assertEqual(self.client.get('/api/portal/reviews/mine', headers={'Authorization': 'client-b'}).json()['reviews'], [])
        self.assertEqual(self.client.get('/api/portal/reviews/mine', headers={'Authorization': 'staff-a'}).status_code, 403)

    def test_chat_requires_authorized_own_case_and_calls_only_local_role(self):
        payload = {'case_id': self.case['id'], 'message': '어떤 서류가 필요한가요?'}
        self.assertEqual(self.post('chat', payload, role=None).status_code, 401)
        self.assertEqual(self.post('chat', payload, role='client-b').status_code, 404)
        mocked = AsyncMock(return_value=self.model_reply())
        with patch.object(model_client, 'generate', new=mocked):
            response = self.post('chat', payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'answered')
        self.assertEqual(mocked.call_args.kwargs['task_role'], 'local')
        sent = mocked.call_args.args[0][1]['content']
        self.assertIn('본인 상담 요약', sent)
        self.assertNotIn('다른 고객의 비밀', sent)
        self.assertFalse(response.json()['external_processing'])
        self.assertIn('급여명세서', response.json()['answer'])
        with store.db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM portal_chat_usage').fetchone()[0], 1)
            self.assertEqual(set(row[1] for row in con.execute('PRAGMA table_info(portal_chat_usage)')), {'id', 'user_id', 'created_at'})

    def test_model_unavailable_returns_labeled_deterministic_faq_fallback(self):
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=httpx.ConnectError('offline'))):
            response = self.post('chat', {'case_id': self.case['id'], 'message': '서류 제출 안내를 알려주세요.'})
        body = response.json()
        self.assertEqual(body['status'], 'reference_only')
        self.assertEqual(body['availability'], 'unavailable')
        self.assertTrue(body['sources'])

    def test_repeated_model_faq_and_request_ids_are_returned_once(self):
        reply = self.model_reply(faq_ids=['documents', 'documents'],
                                 request_ids=['request-one', 'request-one', 'request-one'])
        with patch.object(model_client, 'generate', new=AsyncMock(return_value=reply)):
            response = self.post('chat', {'case_id': self.case['id'], 'message': '서류 제출 문의입니다.'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'answered')
        self.assertEqual(response.json()['faq_ids'], ['documents'])
        self.assertEqual(response.json()['request_ids'], ['request-one'])
        self.assertEqual(response.json()['answer'].count('요청 서류: 급여명세서'), 1)

    def test_model_cannot_emit_unfounded_prose_or_other_case_request(self):
        for reply in [self.model_reply(answer='무조건 100% 인가됩니다.'),
                      self.model_reply(request_ids=['someone-elses-private-request'])]:
            with patch.object(model_client, 'generate', new=AsyncMock(return_value=reply)):
                response = self.post('chat', {'case_id': self.case['id'], 'message': '서류 제출 문의입니다.'})
            self.assertEqual(response.json()['status'], 'reference_only')
            self.assertNotIn('100%', response.json()['answer'])
            self.assertNotIn('someone-else', response.text)

    def test_unsupported_legal_question_requires_staff_instead_of_inventing_advice(self):
        with patch.object(model_client, 'generate', new=AsyncMock(return_value=self.model_reply(faq_ids=[], request_ids=[], requires_staff=True))):
            response = self.post('chat', {'case_id': self.case['id'], 'message': '제 사건에서 배우자 부동산을 처분해야 하나요?'})
        self.assertTrue(response.json()['requires_staff'])
        self.assertIn('담당자 확인', response.json()['answer'])

    def test_explicit_handoff_adds_only_confirmed_message_and_staff_notification_atomically(self):
        payload = {'case_id': self.case['id'], 'expected_version': 1, 'message': '준비할 자료를 담당자와 확인하고 싶어요.'}
        self.assertEqual(self.post('handoff', payload, role='client-b').status_code, 404)
        response = self.post('handoff', payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['messages'][-1]['text'], payload['message'])
        self.assertEqual(response.json()['notifications'], [])
        stored = store.get_case(self.case['id'])
        self.assertEqual(stored['notifications'][-1]['audience'], 'staff')
        self.assertEqual(stored['notifications'][-1]['kind'], 'customer_handoff')
        self.assertEqual(self.post('handoff', payload).status_code, 409)
        self.assertEqual(len(store.get_case(self.case['id'])['messages']), 1)

    def test_chat_size_and_request_rate_are_bounded(self):
        self.assertEqual(self.post('chat', {'case_id': self.case['id'], 'message': '가' * 2001}).status_code, 422)
        with patch.object(model_client, 'generate', new=AsyncMock(return_value=self.model_reply())):
            for _ in range(5):
                self.assertEqual(self.post('chat', {'case_id': self.case['id'], 'message': '서류 안내'}).status_code, 200)
            response = self.post('chat', {'case_id': self.case['id'], 'message': '서류 안내'})
        self.assertEqual(response.json()['code'], 'CHAT_RATE_LIMIT')


if __name__ == '__main__':
    unittest.main()
