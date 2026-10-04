import hashlib
import json
from pathlib import Path
import unittest

import pymupdf
from apps.api import court_forms


class CourtFormTests(unittest.TestCase):
    def setUp(self):
        self.case={'id':'test','case_type':'personal_rehabilitation','client_name':'가상검증','court_id':'CT01','court_name':'서울회생법원','input_revision':1,'documents':[],'extraction_candidates':[],'facts':[],'consultation':{'notes':'가상 상담 진술입니다.'}}

    def test_original_sources_are_real_pdf_and_hash_pinned(self):
        for template in court_forms.catalog()['templates']:
            path=court_forms.original_path(template['id'])
            self.assertTrue(path.read_bytes().startswith(b'%PDF'))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),template['source']['sha256'])
            self.assertIn('scourt.go.kr',template['source']['url'])

    def test_court_coverage_distinguishes_common_and_local(self):
        result=court_forms.catalog('CT02')
        self.assertEqual(len(result['courts']),15)
        self.assertEqual(result['coverage']['national_common_templates'],7)
        self.assertNotIn('BUSAN-ATTACHMENTS',[t['id'] for t in result['templates']])
        with self.assertRaises(ValueError):court_forms.preview(self.case,'BUSAN-ATTACHMENTS')

    def test_render_keeps_original_layout_and_adds_grounded_name(self):
        pdf=court_forms.render_pdf('D5100',self.case,{'resident_id':'TEST-001'})
        out=pymupdf.open(stream=pdf,filetype='pdf');original=pymupdf.open(court_forms.original_path('D5100'))
        self.assertEqual(out[0].rect,original[0].rect)
        text=out[0].get_text()
        self.assertIn('가상검증',text)
        self.assertIn('DRAFT',text)
        self.assertIn('효력없음',text)
        self.assertIn('주민등록번호',text)
        self.assertIn('개인회생절차',text)

    def test_creditor_worked_examples_are_excluded(self):
        pdf=court_forms.render_pdf('D5106',self.case,{'creditors':[{'name':'가상채권자','principal':12345678,'interest':0}]})
        out=pymupdf.open(stream=pdf,filetype='pdf');text=''.join(p.get_text() for p in out)
        self.assertIn('가상채권자',text)
        self.assertIn('12,345,678',text)
        self.assertNotIn('71,388,200',text)
        self.assertNotIn('14,988,200',text)

    def test_unapproved_calculation_cannot_populate(self):
        with self.assertRaises(ValueError):court_forms.preview(self.case,'D5110',calculation={'status':'ready_for_review','input_revision':1,'summary':{'monthly_deposit':123}})
        preview=court_forms.preview(self.case,'D5110',{'monthly_deposit':999,'months':12,'present_value':999})
        self.assertTrue(all(f['value'] is None for f in preview['fields'] if f['key']=='months'))

    def test_approved_calculation_is_protected_and_traced(self):
        calc={'id':'calc','status':'approved','stale':False,'approval':{'by':'test-lawyer'},'input_revision':1,'summary':{'net_monthly_income':2800000,'base_living_cost':1538543,'additional_living_cost':0,'monthly_deposit':1261457,'monthly_creditor_capacity':1261457,'months':36,'total_creditor_payment':45412452,'present_value':42089397,'liquidation_value':12000000},'inputs':{'recognized_household_size':1,'months':36},'creditor_allocations':[]}
        preview=court_forms.preview(self.case,'D5110',{'months':1,'monthly_income':1,'present_value':1},calc)
        values={f['key']:f['value'] for f in preview['fields']}
        self.assertEqual(values['months'],36);self.assertEqual(values['monthly_income'],2800000);self.assertEqual(values['present_value'],42089397)
        calc['stale']=True
        with self.assertRaises(ValueError):court_forms.preview(self.case,'D5110',calculation=calc)

    def test_conflicting_identity_document_is_not_source(self):
        self.case['documents']=[{'id':'wrong','status':'received','automated_check':{'coverage_status':'identity_conflict'}}]
        self.case['extraction_candidates']=[{'key':'monthly_income','value':99999999,'document_id':'wrong'}]
        result=court_forms.preview(self.case,'D5103')
        self.assertTrue(all(f['value'] is None for f in result['fields'] if f['key']=='monthly_income'))

    def test_overflow_is_explicit_and_retained_in_annex(self):
        long_name='가상장문'+('가나다라마바사'*40)
        preview=court_forms.preview(self.case,'D5100',{'client_name':long_name})
        self.assertIn('client_name',preview['overflow_fields']);self.assertFalse(preview['ready_for_review'])
        pdf=court_forms.render_pdf('D5100',self.case,{'client_name':long_name})
        text=''.join(p.get_text() for p in pymupdf.open(stream=pdf,filetype='pdf'))
        self.assertIn('별지',text);self.assertIn('가상장문',text)

    def test_synthetic_finances_reconcile_and_scans_are_image_only(self):
        folder=Path(__file__).resolve().parents[1]/'examples'/'synthetic_case'
        expected=json.loads((folder/'expected.json').read_text(encoding='utf-8'))
        self.assertEqual(sum(c['principal']+c['interest'] for c in expected['creditors']),expected['bank_debt_total'])
        self.assertEqual(expected['bank_debt_total']+expected['family_debt_candidate'],expected['total_debt_after_family_review'])
        self.assertEqual(expected['lease_deposit']+expected['bank_balance']+expected['insurance_surrender'],expected['assets_total'])
        for p in pymupdf.open(folder/'documents'/'01_급여명세서_가상자료.pdf'):
            self.assertFalse(p.get_text().strip());self.assertTrue(p.get_images())

    def test_variable_rounding_has_complete_schedule_annex(self):
        creditors=[{'id':'a','name':'첫째가상','principal':7,'interest':0,'kind':'unsecured'},
                   {'id':'b','name':'둘째가상','principal':5,'interest':0,'kind':'unsecured'}]
        schedule=[{'month':1,'deposit':7,'trustee_fee':0,'creditor_payment':7,'allocations':[{'creditor_id':'a','principal':4,'interest':0,'total':4},{'creditor_id':'b','principal':3,'interest':0,'total':3}]},
                  {'month':2,'deposit':5,'trustee_fee':0,'creditor_payment':5,'allocations':[{'creditor_id':'a','principal':3,'interest':0,'total':3},{'creditor_id':'b','principal':2,'interest':0,'total':2}]}]
        calc={'id':'ledger-test','status':'approved','approval':{'actor':'test-lawyer'},'stale':False,'input_revision':1,'inputs':{'creditors':creditors,'months':2},
          'summary':{'months':2,'total_creditor_payment':12},'schedule':schedule,
          'creditor_allocations':[{'creditor_id':'a','name':'첫째가상','principal':7,'principal_payment':7,'interest_payment':0,'total_payment':7},
                                  {'creditor_id':'b','name':'둘째가상','principal':5,'principal_payment':5,'interest_payment':0,'total_payment':5}]}
        fields={'start_year':2026,'start_month':11,'start_day':1}
        preview=court_forms.preview(self.case,'D5110',fields,calc)
        self.assertEqual(next(f['value'] for f in preview['fields'] if f['key']=='allocations.0.monthly_payment'),'회차별 별지')
        pdf=court_forms.render_pdf('D5110',self.case,fields,calc)
        text=''.join(p.get_text() for p in pymupdf.open(stream=pdf,filetype='pdf'))
        self.assertIn('제1회차 / 계획일 2026-11-01',text)
        self.assertIn('제2회차 / 계획일 2026-12-01',text)
        self.assertIn('원금 4원 + 이자 0원 = 합계 4원',text)
        self.assertIn('원금 2원 + 이자 0원 = 합계 2원',text)
        self.assertIn('채권자 변제 합계 12원',text)

    def test_creditors_beyond_original_four_rows_are_not_lost(self):
        creditors=[{'id':str(i),'name':'초과채권자'+str(i),'principal':100000+i,'interest':i,'kind':'unsecured','address':'가상주소'+str(i)} for i in range(1,8)]
        calc={'id':'many','status':'approved','approval':{'actor':'test-lawyer'},'input_revision':1,'stale':False,'inputs':{'creditors':creditors},'summary':{}}
        pdf=court_forms.render_pdf('D5106',self.case,calculation=calc)
        text=''.join(p.get_text() for p in pymupdf.open(stream=pdf,filetype='pdf'))
        for creditor in creditors:
            self.assertIn(creditor['name'],text)
            self.assertIn(creditor['address'],text)
            self.assertIn(f"원금 {creditor['principal']:,}원 / 이자 {creditor['interest']}원",text)
        self.assertIn('채권자 수: 7명',text)

    def test_official_table_totals_numbers_and_rate_are_in_designated_cells(self):
        creditors=[{'id':'a','name':'가상은행','principal':77000000,'interest':3000000,'kind':'unsecured'}]
        calc={'id':'totals','status':'approved','approval':{'actor':'test-lawyer'},'stale':False,'input_revision':1,
          'inputs':{'creditors':creditors,'months':36},
          'summary':{'principal_total':77000000,'total_principal_payment':45412452,'total_creditor_payment':45412452,'months':36},
          'schedule':[{'month':n,'creditor_payment':1261457,'deposit':1261457,'trustee_fee':0,'allocations':[{'creditor_id':'a','principal':1261457,'interest':0,'total':1261457}]} for n in range(1,37)],
          'creditor_allocations':[{'creditor_id':'a','name':'가상은행','principal':77000000,'principal_payment':45412452,'total_payment':45412452}]}
        preview=court_forms.preview(self.case,'D5110',calculation=calc)
        data=court_forms.render_pdf('D5110',self.case,calculation=calc)
        pdf=pymupdf.open(stream=data,filetype='pdf');page=pdf[6]
        expected={'allocation_principal_total':'77,000,000','allocation_monthly_total':'1,261,457','total_creditor_payment':'45,412,452','principal_repayment_percent':'58.98','allocations.0.number':'1'}
        for field in preview['fields']:
            if field['page']==6 and field['key'] in expected:
                rect=pymupdf.Rect(field['rect']);rect.y1-=1;rect.y0-=2
                self.assertIn(expected[field['key']],page.get_textbox(rect),(field['key'],field['rect']))
        creditors_pdf=pymupdf.open(stream=court_forms.render_pdf('D5106',self.case,calculation=calc),filetype='pdf')
        number=next(f for f in court_forms.TEMPLATES['D5106']['fields'] if f['key']=='creditors.0.number')
        self.assertIn('1',creditors_pdf[0].get_textbox(pymupdf.Rect(number['rect'])))


if __name__=='__main__':unittest.main()
