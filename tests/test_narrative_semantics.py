"""Independent numeric-meaning regressions, with no model or live case access.

The captured bad paragraph is a synthetic local-model output, transcribed here
so the regression does not depend on an ignored .work artifact. Pure checker
tests verify only labelled amount/role/period binding, not general model quality.
"""
import json
import unittest
from unittest.mock import AsyncMock,patch

from apps.api.narrative_claims import money_value, validate_numeric_roles
from apps.api import grounded_drafting,model_client


def source(quote, key='debt_history', **extra):
    return {'kind':'party_statement','key':key,'quote':quote,**extra}


class NarrativeMeaningTests(unittest.TestCase):
    def test_captured_asset_amount_cannot_be_relabelled_as_living_expenses(self):
        body='2022~2023년 불규칙한 소득으로 부족한 생활비를 차용함. 당시 생활비는 2,400,000원으로 자산 합계 2,400,000원임.'
        cited=[source('2022~2023년 불규칙한 소득으로 부족한 생활비를 차용함'),
               source('자산 합계 2,400,000원이며 부동산·자동차·보험·임차보증금은 없습니다.')]
        checks=validate_numeric_roles(body,cited)
        self.assertEqual(len(checks),1)
        self.assertEqual(checks[0]['code'],'NARRATIVE_NUMERIC_ROLE_MISMATCH')
        self.assertEqual(checks[0]['claim_label'],'생활비')
        self.assertFalse(validate_numeric_roles('자산 합계는 240만원입니다.',cited))

    def test_living_expense_and_asset_each_bind_their_own_amount(self):
        rows=[source('월 생활지출: 1,600,000원 / 자산 합계: 2,400,000원')]
        self.assertFalse(validate_numeric_roles('월 생활비는 160만원이며 자산 합계는 240만원입니다.',rows))
        self.assertTrue(validate_numeric_roles('월 생활비는 240만원이며 자산 합계는 160만원입니다.',rows))

    def test_monthly_net_annual_gross_and_annual_deductions_remain_distinct(self):
        rows=[source('월 실수령액: 3,200,000원','income_net',frequency='monthly'),
              source('직전 12개월 총급여: 42,600,000원','income_gross',frequency='annual'),
              source('직전 12개월 세금 및 사회보험료 공제 합계: 4,200,000원','income_deductions',frequency='annual')]
        body='월 실수령 소득은 320만원입니다. 직전 12개월 총급여는 4,260만원이며 공제 합계는 420만원입니다.'
        self.assertEqual(validate_numeric_roles(body,rows),[])
        for bad in ['월 실수령 소득은 4,260만원입니다.','직전 12개월 총급여는 320만원입니다.','직전 12개월 공제 합계는 4,260만원입니다.']:
            with self.subTest(bad=bad):self.assertTrue(validate_numeric_roles(bad,rows))

    def test_same_number_in_wrong_period_does_not_pass(self):
        rows=[source('직전 12개월 총급여: 42,600,000원','income_gross',frequency='annual')]
        checks=validate_numeric_roles('월 총급여는 42,600,000원입니다.',rows)
        self.assertEqual(checks[0]['code'],'NARRATIVE_NUMERIC_PERIOD_MISMATCH')

    def test_net_and_gross_do_not_share_a_bag_of_numbers(self):
        rows=[source('월 총급여: 3,550,000원 / 월 공제액: 350,000원 / 월 실수령액: 3,200,000원')]
        self.assertFalse(validate_numeric_roles('월 총급여는 355만원, 공제액은 35만원, 실수령액은 320만원입니다.',rows))
        self.assertTrue(validate_numeric_roles('월 실수령액은 355만원입니다.',rows))
        self.assertTrue(validate_numeric_roles('월 공제액은 320만원입니다.',rows))

    def test_display_units_are_converted_exactly_without_period_arithmetic(self):
        rows=[source('월 실수령액: 3,200,000원','income_net',frequency='monthly')]
        for spelling in ['320만원','3,200천원','3.2백만원','0.032억원','3,200,000원']:
            with self.subTest(spelling=spelling):
                self.assertEqual(validate_numeric_roles('월 실수령액은 '+spelling+'입니다.',rows),[])
        self.assertEqual(money_value('3천550만원'),35500000)
        self.assertEqual(money_value('1억 2,400만원'),124000000)
        self.assertEqual(money_value('3천5백만원'),35000000)
        self.assertIsNone(money_value('0.00001만원'))

    def test_annual_to_monthly_division_requires_an_actual_calculation_source(self):
        rows=[source('직전 12개월 실수령 합계: 38,400,000원','income_net',frequency='annual')]
        self.assertTrue(validate_numeric_roles('월 실수령액은 320만원입니다.',rows))
        rows.append(source('월 실수령액: 3,200,000원','income_net',kind='code_calculation',frequency='monthly',verified_numeric_values=[3200000]))
        self.assertFalse(validate_numeric_roles('월 실수령액은 320만원입니다.',rows))

    def test_number_witness_metadata_cannot_override_the_quoted_role(self):
        rows=[source('자산 합계: 2,400,000원','living_expenses',verified_numeric_values=[2400000])]
        self.assertTrue(validate_numeric_roles('생활비는 240만원입니다.',rows))

    def test_other_excerpt_from_the_same_document_does_not_support_amount(self):
        # Caller supplies the cited excerpt, not the entire consultation body.
        rows=[source('현재 정규직으로 재직 중입니다.','income_net',frequency='monthly',verified_numeric_values=[3200000])]
        self.assertTrue(validate_numeric_roles('월 실수령액은 320만원입니다.',rows))

    def test_unlabelled_amount_requires_review_and_date_is_not_money(self):
        rows=[source('자산 합계: 2,400,000원')]
        self.assertEqual(validate_numeric_roles('당시 240만원이었습니다.',rows)[0]['code'],'NARRATIVE_NUMERIC_ROLE_REQUIRED')
        self.assertFalse(validate_numeric_roles('2024년 3월부터 재직했고 가구원은 1명입니다.',[]))

    def test_public_rule_amount_is_not_a_customer_fact(self):
        rows=[source('자산 합계: 2,400,000원','assets_total',kind='public_legal_source')]
        self.assertTrue(validate_numeric_roles('자산 합계는 240만원입니다.',rows))

    def test_table_unit_and_reversed_amount_phrase(self):
        rows=[source('단위: 천원\n월 실수령액: 3,200','income_net',frequency='monthly')]
        self.assertFalse(validate_numeric_roles('월 실수령액은 320만원입니다.',rows))
        self.assertFalse(validate_numeric_roles('월 320만원의 실수령 소득입니다.',rows))

    def test_attributed_unit_heading_allowed_but_numeric_multiplier_alone_is_not(self):
        row=source('월 실수령액: 3,200','income_net',frequency='monthly',unit_multiplier=1000)
        self.assertTrue(validate_numeric_roles('월 실수령액은 320만원입니다.',[row]))
        row['source_unit_quote']='단위: 천원'
        self.assertFalse(validate_numeric_roles('월 실수령액은 320만원입니다.',[row]))

    def test_only_known_scalar_calculation_keys_can_support_a_narrative_amount(self):
        rows=[source('"monthly_creditor_capacity": 1000000',kind='code_calculation')]
        self.assertFalse(validate_numeric_roles('월 변제재원은 100만원입니다.',rows))
        self.assertTrue(validate_numeric_roles('월 생활비는 100만원입니다.',rows))
        self.assertTrue(validate_numeric_roles('월 변제재원은 100만원입니다.',[source('"unrelated": 1000000',kind='code_calculation')]))


class NarrativeMeaningIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Scripted model/public-source adapter tests wiring, not their quality."""
    def setUp(self):
        legal=patch.object(grounded_drafting,'_trusted_legal',return_value={
            'id':'law:synthetic-guidance','kind':'public_legal_source','text':'사건의 확인된 사실과 변제 재원을 설명한다.',
            'source_sha256':'synthetic-adapter-only'})
        legal.start();self.addCleanup(legal.stop)
    @staticmethod
    def facts():
        return [
            {'id':'history','key':'debt_history','value':'불규칙한 소득과 부족한 생활비로 차용함',
             'quote':'2022~2023년 불규칙한 소득으로 부족한 생활비를 차용함. 차용을 반복함. 자산 합계 2,400,000원임.',
             'source_ids':['synthetic-history']},
            {'id':'income','key':'income_net','value':3200000,'frequency':'monthly','basis':'net',
             'quote':'월 실수령액: 3,200,000원','source_ids':['synthetic-payroll']},
        ]

    @staticmethod
    def reply(text,evidence_id):
        return {'done':True,'done_reason':'stop','message':{'content':json.dumps({'text':text,'evidence_ids':[evidence_id]},ensure_ascii=False)}}

    async def test_actual_bad_role_is_blocked_even_when_every_number_has_a_citation(self):
        def wrong(messages,schema,**kwargs):
            request=json.loads(messages[1]['content'])
            identifier=next(item['id'] for item in request['evidence'] if item.get('key')=='debt_history')
            return self.reply('2022~2023년 불규칙한 소득으로 부족한 생활비를 차용함. 당시 생활비는 2,400,000원으로 자산 합계 2,400,000원임.',identifier)
        with patch.object(model_client,'generate',new=AsyncMock(side_effect=wrong)) as generate:
            result=await grounded_drafting.compose(self.facts(),[{'id':'synthetic-guidance'}],[],{},section_ids=['statement'])
        self.assertEqual(generate.await_count,2)  # Initial attempt + one bounded repair.
        self.assertEqual(result['status'],'needs_review')
        self.assertFalse(result['verification']['passed'])
        self.assertEqual(result['sections'],[])
        self.assertIn('NARRATIVE_NUMERIC_ROLE_MISMATCH',{row['code'] for row in result['verification']['checks']})

    async def test_correctly_bound_monthly_net_can_pass_the_same_wiring(self):
        def correct(messages,schema,**kwargs):
            request=json.loads(messages[1]['content'])
            key='income_net' if request['topic']=='repayment_basis' else 'debt_history'
            identifier=next(item['id'] for item in request['evidence'] if item.get('key')==key)
            text='현재 월 실수령 소득은 320만원입니다.' if key=='income_net' else '생활비 부족으로 차용을 반복했습니다.'
            return self.reply(text,identifier)
        with patch.object(model_client,'generate',new=AsyncMock(side_effect=correct)) as generate:
            result=await grounded_drafting.compose(self.facts(),[{'id':'synthetic-guidance'}],[],{},section_ids=['statement'])
        self.assertEqual(generate.await_count,3)
        self.assertEqual(result['status'],'completed')
        self.assertTrue(result['verification']['downstream_semantic_review_required'])


if __name__=='__main__':unittest.main()
