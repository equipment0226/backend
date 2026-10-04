"""Income routing regressions; fictional text only, no model or database calls."""
from copy import deepcopy
from pathlib import Path
import unittest

from apps.api import court_rules, rulebook


class IncomeRequestRoutingTests(unittest.TestCase):
    def case(self, notes='', employment=None):
        case = {'id': 'routing-test', 'court_id': 'CT01', 'created_at': '2026-10-04',
                'consultation': {'notes': notes, 'answers': {}}, 'documents': [],
                'requests': [], 'messages': []}
        if employment:
            case['employment_type'] = employment
        return case

    def rules(self, case):
        return {r['id'] for r in rulebook.evaluate_case(case)['matched_rules']}

    def catalogs(self, case):
        return {r['catalog_id'] for r in court_rules.plan(case)['requests']}

    def test_salary_fixture_listed_absences_do_not_request_business_or_absent_assets(self):
        text = (Path(__file__).resolve().parents[1] / 'examples/fresh_start/application.txt').read_text(encoding='utf-8')
        case = self.case(text)
        self.assertIn('WF02', self.rules(case))
        self.assertTrue({'WF03', 'WF08', 'WF12', 'WF15'}.isdisjoint(self.rules(case)))
        self.assertTrue({'D15', 'D16', 'D17', 'D18', 'D21', 'D22', 'D49'}.isdisjoint(self.catalogs(case)))

    def test_negation_is_attached_to_list_not_a_character_window(self):
        case = self.case('급여소득자입니다. 사업소득, 세금 체납, 과거 회생·파산 이력은 없습니다.')
        self.assertTrue({'WF03', 'WF08', 'WF13'}.isdisjoint(self.rules(case)))
        self.assertEqual(rulebook.income_profile(case)['kind'], 'salary')

    def test_positive_clause_is_not_negated_by_next_clause(self):
        case = self.case('급여도 받고 사업소득이 있으며, 세금 체납과 자동차는 없습니다.')
        self.assertEqual(rulebook.income_profile(case)['kind'], 'mixed')
        self.assertIn('D15', self.catalogs(case))
        self.assertNotIn('WF08', self.rules(case))
        self.assertNotIn('WF15', self.rules(case))

    def test_positive_later_sentence_overrides_earlier_absence(self):
        case = self.case('과거에는 사업소득이 없었습니다. 현재 사업소득이 있고 급여도 받습니다.')
        self.assertEqual(rulebook.income_profile(case)['kind'], 'mixed')
        self.assertIn('D21', self.catalogs(case))

    def test_negative_contrast_does_not_become_positive(self):
        case = self.case('사업소득은 없지만 급여를 받고 있습니다.')
        self.assertEqual(rulebook.income_profile(case)['kind'], 'salary')
        self.assertNotIn('D15', self.catalogs(case))

    def test_business_only_and_declared_mixed_keep_business_branch(self):
        for employment in ('business', '사업소득자', '영업소득자', 'freelancer', 'mixed', 'salary_and_business'):
            with self.subTest(employment=employment):
                case = self.case(employment=employment)
                self.assertTrue({'D15', 'D21', 'D22', 'D49'} <= self.catalogs(case))
                if employment in ('mixed', 'salary_and_business'):
                    self.assertTrue({'D07', 'D08'} <= self.catalogs(case))

    def test_unknown_business_becomes_consultation_question_not_document_request(self):
        for text in ('사업소득 여부는 미확인입니다.', '사업자입니까?', '사업소득이 있는지 확인 필요합니다.'):
            with self.subTest(text=text):
                case = self.case(text)
                result = rulebook.evaluate_case(case)
                self.assertNotIn('WF03', self.rules(case))
                self.assertTrue(result['review_questions'])
                self.assertTrue(any(w['code'] == 'INCOME_TYPE_UNCONFIRMED' for w in court_rules.plan(case)['warnings']))
                self.assertNotIn('D15', self.catalogs(case))

    def test_employer_registration_and_blank_document_labels_do_not_prove_business(self):
        case = self.case('급여소득자입니다.')
        case['documents'] = [
            {'id': 'salary', 'status': 'received', 'catalog_id': 'D07',
             'text': '급여명세서\n근무처 사업자등록번호 000-00-00000\n사회보험료 공제'},
            {'id': 'form', 'status': 'received', 'catalog_id': 'D48',
             'text': '사업소득 원천징수영수증\n사업자등록증 제출 여부'}]
        self.assertEqual(rulebook.income_profile(case)['kind'], 'salary')
        self.assertNotIn('D15', self.catalogs(case))
        self.assertNotIn('WF12', self.rules(case))

    def test_past_closed_business_does_not_request_current_business_income(self):
        case = self.case('과거에 사업자로 일했지만 폐업했습니다. 현재 급여소득자입니다.')
        self.assertNotIn('WF03', self.rules(case))
        self.assertNotIn('D15', self.catalogs(case))

    def test_obsolete_wf03_needs_cannot_reactivate_business_branch(self):
        case = self.case('급여소득자이며 사업소득은 없습니다.')
        case['court_id'] = 'CT03'
        stale = [{'catalog_id': doc, 'rule_ids': ['WF03']} for doc in ('D15', 'D21', 'D22', 'D49')]
        result = court_rules.plan(case, stale)
        self.assertEqual(result['requests'], [])
        self.assertEqual(set(result['excluded_income_document_ids']), {'D15', 'D21', 'D22', 'D49'})

    def test_business_busan_tax_additions_remain_conditional_on_business(self):
        case = self.case('사업소득자로 가게를 운영합니다.')
        case['court_id'] = 'CT03'
        self.assertTrue({'D05', 'D15', 'D16', 'D18', 'D21'} <= self.catalogs(case))

    def test_salary_statutory_certificates_do_not_create_pension_or_tax_arrears_requests(self):
        case = self.case('급여소득자이며 다른 소득과 체납은 없습니다.')
        case['documents'] = [
            {'id': 'pension-membership', 'status': 'received',
             'text': '연금산정용 가입내역 확인서\n현재 가입 유지'},
            {'id': 'tax-certificate', 'status': 'received',
             'text': '지방세 세목별 과세증명서\n부동산·차량 과세내역: 없음\n체납액: 0원'}]
        self.assertTrue({'WF08', 'WF17'}.isdisjoint(self.rules(case)))
        self.assertNotIn('D25', self.catalogs(case))
        self.assertNotIn('D40', self.catalogs(case))

    def test_actual_pension_income_and_tax_arrears_still_trigger(self):
        case = self.case('급여소득자입니다.')
        case['documents'] = [
            {'id': 'pension-income', 'status': 'verified', 'text': '국민연금 지급내역\n월 수급액 500,000원'},
            {'id': 'tax-debt', 'status': 'verified', 'text': '국세 체납액: 300,000원'}]
        self.assertTrue({'WF08', 'WF17'} <= self.rules(case))
        self.assertTrue({'D25', 'D40'} <= self.catalogs(case))

    def test_reconcile_retires_wrong_business_requests_but_preserves_uploads_and_overrides(self):
        case = self.case(employment='business')
        court_rules.reconcile(case)
        old = next(r for r in case['requests'] if r['catalog_id'] == 'D15')
        old.update(status='fulfilled', document_ids=['uploaded-registration'])
        case['documents'].append({'id': 'uploaded-registration', 'request_id': old['id'], 'status': 'verified'})
        manual = deepcopy(next(r for r in case['requests'] if r['catalog_id'] == 'D21'))
        manual.update(id='manual-business', manual_override=True)
        ordered = deepcopy(next(r for r in case['requests'] if r['catalog_id'] == 'D22'))
        ordered.update(id='court-business', court_order_id='specific-order')
        case['requests'].extend([manual, ordered])
        case['employment_type'] = 'salary'
        case['consultation']['notes'] = '사업소득은 없습니다. 급여소득자입니다.'
        result = court_rules.reconcile(case)
        self.assertIn(old['id'], result['withdrawn_ids'])
        self.assertEqual(old['status'], 'fulfilled')
        self.assertTrue(old['no_longer_required'])
        self.assertEqual(old['document_ids'], ['uploaded-registration'])
        self.assertEqual(case['documents'][0]['request_id'], old['id'])
        self.assertEqual(manual['status'], 'requested')
        self.assertEqual(ordered['status'], 'requested')
        remaining = [r for r in case['requests'] if r['catalog_id'] in {'D15', 'D21', 'D22', 'D49'}
                     and not r.get('no_longer_required') and r['status'] != 'withdrawn']
        self.assertEqual({r['id'] for r in remaining}, {'manual-business', 'court-business'})
        self.assertFalse(court_rules.reconcile(case)['created_ids'])


if __name__ == '__main__':
    unittest.main()
