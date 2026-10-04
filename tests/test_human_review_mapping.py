"""Server-recorded corrections reach arithmetic without rewriting originals."""
import copy
import unittest

from apps.api import ax_engine, evidence_mapping, source_review, store
from tests.test_evidence_mapping_integrity import bank, debt, document


STAFF = {'id': 'reviewer', 'name': '검토 담당자', 'role': 'staff'}
PAYROLL = '급여명세서\n급여월: 2026-08\n지급총액: 3,550,000원\n공제합계: 350,000원\n실지급액: 3,200,000원'


def make_case(*documents):
    case = {'id': 'synthetic-reviewed-case', 'client_name': '가상 검토인', 'court_id': 'CT01',
            'documents': list(documents), 'requests': [], 'input_revision': 1}
    sources = [{**source, 'title': '가상 원문 검토 자료'} for source in evidence_mapping.sources(case)]
    case['extraction_candidates'] = ax_engine.extract_factor_candidates(sources)
    return case


def edit(case, key, value=None, decision='correct', *, doc_id=None, original_value=None):
    candidate = next(row for row in case['extraction_candidates'] if row['key'] == key
                     and (doc_id is None or row['document_id'] == doc_id)
                     and (original_value is None or row['value'] == original_value))
    doc = next(row for row in case['documents'] if row['id'] == candidate['document_id'])
    source_review.apply_candidate(candidate, decision, value, '원본 이미지와 해당 항목을 직접 대조했습니다.',
                                  STAFF, document=doc, case=case)
    return candidate


class HumanReviewMappingTests(unittest.TestCase):
    def test_monthly_alias_correction_recalculates_forms_and_income_with_separate_provenance(self):
        case = make_case(document('payroll', PAYROLL))
        before = evidence_mapping.build(case)
        original_document = copy.deepcopy(case['documents'][0])
        edit(case, 'monthly_income', 3300000)
        packet = evidence_mapping.build(case)
        self.assertEqual(packet['form_values']['monthly_income'], 3300000)
        self.assertEqual(packet['form_values']['annual_income'], 39600000)
        self.assertEqual(packet['inputs']['income']['monthly_amount'], 3300000)
        self.assertEqual(packet['inputs']['income']['basis'], 'net')
        self.assertEqual(packet['inputs']['income']['evidence_ids'], ['payroll'])
        self.assertEqual(packet['observed_facts'], before['facts'])
        self.assertEqual(case['documents'][0], original_document)
        changed = next(row for row in packet['facts'] if row['key'] == 'income_net')
        self.assertEqual(changed['source_type'], 'human_review')
        self.assertEqual(changed['original_value'], 3200000)
        self.assertIn(changed['original_quote'], PAYROLL)
        self.assertNotIn(changed['quote'], PAYROLL)
        self.assertEqual(changed['quote'], packet['human_review_sources'][0]['text'])
        self.assertEqual(packet['origins']['monthly_income']['type'], 'human_review')
        self.assertFalse(packet['origins']['monthly_income']['ai_verified'])
        self.assertNotEqual(packet['source_signature'], before['source_signature'])
        projected = next(row for row in evidence_mapping.candidate_rows(packet) if row['key'] == 'monthly_income')
        self.assertEqual(projected['source_type'], 'human_review')
        self.assertIn(changed['source_id'], projected['source_ids'])
        self.assertTrue(all(packet['inputs'][key] is None for key in ('months', 'recognized_household_size', 'base_living_cost')))

    def test_stale_source_or_forged_review_does_not_change_calculation(self):
        mutations = [
            lambda c, e: e.update(source_sha256='changed'),
            lambda c, e: e.update(source_version=99),
            lambda c, e: e.update(source_text_sha256='changed'),
            lambda c, e: e.update(source_document_signature='changed'),
            lambda c, e: e.update(actor_id=None),
            lambda c, e: e.update(reason=''),
            lambda c, e: c['documents'][0]['page_texts'][0].update(text=PAYROLL + '\n다른 원문'),
            lambda c, e: c.update(client_name='다른 가상인'),
            lambda c, e: e['reviewed_basis'].update(original_value=1),
            lambda c, e: e['reviewed_basis'].update(quote='다른 인용'),
            lambda c, e: e['reviewed_basis'].update(source_id='doc:foreign:p1:0'),
            lambda c, e: e['reviewed_basis'].update(period_start='2025-01-01'),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                case = make_case(document('payroll', PAYROLL))
                candidate = edit(case, 'monthly_income', 3300000)
                mutate(case, candidate['source_edit'])
                packet = evidence_mapping.build(case)
                self.assertEqual(packet['inputs']['income']['monthly_amount'], 3200000)
                self.assertFalse(packet['human_review_sources'])
                self.assertTrue(packet['errors'])
                self.assertTrue(any(row['code'].startswith('HUMAN_REVIEW_') for row in packet['review_gaps']))

    def test_reconfirmation_preserves_correction_and_a_second_edit_uses_the_original_binding(self):
        case = make_case(document('payroll', PAYROLL))
        candidate = edit(case, 'monthly_income', 3300000)
        corrected = evidence_mapping.build(case)
        source_review.apply_candidate(candidate, 'accept', None, '서류 전체를 다시 확인했습니다.', STAFF,
                                      document=case['documents'][0], case=case)
        reconfirmed = evidence_mapping.build(case)
        self.assertEqual(reconfirmed['source_signature'], corrected['source_signature'])
        self.assertEqual(reconfirmed['inputs']['income']['monthly_amount'], 3300000)
        edit(case, 'monthly_income', 3400000)
        packet = evidence_mapping.build(case)
        self.assertEqual(packet['inputs']['income']['monthly_amount'], 3400000)
        self.assertEqual(packet['human_review_edits'][0]['reviewed_basis']['original_value'], 3200000)

    def test_conflicting_edits_to_alias_and_typed_field_require_review(self):
        case = make_case(document('payroll', PAYROLL))
        edit(case, 'monthly_income', 3300000)
        edit(case, 'income_net', 3400000)
        packet = evidence_mapping.build(case)
        self.assertNotIn('monthly_income', packet['form_values'])
        self.assertIsNone(packet['inputs']['income']['monthly_amount'])
        self.assertIn('HUMAN_REVIEW_CONFLICT', {row['code'] for row in packet['errors']})

    def test_rejected_net_income_does_not_resurrect_or_fall_back_to_gross(self):
        case = make_case(document('payroll', PAYROLL))
        edit(case, 'monthly_income', decision='reject')
        packet = evidence_mapping.build(case)
        self.assertNotIn('monthly_income', packet['form_values'])
        self.assertIsNone(packet['inputs']['income']['monthly_amount'])
        self.assertEqual(next(row for row in packet['observed_facts'] if row['key'] == 'income_net')['value'], 3200000)
        self.assertIn('HUMAN_REVIEW_REJECTED', {row['code'] for row in packet['errors']})

    def test_rejected_account_balance_cannot_publish_a_partial_account_total(self):
        case = make_case(document('one', bank('TEST-ACCOUNT-A', 2400000)),
                         document('two', bank('TEST-ACCOUNT-B', 600000)))
        edit(case, 'cash_balance', decision='reject', doc_id='one')
        packet = evidence_mapping.build(case)
        self.assertNotIn('bank_balance', packet['form_values'])
        self.assertFalse(packet['inputs']['assets'])

    def test_creditor_component_edit_updates_its_loan_and_recalculates_total(self):
        case = make_case(document('one', debt('가상은행', 12000000, 300000, 'TEST-LOAN-A')),
                         document('two', debt('가상은행', 7000000, 200000, 'TEST-LOAN-B')))
        edit(case, 'creditor_principal', 13000000, doc_id='one')
        packet = evidence_mapping.build(case)
        self.assertEqual(sorted(row['principal'] for row in packet['inputs']['creditors']), [7000000, 13000000])
        self.assertEqual(packet['form_values']['total_debt'], 20500000)
        self.assertEqual(packet['origins']['total_debt']['type'], 'human_review')

    def test_aggregate_correction_is_not_distributed_across_loans(self):
        case = make_case(document('one', debt('가상은행', 12000000, 300000, 'TEST-LOAN-A')))
        packet = evidence_mapping.build(case)
        candidate = next(row for row in evidence_mapping.candidate_rows(packet) if row['key'] == 'total_debt')
        case['extraction_candidates'].append(candidate)
        edit(case, 'total_debt', 1)
        result = evidence_mapping.build(case)
        self.assertEqual(result['form_values']['total_debt'], 12300000)
        self.assertEqual(result['inputs']['creditors'][0]['principal'], 12000000)
        self.assertIn('HUMAN_REVIEW_DETAIL_REQUIRED', {row['code'] for row in result['errors']})

    def test_forged_acceptance_and_legal_judgment_key_do_not_authorize_values(self):
        case = make_case(document('payroll', PAYROLL))
        candidate = next(row for row in case['extraction_candidates'] if row['key'] == 'monthly_income')
        candidate.update(status='accepted', value=1)
        self.assertEqual(evidence_mapping.build(case)['form_values']['monthly_income'], 3200000)
        candidate.update(key='months', value=3200000)
        edit(case, 'months', 36)
        packet = evidence_mapping.build(case)
        self.assertIsNone(packet['inputs']['months'])
        self.assertEqual(packet['form_values']['monthly_income'], 3200000)
        self.assertIn('HUMAN_REVIEW_FACT_CHANGED', {row['code'] for row in packet['errors']})

    def test_invalid_type_and_inactive_document_cannot_authorize_correction(self):
        case = make_case(document('payroll', PAYROLL))
        candidate = edit(case, 'income_net', True)
        packet = evidence_mapping.build(case)
        self.assertEqual(packet['form_values']['monthly_income'], 3200000)
        self.assertIn('HUMAN_REVIEW_VALUE_TYPE', {row['code'] for row in packet['errors']})
        case['documents'][0]['status'] = 'quarantined'
        packet = evidence_mapping.build(case)
        self.assertFalse(packet['facts'])
        self.assertFalse(packet['human_review_sources'])


if __name__ == '__main__':
    unittest.main()
