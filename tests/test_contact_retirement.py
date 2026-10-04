"""Contact ownership and retirement scheme facts remain separate from legal values."""
import unittest

from apps.api import ax_engine, court_forms, document_facts, domain, evidence_mapping, legal_calculator


def rows(text):
    return document_facts.extract([ax_engine._source('doc:sample:p1:0', 'case_document', '합성 증빙',
        text, document_id='sample', page=1)])


def source_case(*texts):
    case = domain.new_case('검증인', 'CT01', '서울회생법원', '가상 증빙 검증', True)
    case['documents'] = [{'id': 'source-' + str(index), 'status': 'verified', 'version': 1,
        'sha256': 'synthetic-' + str(index), 'text': text,
        'page_texts': [{'page': 1, 'text': text}]} for index, text in enumerate(texts)]
    return case


def retirement_text(scheme='일반 퇴직금(퇴직연금 미가입)', amount='12,345,678', employer='검증 회사'):
    return ('퇴직금 예상액 확인서\n성명: 검증인\n근무처: ' + employer + '\n퇴직급여 제도: ' + scheme +
            '\n예상 퇴직금 총액: ' + amount + '원\n기준일: 2026-09-30')


class ContactRetirementTests(unittest.TestCase):
    def test_person_phone_has_named_subject_and_never_uses_employer_creditor_or_family_number(self):
        text = ('성명: 검증인\n본인 연락처: 010-1234-5678\n회사 전화번호: 010-2345-6789\n'
                '채권자 휴대전화: 010-3456-7890\n배우자 연락처: 010-4567-8901\n'
                '담당자 휴대폰: 010-5678-9012\n직장 전화번호: 02-123-4567')
        facts = rows(text)
        personal = [row for row in facts if row['key'] == 'phone']
        self.assertEqual([row['value'] for row in personal], ['010-1234-5678'])
        self.assertEqual(personal[0]['subject'], 'document_person')
        self.assertIn(personal[0]['quote'], text)
        packet = evidence_mapping.build(source_case(text))
        self.assertEqual(packet['form_values']['phone'], '010-1234-5678')
        preview = court_forms.preview(source_case(text), 'D5100')
        self.assertEqual(next(row['value'] for row in preview['fields'] if row['key'] == 'phone'), '010-1234-5678')

    def test_unscoped_phone_needs_subject_and_does_not_inherit_third_party_contact(self):
        for text in ('휴대전화: 010-1234-5678', '담당자 성명: 검증인\n휴대전화: 010-1234-5678',
                     '대표자 성명: 검증인\n연락처: 010-1234-5678',
                     '성명: 검증인\n배우자 성명: 다른이\n휴대전화: 010-1234-5678',
                     '성명: 검증인\n본인 연락처 여부: 010-1234-5678'):
            with self.subTest(text=text):
                self.assertFalse(any(row['key'] == 'phone' for row in rows(text)))
        self.assertEqual([row['value'] for row in rows('성명: 검증인\n휴대전화: 010-1234-5678')
                          if row['key'] == 'phone'], ['010-1234-5678'])

    def test_person_and_company_contacts_on_same_line_stay_separate(self):
        text = '성명: 검증인\n회사 전화번호: 02-123-4567 / 본인 연락처: 010-1234-5678'
        facts = rows(text)
        self.assertEqual([row['value'] for row in facts if row['key'] == 'phone'], ['010-1234-5678'])
        self.assertEqual([row['value'] for row in facts if row['key'] == 'employer_phone'], ['02-123-4567'])

    def test_ordinary_severance_gross_amount_is_an_asset_with_unknown_deductions(self):
        case = source_case(retirement_text())
        packet = evidence_mapping.build(case)
        self.assertEqual(packet['form_values']['retirement_expected'], 12345678)
        self.assertNotIn('retirement_value', packet['form_values'])
        asset = next(row for row in packet['inputs']['assets'] if row['id'] == 'retirement_expected')
        self.assertEqual(asset['owned_value'], 12345678)
        for key in ('secured_deduction', 'exempt_deduction', 'disposal_cost'):
            self.assertIsNone(asset[key])
        self.assertEqual(packet['form_values']['assets_total'], 12345678)
        form = court_forms.preview(case, 'D5101')
        self.assertIsNone(next(row['value'] for row in form['fields'] if row['key'] == 'retirement_value'))
        annex = evidence_mapping.form_facts(packet, 'D5101')
        self.assertTrue({'retirement_expected', 'retirement_kind'} <= {row['key'] for row in annex})
        self.assertFalse(packet['legal_approval'])

    def test_pension_and_unclassified_severance_are_not_ordinary_assets(self):
        for scheme in ('확정급여형(DB)', '확정기여형(DC)', '개인형(IRP)', '미확인',
                       '일반 퇴직금이 아님', '일반 퇴직금 해당 없음', '일반 퇴직금 미가입',
                       '일반 퇴직금 및 DB 병행', 'DB 및 DC 병행'):
            with self.subTest(scheme=scheme):
                packet = evidence_mapping.build(source_case(retirement_text(scheme=scheme)))
                self.assertNotIn('retirement_expected', packet['form_values'])
                self.assertFalse(any(asset['id'] == 'retirement_expected' for asset in packet['inputs']['assets']))
                self.assertTrue(any(gap['code'] == 'RETIREMENT_TREATMENT_REVIEW' for gap in packet['review_gaps']))
                self.assertTrue(any(row['key'] == 'retirement_expected' for row in packet['facts']))

    def test_pension_balance_is_not_a_bank_deposit_or_monthly_salary(self):
        text = '성명: 검증인\n퇴직연금 제도: 확정기여형(DC)\n퇴직연금 적립금: 8,200,000원\n퇴직연금 현재잔액: 8,200,000원'
        facts = rows(text)
        self.assertEqual([row['value'] for row in facts if row['key'] == 'retirement_pension_balance'], [8200000])
        self.assertFalse(any(row['key'] in {'cash_balance', 'income_net', 'income_gross'} for row in facts))
        packet = evidence_mapping.build(source_case(text))
        self.assertEqual(packet['inputs']['assets'], [])

    def test_same_employer_duplicate_confirmations_do_not_double_retirement_asset(self):
        packet = evidence_mapping.build(source_case(retirement_text(), retirement_text()))
        self.assertEqual(packet['form_values']['retirement_expected'], 12345678)
        asset = next(row for row in packet['inputs']['assets'] if row['id'] == 'retirement_expected')
        self.assertEqual(asset['owned_value'], 12345678)
        self.assertEqual(len(asset['evidence_ids']), 2)

    def test_conflicting_amount_or_missing_employer_is_never_partially_summed(self):
        for documents in ((retirement_text(), retirement_text(amount='13,000,000')),
                          (retirement_text(employer=''),)):
            with self.subTest(documents=len(documents)):
                packet = evidence_mapping.build(source_case(*documents))
                self.assertNotIn('retirement_expected', packet['form_values'])
                self.assertEqual(packet['inputs']['assets'], [])
                self.assertTrue(packet['errors'])

    def test_explicit_source_units_apply_to_gross_retirement_without_legal_fraction(self):
        text = retirement_text(amount='12,000').replace('예상 퇴직금 총액: 12,000원', '단위: 천원\n예상 퇴직금 총액: 12,000')
        packet = evidence_mapping.build(source_case(text))
        self.assertEqual(packet['form_values']['retirement_expected'], 12000000)
        amount = next(row for row in packet['facts'] if row['key'] == 'retirement_expected')
        self.assertEqual(amount['source_unit_quote'], '단위: 천원')
        self.assertEqual(amount['unit_multiplier'], 1000)
        self.assertNotIn('retirement_value', packet['form_values'])

    def test_only_reviewed_calculation_fills_retirement_net_field_after_deductions(self):
        case = source_case(retirement_text(amount='10,000,000'))
        inputs = legal_calculator.example_payload()
        inputs['assets'] = [{'id': 'retirement_expected', 'label': '일반 퇴직금', 'owned_value': 10000000,
            'secured_deduction': 0, 'exempt_deduction': 5000000, 'disposal_cost': 0, 'evidence_ids': ['source-0']}]
        for row in [inputs['income'], *inputs['creditors']]:
            row['evidence_ids'] = ['source-0']
        calculation = legal_calculator.calculate_legal(case, inputs)
        self.assertEqual(calculation['status'], 'ready_for_review')
        calculation.update(status='approved', approval={'actor': '합성 검토 변호사'}, stale=False)
        fields = court_forms.preview(case, 'D5101', calculation=calculation)['fields']
        self.assertEqual(next(row['value'] for row in fields if row['key'] == 'retirement_value'), 5000000)
        self.assertEqual(next(row['value'] for row in fields if row['key'] == 'assets_total'), 5000000)
        self.assertEqual(next(row['value'] for row in fields if row['key'] == 'exempt_value'), 0)
        self.assertEqual(next(row['value'] for row in fields if row['key'] == 'liquidation_value'), 5000000)
        self.assertEqual(calculation['asset_calculations'][0]['owned_value'], 10000000)

    def test_prior_proceedings_are_explicit_attributed_statements_not_inferred_absence(self):
        for statement in ('개인회생·파산·면책·워크아웃 이용 사실 없음',
                          '2020년 개인회생 신청 후 취하하였다는 본인 진술'):
            text = '성명: 검증인\n과거 절차 이용 이력: ' + statement
            case = source_case(text)
            packet = evidence_mapping.build(case)
            field = next(row for row in packet['facts'] if row['key'] == 'prior_proceedings')
            self.assertEqual(field['source_assertion'], 'party_statement')
            self.assertIs(field['legally_confirmed'], False)
            self.assertIn(field['quote'], text)
            self.assertEqual(packet['form_values']['prior_proceedings'], statement)
            preview = court_forms.preview(case, 'D5105')
            self.assertEqual(next(row['value'] for row in preview['fields'] if row['key'] == 'prior_proceedings'), statement)
        for text in ('신청할 예정입니다.', '과거 절차 이용 이력: 확인 필요', '과거 파산 이력: 없음으로 적어도 되나요?'):
            self.assertFalse(any(row['key'] == 'prior_proceedings' for row in rows(text)))

    def test_explicit_unmapped_statement_fields_are_preserved_in_statement_annex(self):
        text = ('성명: 검증인\n최종 학력: 2016년 검증고등학교 졸업\n과거 경력: 2017년부터 2021년까지 검증업체 근무\n'
                '혼인·이혼 이력: 혼인한 사실 없음\n거주 시작일: 2024-02-01')
        packet = evidence_mapping.build(source_case(text))
        annex = evidence_mapping.form_facts(packet, 'D5105')
        self.assertTrue({'education', 'career_history', 'marriage_history', 'housing_start'} <= {row['key'] for row in annex})
        self.assertTrue(all(row['quote'] in text for row in annex))


if __name__ == '__main__':
    unittest.main()
