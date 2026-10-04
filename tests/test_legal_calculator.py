import copy
import unittest
from decimal import Decimal

from apps.api.legal_calculator import (
    _allocate, calculate_legal, example_payload, minimum_repayment,
    policy, present_value,
)


class LegalCalculatorTests(unittest.TestCase):
    def setUp(self):
        self.payload = example_payload()
        rows = [self.payload['income']] + self.payload['assets'] + self.payload['creditors']
        self.case = {'id': 'synthetic', 'court_id': 'CT01', 'input_revision': 1,
                     'case_type': 'personal_rehabilitation', 'issues': [],
                     'documents': [{'id': i, 'status': 'verified'} for row in rows for i in row['evidence_ids']]}

    def run_calc(self, payload=None):
        return calculate_legal(self.case, payload or self.payload)

    def codes(self, result):
        return {b['code'] for b in result['blockers']}

    def test_synthetic_known_arithmetic_and_allocation_conservation(self):
        result = self.run_calc()
        self.assertEqual(result['status'], 'ready_for_review')
        s = result['summary']
        # Hand-calculated: 2,800,000 - round(2,564,238*.6) = 1,261,457.
        self.assertEqual(s['base_living_cost'], 1538543)
        self.assertEqual(s['monthly_creditor_capacity'], 1261457)
        self.assertEqual(s['total_creditor_payment'], 45412452)
        self.assertEqual(s['total_interest_payment'], 0)
        self.assertEqual(s['liquidation_value'], 12000000)
        self.assertEqual(sum(c['total_payment'] for c in result['creditor_allocations']), 45412452)
        for row in result['schedule']:
            self.assertEqual(sum(c['total'] for c in row['allocations']), row['creditor_payment'])
            self.assertEqual(row['deposit'], row['creditor_payment'] + row['trustee_fee'])

    def test_official_manual_life_nitz_coefficients(self):
        # Official Seoul manual printed p.163-164: n36=33.3657012839,
        # n33=30.77199540; preapproval 3 payments are not discounted.
        value = present_value([1] * 36, 0)
        self.assertAlmostEqual(float(value), 33.3657012839, places=6)
        pre = present_value([1] * 36, 3)
        self.assertAlmostEqual(float(pre), 33.77199540, places=6)
        self.assertGreater(pre, value)

    def test_statutory_minimum_boundary_and_cap(self):
        self.assertEqual(minimum_repayment(49999999), Decimal('2499999.95'))
        self.assertEqual(minimum_repayment(50000000), Decimal('2500000'))
        self.assertEqual(minimum_repayment(78000000), Decimal('3340000'))
        self.assertEqual(minimum_repayment(1000000000), Decimal('30000000'))

    def test_statutory_debt_limit_includes_interest_and_equality(self):
        c = self.payload['creditors'][0]
        c.update(principal=997000000, interest=3000000)
        self.payload['creditors'] = [c]
        at_limit = self.run_calc()
        self.assertNotIn('DEBT_LIMIT_EXCEEDED', self.codes(at_limit))
        c['interest'] += 1
        self.assertIn('DEBT_LIMIT_EXCEEDED', self.codes(self.run_calc()))

    def test_net_pay_double_deduction_is_rejected(self):
        self.payload['income']['taxes_and_social_insurance'] = 300000
        result = self.run_calc()
        self.assertIn('DOUBLE_DEDUCTION', self.codes(result))
        self.assertEqual(result['schedule'], [])

    def test_gross_and_net_equivalence(self):
        net = self.run_calc()['summary']
        self.payload['income'].update(basis='gross', monthly_amount=3100000, taxes_and_social_insurance=300000)
        self.assertEqual(self.run_calc()['summary'], net)

    def test_unknown_zero_fraction_bool_negative_differ(self):
        for value in (None, 2.1, True, -1, '2800000'):
            with self.subTest(value=value):
                self.payload['income']['monthly_amount'] = value
                self.assertIn('INVALID_MONEY', self.codes(self.run_calc()))
        self.payload['income']['monthly_amount'] = 0
        result = self.run_calc()
        self.assertNotIn('INVALID_MONEY', self.codes(result))
        self.assertIn('NO_POSITIVE_REPAYMENT_CAPACITY', self.codes(result))

    def test_document_unverified_allows_preview_but_not_ready(self):
        self.case['documents'][0]['status'] = 'received'
        result = self.run_calc()
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('EVIDENCE_NOT_VERIFIED', self.codes(result))
        self.assertEqual(result['summary']['total_creditor_payment'], 45412452)

    def test_cross_case_evidence_is_blocked(self):
        self.payload['income']['evidence_ids'] = ['foreign-case-document']
        self.assertIn('EVIDENCE_NOT_IN_CASE', self.codes(self.run_calc()))

    def test_confirmed_fact_conflict_and_open_legal_issue_block(self):
        self.case['facts'] = [{'key': 'monthly_income', 'value': 2600000, 'status': 'confirmed'}]
        self.case['issues'] = [{'id': 'third-party-payment', 'status': 'open'}]
        result = self.run_calc()
        self.assertIn('CONFIRMED_FACT_CONFLICT', self.codes(result))
        self.assertIn('UNRESOLVED_LEGAL_ISSUE', self.codes(result))

    def test_asset_double_deduction_not_clamped_silently(self):
        self.payload['assets'][0].update(exempt_deduction=10000000, secured_deduction=1)
        self.assertIn('ASSET_DEDUCTIONS_EXCEED_VALUE', self.codes(self.run_calc()))

    def test_liquidation_uses_discounted_not_nominal_value(self):
        self.payload['assets'][0]['owned_value'] = 41000000  # +2m assets =43m.
        result = self.run_calc()
        self.assertGreater(result['summary']['total_creditor_payment'], 43000000)
        self.assertLess(result['summary']['present_value'], 43000000)
        self.assertIn('NUMERICAL_REQUIREMENT_NOT_MET', self.codes(result))

    def test_priority_is_paid_before_ordinary_principal(self):
        priority = copy.deepcopy(self.payload['creditors'][0])
        priority.update(id='tax', name='합성 조세', kind='priority', principal=2000000, interest=50000)
        self.payload['creditors'].append(priority)
        result = self.run_calc()
        tax = next(c for c in result['creditor_allocations'] if c['creditor_id'] == 'tax')
        self.assertEqual(tax['total_payment'], 2050000)
        first = result['schedule'][0]
        self.assertEqual(next(c for c in first['allocations'] if c['creditor_id'] == 'tax')['total'], 1261457)
        self.assertEqual(sum(c['total'] for c in first['allocations'] if c['creditor_id'] != 'tax'), 0)

    def test_secured_and_business_not_silently_flattened(self):
        self.payload['creditors'][0]['kind'] = 'secured'
        result = self.run_calc()
        self.assertIn('SECURED_PLAN_REQUIRED', self.codes(result))
        self.assertEqual(result['summary']['secured_debt'], 36400000)
        self.assertEqual(result['schedule'], [])
        self.payload['income']['kind'] = 'business'
        self.assertIn('INCOME_TYPE_NOT_SUPPORTED', self.codes(self.run_calc()))

    def test_no_auto_court_transfer_or_future_policy(self):
        self.case['court_id'] = 'CT02'
        self.assertIn('COURT_POLICY_MISMATCH', self.codes(self.run_calc()))
        self.payload.update(living_cost_mode='case_specific', base_living_cost=1600000,
                            annual_discount_rate='0.05')
        self.assertEqual(self.run_calc()['status'], 'ready_for_review')
        self.payload['as_of'] = '2027-01-01'
        self.assertIn('POLICY_DATE_OUTSIDE_SCOPE', self.codes(self.run_calc()))

    def test_period_and_prepaid_guards(self):
        self.payload['months'] = 61
        self.assertIn('INVALID_INTEGER', self.codes(self.run_calc()))
        self.payload.update(months=60, period_exception='liquidation')
        self.assertIn('EXTENDED_PERIOD_REASON_REQUIRED', self.codes(self.run_calc()))
        self.payload['decisions']['period_exception_reason'] = '청산가치 검토'
        self.assertIn('EXTENSION_NOT_NEEDED_FOR_LIQUIDATION', self.codes(self.run_calc()))
        self.payload.update(months=24, period_exception='full_principal')
        self.assertIn('PRINCIPAL_NOT_FULLY_PAID', self.codes(self.run_calc()))

    def test_repayments_never_exceed_debt_and_principal_first(self):
        self.payload['creditors'] = [dict(self.payload['creditors'][0], principal=1000000, interest=500000)]
        result = self.run_calc()
        a = result['schedule'][0]['allocations'][0]
        self.assertEqual(a['principal'], 1000000)
        self.assertEqual(a['interest'], 261457)
        self.assertEqual(result['summary']['total_creditor_payment'], 1500000)
        self.assertEqual(result['schedule'][-1]['deposit'], 0)

    def test_objecting_creditor_needs_individual_liquidation_floor(self):
        self.payload['objection'] = True
        self.assertIn('OBJECTORS_REQUIRED', self.codes(self.run_calc()))
        self.payload['objecting_creditor_ids'] = ['creditor-a']
        self.assertIn('OBJECTOR_LIQUIDATION_REQUIRED', self.codes(self.run_calc()))
        self.payload['creditors'][0]['bankruptcy_dividend'] = 21000000
        self.assertIn('OBJECTOR_LIQUIDATION_NOT_MET', self.codes(self.run_calc()))
        self.payload['creditors'][0]['bankruptcy_dividend'] = 5000000
        self.assertEqual(self.run_calc()['status'], 'ready_for_review')

    def test_rounding_exact_and_input_order_independent(self):
        self.assertEqual(_allocate(2, [('b', 1), ('a', 1), ('c', 1)]), {'b': 1, 'a': 1, 'c': 0})
        r1 = self.run_calc()
        self.payload['creditors'].reverse()
        r2 = self.run_calc()
        self.assertEqual({x['creditor_id']: x['total_payment'] for x in r1['creditor_allocations']},
                         {x['creditor_id']: x['total_payment'] for x in r2['creditor_allocations']})

    def test_determinism_no_mutation_no_client_approval(self):
        before = copy.deepcopy((self.case, self.payload))
        r1 = self.run_calc()
        r2 = self.run_calc()
        self.assertEqual(r1, r2)
        self.assertEqual((self.case, self.payload), before)
        self.assertIsNone(r1['approval'])
        self.payload['approval'] = {'role': 'lawyer', 'approved': True}
        self.assertIsNone(self.run_calc()['approval'])
        self.assertEqual(r1['policy_hash'], policy()['policy_hash'])
        self.case['input_revision'] += 1
        self.assertNotEqual(r1['input_hash'], self.run_calc()['input_hash'])


if __name__ == '__main__':
    unittest.main()
