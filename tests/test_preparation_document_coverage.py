"""Requested evidence supports official writing fields without cloning a fixture."""
import copy
import json
from pathlib import Path
import unittest

from apps.api import court_rules, domain, rulebook

ROOT=Path(__file__).resolve().parents[1]


class PreparationCoverageTests(unittest.TestCase):
    def scenario(self):
        case=domain.new_case('합성고객','CT01','서울회생법원','준비자료 검증',True)
        case['consultation']={'status':'completed','notes':(ROOT/'examples/court_ready_fixture/staff_consultation.txt').read_text(encoding='utf-8')}
        case['intake']={'status':'completed'}
        return case

    def test_every_rich_fixture_evidence_purpose_has_a_request_or_companion_slot(self):
        plan=court_rules.plan(self.scenario(),as_of='2026-10-04')
        manifest=json.loads((ROOT/'examples/court_ready_fixture/manifest.json').read_text(encoding='utf-8'))
        for document in manifest['documents']:
            with self.subTest(document=document['title']):
                self.assertTrue(any(document['catalog_id'] in {row['catalog_id'],*row.get('accepted_catalog_ids',[])} for row in plan['requests']))
        # A bank certificate, twelve payslips and a contract with its receipts
        # are collections. Twenty-nine test PDFs are not twenty-nine duties.
        self.assertLess(len(plan['requests']),len(manifest['documents']))
        self.assertEqual(len(plan['requests']),23)
        self.assertTrue(all(row['supports_multiple_files'] for row in plan['requests']))

    def test_salary_current_period_is_completed_twelve_months_and_not_retired_by_tax_year(self):
        case=self.scenario()
        case['documents']=[{'id':'old-tax-year','status':'verified','catalog_id':'D06','text':'2025 귀속 근로소득원천징수영수증'}]
        plan=court_rules.plan(case,as_of='2026-10-04')
        payroll=next(row for row in plan['requests'] if row['catalog_id']=='D07')
        self.assertEqual(payroll['period_start'],'2025-10-01')
        self.assertEqual(payroll['period_end'],'2026-09-30')
        self.assertEqual(payroll['requirement_kind'],'preparation')
        self.assertIn('법정 공통 요청기간',payroll['qualification'])
        self.assertTrue(payroll['source_refs'])

    def test_vehicle_inventory_title_cannot_request_a_price_for_an_absent_vehicle(self):
        case=self.scenario()
        case['documents']=[{'id':'inventory','status':'received','text':'자동차 소유 조회확인\n성명: 합성고객\n자동차 소유: 없음\n본인 명의 차량은 없습니다.'}]
        needs=rulebook.evaluate_case(case)['required_documents']
        self.assertNotIn('D30',{row['catalog_id'] for row in needs})
        self.assertNotIn('D32',{row['catalog_id'] for row in needs})
        planned={row['catalog_id'] for row in court_rules.plan(case,as_of='2026-10-04')['requests']}
        self.assertIn('SUPPORT-VEHICLE',planned)
        self.assertNotIn('D32',planned)

    def test_affirmatively_owned_vehicle_still_requests_valuation(self):
        case=domain.new_case('합성고객','CT01','서울회생법원','차량 확인',True)
        case['consultation']={'notes':'현재 자동차 1대를 소유하고 있습니다. 중고시세 확인이 필요합니다.'}
        self.assertIn('D32',{row['catalog_id'] for row in court_rules.plan(case)['requests']})

    def test_inventory_is_one_collection_while_account_and_creditor_scopes_remain_separate(self):
        case=self.scenario()
        case['financial_accounts']=[{'id':'one','institution':'검증은행','number':'11112222'}, {'id':'two','institution':'검증은행','number':'33334444'}]
        case['creditors']=[{'id':'credit-a','name':'채권기관A'},{'id':'credit-b','name':'채권기관B'}]
        requests=court_rules.plan(case,as_of='2026-10-04')['requests']
        self.assertEqual(sum(row['catalog_id']=='D35' for row in requests),1)
        self.assertEqual(sum(row['catalog_id']=='D36' for row in requests),2)
        self.assertEqual(sum(row['catalog_id']=='D38' for row in requests),2)

    def test_preparation_does_not_claim_a_court_mandate_or_add_business_registration(self):
        rows=court_rules.plan(self.scenario(),as_of='2026-10-04')['requests']
        self.assertNotIn('D15',{row['catalog_id'] for row in rows})
        self.assertNotIn('D25',{row['catalog_id'] for row in rows})
        self.assertNotIn('D48',{row['catalog_id'] for row in rows})
        for row in rows:
            if 'FORM_EVIDENCE_PREPARATION' in row['rule_ids']:
                self.assertEqual(row['requirement_kind'],'preparation')
                self.assertIn('법정 필수목록이라는 뜻은 아닙니다',row['qualification'])
                self.assertTrue(row['source_refs'])

    def test_reconcile_preserves_uploads_and_refreshes_same_scope_collection_guidance(self):
        case=self.scenario()
        court_rules.reconcile(case,as_of='2026-10-04')
        request=next(row for row in case['requests'] if row['catalog_id']=='D33')
        request.update(document_ids=['contract','receipt'],status='received')
        request.pop('supports_multiple_files')
        case['documents']=[{'id':'contract','request_id':request['id'],'status':'verified','text':'원본1'},
                           {'id':'receipt','request_id':request['id'],'status':'received','text':'원본2'}]
        before=copy.deepcopy(case['documents'])
        result=court_rules.reconcile(case,as_of='2026-10-04')
        self.assertEqual(case['documents'],before)
        self.assertEqual(request['document_ids'],['contract','receipt'])
        self.assertTrue(request['supports_multiple_files'])
        self.assertFalse(result['created_ids'])

    def test_explicit_empty_need_recalculation_does_not_refill_preparation_requests(self):
        self.assertEqual(court_rules.plan(self.scenario(),[],as_of='2026-10-04')['requests'],[])


if __name__=='__main__':
    unittest.main()
