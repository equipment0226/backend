"""Dashboard percentages measure their stated denominator, never court odds."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from apps.api import (ax_engine, case_assessment, court_forms, domain, evidence_mapping,
                      extraction_readiness, legal_calculator)

ROOT = Path(__file__).resolve().parents[1]


def case_with_text(text='성명: 합성 확인인\n급여명세서\n고용형태: 급여소득자\n직장명: 확인회사\n월 실수령액: 2,800,000원', status='verified'):
    case = domain.new_case('합성 확인인', 'CT01', '서울회생법원', '격리된 검증', True)
    case['intake'] = {'status': 'completed'}
    document = {'id': 'document-one', 'status': status, 'version': 1, 'sha256': 'synthetic',
                'filename': '가상 증빙.txt', 'text': text, 'page_texts': [{'page': 1, 'text': text}]}
    case['documents'] = [document]
    case['extraction_candidates'] = ax_engine.extract_factor_candidates(ax_engine.case_sources(case))
    extraction_readiness.capture(case, document)
    return case


def with_calculation(case, modify=None):
    inputs = legal_calculator.example_payload()
    inputs['as_of'] = '2026-10-04'
    for row in [inputs['income']] + inputs['assets'] + inputs['creditors']:
        row['evidence_ids'] = ['document-one']
    if modify:
        modify(inputs)
    calculation = legal_calculator.calculate_legal(case, inputs)
    case['legal_calculations'] = [calculation]
    return calculation


def rule(result, key):
    return next(row for row in result['rules'] if row['id'] == key)


class AssessmentTests(unittest.TestCase):
    def test_empty_intake_is_not_a_failed_legal_case_or_an_approval_percentage(self):
        case = domain.new_case('테스트', 'CT01', '서울회생법원', '신규', True)
        case['intake'] = {'status': 'received'}
        result = case_assessment.build(case)
        self.assertEqual(result['metrics']['required_fields']['source']['covered'], 0)
        self.assertGreater(result['metrics']['required_fields']['source']['total'], 0)
        self.assertIsNone(result['metrics']['documents']['percentage'])
        self.assertEqual(result['metrics']['risks']['confirmed'], 0)
        self.assertEqual(result['metrics']['rules']['met'], 0)
        self.assertEqual(result['actions'][0]['target']['tab'], 'consultation')
        self.assertEqual(result['prediction']['status'], 'not_validated')
        self.assertIsNone(result['prediction']['percentage'])

    def test_completed_extraction_shows_progress_before_unified_review(self):
        case = case_with_text(status='received')
        result = case_assessment.build(case)
        metrics = result['metrics']['required_fields']['source']
        self.assertGreater(metrics['covered'], 0)
        self.assertEqual(metrics['verified'], 0)
        self.assertEqual(result['metrics']['documents']['verified'], 0)
        self.assertEqual(result['metrics']['rules']['met'], 0)
        self.assertTrue(any(field['status'] == 'extracted' for form in result['forms'] for field in form['fields']))
        self.assertEqual(result['reasoning_features']['numeric_facts'], {})

    def test_verification_changes_verified_coverage_not_extraction_ratio(self):
        case = case_with_text(status='received')
        first = case_assessment.build(case)
        case['documents'][0]['status'] = 'verified'
        second = case_assessment.build(case)
        for name in ('total', 'covered', 'percentage'):
            self.assertEqual(first['metrics']['required_fields']['source'][name], second['metrics']['required_fields']['source'][name])
        self.assertGreater(second['metrics']['required_fields']['source']['verified'], 0)

    def test_received_incomplete_or_identity_conflicting_pages_do_not_count(self):
        for change in ({'extraction_status': 'running'}, {'extraction_status': 'partial'},
                       {'automated_check': {'coverage_status': 'identity_conflict'}}, {'status': 'rejected'},
                       {'status': 'quarantined'}, {'status': 'superseded'}):
            with self.subTest(change=change):
                case = case_with_text()
                case['documents'][0].update(change)
                result = case_assessment.build(case)
                self.assertEqual(result['metrics']['required_fields']['source']['covered'], 0)

    def test_page_text_edit_invalidates_old_completion_receipt(self):
        case = case_with_text()
        case['documents'][0]['page_texts'][0]['text'] += '\n다른 내용'
        result = case_assessment.build(case)
        self.assertEqual(result['metrics']['required_fields']['source']['covered'], 0)

    def test_case_record_name_confirmed_fact_and_ai_draft_do_not_fake_source_coverage(self):
        case = domain.new_case('기록에만 있는 이름', 'CT01', '서울회생법원', '신규', True)
        case['facts'] = [{'key': 'monthly_income', 'value': 9000000, 'status': 'confirmed', 'evidence_ids': []}]
        case['extraction_candidates'] = [{'id': 'forged', 'key': 'monthly_income', 'value': 9000000, 'status': 'accepted'}]
        case['drafts'] = [{'input_revision': case['input_revision'], 'status': 'automatically_verified', 'sections': []}]
        self.assertEqual(case_assessment.build(case)['metrics']['required_fields']['source']['covered'], 0)

    def test_same_field_and_unit_conversion_are_counted_once(self):
        case = case_with_text()
        result = case_assessment.build(case)
        all_fields = [field for form in result['forms'] for field in form['fields']]
        unique = {(field['category'], field['metric_key']) for field in all_fields}
        self.assertEqual(result['metrics']['required_fields']['total'], len(unique))
        self.assertGreater(len(all_fields), len(unique))
        source = {field['metric_key'] for field in all_fields if field['category'] == 'source'}
        self.assertNotIn('income_manwon', source)
        self.assertNotIn('household_size', source)
        self.assertNotIn('assets_total', source)
        self.assertNotIn('statement', source)
        for form in result['forms']:
            self.assertEqual(len(form['fields']), len({field['key'] for field in form['fields']}))

    def test_wage_specific_forms_are_deferred_until_income_type_observed(self):
        case = case_with_text('성명: 합성 확인인\n주소: 서울특별시 가상로 3')
        result = case_assessment.build(case)
        self.assertEqual({row['id'] for row in result['deferred_forms']}, {'D5110', 'D5115'})
        self.assertNotIn('D5115', {row['id'] for row in result['forms']})

    def test_zero_from_explicit_no_insurance_counts_but_missing_does_not(self):
        case = case_with_text('성명: 합성 확인인\n보험가입 조회 결과\n보험계약: 없음\n보유 보험: 없음')
        result = case_assessment.build(case)
        field = next(row for form in result['forms'] for row in form['fields'] if row['key'] == 'insurance_surrender')
        self.assertEqual(field['status'], 'covered')
        blank = case_assessment.build(case_with_text('성명: 합성 확인인'))
        missing = next(row for form in blank['forms'] for row in form['fields'] if row['key'] == 'insurance_surrender')
        self.assertEqual(missing['status'], 'missing')

    def test_current_valid_calculation_has_real_thresholds_and_conditional_denominator(self):
        case = case_with_text()
        calculation = with_calculation(case)
        self.assertEqual(calculation['status'], 'ready_for_review')
        result = case_assessment.build(case)
        self.assertEqual(result['freshness']['calculation'], 'current')
        self.assertEqual(rule(result, 'unsecured_debt_limit')['actual'], 78000000)
        self.assertEqual(rule(result, 'unsecured_debt_limit')['threshold'], 1000000000)
        self.assertEqual(rule(result, 'unsecured_debt_limit')['status'], 'met')
        self.assertEqual(rule(result, 'minimum_repayment')['status'], 'not_applicable')
        self.assertEqual(rule(result, 'priority_fully_paid')['status'], 'not_applicable')
        self.assertEqual(rule(result, 'objector_liquidation_floor')['status'], 'not_applicable')
        self.assertEqual(result['metrics']['rules']['percentage'], 100)
        self.assertEqual(result['metrics']['risks']['confirmed'], 0)
        self.assertIsNone(result['prediction']['percentage'])
        self.assertTrue(rule(result, 'liquidation_floor')['source_refs'][0]['url'].startswith('https://www.law.go.kr/'))

    def test_old_revision_stale_policy_or_forged_result_is_not_current(self):
        for mutation in ('revision', 'stale', 'policy', 'summary', 'check', 'source'):
            with self.subTest(mutation=mutation):
                case = case_with_text()
                calculation = with_calculation(case)
                if mutation == 'revision':
                    case['input_revision'] += 1
                elif mutation == 'stale':
                    calculation['stale'] = True
                elif mutation == 'policy':
                    calculation['policy_hash'] = 'old-law'
                elif mutation == 'summary':
                    calculation['summary']['monthly_creditor_capacity'] = 99999999
                elif mutation == 'check':
                    calculation['checks'][0]['passed'] = False
                elif mutation == 'source':
                    case['documents'][0]['text'] += '\n다른 문구'
                result = case_assessment.build(case)
                self.assertNotEqual(result['freshness']['calculation'], 'current')
                self.assertEqual(result['metrics']['rules']['met'], 0)
                self.assertEqual(result['metrics']['risks']['confirmed'], 0)
                self.assertEqual(result['reasoning_features']['calculation'], {})

    def test_numeric_input_errors_never_turn_missing_debt_into_met_limit(self):
        case = case_with_text()
        with_calculation(case, lambda inputs: inputs['creditors'][0].update(principal=None))
        result = case_assessment.build(case)
        self.assertEqual(rule(result, 'unsecured_debt_limit')['status'], 'unknown')
        self.assertEqual(rule(result, 'secured_debt_limit')['status'], 'unknown')
        self.assertEqual(result['metrics']['risks']['confirmed'], 0)

    def test_confirmed_shortfall_has_specific_remedy_and_does_not_suggest_hiding_assets(self):
        case = case_with_text()
        with_calculation(case, lambda inputs: inputs['assets'][0].update(owned_value=200000000))
        result = case_assessment.build(case)
        self.assertEqual(rule(result, 'liquidation_floor')['status'], 'unmet')
        self.assertEqual(result['metrics']['risks']['confirmed'], 1)
        action = next(row for row in result['actions'] if row['id'] == 'liquidation_gap')
        self.assertEqual(action['category'], 'legal')
        self.assertIn('매각대금도 재산', action['summary'])
        self.assertIn('부족액', action['summary'])
        self.assertTrue(action['source_refs'])

    def test_debt_limit_only_counts_once_despite_duplicate_blocker_messages(self):
        case = case_with_text()
        with_calculation(case, lambda inputs: inputs['creditors'][0].update(principal=1200000000))
        result = case_assessment.build(case)
        self.assertEqual(result['metrics']['risks']['confirmed'], 1)
        self.assertEqual(rule(result, 'unsecured_debt_limit')['status'], 'unmet')
        self.assertEqual(sum(row['id'] == 'debt_limit' for row in result['actions']), 1)

    def test_confirmed_period_failure_and_missing_other_inputs_remain_distinct(self):
        case = case_with_text()
        with_calculation(case, lambda inputs: inputs.update(months=48))
        result = case_assessment.build(case)
        self.assertEqual(rule(result, 'repayment_period')['status'], 'unmet')
        self.assertEqual(rule(result, 'repayment_period')['actual'], 48)
        self.assertEqual(rule(result, 'repayment_period')['threshold'], 36)

    def test_withdrawn_and_replaced_documents_do_not_inflate_coverage(self):
        case = case_with_text()
        case['documents'][0]['request_id'] = 'withdrawn'
        case['requests'] = [{'id': 'withdrawn', 'status': 'withdrawn', 'document_ids': ['document-one']}]
        result = case_assessment.build(case)
        self.assertEqual(result['metrics']['documents']['total'], 0)
        self.assertEqual(result['metrics']['required_fields']['source']['covered'], 0)

    def test_fulfilled_label_alone_does_not_complete_a_request(self):
        case = case_with_text(status='received')
        case['requests'] = [{'id': 'request-one', 'status': 'fulfilled', 'document_ids': ['document-one']}]
        result = case_assessment.build(case)
        self.assertEqual(result['metrics']['documents']['requests_complete'], 0)
        case['documents'][0]['status'] = 'verified'
        self.assertEqual(case_assessment.build(case)['metrics']['documents']['requests_complete'], 1)

    def test_other_procedure_has_no_applicable_percentage(self):
        case = case_with_text()
        case['case_type'] = 'bankruptcy'
        result = case_assessment.build(case)
        self.assertIsNone(result['metrics']['required_fields']['source']['percentage'])
        self.assertIsNone(result['metrics']['rules']['percentage'])
        self.assertEqual(result['metrics']['risks']['confirmed'], 0)

    def test_build_does_not_mutate_or_call_document_renderer_or_models(self):
        case = case_with_text(status='received')
        saved = copy.deepcopy(case)
        with patch.object(court_forms, 'preview', side_effect=AssertionError('Renderer must not run in GET')), \
             patch('apps.api.model_client.local_text', create=True, side_effect=AssertionError('No inference')):
            case_assessment.build(case)
        self.assertEqual(case, saved)

    def test_reasoning_features_exclude_names_phones_source_text_and_document_ids(self):
        case = case_with_text('성명: 누출금지개인\n본인 연락처: 010-1111-2222\n급여명세서\n월 실수령액: 2,800,000원')
        result = case_assessment.build(case)
        text = json.dumps(result['reasoning_features'], ensure_ascii=False)
        for forbidden in ('누출금지개인', '010-1111-2222', 'document-one', '성명', 'quote', 'text'):
            self.assertNotIn(forbidden, text)

    def test_pending_law_change_suppresses_only_affected_rule_family(self):
        case = case_with_text()
        with_calculation(case)
        with patch('apps.api.legal_watch.case_policy_status', return_value={'pending_changes': [
                {'source_id': 'LW579', 'affected_source_ids': ['LW579'], 'title': '검토 중인 제579조'}]}):
            result = case_assessment.build(case)
        self.assertEqual(rule(result, 'unsecured_debt_limit')['status'], 'unknown')
        self.assertEqual(rule(result, 'secured_debt_limit')['status'], 'unknown')
        self.assertEqual(rule(result, 'positive_capacity')['status'], 'unknown')
        self.assertEqual(rule(result, 'liquidation_floor')['status'], 'met')
        self.assertEqual(rule(result, 'repayment_period')['status'], 'met')
        self.assertTrue(result['freshness']['policy_review_required'])
        self.assertEqual(result['actions'][0]['id'], 'law_changes')


class RichFixtureAssessmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from scripts.audit_document_extraction import build_case
        cls.case = build_case(ROOT / 'examples/court_ready_fixture')
        for document in cls.case['documents']:
            extraction_readiness.capture(cls.case, document)

    def test_rich_originals_cover_core_source_fields_without_inventing_legal_inputs(self):
        result = case_assessment.build(copy.deepcopy(self.case))
        metrics = result['metrics']['required_fields']
        self.assertEqual(result['metrics']['documents']['verified'], 29)
        self.assertEqual(metrics['source']['covered'], metrics['source']['total'])
        self.assertEqual(metrics['source']['percentage'], 100)
        self.assertEqual(metrics['source']['verified'], metrics['source']['total'])
        self.assertEqual(metrics['legal']['covered'], 0)
        self.assertEqual(result['metrics']['rules']['met'], 2)
        self.assertEqual(rule(result, 'unsecured_debt_limit')['basis'], 'verified_source_arithmetic')
        self.assertEqual(result['metrics']['risks']['confirmed'], 0)
        self.assertEqual(result['reasoning_features']['numeric_facts']['monthly_income'], 3450000)
        self.assertEqual(result['reasoning_features']['numeric_facts']['total_debt'], 69000000)
        self.assertIsNone(result['prediction']['percentage'])

    def test_source_debt_limit_waits_when_another_debt_statement_is_pending(self):
        case = copy.deepcopy(self.case)
        case['requests'] = [{'id': 'missing-creditor', 'catalog_id': 'D38', 'status': 'requested'}]
        result = case_assessment.build(case)
        self.assertEqual(rule(result, 'unsecured_debt_limit')['status'], 'unknown')
        self.assertEqual(rule(result, 'secured_debt_limit')['status'], 'unknown')
        self.assertEqual(result['metrics']['risks']['confirmed'], 0)

    def test_all_observed_creditors_are_in_required_field_scope(self):
        result = case_assessment.build(copy.deepcopy(self.case))
        creditors = next(row for row in result['forms'] if row['id'] == 'D5106')
        keys = {field['key'] for field in creditors['fields']}
        for index in range(3):
            self.assertIn(f'creditors.{index}.name', keys)
            self.assertIn(f'creditors.{index}.principal', keys)
        self.assertNotIn('creditors.3.name', keys)

    def test_rich_fixture_fresh_calculation_advances_legal_coverage_without_probability(self):
        case = copy.deepcopy(self.case)
        packet = evidence_mapping.build(case)
        inputs = copy.deepcopy(packet['inputs'])
        example = json.loads((ROOT / 'examples/court_ready_fixture/legal_review_inputs.json').read_text(encoding='utf-8'))['inputs']
        inputs.update({key: copy.deepcopy(value) for key, value in example.items() if key not in {'income', 'assets', 'creditors'}})
        inputs['as_of'] = '2026-10-04'
        by_id = {row['id']: row for row in example['assets']}
        for asset in inputs['assets']:
            for key in ('secured_deduction', 'exempt_deduction', 'disposal_cost'):
                asset[key] = by_id[asset['id']][key]
        calculation = legal_calculator.calculate_legal(case, inputs)
        self.assertEqual(calculation['status'], 'ready_for_review')
        case['legal_calculations'] = [calculation]
        result = case_assessment.build(case)
        self.assertEqual(result['metrics']['required_fields']['source']['percentage'], 100)
        self.assertGreater(result['metrics']['required_fields']['legal']['covered'], 0)
        self.assertEqual(result['metrics']['rules']['percentage'], 100)
        self.assertIsNone(result['prediction']['percentage'])
        self.assertTrue(any(row['id'] == 'additional_living_evidence' for row in result['actions']))


if __name__ == '__main__':
    unittest.main()
