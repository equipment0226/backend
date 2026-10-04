"""Public provenance, scope and freshness checks for strategy graph context."""
import copy
import json
import unittest
from unittest.mock import patch

from apps.api import legal_knowledge_graph as graph


class LegalKnowledgeGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = graph.load()

    def setUp(self):
        self.data = copy.deepcopy(self.snapshot)
        self.loader = patch.object(graph, 'load', return_value=self.data)
        self.loader.start()
        self.addCleanup(self.loader.stop)
        self.watcher = patch.object(graph, '_watched_versions', return_value={})
        self.watcher.start()
        self.addCleanup(self.watcher.stop)

    def source(self, source_id):
        return next(s for s in self.data['sources'] if s['id'] == source_id)

    def node(self, node_id):
        return next(n for n in self.data['nodes'] if n['id'] == node_id)

    def test_official_sources_have_real_bodies_and_bound_nodes(self):
        self.assertEqual(len(self.data['sources']), 10)
        for node in self.data['nodes']:
            if node['kind'] in {'statute', 'court_rule', 'case'}:
                reference = graph.trusted_reference(node['id'], court_id='CT01')
                self.assertIsNotNone(reference, node['id'])
                self.assertIn(reference['excerpt'], self.source(reference['id'])['body'])
                self.assertTrue(graph._official(reference['url']))

    def test_all_graph_edges_have_real_endpoints(self):
        ids = {node['id'] for node in self.data['nodes']}
        for edge in self.data['edges']:
            self.assertIn(edge['from'], ids)
            self.assertIn(edge['to'], ids)

    def test_reference_rejects_tampered_body(self):
        self.source('LW579')['body'] += 'unreviewed amendment'
        self.assertIsNone(graph.trusted_reference('KG579'))

    def test_reference_rejects_tampered_excerpt(self):
        self.node('KG579')['excerpt'] = 'invented rule that is absent from the public source body'
        self.assertIsNone(graph.trusted_reference('KG579'))

    def test_reference_rejects_unbound_body_even_with_updated_source_hash(self):
        source = self.source('LW579')
        source['body'] += 'changed operative provision'
        source['body_sha256'] = graph._hash(source['body'])
        self.assertIsNone(graph.trusted_reference('KG579'))

    def test_menu_shell_or_unverified_source_is_not_evidence(self):
        self.source('LW579')['status'] = 'downloaded'
        self.assertIsNone(graph.trusted_reference('KG579'))

    def test_wrong_court_rule_excluded_but_national_statute_allowed(self):
        self.assertIsNone(graph.trusted_reference('KGSEOUL406', 'CT02'))
        self.assertIsNotNone(graph.trusted_reference('KG579', 'CT02'))

    def test_pending_watch_change_blocks_related_source_only(self):
        policy = {'pending_changes': [{'source_id': 'LW614'}]}
        self.assertIsNone(graph.trusted_reference('KG614', policy=policy))
        self.assertIsNotNone(graph.trusted_reference('KG579', policy=policy))

    def test_auto_scalar_change_invalidates_old_graph_interpretation(self):
        current = {'LW579': {'status': 'collected', 'applicability_status': 'watch_scalar_validated',
                             'semantic_sha256': 'new-approved-numeric-body', 'source_sha256': 'new'}}
        with patch.object(graph, '_watched_versions', return_value=current):
            self.assertIsNone(graph.trusted_reference('KG579'))

    def test_future_or_expired_rule_not_promoted(self):
        node = self.node('KG579')
        node['effective_from'] = '2099-01-01'
        self.assertIsNone(graph.trusted_reference('KG579', policy={'as_of': '2026-10-04'}))
        node['effective_from'] = '2020-01-01'
        node['valid_through'] = '2021-01-01'
        self.assertIsNone(graph.trusted_reference('KG579', policy={'as_of': '2026-10-04'}))

    def test_remand_is_not_an_approval_sample(self):
        cases = [node for node in self.data['nodes'] if node['kind'] == 'case']
        self.assertEqual(len(cases), 4)
        self.assertTrue(all(node['not_an_approval_sample'] for node in cases))
        self.assertEqual(self.node('KGAXP01')['outcome'], 'remanded')
        self.assertEqual(self.node('KGAXP03')['decision_stage'], 'post_approval')
        self.assertEqual(self.node('KGAXP04')['decision_stage'], 'discharge')

    def test_post_approval_decision_never_used_for_initial_application(self):
        result = graph.retrieve({'court_id': 'CT01', 'procedure_stage': 'initial_application', 'issue_tags': ['performance_risk']})
        self.assertNotIn('KGAXP03', [m['id'] for m in result['matches']])
        later = graph.retrieve({'court_id': 'CT02', 'procedure_stage': 'post_approval', 'issue_tags': ['arrears']})
        self.assertIn('KGAXP03', [m['id'] for m in later['matches']])

    def test_similar_case_requires_relevant_feature_and_stage(self):
        result = graph.retrieve({'court_id': 'CT01', 'family_repayment': True})
        self.assertIn('KGAXP01', [m['id'] for m in result['matches']])
        unrelated = graph.retrieve({'court_id': 'CT01'})
        self.assertNotIn('KGAXP01', [m['id'] for m in unrelated['matches']])
        self.assertNotIn('KGSEOUL408', [m['id'] for m in unrelated['matches']])

    def test_family_repayment_keeps_current_statutes_without_inventing_spouse_property(self):
        result = graph.external_context({'court_id': 'CT01', 'family_repayment': True})
        refs = {row['node_id'] for row in result['references']}
        self.assertTrue({'KG579', 'KG611', 'KG614', 'KGAXP01'} <= refs, refs)
        self.assertNotIn('KGSEOUL406', refs)
        self.assertNotIn('asset_disposal', result['issue_tags'])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))), 10000)

    def test_external_context_contains_no_supplied_private_free_text(self):
        result = graph.external_context({'court_id': 'CT01', 'client_name': 'PRIVATE_CANARY_NAME',
                                         'issue_tags': ['PRIVATE_CANARY_NAME'], 'consultation': 'PRIVATE_CANARY_TEXT',
                                         'numeric_facts': {'address': 'PRIVATE_CANARY_ADDRESS'}})
        encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
        self.assertNotIn('PRIVATE_CANARY', encoded)
        self.assertLessEqual(len(encoded), 10000)
        self.assertLessEqual(len(result['references']), 8)
        ids = {node['id'] for node in result['nodes']}
        for edge in result['edges']:
            self.assertIn(edge['source'], ids)
            self.assertIn(edge['target'], ids)

    def test_signature_changes_for_interpretation_not_poll_time(self):
        before = graph.signature()
        self.data['last_refresh_at'] = '2099-01-01'
        self.source('LW579')['retrieved_at'] = '2099-01-01'
        self.source('LW579')['source_sha256'] = 'new-transport-only-hash'
        self.assertEqual(before, graph.signature())
        self.node('KG579')['summary'] += ' reviewed clarification'
        self.assertNotEqual(before, graph.signature())

    def test_production_risk_codes_and_numeric_summary_select_real_topics(self):
        selected = graph.retrieve({'court_id': 'CT01',
                                   'risk_codes': ['UNSECURED_LIMIT_EXCEEDED', 'EXTENDED_PERIOD_REASON_REQUIRED'],
                                   'calculation': {'summary': {'liquidation_shortfall': 10000, 'additional_living_cost': 50000}}})
        self.assertTrue({'debt_limit', 'repayment_period', 'liquidation_gap', 'additional_living_cost'} <= set(selected['issue_tags']))
        self.assertNotIn('KGAXP01', [node['id'] for node in selected['matches']])

    def test_numeric_strings_and_booleans_do_not_create_disposal_finding(self):
        for value in ('10000000', True):
            selected = graph.retrieve({'numeric_facts': {'disposal_proceeds': value}})
            self.assertNotIn('asset_disposal', selected['issue_tags'])

    def test_untrusted_redirect_url_excluded(self):
        for url in ['http://www.law.go.kr/', 'https://law.go.kr.example.com/', 'https://user:pass@law.go.kr/', 'https://law.go.kr:bad/']:
            self.source('LW579')['url'] = url
            self.assertIsNone(graph.trusted_reference('KG579'))


if __name__ == '__main__':
    unittest.main()
