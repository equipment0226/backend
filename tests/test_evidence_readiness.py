"""File receipt, documentary facts and interview claims remain separate."""
import copy
import unittest

from apps.api import document_facts, evidence_mapping


def document(identifier, text):
    return {'id': identifier, 'status': 'verified', 'text': text}


def case():
    return {'id': 'test-readiness', 'court_id': 'CT01', 'input_revision': 1,
        'intake': {'status': 'completed'},
        'documents': [document('payroll', '급여명세서\n급여월: 2026-09\n월 실수령액: 3,450,000원')],
        'consultation': {'status': 'party_statement', 'version': 2,
            'notes': '무상거주 중입니다.\n월 주거비: 0원\n부동산·자동차·보험·임차보증금은 없습니다.',
            'answers': {}},
        'requests': [{'id': 'salary-request', 'catalog_id': 'D07', 'status': 'fulfilled'}]}


class EvidenceReadinessTests(unittest.TestCase):
    def test_fulfilled_requests_do_not_hide_missing_facts_or_upgrade_interview(self):
        original = case(); before = copy.deepcopy(original)
        packet = evidence_mapping.build(original)
        self.assertEqual(original, before)
        self.assertEqual(packet['form_values']['monthly_income'], 3450000)
        for key in ('housing_type', 'housing_cost', 'housing_deposit', 'insurance_surrender'):
            self.assertNotIn(key, packet['form_values'])
        self.assertTrue(all(row['source_type'] == 'case_document' for row in packet['facts']))
        gaps = {row['code']: row for row in packet['review_gaps']}
        self.assertEqual(set(gaps), {'HOUSING_EVIDENCE_REQUIRED', 'LIVING_EXPENSES_EVIDENCE_REQUIRED', 'INSURANCE_EVIDENCE_REQUIRED'})
        housing = gaps['HOUSING_EVIDENCE_REQUIRED']
        self.assertEqual(housing['reported_value'], {'housing_type': '무상거주', 'housing_cost': 0, 'housing_deposit': 0})
        self.assertEqual(gaps['INSURANCE_EVIDENCE_REQUIRED']['reported_value'], {'insurance_contracts': False})
        self.assertTrue(all(row['source_kind'] == 'party_statement' and row['quote'] in original['consultation']['notes']
                            for gap in gaps.values() for row in gap['reported_sources']))
        self.assertTrue(all(not row['auto_request'] and not row['blocks_first_draft'] for row in gaps.values()))
        self.assertIsNone(packet['inputs']['recognized_household_size'])
        self.assertIsNone(packet['inputs']['monthly_trustee_fee'])

    def test_verified_document_observations_clear_gaps_without_fifteen_file_requirement(self):
        original = case()
        original['documents'].extend([
            document('residence', '무상거주 확인서\n임차보증금: 0원\n월 주거비: 0원'),
            document('expenses', '월 생활지출: 1,730,000원'),
            document('insurance', '보험가입 조회결과\n보험계약은 없습니다.')])
        packet = evidence_mapping.build(original)
        self.assertEqual(packet['review_gaps'], [])
        self.assertEqual(packet['form_values']['living_expenses'], 1730000)
        self.assertEqual(packet['form_values']['insurance_surrender'], 0)

    def test_pending_and_quarantined_consultation_never_supply_reported_values(self):
        for pending, quarantined in [(True, False), (False, True)]:
            original = case()
            if pending:
                original['intake']['status'] = 'received'
            if quarantined:
                original['consultation']['status'] = 'quarantined'
            for gap in evidence_mapping.build(original)['review_gaps']:
                self.assertEqual(gap['reported_value'], {})
                self.assertEqual(gap['reported_sources'], [])

    def test_conflicting_interview_values_keep_both_sources_and_do_not_choose_one(self):
        original = case()
        original['consultation']['answers']['housing'] = '월 주거비: 250,000원'
        gap = next(row for row in evidence_mapping.build(original)['review_gaps'] if row['code'] == 'HOUSING_EVIDENCE_REQUIRED')
        self.assertNotIn('housing_cost', gap['reported_value'])
        self.assertIn('housing_cost', gap['reported_conflicts'])
        self.assertEqual({row['value'] for row in gap['reported_sources'] if row['key'] == 'housing_cost'}, {0, 250000})

    def test_request_fulfilled_and_fact_unknown_are_reported_separately(self):
        original = case()
        original['requests'].append({'id': 'bank-request', 'catalog_id': 'D36', 'status': 'fulfilled'})
        gap = next(row for row in evidence_mapping.build(original)['review_gaps'] if row['code'] == 'LIVING_EXPENSES_EVIDENCE_REQUIRED')
        self.assertEqual(gap['request_status'], 'fulfilled')
        self.assertEqual(gap['request_ids'], ['bank-request'])
        self.assertEqual(gap['fields'], ['living_expenses'])

    def test_listed_absence_has_scope_and_cannot_be_question_history_or_double_negative(self):
        def rows(text):
            return document_facts.extract([{'id': 'test', 'kind': 'party_statement', 'text': text}])
        text = '부동산·자동차·보험·임차보증금은 없습니다.'
        found = rows(text)
        self.assertEqual({row['key']: row['value'] for row in found}, {
            'real_estate_ownership': False, 'vehicle_ownership': False,
            'insurance_contracts': False, 'housing_deposit': 0})
        self.assertTrue(all(row['explicit_absence'] and row['quote'] == text for row in found))
        for other in ('보험은 없는 것은 아닙니다.', '보험·임차보증금은 없나요?',
                      '과거에는 보험·임차보증금은 없었습니다.', '보험·임차보증금은 없습니다. 확인 필요',
                      '건강보험은 없습니다.', '건강 보험은 없습니다.', '배우자의 보험은 없습니다.'):
            self.assertFalse(any(row['key'] in {'insurance_contracts', 'housing_deposit'} for row in rows(other)), other)


if __name__ == '__main__':
    unittest.main()
