import hashlib
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import pymupdf

from apps.api import auto_documents, court_forms, verification, store, evidence_mapping


class ArtifactReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        data = patch.object(store, 'DATA_DIR', Path(directory.name))
        data.start()
        self.addCleanup(data.stop)
        self.case = {'id': 'case', 'input_revision': 1, 'documents': [], 'facts': [], 'client_name': 'synthetic'}
        self.calc = {'id': 'calc', 'summary': {'net_monthly_income': 2800000}, 'inputs': {}}

    def artifact(self, drawn='2800000'):
        pdf = pymupdf.open()
        page = pdf.new_page()
        page.insert_text((30, 40), drawn)
        raw = pdf.tobytes()
        pdf.close()
        directory = store.DATA_DIR / 'generated/case'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'artifact.pdf').write_bytes(raw)
        return {'id': 'artifact', 'sha256': hashlib.sha256(raw).hexdigest(),
            'preview': {'template': {'pages': [0]}, 'missing_fields': [], 'overflow_fields': [],
                'fields': [{'key': 'monthly_income', 'value': 2800000, 'page': 0, 'rect': [20, 20, 220, 65],
                            'source': {'type': 'legal_calculation'}}]},
            'ai_review': {'status': 'passed', 'passed': True}}

    def income_only_artifact(self, remove_selection=False):
        """Use the actual official original, including its two preselected boxes."""
        template = court_forms.TEMPLATES['D5110']
        with pymupdf.open(court_forms.original_path('D5110')) as pdf:
            pdf[0].insert_text((30, 40), '2800000')
            if remove_selection:
                matches = pdf[1].search_for('☑')
                self.assertEqual(len(matches), 1)
                pdf[1].add_redact_annot(matches[0], fill=(1, 1, 1))
                pdf[1].apply_redactions()
            raw = pdf.tobytes()
        record = self.artifact()
        (store.DATA_DIR / 'generated/case/artifact.pdf').write_bytes(raw)
        record.update(template_id='D5110', sha256=hashlib.sha256(raw).hexdigest(),
                      original_sha256=template['source']['sha256'])
        record['preview'].update(template=deepcopy(template), original_sha256=template['source']['sha256'])
        self.calc['summary'].update(monthly_creditor_capacity=1000000, months=2, total_creditor_payment=2000000)
        self.calc['schedule'] = [{'month': 1, 'creditor_payment': 1000000}, {'month': 2, 'creditor_payment': 1000000}]
        return record

    async def test_rendered_pdf_is_independently_checked_without_self_evidence(self):
        record = self.artifact()
        mocked = AsyncMock(return_value={'status': 'passed', 'passed': True, 'findings': []})
        with patch.object(verification, 'run_local_verification_batched', new=mocked):
            result = await auto_documents.verify_artifacts(self.case, [record], self.calc)
        self.assertTrue(result['passed'])
        sent = mocked.call_args.args[1]
        self.assertIn('2800000', sent['items'][0]['rendered_text'])
        self.assertEqual(sent['items'][0]['source_ids'], ['calculation:calc'])
        self.assertFalse(any(source['id'] == 'artifact' for source in sent['sources']))
        self.assertEqual(record['ai_review']['artifact_sha256'], record['sha256'])

    async def test_wrong_rendered_number_cannot_inherit_or_receive_a_pass(self):
        record = self.artifact('9999999')
        with patch.object(verification, 'run_local_verification_batched', new=AsyncMock(return_value={'status': 'passed', 'passed': True, 'findings': []})):
            result = await auto_documents.verify_artifacts(self.case, [record], self.calc)
        self.assertFalse(result['passed'])
        self.assertFalse(record['ai_review']['passed'])
        self.assertEqual(result['issues'][0]['code'], 'RENDERED_FIELD_MISMATCH')

    async def test_changed_file_hash_is_blocked_before_model(self):
        record = self.artifact()
        record['sha256'] = 'not-the-rendered-file'
        with patch.object(verification, 'run_local_verification_batched', new=AsyncMock()) as generate:
            result = await auto_documents.verify_artifacts(self.case, [record], self.calc)
        self.assertFalse(result['passed'])
        self.assertEqual(record['ai_review']['status'], 'unavailable')
        generate.assert_not_awaited()

    async def test_corrected_staff_value_uses_independent_originals_without_calculation(self):
        record = self.artifact('Supported correction')
        field = record['preview']['fields'][0]
        field.update(key='statement', value='Supported correction', source={'type': 'staff_input'})
        self.case['documents'] = [{'id': 'original', 'status': 'verified', 'text': 'Independent original evidence'}]
        mocked = AsyncMock(return_value={'status': 'passed', 'passed': True, 'findings': []})
        with patch.object(verification, 'run_local_verification_batched', new=mocked):
            result = await auto_documents.verify_artifacts(self.case, [record], None)
        self.assertTrue(result['passed'])
        sent = mocked.call_args.args[1]
        self.assertEqual(sent['items'][0]['source_ids'], ['original'])
        self.assertFalse(any('Supported correction' in source['text'] for source in sent['sources']))
        self.assertFalse(record['ai_review']['submission_ready'])

    async def test_invented_staff_value_does_not_override_independent_ai_failure(self):
        record = self.artifact('Invented fact')
        record['preview']['fields'][0].update(key='statement', value='Invented fact', source={'type': 'staff_input'})
        self.case['documents'] = [{'id': 'original', 'status': 'verified', 'text': 'Independent original evidence'}]
        with patch.object(verification, 'run_local_verification_batched', new=AsyncMock(return_value={
                'status': 'needs_review', 'passed': False, 'findings': [{'verdict': 'mismatch'}]})):
            result = await auto_documents.verify_artifacts(self.case, [record], None)
        self.assertFalse(result['passed'])
        self.assertEqual(record['status'], 'verification_required')

    async def test_missing_calculation_leaves_financial_fields_unverified(self):
        record = self.artifact()
        with patch.object(verification, 'run_local_verification_batched', new=AsyncMock()) as run:
            result = await auto_documents.verify_artifacts(self.case, [record], None)
        self.assertFalse(result['passed'])
        self.assertIn('ARTIFACT_FIELD_EVIDENCE_MISSING', [x['code'] for x in result['issues']])
        run.assert_not_awaited()

    async def test_typed_arithmetic_is_recomputed_from_originals_not_rendered_value(self):
        self.case['documents']=[{'id':'pay','status':'verified','text':'급여명세서\n월 실수령액: 3,200,000원'}]
        packet=evidence_mapping.build(self.case)
        record=self.artifact('38400000')
        record['preview']['evidence_source_signature']=packet['source_signature']
        record['preview']['fields'][0].update(key='annual_income',value=38400000,
            source=packet['origins']['annual_income'])
        mocked=AsyncMock(return_value={'status':'passed','passed':True,'findings':[]})
        with patch.object(verification,'run_local_verification_batched',new=mocked):
            result=await auto_documents.verify_artifacts(self.case,[record],None)
        self.assertTrue(result['passed'])
        payload=mocked.call_args.args[1]
        item=payload['items'][0]
        self.assertIn('pay',item['source_ids'])
        arithmetic=next(source for source in payload['sources'] if source['id'] in item['source_ids'] and source.get('kind')=='code_calculation')
        self.assertIn('monthly_times_12_projection',arithmetic['text'])
        self.assertIn('3200000',arithmetic['text'])
        self.assertNotIn('artifact',arithmetic['text'])

    async def test_forged_typed_value_cannot_pass_by_matching_the_pdf(self):
        self.case['documents']=[{'id':'pay','status':'verified','text':'급여명세서\n월 실수령액: 3,200,000원'}]
        packet=evidence_mapping.build(self.case)
        record=self.artifact('9999999')
        record['preview']['evidence_source_signature']=packet['source_signature']
        record['preview']['fields'][0].update(key='monthly_income',value=9999999,
            source=packet['origins']['monthly_income'])
        with patch.object(verification,'run_local_verification_batched',new=AsyncMock()) as run:
            result=await auto_documents.verify_artifacts(self.case,[record],None)
        self.assertFalse(result['passed']);run.assert_not_awaited()
        self.assertIn('DETERMINISTIC_FIELD_CHANGED',[issue['code'] for issue in result['issues']])

    async def test_proposed_legal_number_is_not_a_source_fact_or_ai_pass(self):
        record=self.artifact('36')
        record['preview']['fields'][0].update(key='months',value=36,
            source={'type':'proposed_legal_input','status':'review_required','source_ids':['statute-611']})
        with patch.object(verification,'run_local_verification_batched',new=AsyncMock()) as run:
            result=await auto_documents.verify_artifacts(self.case,[record],None)
        self.assertFalse(result['passed']);run.assert_not_awaited()
        self.assertIn('PROPOSED_LEGAL_INPUT_REVIEW',[issue['code'] for issue in result['issues']])

    async def test_local_outage_does_not_repeat_timeout_for_every_pdf(self):
        first = self.artifact()
        second = deepcopy(first)
        second['id'] = 'artifact-two'
        folder = store.DATA_DIR / 'generated/case'
        (folder / 'artifact-two.pdf').write_bytes((folder / 'artifact.pdf').read_bytes())
        with patch.object(verification, 'run_local_verification_batched', new=AsyncMock(return_value={
                'status': 'unavailable', 'passed': False, 'findings': [], 'error': {'code': 'TIMEOUT'}})) as run:
            result = await auto_documents.verify_artifacts(self.case, [first, second], self.calc)
        self.assertFalse(result['passed'])
        run.assert_awaited_once()
        self.assertFalse(second['ai_review']['attempted'])
        self.assertEqual(second['ai_review']['status'], 'unavailable')

    async def test_repeated_semantic_fields_have_unique_physical_ids_through_real_batcher(self):
        record = self.artifact()
        record['preview']['fields'].append(deepcopy(record['preview']['fields'][0]))

        async def accept(kind, payload):
            return {'status': 'passed', 'passed': True, 'findings': [],
                    'checked_item_ids': [item['id'] for item in payload['items']],
                    'input_sha256': verification._digest(payload)}

        with patch.object(verification, 'run_local_verification', new=AsyncMock(side_effect=accept)) as run:
            result = await auto_documents.verify_artifacts(self.case, [record], self.calc)
        self.assertTrue(result['passed'])
        items = [item for call in run.call_args_list for item in call.args[1]['items']]
        self.assertEqual(len(items), 2)
        self.assertEqual(len({item['id'] for item in items}), 2)
        self.assertEqual({item['key'] for item in items}, {'monthly_income'})
        self.assertEqual(record['ai_review']['coverage']['mapped_field_occurrences'], 2)

    async def test_actual_original_pages_and_selected_clauses_are_reviewed_with_independent_evidence(self):
        record = self.income_only_artifact()
        # Owned assets and valuation disposal expenses do not mean proceeds are
        # being contributed to an income-only repayment plan.
        self.calc['inputs']['assets'] = [{'owned_value': 10000000, 'disposal_cost': 500000}]
        mocked = AsyncMock(return_value={'status': 'passed', 'passed': True, 'findings': []})
        with patch.object(verification, 'run_local_verification_batched', new=mocked):
            result = await auto_documents.verify_artifacts(self.case, [record], self.calc)
        self.assertTrue(result['passed'], result['issues'])
        sent = mocked.call_args.args[1]
        originals = [source for source in sent['sources'] if source['id'].startswith('court-original:')]
        self.assertEqual(len(originals), 7)
        self.assertTrue(all(source['role'] == 'public_form_not_customer_evidence' for source in originals))
        clauses = [item for item in sent['items'] if item['key'] == 'original_clause_applicability']
        self.assertEqual(len(clauses), 2)
        self.assertTrue(all(item['source_ids'] == ['calculation:calc:funding'] for item in clauses))
        self.assertTrue(all('☑' in item['rendered_text'] for item in clauses))
        review = record['ai_review']
        self.assertEqual(review['coverage']['original_pages'], 7)
        self.assertEqual(review['coverage']['selected_original_clauses'], 2)
        self.assertFalse(review['whole_document_verified'])
        self.assertFalse(review['submission_ready'])
        self.assertEqual(review['scope'], 'mapped_fields_and_original_context')

    async def test_property_sale_funding_cannot_pass_preselected_income_only_original(self):
        record = self.income_only_artifact()
        self.calc['inputs']['asset_sale_proceeds'] = 5000000
        with patch.object(verification, 'run_local_verification_batched', new=AsyncMock(return_value={'status': 'passed', 'passed': True, 'findings': []})):
            result = await auto_documents.verify_artifacts(self.case, [record], self.calc)
        self.assertFalse(result['passed'])
        self.assertIn('FORM_FUNDING_VARIANT_UNSUPPORTED', [issue['code'] for issue in result['issues']])

    async def test_missing_schedule_cannot_establish_preselected_funding_type(self):
        record = self.income_only_artifact()
        self.calc['schedule'] = []
        with patch.object(verification, 'run_local_verification_batched', new=AsyncMock(return_value={'status': 'passed', 'passed': True, 'findings': []})):
            result = await auto_documents.verify_artifacts(self.case, [record], self.calc)
        self.assertFalse(result['passed'])
        self.assertIn('FORM_FUNDING_BASIS_MISSING', [issue['code'] for issue in result['issues']])

    async def test_original_context_alone_cannot_verify_unfilled_artifact(self):
        record = self.income_only_artifact()
        record['preview']['fields'] = []
        with patch.object(verification, 'run_local_verification_batched', new=AsyncMock(return_value={'status': 'passed', 'passed': True, 'findings': []})) as run:
            result = await auto_documents.verify_artifacts(self.case, [record], self.calc)
        self.assertFalse(result['passed'])
        self.assertIn('NO_RENDERED_FIELDS', [issue['code'] for issue in result['issues']])
        run.assert_not_awaited()

    async def test_pdf_and_batch_progress_contains_counts_not_private_source_text(self):
        record = self.artifact()
        changes = []
        async def review(kind, payload, progress=None):
            await progress({'completed': 0, 'total': 1})
            await progress({'completed': 1, 'total': 1})
            return {'status': 'passed', 'passed': True, 'findings': []}
        with patch.object(verification, 'run_local_verification_batched', new=AsyncMock(side_effect=review)):
            result = await auto_documents.verify_artifacts(self.case, [record], self.calc, progress=changes.append)
        self.assertTrue(result['passed'])
        self.assertEqual((changes[0]['completed'], changes[-1]['completed']), (0, 1))
        self.assertTrue(all(change['total'] == 1 for change in changes))
        self.assertTrue(any('0/1' in change['label'] for change in changes))
        self.assertNotIn('2800000', str(changes))
        self.assertTrue(all(set(change) == {'completed', 'total', 'label'} for change in changes))

    async def test_changed_preselected_checkbox_cannot_pass_even_with_new_artifact_hash(self):
        record = self.income_only_artifact(remove_selection=True)
        with patch.object(verification, 'run_local_verification_batched', new=AsyncMock(return_value={'status': 'passed', 'passed': True, 'findings': []})):
            result = await auto_documents.verify_artifacts(self.case, [record], self.calc)
        self.assertFalse(result['passed'])
        codes = [issue['code'] for issue in result['issues']]
        self.assertIn('ORIGINAL_SELECTION_CHANGED', codes)
        self.assertIn('ORIGINAL_CLAUSE_CHANGED', codes)

    def test_instruction_example_checkmarks_are_not_preselected_customer_answers(self):
        for template_id in ('D5103', 'D5105'):
            template = court_forms.TEMPLATES[template_id]
            record = {'template_id': template_id, 'original_sha256': template['source']['sha256']}
            preview = {'template': template, 'original_sha256': template['source']['sha256']}
            with pymupdf.open(court_forms.original_path(template_id)) as original:
                _, pages, clauses, issues = auto_documents._original_context(record, preview, original)
            self.assertEqual(issues, [], template_id)
            self.assertEqual(clauses, [], template_id)
            self.assertTrue(any('☑' in page['original_text'] for page in pages))


if __name__ == '__main__':
    unittest.main()
