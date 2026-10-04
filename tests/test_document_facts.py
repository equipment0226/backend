"""Original evidence and unseen variants; no manifest values enter extraction."""
import hashlib
import json
from pathlib import Path
import unittest

import pymupdf

from apps.api import ax_engine as ax, document_facts as facts

ROOT = Path(__file__).resolve().parents[1]


def sources(text, title='검증 자료', document_id='test'):
    return [ax._source('doc:' + document_id + ':p1:0', 'case_document', title, text, document_id=document_id, page=1)]


def values(rows, key):
    return [row['value'] for row in rows if row['key'] == key]


class TypedDocumentTests(unittest.TestCase):
    def test_job_title_keeps_department_text_without_inventing_unknown_position(self):
        rows = facts.extract(sources('재직증명서\n담당업무: 물류 관리 / 급여 지급일: 매월 25일'))
        job = next(row for row in rows if row['key'] == 'job_duties')
        self.assertEqual(job['value'], '물류 관리')
        self.assertIn('담당업무: 물류 관리', job['quote'])
        self.assertFalse(any(row['key'] == 'job_title' for row in facts.extract(sources('직책: 미확인'))))

    def test_negation_scope_does_not_turn_other_income_absence_into_unemployment(self):
        for text in ['급여외 소득 없음', '사업소득 없음', '현재 무직 아님', '현재 실직 상태가 아닙니다.',
                     '현재 소득이 없는 것은 아닙니다.', '기타 소득은 없습니다.', '현재 무직 여부 확인 필요']:
            with self.subTest(text=text):
                self.assertNotIn('무직 진술', values(facts.extract(sources(text)), 'employment_type'))
                self.assertNotIn('무직 진술', values(ax.extract_factor_candidates(sources(text)), 'employment_type'))
        for text in ['현재 무직입니다.', '현재 실직 중입니다.', '현재 소득이 없습니다.']:
            self.assertIn('무직 진술', values(facts.extract(sources(text)), 'employment_type'))
        rows = facts.extract(sources('현재 무직이 아니고 급여소득자입니다. 사업소득은 없습니다.'))
        self.assertEqual(values(rows, 'employment_type'), ['급여소득자'])

    def test_annual_net_and_monthly_average_are_distinct_and_no_annual_monthly_leak(self):
        text = '근로소득원천징수영수증\n소득 확인기간: 2024-01-01 ~ 2024-12-31\n직전 12개월 총급여: 48,600,000원\n직전 12개월 공제 합계: 6,000,000원\n직전 12개월 실수령 합계: 42,600,000원 / 월평균 3,550,000원'
        rows = facts.extract(sources(text))
        net = [row for row in rows if row['key'] == 'income_net']
        self.assertEqual([(row['value'], row['frequency']) for row in net], [(42600000, 'annual'), (3550000, 'monthly')])
        self.assertEqual(net[0]['period_months'], 12)
        self.assertEqual(values(ax.extract_factor_candidates(sources(text)), 'monthly_income'), [3550000])
        for row in rows:
            self.assertIn(row['quote'], text)
            self.assertEqual(row['period_end'], '2024-12-31')

    def test_salary_amounts_bases_and_thousand_won_units_without_fixture_constants(self):
        text = '급여명세서\n단위: 천원\n급여월: 2026-08\n지급총액: 4,250\n공제합계: 475\n실지급액: 3,775'
        rows = facts.extract(sources(text))
        self.assertEqual(values(rows, 'income_gross'), [4250000])
        self.assertEqual(values(rows, 'income_deductions'), [475000])
        self.assertEqual(values(rows, 'income_net'), [3775000])
        self.assertEqual(next(r for r in rows if r['key'] == 'income_net')['unit_multiplier'], 1000)
        self.assertEqual(next(r for r in rows if r['key'] == 'income_net')['source_unit_quote'], '단위: 천원')
        self.assertEqual(values(ax.extract_factor_candidates(sources(text)), 'monthly_income'), [3775000])
        self.assertEqual(values(rows, 'employment_type'), ['급여소득자'])
        self.assertTrue(all(row['quote'] in text and row['line_start'] <= row['line_end'] for row in rows))

    def test_negative_unknown_and_empty_labels_do_not_become_zero(self):
        rows = facts.extract(sources('급여명세서\n실지급액: 미확인\n공제액: -300,000원\n임차보증금: 알 수 없음\n해약환급금: 0원'))
        self.assertEqual(values(rows, 'income_net'), [])
        self.assertEqual(values(rows, 'income_deductions'), [])
        self.assertEqual(values(rows, 'housing_deposit'), [])
        self.assertEqual(values(rows, 'insurance_surrender'), [0])

    def test_creditor_components_stay_separate_from_case_total_and_each_loan(self):
        text = '부채증명서\n채권자: 검증은행\n대출번호: LOAN-A\n원금: 12,300,000원 / 이자: 230,000원\n채무 합계: 12,530,000원\n대출번호: LOAN-B\n원금: 4,000,000원 / 이자: 45,000원\n채무 합계: 4,045,000원'
        rows = facts.extract(sources(text))
        principal = [row for row in rows if row['key'] == 'creditor_principal']
        self.assertEqual([(r['value'], r['loan_key']) for r in principal], [(12300000, 'LOAN-A'), (4000000, 'LOAN-B')])
        self.assertTrue(all(row['institution'] == '검증은행' for row in principal))
        self.assertEqual(values(ax.extract_factor_candidates(sources(text)), 'total_debt'), [])

    def test_monthly_table_columns_cell_lines_and_overlap_preserve_all_periods(self):
        text = '예금계좌 거래내역\n금융기관: 검증은행 / 계좌번호: TEST-ACCOUNT-03\n월\n급여 입금\n생활비 출금\n상환 출금\n2026-06\n3,100,000\n1,450,000\n1,650,000\n2026-07\n3,200,000\n1,500,000\n1,700,000'
        rows = facts.extract(sources(text))
        deposits = [r for r in rows if r['key'] == 'bank_payroll_deposit']
        self.assertEqual([(r['value'], r['period_start']) for r in deposits], [(3100000, '2026-06-01'), (3200000, '2026-07-01')])
        self.assertTrue(all(r['quote'] in text and r['account_key'] == 'TEST-ACCOUNT-03' for r in deposits))
        self.assertEqual(values(rows, 'income_net'), [])

    def test_explicit_absence_and_family_members_are_not_legal_household_decisions(self):
        text = '가족관계증명서\n미혼 / 자녀 없음 / 부양가족 없음\n세대원: 검증인 1명\n소유 부동산 검색 결과: 없음\n소유 자동차: 없음\n보유 보험계약: 없음 / 해약환급금: 0원\n소유자 본인 여부: 아니오\n임차보증금: 0원 / 월 차임: 0원'
        rows = facts.extract(sources(text))
        for key in ('real_estate_ownership', 'vehicle_ownership', 'insurance_contracts', 'housing_ownership'):
            self.assertEqual(values(rows, key), [False])
        for key in ('dependent_count', 'children_count', 'housing_deposit', 'housing_cost', 'insurance_surrender'):
            self.assertEqual(values(rows, key), [0])
        self.assertEqual(values(rows, 'household_size'), [1])
        self.assertNotIn('eligible_household_size', {r['key'] for r in rows})

    def test_non_bank_query_scope_is_not_an_account_scope(self):
        rows = facts.extract(sources('지적전산자료조회\n조회범위: 전국\n보험계약 조회범위: 전체 보험회사 계약조회'))
        self.assertEqual(values(rows, 'account_scope'), [])
        self.assertEqual(values(rows, 'query_scope'), ['전국', '전체 보험회사 계약조회'])

    def test_balance_reference_date_is_distinct_from_later_issue_date(self):
        text = '예금잔액확인\n금융기관: 검증은행 / 계좌번호: TEST-ACCOUNT-A\n잔액 기준일: 2026-08-31\n발급일: 2026-10-04\n예금잔액: 1,200,000원'
        row = next(r for r in facts.extract(sources(text)) if r['key'] == 'cash_balance')
        self.assertEqual(row['as_of'], '2026-08-31')
        self.assertEqual(row['as_of_kind'], 'balance_date')
        self.assertEqual(row['issued_at'], '2026-10-04')
        self.assertIn(row['as_of_quote'], text)
        self.assertIn(row['issued_at_quote'], text)
        issued_only = facts.extract(sources('발급일: 2026-10-04\n예금잔액: 1,200,000원'))
        self.assertNotIn('as_of', next(r for r in issued_only if r['key'] == 'cash_balance'))


class OriginalDocumentCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = {}
        path = ROOT / '.work/ocr-audit/fresh-extraction-baseline.json'
        if path.exists():
            cls.cache = {row['sha256']: row['pages'] for row in json.loads(path.read_text(encoding='utf-8'))['documents']}

    def read_fixture(self, folder):
        documents = []
        for path in sorted((ROOT / 'examples' / folder / 'documents').glob('*.pdf')):
            raw = path.read_bytes()
            with pymupdf.open(stream=raw, filetype='pdf') as pdf:
                pages = [{'page': i+1, 'text': page.get_text(sort=True)} for i, page in enumerate(pdf)]
            if any(not page['text'].strip() for page in pages):
                digest = hashlib.sha256(raw).hexdigest()
                if digest in self.cache:
                    pages = self.cache[digest]
                else:
                    from apps.api.ocr_engine import extract_pages
                    pages = extract_pages(raw, '.pdf')
            documents.append({'id': path.stem, 'filename': path.name, 'page_texts': pages, 'status': 'verified'})
        self.assertEqual(len(documents), 15)
        source_rows = ax.case_sources({'documents': documents})
        typed = facts.extract(source_rows)
        lookup = {row['id']: row for row in source_rows}
        self.assertTrue(all(row['quote'] in lookup[row['source_id']]['text'] and row['line_start'] >= 1 for row in typed))
        return documents, source_rows, typed

    def check_coverage(self, folder, net, gross, deduction, principal, interest, balance):
        documents, sources_, rows = self.read_fixture(folder)
        by_number = {f'{i:02d}': [row for row in rows if row['document_id'].startswith(f'{i:02d}_')] for i in range(1, 16)}
        required = {'01': {'client_name', 'address', 'household_size'}, '02': {'client_name', 'marital_status', 'children_count', 'dependent_count'},
            '03': {'cash_balance', 'institution', 'account_key'}, '04': {'creditor_principal', 'creditor_interest', 'creditor_total', 'creditor_name'},
            '05': {'income_gross', 'income_net', 'income_deductions', 'income_period'}, '06': {'employment_type', 'employer', 'employment_start'},
            '07': {'health_qualification', 'employer'}, '08': {'pension_membership', 'pension_assessed_income'}, '09': {'real_estate_ownership'},
            '10': {'tax_arrears'}, '11': {'insurance_contracts', 'insurance_surrender'}, '12': {'housing_type', 'housing_deposit', 'housing_cost', 'housing_ownership'},
            '13': {'income_net', 'living_expenses', 'debt_history'}, '14': {'bank_payroll_deposit', 'bank_living_expense', 'bank_debt_repayment', 'cash_balance'},
            '15': {'income_gross', 'income_net', 'income_deductions', 'income_period', 'employment_type'}}
        for number, keys in required.items():
            with self.subTest(document=number, folder=folder):
                self.assertLessEqual(keys, {r['key'] for r in by_number[number]})
        self.assertEqual(values(by_number['15'], 'income_net'), [net])
        self.assertEqual(values(by_number['15'], 'income_gross'), [gross])
        self.assertEqual(values(by_number['15'], 'income_deductions'), [deduction])
        self.assertEqual(values(by_number['04'], 'creditor_principal'), [principal])
        self.assertEqual(values(by_number['04'], 'creditor_interest'), [interest])
        self.assertEqual(values(by_number['03'], 'cash_balance'), [balance])
        annual_net = [r for r in by_number['05'] if r['key'] == 'income_net' and r['frequency'] == 'annual']
        self.assertEqual([r['value'] for r in annual_net], [net*12])
        deposits = [r for r in by_number['14'] if r['key'] == 'bank_payroll_deposit']
        self.assertEqual(len(deposits), 12)
        self.assertEqual(len({r['period_start'] for r in deposits}), 12)
        self.assertEqual({r['value'] for r in deposits}, {net})
        self.assertNotIn('무직 진술', values(rows, 'employment_type'))
        self.assertNotIn(net*12, values(ax.extract_factor_candidates(sources_), 'monthly_income'))
        checks = ax.check_documents({'documents': documents, 'client_name': '', 'requests': []}, [])
        self.assertTrue(all(row['classification'] != '문서 종류 미확인' for row in checks))

    def test_original_fresh_start_all_fifteen_documents(self):
        self.check_coverage('fresh_start', 2800000, 3100000, 300000, 58000000, 2000000, 1200000)

    def test_different_case_all_fifteen_documents_without_expected_manifest(self):
        self.check_coverage('ocr_retest', 3200000, 3550000, 350000, 69000000, 3000000, 2400000)
