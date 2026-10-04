import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api import court_forms,extended_routes,statement_authoring as authoring


def case_data():
    return {'id':'synthetic-statement','version':1,'input_revision':1,'org_id':'office','court_id':'CT01',
        'court_name':'서울회생법원','client_name':'가상검증','synthetic':True,
        'consultation':{'notes':'가상 사례, 실제 사건 아님. 월 소득은 280만원이다.'},'facts':[],
        'documents':[{'id':'payroll','status':'verified','version':1,'text':'월 소득 280만원'}],
        'extraction_candidates':[{'id':'income','document_id':'payroll','key':'monthly_income',
            'value':2800000,'quote':'월 소득 280만원','status':'accepted'}]}


def narrative():
    return {'status':'completed','verification':{'passed':True},'sections':[{'id':'statement','paragraphs':[
        {'text':'제 월 소득은 280만원입니다.','source_ids':['fact:income'],
         'quotes':[{'source_id':'fact:income','quote':'월 소득 280만원'}]}]}],
        'source_refs':[{'id':'fact:income','kind':'case_fact','text':'월 소득 280만원','original_source_ids':['payroll']}]}


class StatementAuthoringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack=[]
        for target,name,value in [(authoring.legal_watch,'dependencies_signature','law-version'),
                (authoring.automation,'_retrieve',[{'id':'trusted-law'}]),
                (authoring.automation,'approved_examples',[])]:
            mocked=patch.object(target,name,return_value=value);mocked.start();self.addCleanup(mocked.stop)

    async def test_manual_authoring_uses_local_sources_then_independent_review(self):
        case=case_data()
        with (patch.object(authoring.grounded_drafting,'compose',new=AsyncMock(return_value=narrative())) as compose,
             patch.object(authoring,'run_local_verification_batched',new=AsyncMock(return_value={'passed':True,'status':'passed'})) as review):
            result=await authoring.prepare(case)
        self.assertEqual(result['status'],'completed')
        self.assertNotIn('가상 사례',result['statement'])
        self.assertEqual(compose.call_args.kwargs['consultation']['source_id'],'consultation:notes')
        self.assertEqual(compose.call_args.kwargs['section_ids'],['statement'])
        payload=review.call_args.args[1]
        self.assertEqual(review.call_args.args[0],'document')
        self.assertEqual({item['id'] for item in payload['items']},{'statement:0','fact:income'})
        self.assertNotIn(result['statement'],[source['text'] for source in payload['sources']])
        authoring.save(case,result)
        field=next(field for field in court_forms.preview(case,'D5105')['fields'] if field['key']=='statement')
        self.assertEqual(field['value'],result['statement'])
        self.assertEqual(field['source']['type'],'grounded_narrative')
        self.assertFalse(field['authoring_required'])
        with patch.object(authoring.grounded_drafting,'writing_policy',return_value={'version':'changed-policy'}):
            self.assertIsNone(authoring.current_run(case))

    async def test_unbacked_extraction_or_failed_review_never_falls_back_to_notes(self):
        case=case_data();case['extraction_candidates'][0]['quote']='없는 원문'
        with patch.object(authoring.grounded_drafting,'compose',new=AsyncMock()) as compose:
            result=await authoring.prepare(case)
        compose.assert_not_awaited();self.assertEqual(result['status'],'needs_review')
        with (patch.object(authoring.grounded_drafting,'compose',new=AsyncMock(return_value=narrative())),
             patch.object(authoring,'run_local_verification_batched',new=AsyncMock(return_value={'passed':False,'status':'unavailable'}))):
            result=await authoring.prepare(case_data())
        self.assertIsNone(result['statement'])
        self.assertFalse(result['verification']['passed'])

    async def test_renderer_has_no_raw_consultation_fallback_and_manual_edit_is_unverified(self):
        case=case_data()
        field=next(field for field in court_forms.preview(case,'D5105')['fields'] if field['key']=='statement')
        self.assertIsNone(field['value']);self.assertTrue(field['authoring_required'])
        edited=next(field for field in court_forms.preview(case,'D5105',{'statement':'직원이 직접 작성한 본문입니다.'})['fields'] if field['key']=='statement')
        self.assertEqual(edited['source']['type'],'staff_input')
        self.assertEqual(edited['status'],'review_required')

    async def test_semantic_feedback_repairs_at_most_once_and_timeout_never_retries(self):
        rejection={'passed':False,'status':'needs_review','findings':[{'item_id':'statement:0','status':'uncertain','reason':'경위의 근거 부족'}]}
        with (patch.object(authoring.grounded_drafting,'compose',new=AsyncMock(return_value=narrative())) as compose,
              patch.object(authoring,'run_local_verification_batched',new=AsyncMock(side_effect=[rejection,{'passed':True,'status':'passed'}]))):
            result=await authoring.prepare(case_data())
        self.assertEqual(result['status'],'completed');self.assertEqual(compose.await_count,2)
        self.assertEqual(compose.call_args.kwargs['revision_feedback'],rejection['findings'])
        self.assertEqual(result['revision_attempts'],1)
        with (patch.object(authoring.grounded_drafting,'compose',new=AsyncMock(return_value=narrative())) as compose,
              patch.object(authoring,'run_local_verification_batched',new=AsyncMock(return_value={'passed':False,'status':'unavailable','error':{'code':'MODEL_TIMEOUT'}}))):
            result=await authoring.prepare(case_data())
        self.assertEqual(compose.await_count,1);self.assertEqual(result['status'],'needs_review')

    async def test_old_or_stale_writing_contract_cannot_reuse_a_verified_draft(self):
        case=case_data()
        case['drafts']=[{'id':'draft','input_revision':1,'ai_review':{'passed':True},'narrative_id':'old',
            'sections':[{'id':'statement','content':'이전 한 문장','grounded_paragraphs':narrative()['sections'][0]['paragraphs']}]}]
        case['narrative_runs']=[{'id':'old','version':'local-grounded-prose-v2','verification':{'passed':True},'input_signature':'old'}]
        self.assertIsNone(authoring.current_verified_draft(case))
        case['narrative_runs'][0]['version']=authoring.grounded_drafting.VERSION
        with patch.object(authoring.grounded_drafting,'signature',return_value='new'):
            self.assertIsNone(authoring.current_verified_draft(case))


class ManualStatementRouteTests(unittest.TestCase):
    def setUp(self):
        self.case=case_data();self.actions=[]
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        path=patch.object(extended_routes,'_artifact_path',side_effect=lambda cid,did:Path(self.folder.name)/(did+'.pdf'))
        path.start();self.addCleanup(path.stop)
        self.app=FastAPI()
        user={'id':'staff','name':'staff','role':'staff'}
        def change(case_id,user,version,action,fn):
            self.assertEqual(version,self.case['version'])
            fn(self.case);self.case['version']+=1;self.actions.append(action)
            return copy.deepcopy(self.case)
        extended_routes.attach(self.app,lambda:user,lambda cid,usr:copy.deepcopy(self.case),change)
        self.client=TestClient(self.app)

    def test_failure_records_review_notification_and_409_without_pdf(self):
        failure={'status':'needs_review','code':'STATEMENT_AUTHORING_REQUIRED','message':'원문 근거를 검토해주세요.',
                 'verification':{'passed':False},'input_revision':1,'statement':None}
        with patch.object(authoring,'prepare',new=AsyncMock(return_value=failure)) as prepare:
            response=self.client.post('/api/cases/synthetic-statement/court-documents',json={
                'expected_version':1,'template_id':'D5105','fields':{'statement':'  '}})
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json()['detail']['case_version'],2)
        self.assertEqual(self.case['notifications'][0]['kind'],'review_request')
        self.assertFalse(list(Path(self.folder.name).glob('*.pdf')))
        prepare.assert_awaited_once()

    def test_intentional_manual_text_retains_review_status_without_automatic_authoring(self):
        with patch.object(authoring,'prepare',new=AsyncMock()) as prepare:
            response=self.client.post('/api/cases/synthetic-statement/court-documents',json={
                'expected_version':1,'template_id':'D5105','fields':{'statement':'직원이 검토할 진술 본문입니다.'}})
        self.assertEqual(response.status_code,200,response.text)
        prepare.assert_not_awaited()
        document=self.case['court_documents'][0]
        self.assertEqual(document['statement_source']['status'],'review_required')
        self.assertFalse(document['ai_review']['passed'])

    def test_success_creates_a_new_pdf_from_verified_prose_and_preserves_old_file(self):
        old=Path(self.folder.name)/'old.pdf';old.write_bytes(b'old immutable version')
        paragraphs=narrative()['sections'][0]['paragraphs']
        result={'status':'completed','statement':paragraphs[0]['text'],'paragraphs':paragraphs,
                'narrative':narrative(),'verification':{'passed':True,'status':'passed'},
                'input_revision':1,'source_signature':'current','calculation_id':None,'external_processing':False}
        with (patch.object(authoring,'prepare',new=AsyncMock(return_value=result)),
              patch.object(authoring,'source_signature',return_value='current')):
            response=self.client.post('/api/cases/synthetic-statement/court-documents',json={
                'expected_version':1,'template_id':'D5105','fields':{}})
        self.assertEqual(response.status_code,200,response.text)
        document=self.case['court_documents'][0]
        self.assertTrue(document['statement_review']['passed'])
        self.assertFalse(document['ai_review']['passed'])
        self.assertEqual(document['statement_source']['type'],'grounded_narrative')
        self.assertEqual(old.read_bytes(),b'old immutable version')
        self.assertTrue((Path(self.folder.name)/(document['id']+'.pdf')).is_file())


if __name__=='__main__':unittest.main()
