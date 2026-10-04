"""Real HTTP and SQLite boundaries for legal updates, without network work."""
import os
import tempfile
import unittest
from concurrent.futures import Future
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from apps.api import corpus, legal_watch, main, reasoning_cache, store


class LegalWatchApiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(__file__).resolve().parents[1] / '.work/tests'
        root.mkdir(parents=True, exist_ok=True)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='legal-watch-api-', dir=root))
        self.stack.enter_context(patch.object(store, 'DATA_DIR', Path(directory)))
        self.stack.enter_context(patch.dict(os.environ, {
            'DEBTOFF_DEMO_MODE': '1', 'DEBTOFF_AUTO_AX': '0', 'DEBTOFF_LEGAL_WATCH': '0',
            'DEBTOFF_REASONING_DAILY_CALL_LIMIT': '20'}))
        self.stack.enter_context(patch.object(main, 'LEGAL_WATCH_JOB', None))
        self.stack.enter_context(patch.object(main, 'LOGIN_ATTEMPTS', {}))
        self.stack.enter_context(patch('apps.api.ax_service.maybe_schedule', return_value=None))
        self.client = self.stack.enter_context(TestClient(main.app, raise_server_exceptions=False))
        self.headers = {}
        for role in ('staff', 'lawyer', 'client'):
            response = self.client.post('/api/auth/login', json={'username': 'demo-' + role, 'password': 'debtoff-demo'})
            self.assertEqual(response.status_code, 200, response.text)
            self.headers[role] = {'Authorization': 'Bearer ' + response.json()['token']}

    def get(self, role='staff'):
        return self.client.get('/api/legal-watch', headers=self.headers[role])

    def policy(self, **changes):
        return {'enabled': True, 'interval_hours': 48, 'max_sources': 1,
                'source_ids': [legal_watch.policy()['sources'][0]['id']],
                'daily_reasoning_limit': 7, **changes}

    def save(self, payload=None, role='lawyer'):
        return self.client.post('/api/legal-watch/policy', headers=self.headers[role], json=payload or self.policy())

    def test_read_is_staff_only_with_full_official_catalog_and_safe_budget(self):
        self.assertEqual(self.client.get('/api/legal-watch').status_code, 401)
        self.assertEqual(self.get('client').status_code, 403)
        response = self.get()
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertFalse(data['scheduler_enabled'])
        self.assertEqual({s['id'] for s in data['sources']}, {s['id'] for s in legal_watch.policy()['sources']})
        self.assertTrue(all(s['url'].startswith('https://') for s in data['sources']))
        self.assertEqual({k: data['budget'][k] for k in ('used', 'cached', 'daily_limit')},
                         {'used': 0, 'cached': 0, 'daily_limit': 20})
        self.assertNotIn('prompt_tokens', data['budget'])
        self.assertNotIn('completion_tokens', data['budget'])
        self.assertTrue(reasoning_cache.cache_path().is_relative_to(store.DATA_DIR))

    def test_policy_is_lawyer_only_and_persists_scope_budget_and_audit(self):
        self.assertEqual(self.save(role='client').status_code, 403)
        self.assertEqual(self.save(role='staff').status_code, 422)
        before = legal_watch.policy()['source_ids']
        self.assertGreater(len(before), 1)
        response = self.save()
        self.assertEqual(response.status_code, 200, response.text)
        data = self.get('lawyer').json()
        self.assertEqual(data['interval_hours'], 48)
        self.assertEqual(data['max_sources'], 1)
        self.assertEqual(data['source_ids'], self.policy()['source_ids'])
        self.assertEqual(data['budget']['daily_limit'], 7)
        self.assertEqual(sum(s['enabled'] for s in data['sources']), 1)
        self.assertEqual(reasoning_cache.settings()['daily_call_limit'], 7)
        self.assertTrue((store.DATA_DIR / 'legal_watch/policy.json').is_file())
        with store.db() as con:
            entries = con.execute("SELECT user_id,action FROM access_log WHERE action LIKE 'legal_watch.policy:%'").fetchall()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['user_id'], 'lawyer')

    def test_invalid_policy_cannot_change_budget_or_scope(self):
        before = legal_watch.policy()
        for changes in ({'interval_hours': 23}, {'interval_hours': 721}, {'max_sources': 21},
                        {'max_sources': True}, {'enabled': 'true'}, {'daily_reasoning_limit': 0},
                        {'daily_reasoning_limit': 101}, {'source_ids': []},
                        {'source_ids': ['https://unregistered.example/legal']}, {'untrusted_url': 'https://example.com'}):
            with self.subTest(changes=changes):
                response = self.save(self.policy(**changes))
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(legal_watch.policy(), before)
                self.assertEqual(reasoning_cache.settings()['daily_call_limit'], 20)

    def test_status_exposes_real_usage_and_cache_hits_without_prompt_text(self):
        claim = reasoning_cache.reserve('synthetic-key', 'synthetic-stage', 'strategy', 30)
        response = {'response': '{"passed":true}', 'usage': {'prompt_tokens': 12, 'completion_tokens': 3}}
        reasoning_cache.complete(claim['attempt_id'], 'synthetic-key', response, True)
        self.assertEqual(reasoning_cache.reserve('synthetic-key', 'synthetic-stage', 'strategy', 30)['state'], 'cached')
        data = self.get().json()
        self.assertEqual(data['budget']['used'], 1)
        self.assertEqual(data['budget']['cached'], 1)
        self.assertNotIn('synthetic-key', str(data))
        self.assertNotIn('passed', str(data))

    def test_manual_refresh_requires_staff_and_reuses_inflight_job(self):
        pending = Future()
        with patch.object(main.COLLECTION_POOL, 'submit', return_value=pending) as submit:
            self.assertEqual(self.client.post('/api/legal-watch/run').status_code, 401)
            self.assertEqual(self.client.post('/api/legal-watch/run', headers=self.headers['client']).status_code, 403)
            self.assertEqual(submit.call_count, 0)
            response = self.client.post('/api/legal-watch/run', headers=self.headers['staff'])
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['status'], 'queued')
            repeat = self.client.post('/api/legal-watch/run', headers=self.headers['lawyer'])
            self.assertEqual(repeat.json(), {'status': 'running', 'reused': True})
            self.assertEqual(self.get().json()['status'], 'running')
            submit.assert_called_once_with(main._manual_legal_watch)
            pending.set_result({'status': 'completed'})

    def test_failed_manual_job_is_visible_without_exception_details(self):
        failed = Future()
        failed.set_exception(RuntimeError('sensitive diagnostic must remain internal'))
        with patch.object(main, 'LEGAL_WATCH_JOB', failed):
            response = self.get()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['status'], 'failed')
            self.assertNotIn('sensitive diagnostic', response.text)

    def test_manual_worker_uses_bounded_force_run_then_refreshes_affected_cases(self):
        with patch.object(legal_watch, 'run_once', new=AsyncMock(return_value={'status': 'ready'})) as run, \
             patch.object(main, '_refresh_watch_cases') as refresh:
            main._manual_legal_watch()
            run.assert_awaited_once_with(force=True)
            refresh.assert_called_once_with()

    def test_disabled_policy_is_recorded_and_does_not_enable_scheduler(self):
        response = self.save(self.policy(enabled=False))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['enabled'])
        self.assertFalse(response.json()['scheduler_enabled'])
        self.assertEqual(response.json()['status'], 'disabled')

    def test_source_refresh_is_staff_only_registered_and_persists_selected_scope(self):
        source_id = corpus.seeds()[0]['id']
        endpoint = '/api/knowledge/sources/' + source_id + '/refresh'
        with patch.object(main.COLLECTION_POOL, 'submit') as submit:
            self.assertEqual(self.client.post(endpoint).status_code, 401)
            self.assertEqual(self.client.post(endpoint, headers=self.headers['client']).status_code, 403)
            self.assertEqual(self.client.post('/api/knowledge/sources/NOT_REGISTERED/refresh', headers=self.headers['staff']).status_code, 404)
            submit.assert_not_called()
            response = self.client.post(endpoint, headers=self.headers['staff'])
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(result['status'], 'queued')
            submit.assert_called_once()
            worker, run = submit.call_args.args
            self.assertIs(worker, main.collect_worker)
            self.assertEqual(run['source_ids'], [source_id])
            self.assertEqual(run['id'], result['run_id'])
            stored = self.client.get('/api/knowledge/runs/' + result['run_id'], headers=self.headers['staff'])
            self.assertEqual(stored.status_code, 200, stored.text)
            self.assertEqual(stored.json()['source_ids'], [source_id])
            self.assertEqual(self.client.get('/api/knowledge/runs/' + result['run_id'], headers=self.headers['client']).status_code, 403)
            repeated = self.client.post(endpoint, headers=self.headers['lawyer'])
            self.assertEqual(repeated.json()['run_id'], result['run_id'])
            self.assertEqual(submit.call_count, 1)
            other = next(s['id'] for s in corpus.seeds() if s['id'] != source_id)
            different = self.client.post('/api/knowledge/sources/' + other + '/refresh', headers=self.headers['staff'])
            self.assertEqual(different.status_code, 409, different.text)
            self.assertEqual(submit.call_count, 1)

    def test_collection_worker_fetches_only_selected_source_and_records_completion(self):
        source_id = corpus.seeds()[0]['id']
        endpoint = '/api/knowledge/sources/' + source_id + '/refresh'
        with patch.object(main.COLLECTION_POOL, 'submit') as submit:
            response = self.client.post(endpoint, headers=self.headers['staff'])
            self.assertEqual(response.status_code, 200, response.text)
            run = submit.call_args.args[1]
        with patch.object(corpus, 'ingest_all', new=AsyncMock(return_value={'collected_count': 1})) as ingest, \
             patch.object(main, '_refresh_watch_cases') as refresh:
            main.collect_worker(run)
            ingest.assert_awaited_once_with(source_ids=[source_id])
            refresh.assert_called_once_with()
        stored = self.client.get('/api/knowledge/runs/' + run['id'], headers=self.headers['staff']).json()
        self.assertEqual(stored['status'], 'completed')
        self.assertEqual(stored['result']['collected_count'], 1)
        self.assertTrue(stored['finished_at'])


if __name__ == '__main__':
    unittest.main()
