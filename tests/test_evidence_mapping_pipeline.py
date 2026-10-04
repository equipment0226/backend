"""Code-checked observations survive semantic outages without granting approval."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from apps.api import automation, court_forms, domain, evidence_mapping, grounded_drafting
from apps.api import preliminary_drafting, statement_authoring, store, verification


def fixture():
    case = domain.new_case('검증고객', 'CT01', '서울회생법원', '', True)
    texts = {
        'salary': '급여명세서\n성명: 검증고객\n근무처: 검증회사\n급여기간: 2026-07~2026-09\n급여소득자\n월 실수령액: 3,200,000원\n월 세전급여: 3,550,000원\n공제 합계: 350,000원',
        'annual': '근로소득원천징수영수증\n귀속 연도: 2025\n연간 총급여: 42,600,000원\n연간 실수령액: 38,400,000원',
        'bank': '계좌조회\n금융기관: 검증은행\n계좌번호: TEST-ACCOUNT-1\n잔액 기준일: 2026-10-01\n예금잔액: 2,400,000원\n급여외 소득 없음',
        'debt': '부채증명서\n채권자: 검증은행\n대출번호: TEST-LOAN-1\n원금: 69,000,000원 / 이자: 3,000,000원\n담보 없음',
        'home': '주거 확인\n1인 가구\n부양가족 없음\n무상거주\n임차보증금: 0원\n월 주거비: 0원\n소유 부동산 없음',
        'insurance': '보험가입 확인\n보험계약 없음\n해약환급금: 0원',
        'history': '생활비 진술\n예금: 2,400,000원\n실제 월 생활비: 1,600,000원\n부족한 생활비를 차용하여 원리금 상환 부담이 커졌습니다.',
    }
    case['documents'] = [{'id': key, 'status': 'verified', 'text': text, 'sha256': store.digest(text),
        'filename': key + '.txt', 'page_texts': [{'page': 1, 'text': text}], 'version': 1} for key, text in texts.items()]
    case['requests'] = []
    case['extraction_candidates'] = [{'id':'forged','key':'monthly_income','value':99000000,
        'document_id':'salary','quote':'월 실수령액: 3,200,000원','origin':evidence_mapping.VERSION}]
    return case


class EvidenceMappingPipelineTests(unittest.IsolatedAsyncioTestCase):
    def test_all_seven_forms_receive_observations_without_a_legal_approval(self):
        case = fixture()
        packet = evidence_mapping.build(case)
        expected = {'monthly_income':3200000,'annual_income':38400000,'bank_balance':2400000,
                    'assets_total':2400000,'principal_total':69000000,'interest_total':3000000,
                    'total_debt':72000000,'housing_cost':0,'insurance_surrender':0}
        for key,value in expected.items():
            self.assertEqual(packet['form_values'][key],value,key)
        self.assertEqual(packet['form_values']['employment_type'],'급여소득자')
        self.assertIn('2026-07',packet['form_values']['income_period'])
        self.assertIn('원금 69,000,000원',packet['form_values']['creditors'])
        self.assertIn('이자 3,000,000원',packet['form_values']['creditors'])
        self.assertIsNone(packet['inputs']['monthly_trustee_fee'])
        self.assertIsNone(packet['inputs']['recognized_household_size'])
        self.assertIsNone(packet['inputs']['objection'])
        seen = set()
        for template in court_forms.catalog('CT01')['templates']:
            preview = court_forms.preview(case,template['id'])
            self.assertFalse(preview['submission_ready'])
            seen.update(row['id'] for row in preview['source_facts'])
            for field in preview['fields']:
                if field['key'] in expected:
                    self.assertEqual(field['value'],expected[field['key']],template['id']+':'+field['key'])
                if field['key']=='monthly_trustee_fee':
                    self.assertIsNone(field['value'])
                if field['key']=='months':
                    self.assertEqual(field['value'],36)
                    self.assertEqual(field['source']['type'],'proposed_legal_input')
        self.assertEqual(seen,{row['id'] for row in packet['facts']})

    async def test_outage_keeps_known_numbers_but_never_claims_ai_pass(self):
        case = fixture()
        before = copy.deepcopy(case['documents'])
        with tempfile.TemporaryDirectory() as folder, patch.object(store,'DATA_DIR',Path(folder)), \
                patch('apps.api.grounded_drafting.compose',new=AsyncMock()) as writer, \
                patch('apps.api.auto_documents.prepare',return_value=([],[])), \
                patch('apps.api.verification.run_local_verification_batched',new=AsyncMock()) as review:
            draft = await preliminary_drafting.create(case,[{'code':'OCR_VERIFICATION','reason':'시간 초과'}],{'ocr','analysis'},
                local_failure={'status':'unavailable','error':{'code':'MODEL_TIMEOUT'}})
        writer.assert_not_awaited();review.assert_not_awaited()
        self.assertEqual(draft['ai_review']['status'],'unavailable')
        self.assertFalse(draft['ai_review']['passed'])
        self.assertFalse(draft['submission_ready'])
        values = {field['key']:field['value'] for section in draft['sections'] for field in section['fields']}
        self.assertEqual(values['monthly_income'],3200000)
        self.assertEqual(values['total_debt'],72000000)
        self.assertNotIn(99000000,values.values())
        self.assertEqual(case['documents'],before)
        self.assertIsNone(case['structured_data'][-1]['calculation_inputs']['monthly_trustee_fee'])
        narrative=case['narrative_runs'][-1]
        self.assertEqual(narrative['authoring_method'],'source_summary')
        self.assertFalse(narrative['verification']['passed'])
        self.assertTrue(narrative['verification']['source_binding_passed'])
        body=next(section['content'] for section in draft['sections'] if section['id']=='statement')
        self.assertIn('3,200,000원',body)
        self.assertIn('69,000,000원',body)
        self.assertIn('3,000,000원',body)
        self.assertNotIn('생활비는 2,400,000원',body)
        self.assertIsNone(statement_authoring.current_verified_draft(case))

    def test_residence_does_not_invent_registered_address_and_current_employment_is_explicit(self):
        case=fixture()
        case['documents'].append({'id':'residence','status':'verified','text':'무상거주 확인서\n주소: 서울시 검증구 검증로\n입사일: 2024-03-01'})
        packet=evidence_mapping.build(case)
        self.assertNotIn('registered_address',packet['form_values'])
        self.assertNotIn('employment_period',packet['form_values'])
        case['documents'].append({'id':'registry','status':'verified','text':'주민등록표등본\n주소: 서울시 검증구 검증로'})
        case['documents'].append({'id':'employment','status':'verified','text':'재직증명서\n입사일: 2024-03-01\n현재 재직 중입니다.'})
        packet=evidence_mapping.build(case)
        self.assertEqual(packet['form_values']['registered_address'],'서울시 검증구 검증로')
        self.assertEqual(packet['form_values']['employment_period'],'2024-03-01 ~ 현재')

    async def test_typed_mapper_uses_no_model_and_preserves_legal_unknowns(self):
        case = fixture()
        with patch('apps.api.model_client.generate',new=AsyncMock()) as model:
            mapped = await verification.extract_calculation_inputs(automation._sources(case,True),{'court_id':'CT01'})
        model.assert_not_awaited()
        self.assertEqual(mapped['inputs']['income']['monthly_amount'],3200000)
        self.assertTrue(mapped['semantic_verification_required'])
        self.assertNotIn('passed',mapped)
        self.assertIsNone(mapped['inputs']['additional_living_cost'])

    async def test_small_sources_share_bounded_calls_without_cross_source_pass(self):
        payload={'sources':[{'id':f'd{i}','text':f'예금잔액: {100+i}원'} for i in range(13)],
                 'items':[{'id':f'i{i}','key':'cash_balance','value':100+i,'source_ids':[f'd{i}']} for i in range(13)]}
        calls=[]
        async def answer(kind,batch):
            calls.append(batch)
            return {'status':'passed','passed':True,'checked_item_ids':[row['id'] for row in batch['items']],
                    'findings':[],'input_sha256':store.digest(batch),'error':None}
        with patch.object(verification,'_verify_cached_batch',side_effect=answer):
            result=await verification.run_local_verification_batched('ocr',payload)
        self.assertTrue(result['passed'])
        self.assertEqual([len(batch['items']) for batch in calls],[6,6,1])
        self.assertEqual(set(result['checked_item_ids']),{f'i{i}' for i in range(13)})
        for batch in calls:
            self.assertEqual({row['id'] for row in batch['sources']},{sid for row in batch['items'] for sid in row['source_ids']})

    def test_explicit_absence_supports_its_own_zero_only(self):
        self.assertTrue(verification.numeric_witness(0,'부양가족 없음',key='dependent_count'))
        self.assertFalse(verification.numeric_witness(0,'부양가족 없음',key='monthly_income'))

    def test_source_bound_narrative_can_render_pending_review_but_cannot_bypass_manual_action(self):
        case=fixture();case['evidence_mapping']=evidence_mapping.build(case)
        items=automation._verification_items(case,automation._sources(case,True))
        narrative={'id':'n','version':grounded_drafting.VERSION,'verification':{'passed':True},'input_signature':'test-signature'}
        case['narrative_runs']=[narrative]
        draft={'id':'d','input_revision':case['input_revision'],'ai_review':{'passed':False},'narrative_id':'n',
               'narrative_fact_ids':[row['id'] for row in items],
               'narrative_source_signature':case['evidence_mapping']['source_signature'],'narrative_section_ids':['statement']}
        case['drafts']=[draft]
        with patch.object(automation,'_retrieve',return_value=[]),patch.object(automation,'approved_examples',return_value=[]), \
                patch.object(grounded_drafting,'signature',return_value='test-signature'):
            self.assertIsNone(statement_authoring.current_verified_draft(case))
            self.assertIs(statement_authoring.current_verified_draft(case,require_semantic=False),draft)
            case['documents'][0]['page_texts'][0]['text'] += '\n변경된 원문'
            self.assertIsNone(statement_authoring.current_verified_draft(case,require_semantic=False))


if __name__=='__main__':unittest.main()
