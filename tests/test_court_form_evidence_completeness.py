"""Verified original facts reach actual court blanks without legal assumptions."""
import copy
import unittest

import pymupdf

from apps.api import court_forms, evidence_mapping


def document(identifier, text):
    return {'id': identifier, 'status': 'verified', 'version': 1, 'text': text,
            'page_texts': [{'page': 1, 'text': text}]}


def case_with(*documents):
    return {'id': 'synthetic-form-mapping', 'synthetic': True, 'client_name': '가상검증',
            'court_id': 'CT01', 'court_name': '서울회생법원', 'input_revision': 1,
            'documents': list(documents), 'facts': [], 'extraction_candidates': []}


def history():
    return document('history', '본인 진술 확인서\n성명: 가상검증\n'
                    '최종 학력: 2010-02-15 검증고등학교 졸업\n'
                    '혼인·이혼 이력: 없음\n현 거주 시작일: 2023-07-09')


def employment():
    return document('employment', '재직증명서\n성명: 가상검증\n직장명: 검증회사\n'
                    '입사일: 2020-03-16\n현재 재직 중입니다.\n직위: 사원')


def debt(name='검증은행', loan='TEST-LOAN-A', day='2026-09-30', contacts=True):
    return (f'부채증명서\n채권자: {name}\n대출번호: {loan}\n잔액 기준일: {day}\n'
            '원금: 23,000,000원 / 이자: 700,000원\n담보 없음\n'
            + ('채권자 전화: 02-1234-5678\n채권자 팩스: 02-1234-9876\n' if contacts else ''))


class CourtEvidenceCompletenessTests(unittest.TestCase):
    def test_history_date_parts_have_exact_fact_and_quote_provenance(self):
        case = case_with(history(), employment())
        before = copy.deepcopy(case)
        packet = evidence_mapping.build(case)
        expected = {'education_year': 2010, 'education_month': 2, 'education_day': 15,
                    'education_school': '검증고등학교', 'education_completion': '졸업',
                    'employment_start_year': 2020, 'employment_start_month': 3, 'employment_start_day': 16,
                    'housing_start_year': 2023, 'housing_start_month': 7, 'housing_start_day': 9,
                    'marriage_history': '없음'}
        facts = {row['id']: row for row in packet['facts']}
        originals = {row['id']: row['text'] for row in case['documents']}
        for key, value in expected.items():
            self.assertEqual(packet['form_values'][key], value, key)
            origin = packet['origins'][key]
            self.assertEqual(origin['status'], 'source_checked')
            self.assertTrue(origin['semantic_review_required'])
            self.assertTrue(origin['fact_ids'])
            for identifier in origin['fact_ids']:
                row = facts[identifier]
                self.assertIn(row['quote'], originals[row['document_id']])
        self.assertEqual(case, before)
        self.assertIsNone(packet['inputs']['monthly_trustee_fee'])
        self.assertIsNone(packet['inputs']['recognized_household_size'])

    def test_unknown_partial_invalid_dates_and_historical_job_do_not_invent_days(self):
        for value in ['2023-07', '2023-02-30', '미확인']:
            packet = evidence_mapping.build(case_with(document('history',
                f'현 거주 시작일: {value}\n최종 학력: {value} 검증고등학교 졸업')))
            for prefix in ('housing_start', 'education'):
                self.assertNotIn(prefix + '_day', packet['form_values'])
        packet = evidence_mapping.build(case_with(document('old',
            '퇴직증명서\n직장명: 예전회사\n입사일: 2019-03-01\n퇴사일: 2022-03-01')))
        self.assertNotIn('employment_start_year', packet['form_values'])
        self.assertNotIn('marriage_history', packet['form_values'])

    def test_conflicting_history_cannot_choose_one_date_or_school(self):
        packet = evidence_mapping.build(case_with(history(), document('conflict',
            '최종 학력: 2011-02-15 다른고등학교 졸업\n현 거주 시작일: 2024-07-09')))
        self.assertNotIn('education_school', packet['form_values'])
        self.assertNotIn('housing_start_year', packet['form_values'])

    def test_creditor_contacts_dates_and_formula_are_source_bound(self):
        case = case_with(document('debt-a', debt() +
            '채권 내용: TEST-LOAN-A 생활비 대출\n'
            '원금 산정근거: 최초 대출원금에서 상환원금 차감\n'
            '이자 산정근거: 약정이자 및 지연이자 합계'),
            document('debt-b', debt('다른은행', 'TEST-LOAN-B', contacts=False)))
        packet = evidence_mapping.build(case)
        values = packet['form_values']
        self.assertEqual(values['creditors.0.phone'], '02-1234-5678')
        self.assertEqual(values['creditors.0.fax'], '02-1234-9876')
        self.assertNotIn('creditors.1.phone', values)
        self.assertEqual(values['creditors.0.content'], 'TEST-LOAN-A 생활비 대출')
        self.assertEqual(values['creditors.0.basis'], '최초 대출원금에서 상환원금 차감')
        self.assertEqual(values['creditors.0.interest_basis'], '약정이자 및 지연이자 합계')
        self.assertEqual(values['creditors_as_of_year'], 2026)
        self.assertEqual(values['creditors_as_of_month'], 9)
        self.assertEqual(values['creditors_as_of_day'], 30)
        self.assertIn('2026-09-30', values['creditors.1.basis'])
        self.assertIn('23,000,000원', values['creditors.1.basis'])
        self.assertIn('700,000원', values['creditors.1.interest_basis'])
        self.assertEqual(packet['inputs']['creditors'][1]['basis'], values['creditors.1.basis'])
        self.assertEqual(packet['inputs']['creditors'][1]['interest_basis'], values['creditors.1.interest_basis'])

    def test_personal_or_unlabelled_phone_and_issuance_date_are_not_creditor_facts(self):
        text = debt(contacts=False).replace('잔액 기준일: 2026-09-30', '발급일: 2026-09-30')
        text += '채무자: 가상검증\n채무자 전화: 010-1111-2222\n전화: 02-3333-4444\n팩스: 02-5555-6666'
        values = evidence_mapping.build(case_with(document('debt', text)))['form_values']
        for key in ['creditors.0.phone', 'creditors.0.fax', 'creditors.0.as_of',
                    'creditors.0.basis', 'creditors.0.interest_basis', 'creditors_as_of_year']:
            self.assertNotIn(key, values)

    def test_inline_balance_date_and_original_formula_table_are_preserved(self):
        text = debt().replace('대출번호: TEST-LOAN-A\n잔액 기준일:', '대출번호: TEST-LOAN-A / 채무잔액 기준일:')
        text += '현재액 산정 항목      내역\n원금    대출원금에서 납입원금을 차감한 장부잔액\n미지급 이자    기준일 이전 발생한 미납이자 잔액'
        values = evidence_mapping.build(case_with(document('debt', text)))['form_values']
        self.assertEqual(values['creditors_as_of_day'], 30)
        self.assertEqual(values['creditors.0.basis'], '대출원금에서 납입원금을 차감한 장부잔액')
        self.assertEqual(values['creditors.0.interest_basis'], '기준일 이전 발생한 미납이자 잔액')
        without_header = text.replace('현재액 산정 항목      내역', '약정 상환표')
        other = evidence_mapping.build(case_with(document('debt', without_header)))['form_values']
        self.assertNotEqual(other['creditors.0.basis'], values['creditors.0.basis'])

    def test_different_and_incomplete_balance_dates_leave_shared_header_blank(self):
        for day in ['2026-09-29', '2026-09', '2026-02-30']:
            values = evidence_mapping.build(case_with(document('a', debt()),
                document('b', debt('다른은행', 'TEST-LOAN-B', day))))['form_values']
            self.assertNotIn('creditors_as_of_year', values)

    def test_conflicting_explicit_formula_is_not_replaced_with_generic_fallback(self):
        packet = evidence_mapping.build(case_with(
            document('first', debt() + '원금 산정근거: 기납부 원금 차감'),
            document('second', debt() + '원금 산정근거: 매출채권 정산')))
        self.assertNotIn('creditors.0.basis', packet['form_values'])
        self.assertTrue(any(row['key'] == 'creditor_principal_basis' for row in packet['errors']))

    def test_registered_original_blanks_really_contain_values_not_only_annex(self):
        case = case_with(history(), employment(), document('debt', debt()))
        targets = {'D5105': {'education_year', 'education_month', 'education_day', 'education_school',
                            'education_completion', 'employment_start_year', 'employment_start_month',
                            'employment_start_day', 'housing_start_year', 'housing_start_month',
                            'housing_start_day', 'marriage_history'},
                   'D5100': {'client_name'}, 'D5115': {'employer'},
                   'D5106': {'creditors.0.phone', 'creditors.0.fax', 'creditors_as_of_year',
                            'creditors_as_of_month', 'creditors_as_of_day', 'creditors.0.basis',
                            'creditors.0.interest_basis', 'creditors.0.number', 'creditors.0.principal'}}
        for template_id, keys in targets.items():
            preview = court_forms.preview(case, template_id)
            with pymupdf.open(stream=court_forms.render_pdf(template_id, case), filetype='pdf') as rendered:
                for field in preview['fields']:
                    if field['key'] not in keys:
                        continue
                    self.assertIsNotNone(field['value'], (template_id, field['key']))
                    self.assertTrue(field['fits_original_field'], (template_id, field['key']))
                    expected = court_forms._display(field['value'], field['key'])
                    page = preview['template']['pages'].index(field['page'])
                    actual = rendered[page].get_textbox(pymupdf.Rect(field['rect']))
                    self.assertEqual(''.join(actual.split()), ''.join(expected.split()),
                                     (template_id, field['key'], actual, expected))
                original_pages = len(preview['template']['pages'])
                annex = '\n'.join(page.get_text() for page in list(rendered)[original_pages:])
                for fact in preview['source_facts']:
                    self.assertIn(''.join(fact['quote'].split()), ''.join(annex.split()))

    def test_coverage_is_not_all_original_blanks_or_consent_and_signatures(self):
        case = case_with(history(), employment())
        for template_id in ['D5100', 'D5105', 'D5115', 'D5106']:
            preview = court_forms.preview(case, template_id)
            coverage = preview['mapping_coverage']
            self.assertFalse(coverage['original_layout_fully_mapped'])
            self.assertEqual(coverage['scope'], 'registered_fields_only')
            self.assertEqual(coverage['registered_slots'], len(preview['fields']))
            self.assertTrue(coverage['unmapped_original_sections'])
            self.assertFalse(preview['submission_ready'])
        values = {row['key']: row['value'] for row in court_forms.preview(case, 'D5110')['fields']}
        self.assertIsNone(values['start_year'])
        self.assertIsNone(values['monthly_trustee_fee'])
        with pymupdf.open(stream=court_forms.render_pdf('D5100', case), filetype='pdf') as rendered, \
                pymupdf.open(court_forms.original_path('D5100')) as original:
            # The SMS consent box stays byte-content equivalent as rendered
            # text, even though the separate ordinary applicant name is filled.
            consent = pymupdf.Rect(58, 477, 560, 622)
            self.assertEqual(rendered[1].get_textbox(consent), original[1].get_textbox(consent))
        with pymupdf.open(stream=court_forms.render_pdf('D5115', case), filetype='pdf') as rendered, \
                pymupdf.open(court_forms.original_path('D5115')) as original:
            issuer_signature = pymupdf.Rect(265, 636, 530, 663)
            self.assertEqual(rendered[0].get_textbox(issuer_signature), original[0].get_textbox(issuer_signature))


if __name__ == '__main__':
    unittest.main()
