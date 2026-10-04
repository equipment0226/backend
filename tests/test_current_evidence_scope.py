"""The extraction gate and downstream calculations must use the same originals."""
import copy
import unittest

from apps.api import (automation, ax_engine, court_rules, evidence_mapping, extraction_readiness,
                      preliminary_drafting, rulebook)


def original(doc_id, request_id='', amount=1000000, **extra):
    text = f'급여명세서\n성명: 가상 검증\n급여월: 2026-09\n월 실수령액: {amount:,}원'
    return {'id': doc_id, 'request_id': request_id, 'status': 'verified', 'sha256': 'fixture-' + doc_id,
            'text': text, 'page_texts': [{'page': 1, 'text': text}], **extra}


class CurrentEvidenceScopeTests(unittest.TestCase):
    def test_retired_requests_are_excluded_even_when_originals_remain_verified(self):
        for status, no_longer in [('withdrawn', False), ('cancelled', False), ('superseded', False), ('fulfilled', True)]:
            case = {'requests': [{'id': 'old', 'status': status, 'no_longer_required': no_longer, 'document_ids': ['past']}],
                    'documents': [original('past', 'old', 9000000), original('current', amount=3000000)]}
            before = copy.deepcopy(case)
            self.assertEqual([doc['id'] for doc in extraction_readiness.active_documents(case)], ['current'])
            packet = evidence_mapping.build(case)
            self.assertEqual(packet['form_values']['monthly_income'], 3000000)
            self.assertEqual({row['document_id'] for row in packet['facts']}, {'current'})
            self.assertEqual([row['id'] for row in automation._sources(case, verified_only=True, packet=packet)], ['current'])
            self.assertEqual(case, before)

    def test_explicit_attachment_list_excludes_older_original_not_marked_superseded(self):
        case = {'requests': [{'id': 'req', 'status': 'received', 'document_ids': ['new']}],
                'documents': [original('old', 'req'), original('new', 'req')]}
        self.assertEqual([d['id'] for d in extraction_readiness.active_documents(case)], ['new'])
        case['requests'][0]['document_ids'] = []
        self.assertEqual(extraction_readiness.active_documents(case), [])

    def test_standalone_and_legacy_unknown_request_evidence_remain_available(self):
        case = {'documents': [original('standalone'), original('legacy', 'imported-request')]}
        self.assertEqual([d['id'] for d in extraction_readiness.active_documents(case)], ['standalone', 'legacy'])

    def test_inactive_originals_and_meeting_sources_are_not_current_filing_evidence(self):
        case = {'documents': [original('a', status='superseded'), original('b', status='rejected'),
                              original('c', status='quarantined'), original('meeting', source_type='meeting'),
                              original('current')]}
        self.assertEqual([d['id'] for d in extraction_readiness.active_documents(case)], ['current'])
        self.assertEqual({row['document_id'] for row in evidence_mapping.sources(case)}, {'current'})

    def test_received_original_is_in_scope_but_not_yet_calculation_evidence(self):
        case = {'documents': [original('received', status='received')]}
        self.assertEqual([d['id'] for d in extraction_readiness.active_documents(case)], ['received'])
        self.assertEqual(evidence_mapping.sources(case), [])
        self.assertEqual(automation._sources(case, verified_only=True, packet={'human_review_sources': []}), [])

    def test_reviewed_correction_cannot_resurrect_a_retired_document(self):
        case = {'requests': [{'id': 'retired', 'status': 'withdrawn', 'document_ids': ['old']}],
                'documents': [original('old', 'retired'), original('new')]}
        packet = {'human_review_sources': [{'id': 'edit-old', 'document_id': 'old', 'text': 'retired correction'},
                                           {'id': 'edit-new', 'document_id': 'new', 'text': 'current correction'}]}
        self.assertEqual([row['id'] for row in automation._sources(case, verified_only=True, packet=packet)], ['new', 'edit-new'])

    def test_preliminary_render_does_not_restore_retired_candidates_or_facts(self):
        case = {'requests': [{'id': 'retired', 'status': 'fulfilled', 'no_longer_required': True, 'document_ids': ['old']}],
                'documents': [original('old', 'retired', 9000000), original('new', amount=3000000)],
                'extraction_candidates': [{'id': 'old-candidate', 'key': 'monthly_income', 'document_id': 'old',
                    'value': 9000000, 'quote': '월 실수령액: 9,000,000원', 'status': 'accepted',
                    'source_edit': {'reason': 'historic review'}}],
                'facts': [{'id': 'old-fact', 'key': 'monthly_income', 'value': 9000000,
                    'evidence_ids': ['old'], 'status': 'confirmed', 'stale': False}]}
        before = copy.deepcopy(case)
        safe, _ = preliminary_drafting.render_case(case, {'passed': True, 'checked_item_ids': ['old-candidate']})
        self.assertEqual([doc['id'] for doc in safe['documents']], ['new'])
        self.assertEqual(safe['source_review_candidates'], [])
        self.assertEqual(safe['facts'], [])
        self.assertNotIn('old-candidate', [row['id'] for row in safe['extraction_candidates']])
        self.assertEqual(safe['evidence_mapping']['form_values']['monthly_income'], 3000000)
        self.assertEqual(case, before)

    def test_ax_context_excludes_retired_original_and_its_confirmed_fact(self):
        case = {'requests': [{'id': 'retired', 'status': 'withdrawn', 'document_ids': ['old']}],
                'documents': [original('old', 'retired'), original('new')],
                'facts': [{'id': 'old-fact', 'key': 'monthly_income', 'value': 9000000,
                           'status': 'confirmed', 'evidence_ids': ['old']}]}
        sources = ax_engine.case_sources(case)
        self.assertEqual({row['document_id'] for row in sources if row.get('document_id')}, {'new'})
        self.assertNotIn('fact:old-fact', [row['id'] for row in sources])
        # Single-document parsing is independent of request history and stays usable.
        self.assertTrue(extraction_readiness.candidates(case['documents'][0]))

    def test_retired_business_evidence_does_not_recreate_business_requests(self):
        business_text = '사업소득 확인서\n사업소득 월 4,000,000원이 있으며 현재 음식점을 운영합니다.'
        for request_state in ({'status': 'withdrawn'}, {'status': 'fulfilled', 'no_longer_required': True}):
            case = {'court_id': 'CT01', 'application_date': '2026-10-04',
                    'requests': [{'id': 'retired', 'catalog_id': 'D15', 'document_ids': ['old'], **request_state}],
                    'documents': [original('old', 'retired', text=business_text,
                                           page_texts=[{'page': 1, 'text': business_text}]), original('payroll')],
                    'extraction_candidates': [{'id': 'old-business', 'document_id': 'old', 'key': 'business_revenue',
                                               'value': 4000000, 'status': 'accepted'}]}
            self.assertFalse(rulebook.income_profile(case)['business'])
            self.assertTrue(rulebook.income_profile(case)['salary'])
            evaluation = rulebook.evaluate_case(case)
            self.assertNotIn('WF03', [row['id'] for row in evaluation['matched_rules']])
            plan = court_rules.plan(case, evaluation['required_documents'])
            forbidden = set(court_rules.definition()['income_eligibility']['business_only_document_ids'])
            self.assertFalse(forbidden & {row['catalog_id'] for row in plan['requests']})

    def test_replaced_income_file_is_excluded_even_before_legacy_status_is_repaired(self):
        case = {'requests': [{'id': 'income', 'status': 'received', 'document_ids': ['new']}],
                'documents': [original('old', 'income', 9000000), original('new', 'income', 3000000)]}
        self.assertEqual({row['document_id'] for row in ax_engine.case_sources(case) if row.get('document_id')}, {'new'})
        self.assertEqual({row['document_id'] for row in rulebook.sources(case) if row.get('document_id')}, {'new'})


if __name__ == '__main__':
    unittest.main()
