"""Labels bind current balances, repayments, duties and owned-policy scope."""
import unittest

from apps.api import document_facts, evidence_mapping
from tests.test_contact_retirement import source_case, rows


class SourceFieldRoleTests(unittest.TestCase):
    def test_monthly_payment_and_date_do_not_conflict_with_current_debt(self):
        text = ('부채증명서\n채권자: 검증은행\n대출번호: LOAN-VERIFY-01\n'
                '기준일: 2026-09-30\n원금: 24,000,000원\n미지급 이자: 450,000원\n'
                '미지급 이자 2024-11-30 이전 발생한 이자의 유예잔액\n'
                '현재 상환방식 월 원금 350,000원 및 잔액별 약정이자\n'
                '약정 이자 6.5% / 약정 기간 36개월\n무담보')
        facts = rows(text)
        self.assertEqual([r['value'] for r in facts if r['key'] == 'creditor_principal'], [24000000])
        self.assertEqual([r['value'] for r in facts if r['key'] == 'creditor_interest'], [450000])
        payment = next(r for r in facts if r['key'] == 'loan_principal_payment')
        self.assertEqual((payment['value'], payment['frequency'], payment['basis']), (350000, 'monthly', 'payment'))
        packet = evidence_mapping.build(source_case(text))
        self.assertEqual(packet['form_values']['total_debt'], 24450000)
        self.assertFalse(any(r['key'].startswith('creditor_') for r in packet['errors']))
        self.assertIn('loan_principal_payment', {r['key'] for r in evidence_mapping.form_facts(packet, 'D5106')})

    def test_dates_rates_and_counts_are_not_united_amounts(self):
        for value in ['2025-01-31 이후 발생', '2025.01.31 이후 발생', '2025년 1월 31일 이후 발생',
                      '5%', '36개월', '3명', '1건']:
            with self.subTest(value=value):
                self.assertFalse(any(r['key'] == 'creditor_interest' for r in rows('미지급 이자 ' + value)))
        # Monetary tables need not repeat the unit on each row.
        fact = next(r for r in rows('단위: 천원\n이자 2,345') if r['key'] == 'creditor_interest')
        self.assertEqual(fact['value'], 2345000)
        self.assertEqual(fact['source_unit_quote'], '단위: 천원')

    def test_explicit_payment_labels_do_not_become_current_balance(self):
        facts = rows('원금 상환액: 200,000원\n이자 납부액: 35,000원\n지급 이자: 34,000원')
        self.assertEqual([r['value'] for r in facts if r['key'] == 'loan_principal_payment'], [200000])
        self.assertEqual([r['value'] for r in facts if r['key'] == 'loan_interest_payment'], [35000, 34000])
        self.assertFalse(any(r['key'].startswith('creditor_') for r in facts))

    def test_indented_address_and_explicit_postcode_are_reconciled_with_quotes(self):
        address = '검증시 확인구 근거로 18 검증아파트 202호'
        texts = ['주민등록표 등본\n  주소: (우편번호 12345) ' + address,
                 '채무자 진술\n 실거주 주소: ' + address]
        case = source_case(*texts)
        packet = evidence_mapping.build(case)
        self.assertEqual(packet['form_values']['address'], address)
        self.assertEqual(packet['form_values']['registered_address'], address)
        self.assertTrue(all(r['quote'] in texts[int(r['document_id'].split('-')[-1])]
                            for r in packet['facts']))
        self.assertEqual([r['value'] for r in packet['facts'] if r['key'] == 'postal_code'], ['12345'])
        self.assertFalse(any(r['key'] == 'address' for r in rows('임대인 주소: 타인 주소\n직장 주소: 직장 주소')))
        self.assertEqual([r['value'] for r in rows('주소: 12345번지 검증동') if r['key'] == 'address'],
                         ['12345번지 검증동'])

    def test_distinct_addresses_are_not_merged(self):
        packet = evidence_mapping.build(source_case('주소: 검증시 첫째로 1', '주소: 검증시 둘째로 2'))
        self.assertNotIn('address', packet['form_values'])
        self.assertTrue(any(r['key'] == 'address' for r in packet['errors']))

    def test_duties_are_separate_from_rank_and_prefix_variant_is_supported(self):
        text = '재직증명서\n근무처: 검증회사\n담당업무: 출고 일정·재고 관리 / 직위: 주임 / 현재 재직 중'
        slip = '급여명세서\n근무처: 검증회사\n직위: 물류관리 주임'
        packet = evidence_mapping.build(source_case(text, slip))
        self.assertEqual(packet['form_values']['job_title'], '물류관리 주임')
        self.assertEqual([r['value'] for r in packet['facts'] if r['key'] == 'job_duties'], ['출고 일정·재고 관리'])
        self.assertEqual({r['key'] for r in evidence_mapping.form_facts(packet, 'D5115')} & {'job_title', 'job_duties'},
                         {'job_title', 'job_duties'})
        self.assertFalse(any(r['key'] == 'job_title' for r in packet['errors']))
        fallback = evidence_mapping.build(source_case('담당업무: 배송 관리'))
        self.assertEqual(fallback['form_values']['job_title'], '배송 관리')
        self.assertEqual(fallback['origins']['job_title']['operation'], 'explicit_duties_as_job_description')

    def test_different_rank_or_employer_still_requires_review(self):
        for left, right in [('근무처: 검증회사\n직위: 주임', '근무처: 검증회사\n직위: 과장'),
                            ('근무처: 첫째회사\n직위: 주임', '근무처: 둘째회사\n직위: 물류관리 주임')]:
            packet = evidence_mapping.build(source_case(left, right))
            self.assertNotIn('job_title', packet['form_values'])
            self.assertTrue(any(r['key'] == 'job_title' for r in packet['errors']))

    def test_explicit_housing_type_and_held_policy_count_preserve_scope(self):
        facts = rows('임대차 종류       보증금 있는 월세\n보유 보험계약: 2건 / 다른 보험계약 없음')
        self.assertEqual([r['value'] for r in facts if r['key'] == 'housing_type'], ['월세'])
        self.assertEqual([r['value'] for r in facts if r['key'] == 'insurance_contracts'], [True])
        self.assertEqual([r['value'] for r in rows('보유 보험계약: 0건') if r['key'] == 'insurance_contracts'], [False])
        for text in ['과거 보유 보험계약: 1건', '가족 보유 보험계약: 1건', '보유 보험계약: 1건인지 확인 필요',
                     '임대차 종류: 월세 아님', '임대차 종류: 월세 여부 확인 필요']:
            self.assertFalse(any(r['key'] in {'housing_type', 'insurance_contracts'} for r in rows(text)), text)


if __name__ == '__main__':
    unittest.main()
