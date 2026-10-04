import asyncio
import copy
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from apps.api import model_client, verification


def payload(count=6, padding=''):
    sources=[];items=[]
    for index in range(count):
        source='document-original-long-identifier-'+str(index)
        quote=f'월 실수령액 {2800000+index:,}원'
        sources.append({'id':source,'version':3,'text':quote+'\n'+padding})
        items.append({'id':'typed-fact-long-identifier-'+str(index),'key':'income_net',
            'value':2800000+index,'source_ids':[source],'quote':quote,'basis':'net',
            'frequency':'monthly','unit':'KRW','covered_fact_ids':['original-'+str(index)]})
    return {'sources':sources,'items':items,'context':{'scope':'월 실수령액과 원문 전체 대조'}}


def reply(checks):
    return {'done':True,'done_reason':'stop','message':{'content':json.dumps({'checks':checks})},
            'prompt_eval_count':100,'eval_count':150}


def answer(messages,schema,**kwargs):
    sent=json.loads(messages[1]['content'])
    if schema['properties']['checks']['type'] == 'object':
        return reply({str(item['i']): 'match' for item in sent['items']})
    return reply([{'i':item['i'],'v':'supported','e':item['evidence'][0],'r':'match'} for item in sent['items']])


class CompactOcrRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        verification.clear_local_verification_cache()
        self.addCleanup(verification.clear_local_verification_cache)

    async def test_compact_reply_restores_original_ids_quotes_and_checks_every_value(self):
        original=payload()
        with patch.object(model_client,'generate',new=AsyncMock(side_effect=answer)) as generate:
            result=await verification.run_local_verification('ocr',original)
        self.assertTrue(result['passed'])
        self.assertEqual(result['checked_item_ids'],[item['id'] for item in original['items']])
        self.assertEqual([f['quote'] for f in result['findings']],[item['quote'] for item in original['items']])
        args=generate.call_args
        wire=json.loads(args.args[0][1]['content'])
        self.assertEqual([source['text'] for source in wire['sources']],[source['text'] for source in original['sources']])
        self.assertEqual(wire['context'],original['context'])
        self.assertNotIn('typed-fact-long-identifier',args.args[0][1]['content'])
        self.assertNotIn('document-original-long-identifier',args.args[0][1]['content'])
        self.assertEqual(args.kwargs['local_context_tokens'],8192)
        self.assertLessEqual(args.kwargs['max_tokens'],110)
        self.assertEqual(args.args[1]['properties']['checks']['type'], 'object')
        self.assertEqual(result['execution']['eval_count'],150)

    async def test_wrong_item_quote_duplicate_coverage_and_unsupported_number_never_pass(self):
        original=payload(2)
        valid=[{'i':1,'v':'supported','e':'E1','r':'match'},{'i':2,'v':'supported','e':'E2','r':'match'}]
        # Multi-source inputs deliberately retain explicit evidence selection;
        # exercise wrong-citation and duplicate-ID rejection in that protocol.
        multichoice=copy.deepcopy(original)
        for row in multichoice['items']:
            row['source_ids']=[source['id'] for source in multichoice['sources']]
        variants=[([dict(valid[0],e='E2'),valid[1]],multichoice),([valid[0],valid[0]],multichoice)]
        changed=copy.deepcopy(original);changed['items'][0]['value']=9999999
        variants.append(({'1': 'match', '2': 'match'},changed))
        for checks,data in variants:
            with self.subTest(checks=checks),patch.object(model_client,'generate',new=AsyncMock(return_value=reply(checks))):
                result=await verification.run_local_verification('ocr',data)
            self.assertFalse(result['passed'])

    async def test_semantic_mismatch_remains_visible_despite_exact_numeric_witness(self):
        original=payload(1)
        with patch.object(model_client,'generate',new=AsyncMock(return_value=reply({'1':'period'}))):
            result=await verification.run_local_verification('ocr',original)
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'],'needs_review')
        self.assertIn('기간',result['findings'][0]['reason'])

    async def test_indexed_keys_must_cover_every_item_exactly_once(self):
        original = payload(2)
        variants = [reply({'1':'match'}), reply({'1':'match','2':'match','3':'match'}),
                    reply({'1':'match','2':'not_a_verdict'}),
                    {'done':True,'done_reason':'stop','message':{'content':'{"checks":{"1":"match","1":"match","2":"match"}}'}}]
        for response in variants:
            with self.subTest(response=response), patch.object(model_client,'generate',new=AsyncMock(return_value=response)):
                result = await verification.run_local_verification('ocr', original)
            self.assertFalse(result['passed'])
            self.assertEqual(result['checked_item_ids'], [])

    async def test_indexed_uncertainty_is_not_a_confident_mismatch_or_support(self):
        with patch.object(model_client,'generate',new=AsyncMock(return_value=reply({'1':'unclear'}))):
            result = await verification.run_local_verification('ocr', payload(1))
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['findings'][0]['status'], 'uncertain')
        self.assertIsNone(result['findings'][0]['source_id'])
        self.assertEqual(result['findings'][0]['quote'], '')

    async def test_multisource_evidence_keeps_explicit_choice_protocol(self):
        original = payload(1)
        original['sources'].append(dict(original['sources'][0], id='second-original'))
        original['items'][0]['source_ids'].append('second-original')
        with patch.object(model_client,'generate',new=AsyncMock(side_effect=answer)) as generate:
            result = await verification.run_local_verification('ocr', original)
        self.assertTrue(result['passed'])
        self.assertEqual(generate.call_args.args[1]['properties']['checks']['type'], 'array')
        self.assertEqual(result['findings'][0]['source_id'], original['sources'][0]['id'])

    async def test_context_budget_splits_without_losing_any_sources_or_items(self):
        original=payload(6,padding='전체 원문 보존 '*100)
        calls=[]
        async def bounded(messages,schema,**kwargs):
            calls.append((messages,kwargs))
            self.assertLessEqual(sum(len(m['content'].encode('utf-8')) for m in messages)+kwargs['max_tokens']+512,8192)
            return answer(messages,schema,**kwargs)
        with patch.object(model_client,'generate',new=AsyncMock(side_effect=bounded)):
            result=await verification.run_local_verification_batched('ocr',original)
        self.assertTrue(result['passed'])
        self.assertGreater(len(calls),1)
        self.assertEqual(set(result['checked_item_ids']),{item['id'] for item in original['items']})
        sent_text={source['text'] for messages,_ in calls for source in json.loads(messages[1]['content'])['sources']}
        self.assertEqual(sent_text,{source['text'] for source in original['sources']})

    async def test_single_source_too_large_is_explicitly_rejected_without_trimming(self):
        original=payload(1,padding='원문 '*1400)
        with patch.object(model_client,'generate',new=AsyncMock()) as generate:
            result=await verification.run_local_verification('ocr',original)
        generate.assert_not_called()
        self.assertFalse(result['passed'])
        self.assertEqual(result['error']['code'],'VERIFICATION_CONTEXT_REQUIRED')

    async def test_timeout_records_small_request_but_never_coverage_or_pass(self):
        with patch.object(model_client,'generate',new=AsyncMock(side_effect=asyncio.TimeoutError())):
            result=await verification.run_local_verification('ocr',payload())
        self.assertFalse(result['passed'])
        self.assertEqual(result['checked_item_ids'],[])
        self.assertEqual(result['error']['code'],'MODEL_TIMEOUT')
        self.assertEqual(result['execution']['format'],'evidence_selection')
        self.assertLessEqual(result['execution']['output_token_limit'],400)

    async def test_local_transport_receives_bounded_context_without_provider_switch(self):
        real_client=httpx.AsyncClient
        def handler(request):
            self.assertEqual(request.url.host,'127.0.0.1')
            data=json.loads(request.content)
            self.assertEqual(data['options']['num_ctx'],8192)
            self.assertEqual(data['options']['num_predict'],390)
            self.assertTrue(data['options']['use_mmap'])
            self.assertEqual(data['options']['num_batch'],128)
            return httpx.Response(200,json={'done':True,'message':{'content':'{}'}})
        with patch.dict(os.environ,{'OLLAMA_BASE_URL':'http://127.0.0.1:11434','DEBTOFF_MODEL_PROVIDER':'deepseek'}), \
             patch.object(model_client.httpx,'AsyncClient',side_effect=lambda **kwargs:real_client(**kwargs,transport=httpx.MockTransport(handler))):
            result=await model_client.generate([{'role':'user','content':'원문'}],{},task_role='ocr',max_tokens=390,local_context_tokens=8192)
        self.assertFalse(result['external_processing'])

    async def test_raw_local_stages_share_loader_and_context_configuration(self):
        real_client=httpx.AsyncClient
        options=[]
        def handler(request):
            self.assertEqual(request.url.host,'127.0.0.1')
            options.append(json.loads(request.content)['options'])
            return httpx.Response(200,json={'done':True,'message':{'content':'{}'}})
        with patch.dict(os.environ,{'OLLAMA_BASE_URL':'http://127.0.0.1:11434'}), \
             patch.object(model_client.httpx,'AsyncClient',side_effect=lambda **kwargs:real_client(**kwargs,transport=httpx.MockTransport(handler))):
            for role in ['consultation','ocr','document','document_selection','calculation_extraction','grounded_drafting']:
                await model_client.generate([{'role':'user','content':'동일 원문'}],{},task_role=role)
        self.assertEqual(len(options),6)
        self.assertTrue(all(value==options[0] for value in options))

    async def test_all_local_stages_reject_oversize_text_or_schema_before_transport(self):
        for messages,schema in [([{'role':'user','content':'원문 전체 '*1000}],{}),
                                ([{'role':'user','content':'원문'}],{'enum':['긴 원문 인용 '*1000]})]:
            with patch.object(model_client.httpx,'AsyncClient') as transport:
                with self.assertRaises(model_client.ModelClientError) as caught:
                    await model_client.generate(messages,schema,task_role='document')
                self.assertEqual(caught.exception.code,'LOCAL_CONTEXT_LIMIT')
                transport.assert_not_called()


if __name__=='__main__':unittest.main()
