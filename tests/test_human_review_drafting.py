"""Explicit correction provenance survives narrative, form fill and PDF review."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import pymupdf

from apps.api import (auto_documents, evidence_mapping, grounded_drafting, model_client,
                      narrative_claims, preliminary_drafting, store, verification)
from tests.test_human_review_mapping import PAYROLL, edit, make_case
from tests.test_evidence_mapping_integrity import document


def corrected_case(text=PAYROLL, value=3300000):
    case = make_case(document('payroll', text))
    case.update(id='case', facts=[], org_id='test-office', consultation={
        'notes': '생활비가 부족하여 돈을 빌렸습니다. 생활비 부족이 계속되어 차용을 반복하였습니다.',
        'status': 'party_statement'})
    edit(case, 'monthly_income', value)
    return case


class HumanReviewDraftingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        data = patch.object(store, 'DATA_DIR', Path(directory.name))
        data.start()
        self.addCleanup(data.stop)

    def artifact(self, case, packet, key='monthly_income', value=3300000):
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((30, 40), str(value))
            raw = pdf.tobytes()
        directory = store.DATA_DIR / 'generated' / case['id']
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'reviewed-artifact.pdf').write_bytes(raw)
        return {'id': 'reviewed-artifact', 'sha256': hashlib.sha256(raw).hexdigest(),
            'preview': {'template': {'pages': [0]}, 'missing_fields': [], 'overflow_fields': [],
                'evidence_source_signature': packet['source_signature'],
                'fields': [{'key': key, 'value': value, 'page': 0, 'rect': [20, 20, 220, 65],
                            'source': copy.deepcopy(packet['origins'][key])}]}}

    def test_render_projection_keeps_signed_edit_without_double_applying_it(self):
        case = corrected_case()
        packet = evidence_mapping.build(case)
        safe, _ = preliminary_drafting.render_case(case, {'passed': False, 'findings': []})
        self.assertTrue(safe['source_review_candidates'])
        rebuilt = evidence_mapping.build(safe)
        self.assertEqual(rebuilt['form_values']['monthly_income'], 3300000)
        self.assertEqual(rebuilt['source_signature'], packet['source_signature'])
        case['source_review_candidates'] = copy.deepcopy(case['extraction_candidates'])
        self.assertEqual(len(evidence_mapping.build(case)['human_review_sources']), 1)

    def test_reviewed_won_amount_does_not_inherit_original_thousand_won_multiplier(self):
        case = corrected_case('급여명세서\n단위: 천원\n급여월: 2026-08\n실지급액: 3,200', 3300000)
        packet = evidence_mapping.build(case)
        fact = next(row for row in packet['facts'] if row['key'] == 'income_net')
        self.assertEqual(fact['original_unit_multiplier'], 1000)
        self.assertEqual(fact['unit_multiplier'], 1)
        context, problems = grounded_drafting.build_context([fact], [], [], {})
        self.assertFalse(problems)
        source = context['sources'][0]
        self.assertEqual(source['kind'], 'human_review')
        self.assertFalse(narrative_claims.validate_numeric_roles('월 실수령 소득은 3,300,000원입니다.', [source]))
        self.assertTrue(narrative_claims.validate_numeric_roles('월 실수령 소득은 3,200,000원입니다.', [source]))

    async def test_narrative_uses_corrected_income_as_human_review_not_original_ocr(self):
        case = corrected_case()
        packet = evidence_mapping.build(case)
        facts = [row for row in packet['facts'] if row['key'] == 'income_net']
        law = {'id': 'law:test', 'kind': 'public_legal_source', 'text': '변제계획의 수행 가능성을 검토한다.',
               'url': 'https://www.law.go.kr/test', 'source_sha256': 'public-test'}

        def sentence(messages, schema, **kwargs):
            request = json.loads(messages[1]['content'])
            if request['topic'] == 'repayment_basis':
                ref = next(row for row in request['evidence'] if row['kind'] == 'human_review')
                text = '현재 월 실수령 소득은 3,300,000원입니다.'
            else:
                ref = next(row for row in request['evidence'] if row['kind'] == 'party_statement')
                text = ('저는 부족한 생활비를 충당하기 위하여 돈을 빌렸습니다.' if request['topic'] == 'debt_origin'
                        else '생활비 부족이 이어지면서 차용을 반복하였습니다.')
            return {'done': True, 'done_reason': 'stop', 'message': {'content': json.dumps(
                {'text': text, 'evidence_ids': [ref['id']]}, ensure_ascii=False)}}

        with patch.object(grounded_drafting, '_trusted_legal', return_value=law), \
                patch.object(model_client, 'generate', new=AsyncMock(side_effect=sentence)):
            result = await grounded_drafting.compose(facts, [{}], [], {}, section_ids=['statement'],
                consultation={'source_id': 'consultation:notes', 'text': case['consultation']['notes']})
        self.assertEqual(result['status'], 'completed', result.get('verification'))
        review = next(row for row in result['source_refs'] if row['kind'] == 'human_review')
        self.assertIn('staff_correction', review['corroboration'])
        self.assertEqual(review['original_value'], 3200000)
        self.assertTrue(result['verification']['downstream_semantic_review_required'])

    async def test_pdf_compares_original_and_bound_review_without_self_source(self):
        case = corrected_case()
        packet = evidence_mapping.build(case)
        record = self.artifact(case, packet)
        mocked = AsyncMock(return_value={'status': 'passed', 'passed': True, 'findings': []})
        with patch.object(verification, 'run_local_verification_batched', new=mocked):
            result = await auto_documents.verify_artifacts(case, [record], None)
        self.assertTrue(result['passed'], result)
        payload = mocked.call_args.args[1]
        item = payload['items'][0]
        review = next(row for row in payload['sources'] if row.get('kind') == 'human_review')
        self.assertIn('payroll', item['source_ids'])
        self.assertIn(review['id'], item['source_ids'])
        self.assertFalse(review['ai_verified'])
        self.assertFalse(any(row['id'] == record['id'] for row in payload['sources']))
        self.assertFalse(record['ai_review']['submission_ready'])

    async def test_reviewed_arithmetic_keeps_review_operands_separate_from_original(self):
        case = corrected_case()
        packet = evidence_mapping.build(case)
        record = self.artifact(case, packet, 'annual_income', 39600000)
        mocked = AsyncMock(return_value={'status': 'passed', 'passed': True, 'findings': []})
        with patch.object(verification, 'run_local_verification_batched', new=mocked):
            result = await auto_documents.verify_artifacts(case, [record], None)
        self.assertTrue(result['passed'])
        payload = mocked.call_args.args[1]
        arithmetic = next(row for row in payload['sources'] if row.get('kind') == 'code_calculation'
                          and row['id'] in payload['items'][0]['source_ids'])
        operand = json.loads(arithmetic['text'])['operands'][0]
        self.assertEqual(operand['source_type'], 'human_review')
        self.assertTrue(operand['source_id'].startswith('human-review:'))
        self.assertEqual(operand['document_id'], 'payroll')

    async def test_forged_or_stale_review_cannot_pass_by_matching_printed_value(self):
        for tamper in ('value', 'source'):
            with self.subTest(tamper=tamper):
                case = corrected_case()
                packet = evidence_mapping.build(case)
                record = self.artifact(case, packet, value=3300001 if tamper == 'value' else 3300000)
                if tamper == 'source':
                    case['documents'][0]['page_texts'][0]['text'] += '\n원문 변경'
                with patch.object(verification, 'run_local_verification_batched', new=AsyncMock()) as run:
                    result = await auto_documents.verify_artifacts(case, [record], None)
                self.assertFalse(result['passed'])
                run.assert_not_awaited()
                self.assertIn('DETERMINISTIC_FIELD_CHANGED', {row['code'] for row in result['issues']})


if __name__ == '__main__':
    unittest.main()
