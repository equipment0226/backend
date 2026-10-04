"""Finite slow-CPU recovery never substitutes a subset for complete coverage."""
import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from apps.api import model_client, verification as verify
from tests.test_ocr_verification_runtime import answer, payload, reply


class VerificationTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        verify.clear_local_verification_cache()
        self.addCleanup(verify.clear_local_verification_cache)

    def test_default_budget_scales_with_context_and_remains_finite(self):
        with patch.dict(os.environ, {'DEBTOFF_LOCAL_VERIFICATION_TIMEOUT': ''}):
            small = verify.local_verification_timeout(1000, 70)
            large = verify.local_verification_timeout(8000, 102)
            self.assertEqual(small, 180)
            self.assertGreater(large, small)
            self.assertLessEqual(large, 300)
            self.assertEqual(verify.local_verification_timeout(100000, 12000), 300)

    def test_operator_override_is_bounded_and_invalid_settings_use_adaptive_default(self):
        for setting, expected in [('240', 240), ('1', 120), ('999999', 600), ('oops', 180), ('inf', 180), ('', 180)]:
            with self.subTest(setting=setting), patch.dict(os.environ, {'DEBTOFF_LOCAL_VERIFICATION_TIMEOUT': setting}):
                self.assertEqual(verify.local_verification_timeout(1000, 70), expected)

    async def test_timeout_splits_only_failed_batch_once_and_caches_complete_recovery(self):
        calls = []
        def slow_once(messages, schema, **kwargs):
            calls.append((json.loads(messages[1]['content']), kwargs))
            if len(calls) == 1:
                raise asyncio.TimeoutError()
            return answer(messages, schema, **kwargs)
        original = payload()
        updates = []
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=slow_once)) as generate:
            result = await verify.run_local_verification_batched('ocr', original, updates.append)
            again = await verify.run_local_verification_batched('ocr', original)
        self.assertTrue(result['passed'] and again['passed'])
        self.assertEqual(generate.await_count, 3)
        self.assertEqual([len(value['items']) for value, _ in calls], [6, 3, 3])
        self.assertTrue(all(180 <= options['timeout'] <= 300 for _, options in calls))
        self.assertEqual(set(result['checked_item_ids']), {row['id'] for row in original['items']})
        self.assertEqual(again['cached_batch_count'], 1)
        retry = result['batches'][0]['execution']['timeout_recovery']
        self.assertEqual(retry['strategy'], 'split_once')
        self.assertEqual(retry['unattempted_parts'], 0)
        self.assertEqual([part['item_count'] for part in retry['parts']], [3, 3])
        self.assertEqual((updates[-1]['completed'], updates[-1]['total']), (1, 1))

    async def test_recovered_uncertainty_remains_negative_with_complete_coverage(self):
        calls = 0
        def uncertain(messages, schema, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise asyncio.TimeoutError()
            wire = json.loads(messages[1]['content'])
            return reply({str(row['i']): 'unclear' if calls == 2 else 'match' for row in wire['items']})
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=uncertain)) as generate:
            result = await verify.run_local_verification_batched('ocr', payload())
            again = await verify.run_local_verification_batched('ocr', payload())
        self.assertFalse(result['passed'] or again['passed'])
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(len(result['checked_item_ids']), 6)
        self.assertEqual(sum(row['status'] == 'uncertain' for row in result['findings']), 3)
        self.assertEqual(generate.await_count, 3)
        self.assertEqual(again['cached_batch_count'], 1)

    async def test_repeated_timeout_stops_without_recursion_or_false_coverage(self):
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=asyncio.TimeoutError())) as generate:
            result = await verify.run_local_verification_batched('ocr', payload())
        self.assertEqual(generate.await_count, 2)
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['checked_item_ids'], [])
        self.assertEqual(result['error']['code'], 'MODEL_TIMEOUT')
        self.assertEqual(result['batches'][0]['execution']['timeout_recovery']['unattempted_parts'], 1)

    async def test_singleton_and_cancellation_do_not_trigger_split(self):
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=asyncio.TimeoutError())) as generate:
            result = await verify.run_local_verification_batched('ocr', payload(1))
        self.assertFalse(result['passed'])
        self.assertEqual(generate.await_count, 1)
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=asyncio.CancelledError())) as generate:
            with self.assertRaises(asyncio.CancelledError):
                await verify.run_local_verification_batched('ocr', payload())
        self.assertEqual(generate.await_count, 1)

    async def test_complete_local_batches_survive_two_hour_run_but_expire_after_six_hours(self):
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=answer)) as generate:
            first = await verify.run_local_verification_batched('ocr', payload(1))
            self.assertTrue(first['passed'])
            with verify._LOCAL_SUCCESS_LOCK:
                for key, (at, value) in list(verify._LOCAL_SUCCESS_CACHE.items()):
                    verify._LOCAL_SUCCESS_CACHE[key] = (at - 2 * 3600, value)
            second = await verify.run_local_verification_batched('ocr', payload(1))
            self.assertTrue(second['passed'])
            self.assertEqual(generate.await_count, 1)
            with verify._LOCAL_SUCCESS_LOCK:
                for key, (at, value) in list(verify._LOCAL_SUCCESS_CACHE.items()):
                    verify._LOCAL_SUCCESS_CACHE[key] = (at - 4 * 3600 - 1, value)
            third = await verify.run_local_verification_batched('ocr', payload(1))
            self.assertTrue(third['passed'])
            self.assertEqual(generate.await_count, 2)
        self.assertEqual(verify._LOCAL_SUCCESS_LIMIT, 256)


if __name__ == '__main__':
    unittest.main()
