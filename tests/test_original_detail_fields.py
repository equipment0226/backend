"""Source-supported original form details; no signatures or issuance asserted."""
import unittest

import pymupdf

from apps.api import court_forms, evidence_mapping
from tests.test_contact_retirement import source_case


class OriginalDetailFieldsTests(unittest.TestCase):
    def test_two_accounts_have_separate_original_cells_and_traceable_balances(self):
        texts = ['금융기관: 검증가은행\n계좌번호: BANK-A-001\n기준일: 2026-09-30\n예금잔액: 1700000원',
                 '금융기관: 검증나은행\n계좌번호: BANK-B-002\n기준일: 2026-09-30\n예금잔액: 800000원']
        case = source_case(*texts)
        packet = evidence_mapping.build(case)
        values = packet['form_values']
        self.assertEqual([values[k] for k in ['bank_name','bank_name_2','bank_account','bank_account_2']],
                         ['검증가은행','검증나은행','BANK-A-001','BANK-B-002'])
        self.assertEqual([values[k] for k in ['bank_balance','bank_balance_1','bank_balance_2']],
                         [2500000,1700000,800000])
        for key, doc in [('bank_account','source-0'),('bank_account_2','source-1')]:
            self.assertEqual(packet['origins'][key]['source_ids'], [doc])
        preview = court_forms.preview(case,'D5101')
        fields = {r['key']:r for r in preview['fields']}
        for key in ['bank_account','bank_account_2','bank_balance_1','bank_balance_2']:
            self.assertTrue(fields[key]['fits_original_field'],key)
        self.assertLess(fields['bank_account']['rect'][2],fields['bank_account_2']['rect'][0])
        pdf = pymupdf.open(stream=court_forms.render_pdf('D5101',case),filetype='pdf')
        first = pdf[0].get_text()
        self.assertIn('BANK-A-001',first)
        self.assertIn('BANK-B-002',first)
        self.assertIn('1,700,000',first)
        self.assertIn('800,000',first)

    def test_explicit_insurance_identity_fills_policy_cells_and_keeps_quote(self):
        text = ('보험 해약환급금 확인서\n보험회사: 검증생명 / 상품명: 본인 보장보험\n'
                '증권번호: POLICY-001 / 계약일: 2020-01-02\n해약환급금: 1200000원')
        case = source_case(text)
        packet = evidence_mapping.build(case)
        self.assertEqual(packet['form_values']['insurance_name'],'검증생명')
        self.assertEqual(packet['form_values']['insurance_policy'],'POLICY-001')
        self.assertTrue(all(r['quote'] in text for r in packet['facts']))
        fields = court_forms.preview(case,'D5101')['fields']
        self.assertEqual(len([r for r in fields if r['key']=='insurance_surrender' and r['value']==1200000]),2)
        pdf = pymupdf.open(stream=court_forms.render_pdf('D5101',case),filetype='pdf')
        self.assertIn('POLICY-001',pdf[0].get_text())
        self.assertIn('검증',pdf[0].get_text())
        self.assertGreaterEqual(pdf[0].get_text().count('1,200,000'),2)

    def test_income_period_date_is_not_hire_date_or_issuance_date(self):
        text = ('급여명세서\n근무처: 검증회사\n입사일: 2019-03-01\n발급일: 2026-10-04\n'
                '급여 확인기간: 2026-09-01 ~ 2026-09-30\n월 실수령액: 3100000원')
        case = source_case(text)
        packet = evidence_mapping.build(case)
        self.assertEqual([packet['form_values']['income_start_'+key] for key in ['year','month','day']], [2026,9,1])
        preview = court_forms.preview(case,'D5115')
        for field in preview['fields']:
            if field['key'].startswith('income_start_'):
                self.assertTrue(field['fits_original_field'],field['key'])
                self.assertEqual(field['source']['type'],'deterministic_evidence')
        no_period = source_case('근무처: 검증회사\n입사일: 2019-03-01\n현재 재직 중\n월 실수령액: 3100000원')
        self.assertTrue(all(r['value'] is None for r in court_forms.preview(no_period,'D5115')['fields']
                            if r['key'].startswith('income_start_')))
        self.assertFalse(any('issu' in r['key'] or 'sign' in r['key'] for r in preview['fields']))
        pay_month = source_case('급여명세서\n급여월: 2025-10 / 지급일: 2025-10-25\n실수령액: 3100000원')
        mapped = evidence_mapping.build(pay_month)
        self.assertEqual([mapped['form_values']['income_start_'+key] for key in ['year','month','day']], [2025,10,1])
        self.assertTrue(any(r.get('period_quote') == '급여월: 2025-10' for r in mapped['facts']))

    def test_overflow_is_marked_on_original_and_full_text_is_preserved_in_annex(self):
        name = '검증용아주긴금융기관은행'
        case = source_case('금융기관: '+name+'\n계좌번호: VERY-LONG-ACCOUNT-0000000001\n예금잔액: 150000원')
        preview = court_forms.preview(case,'D5101')
        bank = next(r for r in preview['fields'] if r['key']=='bank_name')
        self.assertFalse(bank['fits_original_field'])
        self.assertTrue(bank['annex_reference_layout'])
        self.assertIn('bank_name',preview['overflow_fields'])
        pdf = pymupdf.open(stream=court_forms.render_pdf('D5101',case),filetype='pdf')
        self.assertIn('별지',pdf[0].get_text())
        self.assertIn(name,''.join(p.get_text() for p in pdf))
        self.assertFalse(preview['ready_for_review'])


if __name__ == '__main__':
    unittest.main()
