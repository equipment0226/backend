"""Assumption-based first documents never become approved calculation output."""
import copy
import unittest

import pymupdf

from apps.api import court_forms, evidence_mapping, legal_calculator
from tests.test_evidence_mapping_pipeline import fixture


class ProvisionalCourtFormTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture()
        self.before = copy.deepcopy(self.case)
        self.calculation = legal_calculator.calculate_provisional(self.case)
        self.assertEqual(self.calculation['status'], 'provisional')
        self.assertTrue(legal_calculator.validate_provisional(self.case, self.calculation))

    def test_normal_term_is_explicit_draft_default_not_an_extracted_fact(self):
        packet = evidence_mapping.build(self.case)
        self.assertEqual(packet['inputs']['months'], legal_calculator.policy()['normal_max_months'])
        self.assertEqual(packet['inputs']['months'], 36)
        self.assertNotIn('months', packet['form_values'])
        term = next(row for row in packet['proposed_decisions'] if row['key'] == 'months')
        self.assertEqual(term['status'], 'proposed')
        self.assertIn('statute-611', term['source_ids'])
        self.assertIsNone(packet['inputs']['monthly_trustee_fee'])
        self.assertIsNone(packet['inputs']['recognized_household_size'])

    def test_values_are_provisional_and_assumptions_visible_without_changing_original_case(self):
        preview = court_forms.preview(self.case, 'D5110',
            fields={'monthly_deposit': 1, 'months': 12}, calculation=self.calculation)
        for row in preview['fields']:
            if row['key'] in court_forms.CALC_KEYS and row['value'] is not None:
                self.assertEqual(row['status'], 'provisional', row['key'])
                self.assertNotIn(row['status'], {'approved', 'automatically_verified'})
        values = {row['key']: row['value'] for row in preview['fields']}
        self.assertEqual(values['months'], 36)
        self.assertEqual(values['monthly_disposable_income'], self.calculation['summary']['monthly_disposable_income'])
        self.assertIsNone(values['start_year'])
        self.assertIsNone(values['end_year'])
        self.assertFalse(preview['submission_ready'])
        review = preview['calculation_review']
        self.assertEqual(review['scope'], 'assumption_based_draft')
        self.assertTrue(review['requires_human_review'])
        self.assertEqual(review['assumptions'], self.calculation['assumptions'])
        self.assertEqual(review['pending_conditions'], self.calculation['pending_conditions'])
        self.assertEqual(self.case, self.before)

    def test_provisional_cannot_pass_as_approved_or_automatic_and_incomplete_claims_are_rejected(self):
        variants = [
            {'status': 'approved', 'approval': {'by': 'pretend-lawyer'}},
            {'status': 'ready_for_review', 'auto_preparation': {'passed': True}},
            {'provisional': False}, {'submission_ready': True}, {'approval': {'by': 'pretend-lawyer'}},
            {'auto_preparation': {'passed': True}}, {'stale': True},
            {'input_revision': self.case['input_revision'] + 1},
        ]
        for changes in variants:
            altered = copy.deepcopy(self.calculation)
            altered.update(changes)
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                court_forms.preview(self.case, 'D5110', calculation=altered)
        for status in ['provisional', 'ready_for_review', 'blocked']:
            with self.subTest(status=status), self.assertRaises(ValueError):
                court_forms.preview(self.case, 'D5110', calculation={
                    'status': status, 'input_revision': self.case['input_revision'], 'summary': {'monthly_deposit': 9}})

    def test_tampered_results_assumptions_policy_and_source_cannot_render(self):
        mutations = [
            lambda result: result['summary'].__setitem__('monthly_disposable_income', 1),
            lambda result: result['schedule'][0].__setitem__('deposit', 1),
            lambda result: result['inputs'].__setitem__('months', 12),
            lambda result: result['assumptions'][0].__setitem__('reason', '확인된 사실이라고 잘못 표기'),
            lambda result: result.__setitem__('policy_hash', 'wrong'),
            lambda result: result.__setitem__('result_hash', 'wrong'),
            lambda result: result.__setitem__('evidence_source_signature', 'wrong'),
            lambda result: result.__setitem__('baseline_blockers', []),
        ]
        for index, change in enumerate(mutations):
            altered = copy.deepcopy(self.calculation)
            change(altered)
            with self.subTest(index=index), self.assertRaises(ValueError):
                court_forms.render_pdf('D5110', self.case, calculation=altered)
        changed_case = copy.deepcopy(self.case)
        changed_case['documents'][0]['page_texts'][0]['text'] += '\n원문 추가 기재'
        changed_case['documents'][0]['text'] += '\n원문 추가 기재'
        with self.assertRaises(ValueError):
            court_forms.preview(changed_case, 'D5110', calculation=self.calculation)

    def test_original_pdf_has_numbers_and_every_page_is_identified_as_assumption_draft(self):
        preview = court_forms.preview(self.case, 'D5110', calculation=self.calculation)
        data = court_forms.render_pdf('D5110', self.case, calculation=self.calculation)
        with pymupdf.open(stream=data, filetype='pdf') as pdf:
            original_pages = len(preview['template']['pages'])
            for index, page in enumerate(pdf):
                self.assertIn('가정 계산', page.get_text(), index)
            for row in preview['fields']:
                if row['key'] in {'months', 'monthly_disposable_income', 'allocation_principal_total'} and row['fits_original_field']:
                    expected = court_forms._display(row['value'], row['key'])
                    actual = pdf[row['page']].get_textbox(pymupdf.Rect(row['rect']))
                    self.assertIn(''.join(expected.split()), ''.join(actual.split()), row['key'])
            annex = ''.join(page.get_text() for page in list(pdf)[original_pages:])
            normalized = ''.join(annex.split())
            for assumption in self.calculation['assumptions']:
                self.assertIn(''.join(assumption['label'].split()), normalized)
                self.assertIn(''.join(assumption['reason'].split()), normalized)
            self.assertIn('담당자확인필요', normalized)
            self.assertIn('법률판단및제출승인전', normalized)
            self.assertIn('제36회차', normalized)
            self.assertNotIn('자동검증완료', normalized)
            self.assertNotIn('seoul_median_60', normalized)
            self.assertIn('생계비인원인정근거', normalized)
        self.assertEqual(self.case, self.before)

    def test_full_principal_percentage_fits_original_cell_without_changing_precision(self):
        case = fixture()
        debt = next(row for row in case['documents'] if row['id'] == 'debt')
        debt['text'] = debt['text'].replace('69,000,000', '30,000,000')
        debt['page_texts'][0]['text'] = debt['text']
        calculation = legal_calculator.calculate_provisional(case)
        preview = court_forms.preview(case, 'D5110', calculation=calculation)
        rate = next(row for row in preview['fields'] if row['key'] == 'principal_repayment_percent')
        self.assertEqual(rate['value'], '100.00')
        self.assertTrue(rate['fits_original_field'])
        self.assertEqual(court_forms._display(rate['value'], rate['key']), '100')
        with pymupdf.open(stream=court_forms.render_pdf('D5110', case, calculation=calculation), filetype='pdf') as pdf:
            self.assertEqual(pdf[6].get_textbox(pymupdf.Rect(rate['rect'])).strip(), '100')
        self.assertEqual(rate['value'], '100.00')


if __name__ == '__main__':
    unittest.main()
