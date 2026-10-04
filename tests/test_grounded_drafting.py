import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from apps.api import grounded_drafting as drafting, model_client


def facts():
    return [{'id': 'income', 'key': 'monthly_income', 'value': 2800000,
             'source_ids': ['original-payroll'], 'quote': '월 소득 280만원'},
            {'id': 'history', 'key': 'debt_reason', 'value': '생활비 부족으로 차용을 반복함',
             'source_ids': ['debtor-history'], 'quote': '생활비 부족으로 차용을 반복하였습니다.'}]


def law():
    return {'id': 'law:official', 'kind': 'public_legal_source',
            'text': '변제계획은 수행 가능해야 한다.', 'url': 'https://www.law.go.kr/official', 'source_sha256': 'verified'}


def reply(text='월 소득은 280만원입니다.', evidence_ids=None):
    return {'done': True, 'done_reason': 'stop', 'message': {'content': json.dumps({
        'text': text, 'evidence_ids': evidence_ids or ['e1']}, ensure_ascii=False)}}


def reply_for_section(messages, schema, **kwargs):
    request = json.loads(messages[1]['content'])
    income = next((item['id'] for item in request['evidence'] if item.get('key') == 'monthly_income'), None)
    history = next((item['id'] for item in request['evidence'] if item.get('key') in {'debt_reason', 'debt_history'}), None)
    legal = next((item['id'] for item in request['evidence'] if item['kind'] == 'public_legal_source'), None)
    calculation = next((item['id'] for item in request['evidence'] if item['kind'] == 'code_calculation'), None)
    if request['section'] == 'strategy':
        return reply('월 소득은 280만원이며 변제계획은 수행 가능해야 합니다.', [income, legal])
    if request['section'] == 'repayment_plan':
        return reply('월 변제재원은 100만원입니다.', [calculation])
    if request['topic'] == 'debt_origin':
        return reply('저는 부족한 생활비를 충당하기 위해 돈을 빌렸습니다.', [history])
    if request['topic'] == 'debt_progression':
        return reply('생활비 부족이 이어지면서 차용을 반복하게 되었습니다.', [history])
    return reply('현재 월 소득은 280만원입니다.', [income])


class GroundedDraftingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        source = patch.object(drafting, '_trusted_legal', return_value=law())
        source.start()
        self.addCleanup(source.stop)

    async def test_local_grounded_sentence_has_resolvable_original_evidence(self):
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=reply_for_section)) as generate:
            result = await drafting.compose(facts(), [{'id': 'official'}], [], {'monthly_creditor_capacity': 1000000}, court_id='CT01', org_id='office')
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['verification']['passed'])
        self.assertTrue(result['verification']['downstream_semantic_review_required'])
        self.assertEqual(generate.call_args.kwargs['task_role'], 'grounded_drafting')
        self.assertEqual(generate.await_count, 5)
        self.assertTrue(all(call.kwargs['max_tokens'] <= 650 for call in generate.call_args_list))
        self.assertFalse(result['external_processing'])
        source = next(source for source in result['source_refs'] if source['id'] == 'fact:income')
        self.assertEqual(source['original_source_ids'], ['original-payroll'])
        self.assertEqual(len(result['sections'][0]['paragraphs']), 3)
        self.assertEqual(result['sections'][0]['paragraphs'][0]['quotes'][0]['quote'], facts()[1]['quote'])
        self.assertEqual(result['input_signature'], drafting.signature(facts(), [{'id': 'official'}], [], {'monthly_creditor_capacity': 1000000}, court_id='CT01', org_id='office'))

    async def test_unknown_evidence_invented_numbers_and_unbacked_sentence_fail(self):
        for output in (reply(evidence_ids=['unknown']), reply(text='월 소득은 999만원입니다.'),
                       reply(text='월 소득은 280만원입니다. 인가는 확정됩니다.'),
                       reply(evidence_ids=['other-case'])):
            with patch.object(model_client, 'generate', new=AsyncMock(return_value=output)):
                result = await drafting.compose(facts(), [{}], [], {})
            self.assertEqual(result['status'], 'needs_review')
            self.assertFalse(result['sections'])

    async def test_missing_fact_quote_or_uncollected_law_does_not_call_model(self):
        broken = facts()
        broken[0]['quote'] = ''
        with patch.object(model_client, 'generate', new=AsyncMock()) as generate:
            bad_fact = await drafting.compose(broken, [{}], [], {})
            with patch.object(drafting, '_trusted_legal', return_value=None):
                bad_law = await drafting.compose(facts(), [{'text': 'unverified caller law'}], [], {})
        generate.assert_not_awaited()
        self.assertEqual(bad_fact['status'], 'needs_review')
        self.assertEqual(bad_law['verification']['code'], 'CURRENT_PUBLIC_LAW_REQUIRED')

    async def test_only_verified_same_scope_approved_structures_reused_without_customer_text(self):
        base = {'id': 'one', 'org_id': 'office', 'court_id': 'CT01', 'outcome': 'approved', 'evidence_verified': True,
                'synthetic': False, 'features': {'field_presence': {'monthly_income': True}},
                'generated_snapshot': {'sections': [{'id': 'statement', 'content': 'OTHER_CUSTOMER_PRIVATE_STORY'}]}}
        examples = [base, {**base, 'org_id': 'other-office'}, {**base, 'court_id': 'CT06'},
                    {**base, 'outcome': 'remanded'}, {**base, 'synthetic': True}, {**base, 'evidence_verified': False}]
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=reply_for_section)) as generate:
            result = await drafting.compose(facts(), [{}], examples, {}, court_id='CT01', org_id='office')
        self.assertEqual(result['approved_example_count'], 1)
        self.assertNotIn('OTHER_CUSTOMER_PRIVATE_STORY', json.dumps([call.args[0] for call in generate.call_args_list]))
        self.assertEqual(result['status'], 'completed')

    async def test_strategy_sentence_requires_case_and_law_sources(self):
        def without_law(messages, schema, **kwargs):
            request = json.loads(messages[1]['content'])
            if request['section'] == 'strategy':
                return reply(evidence_ids=[next(item['id'] for item in request['evidence'] if item.get('key') == 'monthly_income')])
            return reply_for_section(messages, schema, **kwargs)
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=without_law)):
            result = await drafting.compose(facts(), [{}], [], {})
        self.assertEqual(result['status'], 'needs_review')
        self.assertIn('LEGAL_SOURCE_REQUIRED', {check['code'] for check in result['verification']['checks']})

    async def test_truncated_model_response_cannot_be_used_as_prose(self):
        output = reply()
        output['done_reason'] = 'length'
        with patch.object(model_client, 'generate', new=AsyncMock(return_value=output)):
            result = await drafting.compose(facts(), [{}], [], {})
        self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(result['sections'])

    async def test_missing_debt_history_requests_information_without_inventing_story(self):
        with patch.object(model_client, 'generate', new=AsyncMock()) as generate:
            result = await drafting.compose(facts()[:1], [{}], [], {})
        generate.assert_not_awaited()
        self.assertEqual(result['verification']['code'], 'STATEMENT_INFORMATION_REQUIRED')

    async def test_consultation_is_local_attributed_data_never_accepted_as_pasted_body(self):
        consultation = {'source_id': 'consultation:notes', 'text':
            '가상 사례: 생활비 부족으로 차용을 반복하였습니다. 자금의 실제 사용처와 차입 경위는 제출한 상담 진술에 정리되어 있습니다. 실제사건이 아닙니다.'}
        with patch.object(model_client, 'generate', new=AsyncMock(return_value=reply(consultation['text'], ['e1']))) as generate:
            result = await drafting.compose(facts()[:1], [{}], [], {}, consultation=consultation)
        self.assertEqual(result['status'], 'needs_review')
        self.assertIn('CONSULTATION_METADATA_IN_BODY', {check['code'] for check in result['verification']['checks']})
        self.assertTrue(all(call.kwargs['task_role'] == 'grounded_drafting' for call in generate.call_args_list))

    async def test_public_and_approved_guidance_reaches_each_statement_without_other_client_prose(self):
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=reply_for_section)) as generate:
            result = await drafting.compose(facts(), [{}], [], {})
        self.assertEqual(result['status'], 'completed')
        statements = [json.loads(call.args[0][1]['content']) for call in generate.call_args_list
                      if json.loads(call.args[0][1]['content'])['section'] == 'statement']
        self.assertEqual([item['topic'] for item in statements], ['debt_origin', 'debt_progression', 'repayment_basis'])
        self.assertTrue(all(item['public_writing_guidance'] for item in statements))
        self.assertNotIn('previous_paragraphs', statements[-1])

    async def test_machine_citation_markers_do_not_become_fake_financial_numbers(self):
        def marked(messages, schema, **kwargs):
            result = reply_for_section(messages, schema, **kwargs)
            value = json.loads(result['message']['content'])
            value['text'] += ' (' + ', '.join(value['evidence_ids']) + ').'
            result['message']['content'] = json.dumps(value, ensure_ascii=False)
            return result
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=marked)):
            result = await drafting.compose(facts(), [{}], [], {})
        self.assertEqual(result['status'], 'completed')
        body = ' '.join(p['text'] for s in result['sections'] for p in s['paragraphs'])
        self.assertNotIn('(e', body)
        self.assertIn('280만원', body)

    async def test_model_role_or_english_explanation_cannot_enter_court_body(self):
        def contaminated(messages, schema, **kwargs):
            result = reply_for_section(messages, schema, **kwargs)
            value = json.loads(result['message']['content'])
            value['text'] += ' assistantassistant Here is JSON output.'
            result['message']['content'] = json.dumps(value, ensure_ascii=False)
            return result
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=contaminated)):
            result = await drafting.compose(facts(), [{}], [], {})
        self.assertEqual(result['status'], 'needs_review')
        self.assertIn('MODEL_FORMATTING_IN_BODY', {item['code'] for item in result['verification']['checks']})

    def test_simulator_annotations_removed_from_prompt_but_exact_original_quotes_preserved(self):
        notes = '가상 사례: 생활비 부족으로 차용을 반복했습니다. 금융채무 7800만원을 이 테스트 계산에 반영합니다. 실제사건이 아닙니다.'
        context, _ = drafting.build_context(facts(), [{}], [], {}, consultation={'text': notes})
        plan = drafting._section_plan(context)
        quotations = [item['quote'] for _, _, items in plan for item in items if item['kind'] == 'party_statement']
        self.assertTrue(quotations)
        self.assertTrue(all(quote in notes for quote in quotations))
        self.assertFalse(any('테스트' in quote or '가상 사례' in quote or '아닙니다' in quote for quote in quotations))
        self.assertEqual(next(source['text'] for source in context['sources'] if source['kind'] == 'party_statement'), notes)


if __name__ == '__main__':
    unittest.main()
