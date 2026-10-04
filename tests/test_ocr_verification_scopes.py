"""Long originals and corroborating pages retain complete local review coverage."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from apps.api import model_client, verification as verify
from tests.test_ocr_verification_runtime import answer, reply


def paged_source(page_count=12):
    pages = []
    for page in range(page_count):
        pages.append(f'가상 급여명세서\n단위: 원\n대상 기간: {page + 1}월\n'
                     + '서류 안내 및 부가 기재사항입니다.\n' * 38
                     + f'월 실수령액 {2800000 + page:,}원\n')
    text, ranges = '', []
    for page, contents in enumerate(pages, 1):
        if text:
            text += '\n'
        start = len(text)
        text += contents
        ranges.append({'page': page, 'start': start, 'end': len(text)})
    return {'id': 'payroll-original', 'kind': 'case_document', 'version': 1,
            'text': text, 'page_ranges': ranges}


class OcrScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        verify.clear_local_verification_cache()
        self.addCleanup(verify.clear_local_verification_cache)

    async def test_354_facts_on_long_monthly_original_keep_every_page_and_original_id(self):
        source = paged_source()
        items = []
        for index in range(354):
            month = index % 12
            quote = f'월 실수령액 {2800000 + month:,}원'
            items.append({'id': f'fact-{index}', 'key': 'income_net', 'value': 2800000 + month,
                          'quote': quote, 'source_ids': [source['id']], 'period_start': f'2026-{month + 1:02}-01',
                          'source_evidence': {source['id']: [{'quote': quote, 'page': month + 1}]}})
        payload = {'sources': [source], 'items': items}
        calls = []
        def bounded(messages, schema, **kwargs):
            bound = (sum(len(message['content'].encode('utf-8')) for message in messages)
                     + len(json.dumps(schema, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
                     + kwargs['max_tokens'] + 512)
            self.assertLessEqual(bound, 8192)
            calls.append(json.loads(messages[1]['content']))
            return answer(messages, schema, **kwargs)
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=bounded)):
            result = await verify.run_local_verification_batched('ocr', payload)
        self.assertTrue(result['passed'], result.get('error'))
        self.assertEqual(set(result['checked_item_ids']), {row['id'] for row in items})
        self.assertEqual(len(result['findings']), 354)
        self.assertEqual(result['input_sha256'], verify._digest(payload))
        self.assertEqual(result['source_refs'][0]['sha256'], verify._digest(source['text']))
        self.assertEqual({row['excerpt_provenance']['page'] for row in result['source_scopes']}, set(range(1, 13)))
        self.assertLess(len(calls), 100)
        for scoped in result['source_scopes']:
            provenance = scoped['excerpt_provenance']
            self.assertEqual(provenance['original_sha256'], verify._digest(source['text']))
            page = source['page_ranges'][provenance['page'] - 1]
            self.assertTrue(any(row['start'] <= page['start'] and row['end'] >= page['end'] for row in provenance['ranges']))

    async def test_merged_fifteen_documents_requires_all_corroborations_and_keeps_negative(self):
        sources = [{'id': f'original-{index:02}', 'kind': 'case_document',
                    'text': f'기관 {index} 발급\n성명: 가상인\n' + '발급 안내\n' * 40} for index in range(15)]
        item = {'id': 'identity', 'key': 'client_name', 'value': '가상인', 'quote': '성명: 가상인',
                'source_ids': [source['id'] for source in sources],
                'source_quotes': {source['id']: ['성명: 가상인'] for source in sources}}
        def one_negative(messages, schema, **kwargs):
            wire = json.loads(messages[1]['content'])
            checks = {}
            by_id = {source['id']: source for source in wire['sources']}
            for row in wire['items']:
                mismatch = '기관 14 발급' in by_id[row['sources'][0]]['text']
                checks[str(row['i'])] = 'person' if mismatch else 'match'
            return reply(checks)
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=one_negative)):
            result = await verify.run_local_verification_batched('ocr', {'sources': sources, 'items': [item]})
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['checked_item_ids'], ['identity'])
        finding = result['findings'][0]
        self.assertEqual(finding['status'], 'mismatch')
        self.assertEqual(finding['source_id'], 'original-14')
        self.assertEqual({row['source_id'] for row in finding['scope_findings']}, set(item['source_ids']))

    async def test_timeout_in_one_source_never_completes_merged_item(self):
        sources = [{'id': str(index), 'text': '성명: 가상인\n' + '발급 안내\n' * 60} for index in range(10)]
        item = {'id': 'identity', 'key': 'client_name', 'value': '가상인', 'quote': '성명: 가상인',
                'source_ids': [source['id'] for source in sources]}
        count = 0
        def timeout(messages, schema, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise asyncio.TimeoutError()
            return answer(messages, schema, **kwargs)
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=timeout)):
            result = await verify.run_local_verification_batched('ocr', {'sources': sources, 'items': [item]})
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['checked_item_ids'], [])
        self.assertEqual(result['findings'], [])

    async def test_later_page_retains_remote_unit_header_and_long_immutable_quote(self):
        header = '가상 거래명세서\n단위: 천원\n' + '서류 안내\n' * 80
        quote = '급여 내역을 확인합니다. ' * 55 + '월 실수령액 2,800천원'
        second = '두 번째 페이지\n' + quote + '\n'
        source = {'id': 'original', 'kind': 'case_document', 'text': header + second,
                  'page_ranges': [{'page': 1, 'start': 0, 'end': len(header)},
                                  {'page': 2, 'start': len(header), 'end': len(header + second)}]}
        item = {'id': 'income', 'key': 'income_net', 'value': 2800000, 'quote': quote,
                'source_ids': ['original'], 'source_unit_quote': '단위: 천원',
                'source_evidence': {'original': [{'quote': quote, 'page': 2}]}}
        def unit_context(messages, schema, **kwargs):
            wire = json.loads(messages[1]['content'])
            self.assertIn('단위: 천원', wire['sources'][0]['text'])
            self.assertIn(quote, wire['sources'][0]['text'])
            self.assertEqual(wire['sources'][0]['excerpt_provenance']['page'], 2)
            return answer(messages, schema, **kwargs)
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=unit_context)):
            result = await verify.run_local_verification_batched('ocr', {'sources': [source], 'items': [item]})
        self.assertTrue(result['passed'], result.get('error'))
        self.assertEqual(result['findings'][0]['quote'], quote)

    async def test_quote_attributed_to_wrong_small_page_is_rejected(self):
        text = '첫 페이지\n월 실수령액 2,800,000원\n둘째 페이지\n다른 기재사항'
        boundary = text.index('둘째 페이지')
        source = {'id': 'original', 'text': text, 'page_ranges': [
            {'page': 1, 'start': 0, 'end': boundary}, {'page': 2, 'start': boundary, 'end': len(text)}]}
        item = {'id': 'income', 'key': 'income_net', 'value': 2800000, 'quote': '월 실수령액 2,800,000원',
                'source_ids': ['original'], 'source_evidence': {'original': [{'quote': '월 실수령액 2,800,000원', 'page': 2}]}}
        with patch.object(model_client, 'generate', new=AsyncMock()) as generate:
            result = await verify.run_local_verification_batched('ocr', {'sources': [source], 'items': [item]})
        generate.assert_not_called()
        self.assertFalse(result['passed'])
        self.assertEqual(result['error']['code'], 'INVALID_VERIFICATION_SCOPE')

    async def test_missing_or_ambiguous_declared_page_never_falls_back_to_another_page(self):
        source = paged_source(2)
        quote = '월 실수령액 2,800,000원'
        item = {'id': 'income', 'key': 'income_net', 'value': 2800000, 'quote': quote,
                'source_ids': [source['id']], 'source_evidence': {source['id']: [{'quote': quote, 'page': 3}]}}
        for ranges in (source['page_ranges'], source['page_ranges'] + [dict(source['page_ranges'][0], page=3)] * 2):
            with patch.object(model_client, 'generate', new=AsyncMock()) as generate:
                result = await verify.run_local_verification_batched('ocr', {
                    'sources': [{**source, 'page_ranges': ranges}], 'items': [item]})
            generate.assert_not_called()
            self.assertFalse(result['passed'])
            self.assertEqual(result['error']['code'], 'INVALID_VERIFICATION_SCOPE')

    async def test_source_specific_unit_and_remote_period_context_are_not_inherited(self):
        first = {'id': 'a', 'kind': 'case_document', 'text': '단위: 천원\n월 실수령액 2,800천원'}
        second_text = ('거래 명세서\n' + '거래 안내사항\n' * 200
                       + '대상 기간: 2026년 5월\n' + '거래 안내사항\n' * 200 + '월 실수령액 2,800,000원')
        second = {'id': 'b', 'kind': 'case_document', 'text': second_text}
        item = {'id': 'income', 'key': 'income_net', 'value': 2800000, 'quote': '월 실수령액 2,800천원',
                'source_ids': ['a', 'b'], 'source_unit_quote': '단위: 천원', 'unit_multiplier': 1000,
                'source_evidence': {'a': [{'quote': '월 실수령액 2,800천원', 'source_unit_quote': '단위: 천원', 'unit_multiplier': 1000}],
                                    'b': [{'quote': '월 실수령액 2,800,000원', 'period_quote': '대상 기간: 2026년 5월'}]}}
        def check_context(messages, schema, **kwargs):
            wire = json.loads(messages[1]['content'])
            sources = {row['id']: row for row in wire['sources']}
            for row in wire['items']:
                source = sources[row['sources'][0]]
                if '거래 명세서' in source['text']:
                    self.assertIn('대상 기간: 2026년 5월', source['text'])
                    self.assertNotIn('source_unit_quote', row)
                    self.assertEqual(row['unit_multiplier'], 1)
            return answer(messages, schema, **kwargs)
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=check_context)):
            result = await verify.run_local_verification_batched('ocr', {'sources': [first, second], 'items': [item]})
        self.assertTrue(result['passed'], result.get('error'))
        self.assertEqual(len(result['findings'][0]['scope_findings']), 2)

    async def test_change_outside_selected_windows_invalidates_local_cache(self):
        quote = '월 실수령액 2,800,000원'
        source = {'id': 'original', 'kind': 'case_document',
                  'text': '가상 거래자료\n' + '월별 거래 내역\n' * 2000 + quote + '\n마지막 기재사항'}
        item = {'id': 'income', 'key': 'income_net', 'value': 2800000, 'quote': quote, 'source_ids': ['original']}
        payload = {'sources': [source], 'items': [item]}
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=answer)) as generate:
            first = await verify.run_local_verification_batched('ocr', payload)
            again = await verify.run_local_verification_batched('ocr', payload)
            self.assertTrue(first['passed'] and again['passed'])
            self.assertEqual(generate.await_count, 1)
            changed = copy.deepcopy(payload)
            changed['sources'][0]['text'] = changed['sources'][0]['text'][:10000] + '변경' + changed['sources'][0]['text'][10002:]
            updated = await verify.run_local_verification_batched('ocr', changed)
            self.assertTrue(updated['passed'])
            self.assertEqual(generate.await_count, 2)
            self.assertNotEqual(first['source_refs'], updated['source_refs'])


if __name__ == '__main__':
    unittest.main()
