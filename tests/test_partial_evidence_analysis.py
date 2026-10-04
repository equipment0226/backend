"""Semantic failures preserve useful analysis without authorizing unverified filing."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from apps.api import preliminary_drafting, store, verification
from tests.test_evidence_mapping_pipeline import fixture


class PartialEvidenceAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_passes_only_known_numeric_facts_and_links_all_outputs(self):
        case = fixture()
        documents = copy.deepcopy(case['documents'])
        reasoning = AsyncMock(return_value={'status':'passed', 'passed':True, 'findings':[]})
        with tempfile.TemporaryDirectory() as folder, patch.object(store, 'DATA_DIR', Path(folder)), \
                patch('apps.api.automation._retrieve', return_value=[]), \
                patch('apps.api.automation.approved_examples', return_value=[]), \
                patch('apps.api.auto_documents.prepare', return_value=([], [])), \
                patch('apps.api.verification.run_local_verification_batched', new=AsyncMock()) as local:
            draft = await preliminary_drafting.create(case,
                [{'code':'OCR_VERIFICATION', 'stage':'ocr', 'reason':'시간 초과'}], {'ocr','analysis'},
                ocr_check={'id':'v-timeout', 'passed':False},
                local_failure={'status':'unavailable', 'error':{'code':'MODEL_TIMEOUT'}},
                strategy_review=reasoning)
        local.assert_not_awaited()
        reasoning.assert_awaited_once()
        structured, calculation, refs = reasoning.await_args.args
        self.assertEqual(structured['facts']['monthly_income'], 3200000)
        self.assertEqual(structured['facts']['total_debt'], 72000000)
        self.assertEqual(structured['income_basis'], 'net')
        self.assertEqual(structured['employment_type'], 'wage')
        self.assertTrue(all(type(value) in (int,float,bool) for value in structured['facts'].values()))
        payload = verification.build_safe_strategy_payload(structured, calculation, refs)
        self.assertNotIn(case['client_name'], store.dumps(payload))
        self.assertNotIn('TEST-ACCOUNT-1', store.dumps(payload))
        self.assertEqual(payload['case_features']['income_basis'], 'net')
        strategy = case['strategy_analyses'][-1]
        self.assertEqual(strategy['evidence_status'], 'semantic_review_pending')
        self.assertEqual(strategy['decision'], 'review_required')
        self.assertFalse(strategy['submission_ready'])
        self.assertEqual(draft['strategy_id'], strategy['id'])
        self.assertEqual(draft['analysis_calculation_id'], calculation['id'])
        self.assertEqual(draft['structured_data_id'], case['structured_data'][-1]['id'])
        self.assertFalse(case['structured_data'][-1]['semantic_verified'])
        self.assertFalse(draft['submission_ready'])
        self.assertIsNone(draft['calculation_id'])
        self.assertTrue(calculation['blockers'])
        self.assertEqual(case['documents'], documents)

    async def test_existing_analysis_is_reused_without_duplicate_external_reasoning(self):
        case = fixture()
        calculation = {'id':'calc-existing', 'blockers':[], 'summary':{}}
        case['strategy_analyses'] = [{'id':'strategy-existing', 'calculation_id':calculation['id'],
            'structured_data_id':'structured-original', 'strategies':[], 'reasons':[]}]
        reasoning = AsyncMock()
        with tempfile.TemporaryDirectory() as folder, patch.object(store, 'DATA_DIR', Path(folder)), \
                patch('apps.api.automation._retrieve', return_value=[]), \
                patch('apps.api.automation.approved_examples', return_value=[]), \
                patch('apps.api.auto_documents.prepare', return_value=([], [])):
            draft = await preliminary_drafting.create(case, [], {'analysis'}, calculation=calculation,
                local_failure={'status':'unavailable'}, strategy_review=reasoning, strategy=case['strategy_analyses'][0])
        reasoning.assert_not_awaited()
        self.assertEqual(len(case['strategy_analyses']), 1)
        self.assertEqual(draft['strategy_id'], 'strategy-existing')

    async def test_strategy_configuration_error_cannot_discard_extraction_or_draft(self):
        case = fixture()
        with tempfile.TemporaryDirectory() as folder, patch.object(store, 'DATA_DIR', Path(folder)), \
                patch('apps.api.automation._retrieve', return_value=[]), \
                patch('apps.api.automation.approved_examples', return_value=[]), \
                patch('apps.api.auto_documents.prepare', return_value=([], [])):
            draft = await preliminary_drafting.create(case, [], {'ocr','analysis'},
                local_failure={'status':'unavailable'}, strategy_review=AsyncMock(side_effect=ValueError('bad configuration')))
        self.assertEqual(case['strategy_analyses'][-1]['verification']['status'], 'unavailable')
        self.assertTrue(case['structured_data'][-1]['facts'])
        self.assertTrue(case['legal_calculations'])
        self.assertEqual(case['drafts'][-1]['id'], draft['id'])
        self.assertFalse(draft['submission_ready'])


if __name__ == '__main__':
    unittest.main()
