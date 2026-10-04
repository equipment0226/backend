import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from apps.api import model_client, reasoning_cache as cache, verification


def response(decision='review_required'):
    value = {'decision': decision, 'findings': []}
    return {'done': True, 'done_reason': 'stop', 'provider': 'deepseek', 'external_processing': True,
            'message': {'content': json.dumps(value)}, 'prompt_eval_count': 100,
            'eval_count': 25, 'usage': {'completion_tokens_details': {'reasoning_tokens': 20}}}


class ReasoningCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        for changed in (patch.object(cache, 'cache_path', return_value=self.folder / 'cache.sqlite3'),
                        patch.dict(os.environ, {'DEBTOFF_REASONING_DAILY_CALL_LIMIT': '20', 'DEBTOFF_REASONING_CACHE_TTL_SECONDS': '604800'})):
            changed.start()
            self.addCleanup(changed.stop)

    async def run_cached(self, operation, key='one', validate=lambda raw: raw):
        return await cache.run(key=key, stage_key=key, stage='strategy', operation=operation,
                               validate=validate, timeout=1)

    async def test_exact_cache_hit_spares_call_and_records_numeric_usage(self):
        operation = AsyncMock(return_value=response())
        first = await self.run_cached(operation)
        second = await self.run_cached(operation)
        self.assertFalse(first['cost_control']['cache_hit'])
        self.assertTrue(second['cost_control']['cache_hit'])
        self.assertEqual(operation.await_count, 1)
        metrics = cache.operational_metrics()
        self.assertEqual(metrics['calls'], 1)
        self.assertEqual(metrics['cache_hits'], 1)
        self.assertEqual(metrics['prompt_tokens'], 100)
        self.assertEqual(metrics['reasoning_tokens'], 20)

    async def test_concurrent_requests_share_one_expensive_call(self):
        async def answer():
            await asyncio.sleep(.05)
            return response()
        operation = AsyncMock(side_effect=answer)
        results = await asyncio.gather(self.run_cached(operation), self.run_cached(operation))
        self.assertEqual(operation.await_count, 1)
        self.assertEqual(sum(r['cost_control']['new_expensive_calls'] for r in results), 1)
        self.assertTrue(any(r['cost_control']['shared_inflight'] for r in results))

    async def test_at_most_primary_and_one_repair_for_incomplete_answer(self):
        operation = AsyncMock(return_value=response())
        first = await self.run_cached(operation, validate=lambda raw: None)
        second = await self.run_cached(operation, validate=lambda raw: None)
        self.assertEqual(first['cost_control']['attempt'], 'primary')
        self.assertEqual(second['cost_control']['attempt'], 'repair')
        with self.assertRaises(cache.ReasoningLimitError) as caught:
            await self.run_cached(operation, validate=lambda raw: None)
        self.assertEqual(caught.exception.code, 'REASONING_STAGE_BUDGET_EXHAUSTED')
        self.assertEqual(operation.await_count, 2)

    async def test_transport_failure_is_not_cached_and_still_counts(self):
        operation = AsyncMock(side_effect=ConnectionError('synthetic failure'))
        with self.assertRaises(ConnectionError):
            await self.run_cached(operation)
        self.assertEqual(cache.operational_metrics()['calls'], 1)
        self.assertEqual((await self.run_cached(AsyncMock(return_value=response())))['cost_control']['attempt'], 'repair')

    async def test_daily_limit_blocks_new_facts_but_keeps_existing_cache_available(self):
        operation = AsyncMock(return_value=response())
        with patch.dict(os.environ, {'DEBTOFF_REASONING_DAILY_CALL_LIMIT': '1'}):
            await self.run_cached(operation)
            self.assertTrue((await self.run_cached(operation))['cost_control']['cache_hit'])
            with self.assertRaises(cache.ReasoningLimitError) as caught:
                await self.run_cached(operation, key='changed-facts')
        self.assertEqual(caught.exception.code, 'REASONING_DAILY_BUDGET_EXHAUSTED')
        self.assertEqual(operation.await_count, 1)

    async def test_ttl_expiry_starts_new_bounded_window(self):
        operation = AsyncMock(return_value=response())
        with patch.object(cache.time, 'time', return_value=2_000_000_000):
            await self.run_cached(operation)
        with patch.object(cache.time, 'time', return_value=2_000_604_801):
            result = await self.run_cached(operation)
        self.assertFalse(result['cost_control']['cache_hit'])
        self.assertEqual(result['cost_control']['attempt'], 'primary')
        self.assertEqual(operation.await_count, 2)

    def test_facts_law_prompt_model_and_schema_all_affect_identity(self):
        baseline = {'safe_payload': {'monthly_income': 100}, 'messages': [{'content': 'fixed prompt'}],
                    'schema': {'type': 'object'}, 'model': 'model-one', 'provider': 'deepseek',
                    'stage': 'strategy', 'legal_signature': {'effective_date': '2026-10-02', 'sha256': 'first'}}
        key = cache.identity(**baseline)[0]
        for field, value in [('safe_payload', {'monthly_income': 101}), ('messages', [{'content': 'new prompt'}]),
                             ('schema', {'type': 'array'}), ('model', 'model-two'),
                             ('legal_signature', {'effective_date': '2026-10-03', 'sha256': 'second'})]:
            changed = copy.deepcopy(baseline)
            changed[field] = value
            self.assertNotEqual(key, cache.identity(**changed)[0])

    async def test_sqlite_stores_hashes_and_answer_without_request_payload(self):
        marker = 'SOURCE_CONTENT_MUST_NOT_BE_PERSISTED'
        key, stage_key = cache.identity(safe_payload={'public_excerpt': marker}, messages=[], schema={},
            model='model', provider='deepseek', stage='strategy', legal_signature={})
        await cache.run(key=key, stage_key=stage_key, stage='strategy', operation=AsyncMock(return_value=response()),
                        validate=lambda raw: raw, timeout=1)
        for file in self.folder.iterdir():
            self.assertNotIn(marker.encode(), file.read_bytes())

    async def test_provider_boundary_caches_valid_review_but_not_insufficient_evidence(self):
        payload = verification.build_safe_strategy_payload({'monthly_income': 2800000}, {'monthly_payment': 900000})
        config = {'provider': 'deepseek', 'model': 'synthetic-test-model', 'external_processing': True}
        with patch.object(model_client, 'provider_config', return_value=config), patch.object(model_client, '_generate_uncached', new=AsyncMock(return_value=response())) as network:
            first = await model_client.generate([], {}, task_role='strategy_verification', structured_payload=payload)
            second = await model_client.generate([{'content': 'ignored raw caller text'}], {'ignored': True}, task_role='strategy_verification', structured_payload=payload)
        self.assertEqual(network.await_count, 1)
        self.assertTrue(second['cost_control']['cache_hit'])
        self.assertFalse(first['cost_control']['cache_hit'])
        changed = verification.build_safe_strategy_payload({'monthly_income': 3100000}, {'monthly_payment': 900000})
        with patch.object(model_client, 'provider_config', return_value=config), patch.object(model_client, '_generate_uncached', new=AsyncMock(return_value=response('insufficient_evidence'))) as network:
            await model_client.generate([], {}, task_role='strategy_verification', structured_payload=changed)
            await model_client.generate([], {}, task_role='strategy_verification', structured_payload=changed)
        self.assertEqual(network.await_count, 2)

    def test_lawyer_budget_setting_persists_and_rejects_invalid_values(self):
        self.assertEqual(cache.configure({'daily_call_limit': 7})['daily_call_limit'], 7)
        self.assertEqual(cache.settings()['daily_call_limit'], 7)
        self.assertEqual(cache.operational_metrics()['daily_limit'], 7)
        for value in (0, 101, True, '7'):
            with self.assertRaises(ValueError):
                cache.configure({'daily_call_limit': value})

    def test_unrelated_court_and_download_timestamp_do_not_invalidate(self):
        data = self.folder / 'data'
        (data / 'legal_research').mkdir(parents=True)
        rules = {'version': 'one', 'courts': {'CT01': {'source_ids': ['SEOUL'], 'months': 6}, 'CT06': {'months': 12}},
                 'sources': {'SEOUL': {'url': 'official'}}, 'split': {'account': True}}
        rules_file = data / 'court_request_rules.json'
        rules_file.write_text(json.dumps(rules), encoding='utf-8')
        manifest = {'sources': [{'id': 'AXP01', 'text_sha256': 'same', 'retrieved_at': 'one', 'status': 'downloaded'}]}
        manifest_file = data / 'legal_research/manifest.json'
        manifest_file.write_text(json.dumps(manifest), encoding='utf-8')
        payload = {'case_features': {'court_id': 'CT01'}, 'legal_references': [{'public_source_id': 'AXP01'}]}
        with patch.object(model_client, 'ROOT', self.folder):
            before = model_client._active_legal_signature(payload)
            rules['version'] = 'two'
            rules['courts']['CT06']['months'] = 24
            rules_file.write_text(json.dumps(rules), encoding='utf-8')
            manifest['sources'][0]['retrieved_at'] = 'two'
            manifest_file.write_text(json.dumps(manifest), encoding='utf-8')
            self.assertEqual(before, model_client._active_legal_signature(payload))
            rules['courts']['CT01']['months'] = 12
            rules_file.write_text(json.dumps(rules), encoding='utf-8')
            self.assertNotEqual(before, model_client._active_legal_signature(payload))


if __name__ == '__main__':
    unittest.main()
