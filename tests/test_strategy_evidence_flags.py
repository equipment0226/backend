"""Externally visible evidence receipts contain no identities or source prose."""
from copy import deepcopy
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from apps.api import (automation, ax_engine, evidence_mapping, extraction_readiness,
                      source_review, store, strategy_context, verification)
from tests.test_evidence_mapping_pipeline import fixture


def reviewed_case():
    case = fixture()
    case['extraction_candidates'] = ax_engine.extract_factor_candidates(ax_engine.case_sources(case))
    for document in case['documents']:
        extraction_readiness.capture(case, document)
        source_review.apply_document(case, document, SimpleNamespace(
            scope_confirmed=True, content_confirmed=True, person_confirmed=True, reason='원본·추출값 통합 검토'),
            {'id': 'private-reviewer-id', 'name': 'PRIVATE_REVIEWER_NAME'})
    return case


def mark_current_automatic_ocr(case):
    # Exercise the production scope-review receipt, with only the local model
    # response stubbed. Its signature must still bind the actual document set.
    case['requests'] = []
    for document in case['documents']:
        request_id = 'request-' + document['id']
        document['request_id'] = request_id
        case['requests'].append({'id': request_id, 'catalog_id': 'D07', 'title': '원본 검토',
                                 'status': 'received', 'document_ids': [document['id']]})
    async def supported(kind, payload, **kwargs):
        return {'passed': True, 'status': 'passed', 'checked_item_ids': [item['id'] for item in payload['items']],
                'findings': [{'item_id': item['id'], 'status': 'supported'} for item in payload['items']]}
    with patch('apps.api.verification.run_local_verification_batched', side_effect=supported):
        asyncio.run(automation._validate_requests(case, []))
    packet = evidence_mapping.build(case)
    sources = automation._sources(case, verified_only=True, packet=packet)
    items = automation._verification_items(case, sources, packet=packet)
    case['verification_runs'] = [{'kind': 'ocr', 'version': verification.VERSION,
        'signature': store.digest([sources, items, automation.VERSION]),
        'passed': True, 'status': 'passed', 'checked_item_ids': sorted(item['id'] for item in items)}]


class StrategyEvidenceFlagTests(unittest.TestCase):
    def test_manual_current_source_review_produces_specific_flags_without_mutation(self):
        case = reviewed_case()
        before = deepcopy(case)
        features = strategy_context.build(case)
        for flag in ('income_evidence_verified', 'debt_evidence_verified', 'bank_balance_evidence_verified',
                     'housing_evidence_verified', 'living_expenses_evidence_verified', 'insurance_evidence_verified'):
            self.assertTrue(features[flag], flag)
        self.assertFalse(features['retirement_evidence_verified'])
        self.assertEqual(case, before)

    def test_received_or_incomplete_source_cannot_be_called_verified(self):
        for change in ({'status': 'received'}, {'extraction_status': 'running'},
                       {'ocr_summary': {'page_count': 2}}, {'status': 'superseded'},
                       {'automated_check': {'coverage_status': 'identity_conflict'}}):
            case = reviewed_case()
            case['documents'][0].update(change)
            self.assertFalse(strategy_context.build(case)['income_evidence_verified'], change)

    def test_old_review_signature_or_changed_original_is_not_current(self):
        for change in ({'version': 2}, {'text': '급여명세서\n월 실수령액: 5,000,000원'},
                       {'document_metadata': {'institution': '변경기관'}}):
            case = reviewed_case()
            document = case['documents'][0]
            document.update(change)
            # Even after re-extraction, the old review cannot approve new content.
            case['extraction_candidates'] = ax_engine.extract_factor_candidates(ax_engine.case_sources(case))
            extraction_readiness.capture(case, document)
            self.assertFalse(strategy_context.build(case)['income_evidence_verified'], change)

    def test_withdrawn_request_and_replaced_original_do_not_promote_receipt(self):
        for request in ({'id': 'income-request', 'status': 'withdrawn'},
                        {'id': 'income-request', 'status': 'fulfilled', 'document_ids': []}):
            case = reviewed_case()
            case['documents'][0]['request_id'] = 'income-request'
            case['requests'] = [request]
            self.assertFalse(strategy_context.build(case)['income_evidence_verified'])

    def test_arbitrary_case_facts_input_flags_and_forged_packet_cannot_promote_receipts(self):
        case = fixture()
        case['facts'] = [{'key': flag, 'value': True, 'status': 'confirmed', 'evidence_ids': ['salary']}
                         for flag in verification.EVIDENCE_FEATURE_FLAGS]
        calculation = {'inputs': {flag: True for flag in verification.EVIDENCE_FEATURE_FLAGS}}
        packet = {'form_values': {'monthly_income': 3450000},
                  'origins': {'monthly_income': {'source_ids': ['salary'], 'status': 'verified'}}}
        flags = strategy_context.build(case, calculation, packet)
        self.assertTrue(all(flags[flag] is False for flag in verification.EVIDENCE_FEATURE_FLAGS))

    def test_selected_value_different_from_reviewed_source_is_not_marked_verified(self):
        case = reviewed_case()
        packet = evidence_mapping.build(case)
        packet['form_values']['monthly_income'] = 99000000
        self.assertFalse(strategy_context.build(case, packet=packet)['income_evidence_verified'])

    def test_current_complete_automatic_ocr_review_can_verify_evidence(self):
        case = reviewed_case()
        for document in case['documents']:
            document.pop('manual_verification', None)
            document.pop('verified_by', None)
        self.assertFalse(strategy_context.build(case)['income_evidence_verified'])
        mark_current_automatic_ocr(case)
        self.assertTrue(strategy_context.build(case)['income_evidence_verified'])
        self.assertTrue(strategy_context.build(case)['debt_evidence_verified'])

    def test_incomplete_failed_stale_or_unbound_ocr_pass_is_not_a_receipt(self):
        base = reviewed_case()
        for document in base['documents']:
            document.pop('verified_by', None)
        mark_current_automatic_ocr(base)
        for changes in ({'signature': 'old-original'}, {'version': 'old-parser'}, {'checked_item_ids': []},
                        {'unattempted_batch_count': 1}, {'passed': False}, {'stale': True},
                        {'error': {'code': 'MODEL_TIMEOUT'}}):
            case = deepcopy(base)
            case['verification_runs'][0].update(changes)
            self.assertFalse(strategy_context.build(case)['income_evidence_verified'], changes)

    def test_scope_or_person_change_invalidates_auto_receipt_even_when_ocr_text_is_unchanged(self):
        base = reviewed_case()
        for document in base['documents']:
            document.pop('verified_by', None)
        mark_current_automatic_ocr(base)
        self.assertTrue(strategy_context.build(base)['income_evidence_verified'])
        for target in ('person', 'period', 'metadata', 'new_attachment'):
            case = deepcopy(base)
            if target == 'person':
                case['client_name'] = '다른고객'
            elif target == 'period':
                case['requests'][0]['period_start'] = '2026-01-01'
            elif target == 'metadata':
                case['documents'][0]['document_metadata'] = {'institution': '다른기관'}
            else:
                second = deepcopy(case['documents'][0])
                second['id'] = 'new-salary-original'
                case['documents'].append(second)
                case['requests'][0]['document_ids'].append(second['id'])
            self.assertFalse(strategy_context.build(case)['income_evidence_verified'], target)

    def test_all_contributing_originals_must_be_currently_reviewed(self):
        case = reviewed_case()
        debt = next(document for document in case['documents'] if document['id'] == 'debt')
        second = deepcopy(debt)
        second.update(id='second-loan', text=debt['text'].replace('TEST-LOAN-1', 'TEST-LOAN-2'),
                      page_texts=[{'page': 1, 'text': debt['text'].replace('TEST-LOAN-1', 'TEST-LOAN-2')}])
        case['documents'].append(second)
        case['extraction_candidates'] = ax_engine.extract_factor_candidates(ax_engine.case_sources(case))
        extraction_readiness.capture(case, second)
        self.assertFalse(strategy_context.build(case)['debt_evidence_verified'])
        self.assertTrue(strategy_context.build(case)['income_evidence_verified'])

    def test_external_payload_exposes_booleans_but_no_names_numbers_of_accounts_or_quotes(self):
        case = reviewed_case()
        features = strategy_context.build(case)
        features.update(private_canary='PRIVATE_FREE_TEXT', client_name='PRIVATE_CLIENT_NAME',
                        evidence_ids=['PRIVATE_DOCUMENT_ID'], verified_by='PRIVATE_REVIEWER_NAME')
        payload = verification.build_safe_strategy_payload(features)
        messages = verification.safe_strategy_messages(payload)
        encoded = json.dumps(messages, ensure_ascii=False)
        for secret in ('PRIVATE_', case['client_name'], 'TEST-ACCOUNT-1', 'TEST-LOAN-1', '검증은행', '검증회사'):
            self.assertNotIn(secret, encoded)
        self.assertTrue(payload['case_features']['income_evidence_verified'])
        self.assertIn('동일 자료를 다시 일괄 요청하지 않는다', messages[0]['content'])
        self.assertIn('미제출 확정이 아니다', messages[0]['content'])

    def test_evidence_flag_type_is_closed_boolean_not_a_customer_text_channel(self):
        features = strategy_context.build(reviewed_case())
        features['income_evidence_verified'] = 'PRIVATE_SOURCE_TEXT'
        payload = verification.build_safe_strategy_payload(features)
        self.assertNotIn('income_evidence_verified', payload['case_features'])
        self.assertNotIn('PRIVATE_SOURCE_TEXT', json.dumps(payload))


if __name__ == '__main__':
    unittest.main()
