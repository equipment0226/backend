"""The overview uses source facts and current public grounds without side effects."""
import copy
import json
import unittest
from unittest.mock import patch

from apps.api import (dashboard_insights, legal_knowledge_graph, model_client,
                      strategy_context, verification)
from test_case_assessment import case_with_text, with_calculation


class DashboardInsightsTests(unittest.TestCase):
    def test_source_gaps_and_public_actions_can_be_sorted_together_without_model_call(self):
        case = case_with_text()
        case['facts'].append({'key': 'family_repayment', 'value': True, 'status': 'confirmed',
                              'evidence_ids': ['document-one']})
        before = copy.deepcopy(case)
        with patch.object(model_client, 'generate', side_effect=AssertionError('GET must not call AI')):
            result = dashboard_insights.build(case)
        self.assertEqual(case, before)
        self.assertTrue(result['actions'])
        self.assertNotIn('reasoning_features', result)
        self.assertFalse(result['strategy_review']['model_called_on_page_load'])
        self.assertIsNone(result['prediction']['percentage'])
        self.assertTrue(any(row['origin'] == 'public_issue_match'
                            for row in result['actions'] if row.get('origin')))
        self.assertTrue(all(isinstance(row['priority'], str) for row in result['actions']))

    def test_replaced_calculation_or_changed_graph_cannot_show_old_ai_strategy(self):
        case = case_with_text()
        calculation = with_calculation(case)
        first = dashboard_insights.build(case)
        self.assertEqual(first['freshness']['calculation'], 'current')
        case['strategy_analyses'] = [{
            'id': 'synthetic-strategy', 'input_revision': case['input_revision'],
            'knowledge_signature': first['knowledge']['signature'], 'calculation_id': calculation['id'],
            'verification': {'status': 'needs_review'},
            'strategies': [{'origin': 'ai_strategy_review', 'title': '확인된 제안',
                'description': '추가 증빙 후 다시 계산합니다.', 'source_refs': first['knowledge']['references'][:1]}]}]
        current = dashboard_insights.build(case)
        self.assertEqual(current['strategy_review']['status'], 'needs_review')
        self.assertTrue(any(row.get('origin') == 'ai_strategy_review' for row in current['actions']))
        current_risks = current['metrics']['risks']
        case['strategy_analyses'][0]['knowledge_signature'] = 'older-public-grounds'
        stale = dashboard_insights.build(case)
        self.assertEqual(stale['strategy_review']['status'], 'stale')
        self.assertFalse(any(row.get('origin') == 'ai_strategy_review' for row in stale['actions']))
        self.assertEqual(stale['metrics']['risks'], current_risks)
        case['strategy_analyses'][0]['knowledge_signature'] = first['knowledge']['signature']
        case['legal_calculations'][-1]['stale'] = True
        self.assertEqual(dashboard_insights.build(case)['strategy_review']['status'], 'stale')

    def test_external_graph_is_reconstructed_and_rejects_injected_text_or_edges(self):
        payload = verification.build_safe_strategy_payload({
            'court_id': 'CT01', 'employment_type': 'wage', 'family_repayment': True,
            'client_name': 'PRIVATE_NAME_CANARY', 'consultation': 'PRIVATE_CONVERSATION_CANARY',
            'facts': {'monthly_income': 3450000, 'account_number': 'PRIVATE_ACCOUNT_CANARY'}},
            {'summary': {'months': 36}, 'blockers': []})
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('PRIVATE_', encoded)
        self.assertTrue(payload['knowledge_graph']['nodes'])
        self.assertTrue(any(ref.get('public_graph_node_id') == 'KGAXP01'
                            for ref in payload['legal_references']))
        verification.safe_strategy_messages(payload)
        for target in ('node', 'edge', 'reference'):
            altered = copy.deepcopy(payload)
            if target == 'node':
                altered['knowledge_graph']['nodes'][0]['title'] += 'PRIVATE_INJECTION'
            elif target == 'edge':
                altered['knowledge_graph']['edges'].append({'source': 'forged', 'relation': 'approves', 'target': 'all'})
            else:
                altered['legal_references'][-1]['excerpt'] += 'PRIVATE_INJECTION'
            with self.subTest(target=target), self.assertRaises(model_client.ModelClientError):
                verification.safe_strategy_messages(altered)

    def test_later_decisions_require_verified_evidence_not_workflow_name(self):
        case = case_with_text()
        case['stage'] = 'discharge'
        case['court_outcomes'] = [{'decision_type': 'discharge', 'decision_verified': True,
                                  'evidence_verified': False}]
        self.assertEqual(strategy_context.build(case)['procedure_stage'], 'initial_application')
        case['court_outcomes'][0]['evidence_verified'] = True
        self.assertEqual(strategy_context.build(case)['procedure_stage'], 'discharge')

    def test_ai_aliases_resolve_to_public_links_without_changing_verdict(self):
        reference = legal_knowledge_graph.trusted_reference('KG579', 'CT01')
        cards = strategy_context.finding_cards({'status': 'needs_review',
            'reference_sources': [{'ref': 'LAW1', **reference}],
            'findings': [{'code': 'debt_limit', 'reason': '채무의 구분을 확인해야 합니다.',
                          'strategy': '담보내역을 추가 확인합니다.', 'source_refs': ['LAW1']}]})
        self.assertTrue(cards[0]['source_refs'][0]['url'].startswith('https://'))
        self.assertEqual(cards[0]['verification_status'], 'needs_review')
        self.assertEqual(cards[0]['origin'], 'ai_strategy_review')


if __name__ == '__main__':
    unittest.main()
