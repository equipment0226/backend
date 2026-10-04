import asyncio
import copy
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from apps.api import model_client, verification
from tests.test_ocr_verification_runtime import answer, payload, reply


class CompleteReviewCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        verification.clear_local_verification_cache()
        self.addCleanup(verification.clear_local_verification_cache)

    async def negative(self, data):
        with patch.object(model_client, 'generate', new=AsyncMock(return_value=reply([
                {'i':1,'v':'mismatch','e':'E1','r':'period'}]))):
            return await verification.run_local_verification('ocr', data)

    async def test_complete_semantic_negative_is_reused_without_promoting_or_mutating_it(self):
        data = payload(1)
        with patch.object(model_client, 'generate', new=AsyncMock(return_value=reply([
                {'i':1,'v':'mismatch','e':'E1','r':'period'}]))) as generate:
            first = await verification.run_local_verification_batched('ocr', data)
            second = await verification.run_local_verification_batched('ocr', data)
            self.assertEqual(generate.await_count, 1)
            self.assertEqual(second['cached_batch_count'], 1)
            self.assertEqual(first['findings'], second['findings'])
            self.assertEqual(second['status'], 'needs_review')
            self.assertIs(second['passed'], False)
            second['findings'][0]['status'] = 'supported'
            second['findings'][0]['quote'] = 'caller changed'
            third = await verification.run_local_verification_batched('ocr', data)
        self.assertEqual(third['findings'], first['findings'])
        self.assertFalse(third['passed'])

    async def test_retry_after_later_timeout_reuses_finished_negative_and_checks_remaining_items(self):
        data = payload(7)
        def first_negative(messages, schema, **kwargs):
            wire = json.loads(messages[1]['content'])
            return reply([{'i':item['i'], 'v':'mismatch' if i == 0 else 'supported',
                           'e':item['evidence'][0], 'r':'period' if i == 0 else 'match'}
                          for i,item in enumerate(wire['items'])])
        calls = 0
        async def interrupted(messages, schema, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise asyncio.TimeoutError()
            return first_negative(messages, schema, **kwargs)
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=interrupted)):
            initial = await verification.run_local_verification_batched('ocr', data)
        self.assertEqual(initial['status'], 'unavailable')
        self.assertEqual(len(initial['checked_item_ids']), 6)
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=answer)) as generate:
            retried = await verification.run_local_verification_batched('ocr', data)
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(retried['cached_batch_count'], 1)
        self.assertEqual(len(retried['checked_item_ids']), 7)
        self.assertEqual(retried['status'], 'needs_review')
        self.assertIs(retried['passed'], False)
        self.assertEqual(retried['findings'][0]['status'], 'mismatch')

    async def test_partial_error_stale_refs_or_malformed_quotes_are_not_cached(self):
        data = payload(1)
        complete = await self.negative(data)
        variants = []
        for change in [
                {'status':'unavailable'}, {'status':'partial'}, {'error':{'code':'MODEL_TIMEOUT'}},
                {'checked_item_ids':[]}, {'findings':[]}, {'input_sha256':'old'},
                {'version':'old'}, {'source_refs':[]}, {'passed':True}, {'external_processing':True}]:
            variants.append({**copy.deepcopy(complete), **change})
        malformed = copy.deepcopy(complete)
        malformed['findings'][0]['quote'] = 'not in current source'
        variants.append(malformed)
        duplicate = copy.deepcopy(complete)
        duplicate['findings'] *= 2
        variants.append(duplicate)
        for result in variants:
            with self.subTest(result=result):
                verification.clear_local_verification_cache()
                with patch.object(verification, 'run_local_verification', new=AsyncMock(return_value=result)) as run:
                    await verification._verify_cached_batch('ocr', data)
                    await verification._verify_cached_batch('ocr', data)
                self.assertEqual(run.await_count, 2)

    async def test_source_item_context_version_and_model_changes_invalidate_negative(self):
        data = payload(1)
        result = await self.negative(data)
        async def negative(kind, incoming):
            updated = copy.deepcopy(result)
            updated['input_sha256'] = verification._digest(incoming)
            updated['source_refs'] = [{'source_id':s['id'],'version':s.get('version'),
                                      'sha256':verification._digest(s['text'])} for s in incoming['sources']]
            return updated
        with patch.object(verification, 'run_local_verification', new=AsyncMock(side_effect=negative)) as run:
            await verification._verify_cached_batch('ocr', data)
            await verification._verify_cached_batch('ocr', data)
            self.assertEqual(run.await_count, 1)
            for part, key, value in [('sources','version',4), ('sources','text',data['sources'][0]['text']+'\nnew original'),
                                      ('items','value',2900000), ('items','frequency','annual')]:
                changed = copy.deepcopy(data)
                changed[part][0][key] = value
                await verification._verify_cached_batch('ocr', changed)
            changed = copy.deepcopy(data)
            changed['context']['scope'] = 'changed review scope'
            await verification._verify_cached_batch('ocr', changed)
            with patch.dict(os.environ, {'OLLAMA_MODEL':'different-local-review-model'}):
                await verification._verify_cached_batch('ocr', data)
            with patch.object(verification, 'VERSION', 'new-review-rules'):
                await verification._verify_cached_batch('ocr', data)
            self.assertEqual(run.await_count, 8)

    async def test_complete_missing_finding_with_explicit_no_source_stays_negative(self):
        data = payload(1)
        response = reply([{'i':1,'v':'missing','e':None,'r':'missing'}])
        with patch.object(model_client, 'generate', new=AsyncMock(return_value=response)) as generate:
            first = await verification._verify_cached_batch('ocr', data)
            second = await verification._verify_cached_batch('ocr', data)
        self.assertEqual(generate.await_count, 1)
        self.assertFalse(second['passed'])
        self.assertEqual(second['status'], 'needs_review')
        self.assertEqual(second['findings'], first['findings'])


if __name__ == '__main__':
    unittest.main()
