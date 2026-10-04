"""Effective human corrections cross the private OCR and calculation boundary."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from apps.api import automation, ax_service, domain, evidence_mapping, extraction_readiness
from tests.test_human_review_mapping import PAYROLL, edit, make_case
from tests.test_evidence_mapping_integrity import document


class ReviewedAutomationHandoffTests(unittest.TestCase):
    def case(self):
        case = domain.new_case('가상 검토인', 'CT01', '서울회생법원', '합성 자료 검토', True)
        parsed = make_case(document('payroll', PAYROLL))
        case.update(documents=parsed['documents'], requests=[], extraction_candidates=[])
        ax_service.enrich_candidates(case, extraction_readiness.candidates(case['documents'][0]))
        extraction_readiness.capture(case, case['documents'][0])
        edit(case, 'monthly_income', 3300000)
        return case

    def test_verification_distinguishes_review_from_unchanged_original(self):
        case = self.case()
        packet = evidence_mapping.build(case)
        sources = automation._sources(case, verified_only=True, packet=packet)
        items = automation._verification_items(case, sources, packet=packet)
        source_map = {row['id']: row for row in sources}
        row = next(row for row in items if row['key'] == 'income_net')
        self.assertEqual(row['value'], 3300000)
        self.assertEqual(row['source_type'], 'human_review')
        self.assertTrue(all(source_map[ref]['kind'] == 'human_review' for ref in row['source_ids']))
        self.assertEqual(source_map['payroll']['text'], PAYROLL)
        self.assertNotIn(row['quote'], PAYROLL)
        self.assertTrue(all(row['quote'] in source_map[ref]['text'] for ref in row['source_ids']))

    def test_normal_pipeline_uses_effective_mapping_without_document_only_reparse(self):
        case = self.case()
        async def supported(kind, payload, **kwargs):
            return {'status': 'passed', 'passed': True,
                    'checked_item_ids': [row['id'] for row in payload['items']], 'findings': []}
        with patch('apps.api.automation._validate_requests', new=AsyncMock(return_value=(True, False))), \
             patch('apps.api.verification.run_local_verification_batched', side_effect=supported), \
             patch('apps.api.verification.extract_calculation_inputs', new=AsyncMock()) as reparse, \
             patch('apps.api.verification.run_strategy_verification', new=AsyncMock(return_value={'status':'passed','passed':True})), \
             patch('apps.api.automation._retrieve', return_value=[]), \
             patch('apps.api.automation.learned_patterns', return_value=[]), \
             patch('apps.api.approval_estimator.estimate', return_value={}), \
             patch('apps.api.preliminary_drafting.create', new=AsyncMock(return_value={'id':'review-draft','review_pending':[]})):
            asyncio.run(automation.advance(case))
        reparse.assert_not_awaited()
        mapped = case['structured_data'][-1]
        self.assertEqual(mapped['calculation_inputs']['income']['monthly_amount'], 3300000)
        self.assertEqual(mapped['mapping']['status'], 'human_reviewed')
        self.assertEqual(case['legal_calculations'][-1]['inputs']['income']['monthly_amount'], 3300000)
        self.assertTrue(mapped['mapping']['human_review_sources'])
        self.assertFalse(case['ax_pipeline']['submission_ready'])

    def test_merged_equal_values_keep_each_original_quote(self):
        sources = [{'id':'pay','text':'당월 실수령액 3,200,000원'},
                   {'id':'certificate','text':'월 평균 급여 실수령 3,200,000원'}]
        facts = [{'id':str(index), 'document_id':source['id'], 'key':'income_net',
                  'value':3200000, 'quote':source['text'], 'frequency':'monthly', 'basis':'net'}
                 for index, source in enumerate(sources)]
        items = automation._verification_items({}, sources, packet={'facts':facts})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['source_ids'], ['pay','certificate'])
        self.assertEqual(items[0]['covered_fact_ids'], ['0','1'])
        self.assertEqual(items[0]['source_quotes'], {source['id']:[source['text']] for source in sources})

    def test_verification_retains_page_and_period_for_repeated_monthly_values(self):
        pages = [{'page':1, 'text':'2026년 1월\n실수령 3,200,000원'},
                 {'page':2, 'text':'2026년 2월\n실수령 3,200,000원'}]
        text = '\n'.join(page['text'] for page in pages)
        case = {'documents':[{'id':'pay', 'text':text, 'page_texts':pages, 'status':'verified'}]}
        facts = [{'id':f'fact-{page["page"]}', 'document_id':'pay', 'key':'income_net',
                  'value':3200000, 'quote':'실수령 3,200,000원', 'page':page['page'],
                  'line_start':2, 'line_end':2, 'period_start':f'2026-0{page["page"]}-01',
                  'period_quote':page['text'].split('\n')[0]} for page in pages]
        packet = {'facts':facts}
        sources = automation._sources(case, verified_only=True, packet=packet)
        self.assertEqual(len(sources[0]['page_ranges']), 2)
        for source_page, page in zip(sources[0]['page_ranges'], pages):
            self.assertEqual(text[source_page['start']:source_page['end']], page['text'])
        items = automation._verification_items(case, sources, packet=packet)
        self.assertEqual(len(items), 2)
        for item, fact in zip(items, facts):
            self.assertEqual(item['source_evidence']['pay'][0]['page'], fact['page'])
            self.assertEqual(item['period_start'], fact['period_start'])
            self.assertEqual(item['period_quote'], fact['period_quote'])


if __name__ == '__main__':
    unittest.main()
