"""Real API/SQLite integration tests using synthetic records only."""
import concurrent.futures
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
(ROOT/'.work/tests').mkdir(parents=True,exist_ok=True)
# Direct module runs must be safe too. Class-scoped patching below is the actual
# boundary: another test may have imported store before this module is found.
os.environ['DEBTOFF_DEMO_MODE']='1'
os.environ['DEBTOFF_AUTO_AX']='0'

from fastapi.testclient import TestClient
from apps.api.main import app
from apps.api import domain, store


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = Path(tempfile.mkdtemp(prefix='workflow-', dir=ROOT/'.work/tests')).resolve()
        cls.data_patch = patch.object(store, 'DATA_DIR', cls.data_dir)
        cls.data_patch.start()
        cls.addClassCleanup(cls.data_patch.stop)
        cls.auto_ax_patch = patch.dict(os.environ, {'DEBTOFF_AUTO_AX': '0'})
        cls.auto_ax_patch.start()
        cls.addClassCleanup(cls.auto_ax_patch.stop)
        cls.client_context=TestClient(app)
        cls.client=cls.client_context.__enter__()
        cls.headers={}
        for role in ('staff','lawyer','client'):
            response=cls.client.post('/api/auth/login',json={'username':f'demo-{role}','password':'debtoff-demo'})
            cls.headers[role]={'Authorization':'Bearer '+response.json()['token']}

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None,None,None)

    def setUp(self):
        self.case=domain.new_case('테스트','CT01','서울회생법원','통제 검증용 합성 사례',True)
        self.case['client_user_id']='client'
        store.insert_case(self.case)
        self.base='/api/cases/'+self.case['id']

    def read(self,role='staff'):
        return self.client.get(self.base,headers=self.headers[role]).json()

    def post(self,path,data=None,role='staff',version=None):
        payload={'expected_version':version or self.read()['version'],**(data or {})}
        return self.client.post(self.base+path,json=payload,headers=self.headers[role])

    def upload(self,text='월 급여 2800000원, 총 채무 42000000원. 원문 확인용 합성 증빙.',filename='evidence.txt',role='staff',request_id=''):
        return self.client.post(self.base+'/documents',headers=self.headers[role],files={'file':(filename,text.encode() if isinstance(text,str) else text)},data={'expected_version':self.read()['version'],'request_id':request_id})

    def verified_doc(self):
        result=self.upload()
        self.assertEqual(result.status_code,200,result.text)
        doc=result.json()['documents'][-1]
        result=self.post('/documents/'+doc['id']+'/verify',{'scope_confirmed':True,'content_confirmed':True,'person_confirmed':True,'reason':'합성 원문 인물·기간·내용 전수 대조'})
        self.assertEqual(result.status_code,200,result.text)
        return doc['id']

    def confirm_all(self,doc_id):
        for fact,value in (('income',2800000),('debt',42000000)):
            result=self.post('/facts/'+fact+'/confirm',{'value':value,'evidence_ids':[doc_id],'reason':'원문 금액과 기간을 확인함'})
            self.assertEqual(result.status_code,200,result.text)

    def test_auth_required(self):
        self.assertEqual(self.client.get(self.base).status_code,401)

    def test_suite_storage_isolated_from_development_data(self):
        self.assertEqual(store.DATA_DIR.resolve(), self.data_dir)
        self.assertNotEqual(store.DATA_DIR.resolve(), (ROOT/'.local').resolve())
        self.assertTrue(store.DATA_DIR.resolve().is_relative_to((ROOT/'.work/tests').resolve()))
        self.assertTrue((store.DATA_DIR/'debtoff.sqlite3').is_file())

    def test_client_case_isolation(self):
        self.assertEqual(self.client.get('/api/cases/demo-002',headers=self.headers['client']).status_code,404)

    def test_client_projection_hides_strategy_and_extracted_text(self):
        self.upload(role='client')
        body=self.read('client')
        for key in ('issues','facts','audit','calculations','approvals','agent_runs','summary','members'):
            self.assertNotIn(key,body)
        self.assertNotIn('text',body['documents'][0])
        self.assertNotIn('storage_name',body['documents'][0])

    def test_client_cannot_access_registry_or_confirm(self):
        self.assertEqual(self.client.get('/api/registry',headers=self.headers['client']).status_code,403)
        self.assertEqual(self.post('/facts/income/confirm',{'value':1,'evidence_ids':[],'reason':'고객'},role='client').status_code,403)

    def test_stale_mutation_returns_409(self):
        version=self.read()['version']
        self.assertEqual(self.post('/messages',{'text':'첫 메시지'},version=version).status_code,200)
        self.assertEqual(self.post('/messages',{'text':'오래된 메시지'},version=version).status_code,409)

    def test_concurrent_writes_only_one_wins(self):
        version=self.read()['version']
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(lambda n:self.post('/messages',{'text':f'동시 저장 {n}'},version=version).status_code,range(2)))
        self.assertEqual(sorted(results),[200,409])

    def test_receipt_does_not_confirm_facts(self):
        self.upload()
        body=self.read()
        self.assertEqual(body['documents'][0]['status'],'received')
        self.assertEqual(body['facts'][0]['status'],'unknown')
        self.assertIsNone(body['facts'][0]['value'])

    def test_unverified_evidence_cannot_confirm(self):
        doc=self.upload().json()['documents'][0]['id']
        self.assertEqual(self.post('/facts/income/confirm',{'value':2800000,'evidence_ids':[doc],'reason':'대조 안된 근거'}).json()['code'],'EVIDENCE_UNVERIFIED')

    def test_unknown_never_becomes_zero(self):
        doc=self.verified_doc()
        response=self.post('/facts/income/confirm',{'value':None,'evidence_ids':[doc],'reason':'미확인 입력을 유지'})
        self.assertEqual(response.status_code,422)
        self.assertIsNone(self.read()['facts'][0]['value'])

    def test_cross_case_evidence_rejected(self):
        self.assertEqual(self.post('/facts/income/confirm',{'value':1,'evidence_ids':['another-case-document'],'reason':'다른 사건 근거 시도'}).status_code,422)

    def test_fact_changes_expire_approvals_and_outputs(self):
        doc=self.verified_doc()
        self.confirm_all(doc)
        self.post('/approvals',{'gate':'H1','reason':'상담과 조사범위 검토 완료'},role='lawyer')
        self.post('/calculate',{'mode':'scenario','income':2800000,'expenses':1500000,'months':36,'liquidation_value':0})
        bundle=self.post('/bundles').json()['bundles'][-1]
        approved=self.post('/bundles/'+bundle['id']+'/approve',{'reason':'원문과 검토 결과 대조 완료'},role='lawyer')
        self.assertEqual(approved.status_code,200,approved.text)
        self.post('/facts/income/confirm',{'value':2700000,'evidence_ids':[doc],'reason':'원문 소득 정정 확인'})
        body=self.read()
        self.assertTrue(all(x['stale'] for x in body['approvals']+body['bundles']+body['calculations']))
        self.assertEqual(self.post('/bundles/'+bundle['id']+'/approve',{'reason':'과거 버전 승인 시도'},role='lawyer').json()['code'],'STALE_APPROVAL')

    def test_staff_cannot_approve(self):
        self.assertEqual(self.post('/approvals',{'gate':'H1','reason':'직원 최종승인 시도'}).json()['code'],'LAWYER_ONLY')

    def test_each_issue_requires_reason_and_verified_evidence(self):
        doc=self.verified_doc()
        c=store.get_case(self.case['id'])
        store.mutate(c['id'],c['version'],{'name':'test','role':'staff'},'fixture',lambda x:x['issues'].append({'id':'legal','title':'최근 완납','description':'검토','status':'open','evidence_ids':[]}))
        self.assertEqual(self.post('/issues/legal/decide',{'decision':'보류','reason':'','evidence_ids':[doc]},role='lawyer').status_code,422)
        result=self.post('/issues/legal/decide',{'decision':'증빙에 따라 소명','reason':'원문과 반대 자료 검토 완료','evidence_ids':[doc]},role='lawyer')
        self.assertEqual(result.status_code,200,result.text)

    def test_operational_calculation_requires_legal_rules(self):
        doc=self.verified_doc()
        self.confirm_all(doc)
        result=self.post('/calculate',{'mode':'operational','income':2800000,'expenses':1500000,'months':36,'liquidation_value':0})
        self.assertEqual(result.json()['code'],'LEGAL_RULESET_NOT_APPROVED')

    def test_scenario_arithmetic_and_negative_cashflow(self):
        response=self.post('/calculate',{'mode':'scenario','income':100,'expenses':200,'months':36,'liquidation_value':50})
        result=response.json()['calculations'][-1]
        self.assertEqual(result['monthly_available'],-100)
        self.assertEqual(result['total'],0)
        self.assertEqual(result['liquidation_gap'],50)

    def test_registry_preserves_inactive_court_rules(self):
        data=self.client.get('/api/registry',headers=self.headers['staff']).json()
        self.assertEqual(len(data['courts']),15)
        self.assertEqual(len(data['documents']),50)
        self.assertFalse(data['meta']['operational'])

    def test_all_requirement_ids_preserved_without_collision(self):
        data=self.client.get('/api/requirements',headers=self.headers['staff']).json()
        self.assertEqual(len(data['items']),265)
        self.assertEqual(len({x['id'] for x in data['items']}),265)

    def test_fake_pdf_rejected_and_no_partial_write(self):
        response=self.upload('not a pdf','bad.pdf')
        self.assertEqual(response.status_code,422)
        self.assertEqual(self.read()['documents'],[])

    def test_executable_upload_rejected(self):
        self.assertEqual(self.upload('malicious','danger.html').status_code,422)

    def test_duplicate_file_rejected(self):
        self.assertEqual(self.upload().status_code,200)
        self.assertEqual(self.upload().json()['code'],'DUPLICATE_FILE')
        self.assertEqual(len(self.read()['documents']),1)

    def test_original_download_requires_permission(self):
        doc=self.verified_doc()
        self.assertEqual(self.client.get(self.base+'/documents/'+doc+'/download',headers=self.headers['client']).status_code,403)
        self.assertEqual(self.client.get(self.base+'/documents/'+doc+'/download',headers=self.headers['staff']).status_code,200)

    def test_partial_scope_does_not_fulfill(self):
        self.post('/requests',{'catalog_id':'D01','period':'현재 주소·필요 표시 범위','reason':'관할 후보와 주소 검토'})
        req=self.read()['requests'][0]['id']
        doc=self.upload(request_id=req).json()['documents'][0]['id']
        self.post('/documents/'+doc+'/verify',{'scope_confirmed':False,'content_confirmed':True,'person_confirmed':True,'reason':'기간 범위 일부가 누락됨'})
        self.assertEqual(self.read()['requests'][0]['status'],'needs_more')

    def test_correction_requires_source_and_deadline_evidence(self):
        doc=self.verified_doc()
        result=self.post('/corrections',{'title':'보정','source_document_id':doc,'source_page':1,'requirement':'계좌 거래내역과 사용처 제출','period':'원문 기간','due_date':'2026-10-17'})
        self.assertEqual(result.status_code,422)

    def test_correction_response_and_lawyer_approval(self):
        doc=self.verified_doc()
        result=self.post('/corrections',{'title':'거래 사용처','source_document_id':doc,'source_page':1,'requirement':'요구 계좌 범위와 거래 사용처 소명','period':'원문 지정 기간','due_date':'2026-10-17','deadline_evidence_id':doc})
        self.assertEqual(result.status_code,200,result.text)
        corr=result.json()['corrections'][0]['id']
        result=self.post('/corrections/'+corr+'/respond',{'answer':'요구 계좌와 사용처 자료를 원문과 대조함','evidence_ids':[doc],'reason':'범위·기간·설명 충족 검토 완료'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(self.post('/corrections/'+corr+'/approve',{'reason':'보정 항목 및 증빙 검토 완료'}).status_code,422)
        result=self.post('/corrections/'+corr+'/approve',{'reason':'보정 항목 및 증빙 검토 완료'},role='lawyer')
        self.assertEqual(result.json()['corrections'][0]['status'],'approved')

    def test_held_case_deadline_still_alerts(self):
        c=self.read()
        store.mutate(c['id'],c['version'],{'name':'test','role':'staff'},'fixture',lambda x:x.update(stage='보류',deadlines=[{'id':'dl','title':'확인','due_date':'2020-01-01'}]))
        self.assertEqual(self.read()['deadline_alerts'][0]['severity'],'overdue')

    def test_unknown_service_date_is_not_guessed(self):
        c=self.read()
        store.mutate(c['id'],c['version'],{'name':'test','role':'staff'},'fixture',lambda x:x.update(deadlines=[{'id':'dl','title':'송달 확인','due_date':None}]))
        self.assertEqual(self.read()['deadline_alerts'][0]['severity'],'urgent')
        self.assertIsNone(self.read()['deadline_alerts'][0]['due_date'])

    def test_review_bundle_cannot_be_filed(self):
        self.assertEqual(self.post('/submissions',{'bundle_id':'fake','receipt_document_id':'fake','court_case_number':'fake'}).json()['code'],'FILING_TEMPLATE_NOT_APPROVED')

    def test_export_escapes_untrusted_text(self):
        c=self.read()
        store.mutate(c['id'],c['version'],{'name':'test','role':'staff'},'fixture',lambda x:x.update(title='<script>alert(1)</script>'))
        bundle=self.post('/bundles').json()['bundles'][-1]
        response=self.client.get(self.base+'/bundles/'+bundle['id']+'/download',headers=self.headers['staff'])
        self.assertIn('&lt;script&gt;',response.text)
        self.assertNotIn('<script>',response.text)

    def test_history_and_audit_are_persistent(self):
        self.post('/messages',{'text':'감사 이력 확인'})
        with store.db() as con:
            count=con.execute('SELECT count(*) FROM history WHERE case_id=?',(self.case['id'],)).fetchone()[0]
        self.assertEqual(count,2)
        self.assertEqual(self.read()['audit'][-1]['action'],'message.created')

    def test_log_out_revokes_session(self):
        response=self.client.post('/api/auth/login',json={'username':'demo-staff','password':'debtoff-demo'})
        headers={'Authorization':'Bearer '+response.json()['token']}
        self.client.post('/api/auth/logout',headers=headers)
        self.assertEqual(self.client.get('/api/cases',headers=headers).status_code,401)

    def test_legal_connector_has_explicit_unconfigured_state(self):
        if os.getenv('LAW_API_OC'):
            self.skipTest('configured external credentials')
        response=self.client.get('/api/legal/search',params={'q':'개인회생','target':'law'},headers=self.headers['staff'])
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['status'],'unconfigured')

    def test_money_does_not_accept_bool_or_numeric_string(self):
        doc=self.verified_doc()
        for value in (True,'2800000',2.5):
            self.assertEqual(self.post('/facts/income/confirm',{'value':value,'evidence_ids':[doc],'reason':'잘못된 금액형식 검증'}).status_code,422)

    def test_internal_document_metadata_not_in_client_response(self):
        self.upload(filename='internal-strategy.txt')
        body=self.read('client')
        self.assertEqual(body['documents'],[])
        self.assertNotIn('internal-strategy',json.dumps(body))

    def test_client_case_summary_has_no_internal_blockers(self):
        self.upload(filename='internal-strategy.txt')
        result=self.client.get('/api/cases',headers=self.headers['client']).json()
        for summary in result['cases']:
            for key in ('next_action','blocker_count','correction_count','deadline_alerts','summary'):
                self.assertNotIn(key,summary)
        ours=next(c for c in result['cases'] if c['id']==self.case['id'])
        self.assertEqual(ours['document_count'],0)

    def test_downgraded_evidence_blocks_new_report_approval(self):
        doc=self.verified_doc()
        self.confirm_all(doc)
        self.post('/documents/'+doc+'/verify',{'scope_confirmed':False,'content_confirmed':True,'person_confirmed':True,'reason':'대조 후 일부 범위 누락 발견'})
        bundle=self.post('/bundles').json()['bundles'][-1]
        result=self.post('/bundles/'+bundle['id']+'/approve',{'reason':'충족되지 않은 자료 승인 시도'},role='lawyer')
        self.assertEqual(result.json()['code'],'UNRESOLVED_INPUTS')

    def test_multiple_corrections_can_be_approved_independently(self):
        doc=self.verified_doc()
        ids=[]
        for n in range(2):
            result=self.post('/corrections',{'title':f'보정 {n}','source_document_id':doc,'source_page':1,'requirement':'요구 계좌 범위와 거래 사용처 소명','period':'원문 지정 기간','due_date':'2026-10-17','deadline_evidence_id':doc})
            ids.append(result.json()['corrections'][-1]['id'])
        for corr in ids:
            result=self.post('/corrections/'+corr+'/respond',{'answer':'요구자료 범위와 거래 사용처를 대조함','evidence_ids':[doc],'reason':'각 항목의 요구 범위 충족 확인'})
            self.assertEqual(result.status_code,200,result.text)
        for corr in ids:
            result=self.post('/corrections/'+corr+'/approve',{'reason':'원문 및 해당 항목 충족 검토'},role='lawyer')
            self.assertEqual(result.status_code,200,result.text)
        self.assertTrue(all(c['status']=='approved' for c in self.read()['corrections']))

    def test_deadline_can_be_confirmed_after_initial_registration(self):
        doc=self.verified_doc()
        result=self.post('/corrections',{'title':'후속 기한 확인','source_document_id':doc,'source_page':1,'requirement':'자료와 설명을 함께 보완','period':'원문 지정 기간'})
        deadline=result.json()['deadlines'][-1]['id']
        result=self.post('/deadlines/'+deadline+'/confirm',{'due_date':'2026-10-17','evidence_id':doc,'reason':'송달 증거와 만료일 수동 검토'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['corrections'][-1]['due_date'],'2026-10-17')

    def test_new_customer_statement_invalidates_old_approval(self):
        self.post('/approvals',{'gate':'H1','reason':'상담 범위 확인 후 승인'},role='lawyer')
        old=self.read()['input_revision']
        self.post('/messages',{'text':'최근 직장을 그만두었습니다.'},role='client')
        case=self.read()
        self.assertGreater(case['input_revision'],old)
        self.assertTrue(case['approvals'][0]['stale'])
        self.assertTrue(case['issues'])

    def test_manual_consultation_is_statement_not_verified_fact(self):
        response=self.post('/consultation',{'notes':'수동 상담 메모','answers':{'income':'월 소득 280만원이라고 진술함','unknowns':'증빙 확인 전'},'consent':True})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['consultation']['status'],'party_statement')
        self.assertIsNone(response.json()['facts'][0]['value'])

    def test_case_list_is_summary_without_document_text(self):
        self.upload()
        result=self.client.get('/api/cases',headers=self.headers['staff']).json()
        for summary in result['cases']:
            self.assertNotIn('documents',summary)
            self.assertNotIn('bundles',summary)
            self.assertIn('correction_count',summary)

    def test_backup_restores_documents_db_and_revokes_sessions(self):
        from scripts.backup import create_backup,restore_backup
        doc=self.verified_doc()
        root=Path(tempfile.mkdtemp(prefix='backup-',dir=ROOT/'.work/tests'))
        archive=root/'snapshot.zip'
        create_backup(archive)
        restored=root/'restored'
        result=restore_backup(archive,restored)
        self.assertEqual(result['integrity'],'ok')
        import sqlite3
        with sqlite3.connect(restored/'debtoff.sqlite3') as con:
            self.assertEqual(con.execute('SELECT count(*) FROM sessions').fetchone()[0],0)
            self.assertIsNotNone(con.execute('SELECT id FROM cases WHERE id=?',(self.case['id'],)).fetchone())
        self.assertTrue(list((restored/'uploads'/self.case['id']).iterdir()))

    def test_persisted_running_job_is_failed_on_restart(self):
        run_id=store.uid('run')
        with store.db() as con:
            con.execute('INSERT INTO runs VALUES (?,?,?,?,?)',(run_id,self.case['id'],1,'running',json.dumps({'id':run_id,'status':'running'})))
        store.initialize()
        with store.db() as con:
            self.assertEqual(con.execute('SELECT status FROM runs WHERE id=?',(run_id,)).fetchone()[0],'failed')

    def test_finished_agent_report_is_marked_stale_after_change(self):
        run_id=store.uid('run')
        with store.db() as con:
            con.execute('INSERT INTO runs VALUES (?,?,?,?,?)',(run_id,self.case['id'],1,'needs_review',json.dumps({'id':run_id,'status':'needs_review','input_revision':1,'proposals':[]})))
        self.post('/messages',{'text':'새 검토 자료 회신'},role='client')
        result=self.client.get('/api/agent-runs/'+run_id,headers=self.headers['staff'])
        self.assertEqual(result.json()['status'],'stale')


if __name__=='__main__':
    unittest.main(verbosity=2)
