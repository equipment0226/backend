"""Report gaps follow current original evidence, not discarded reading labels."""
import copy
import unittest

from apps.api import evidence_mapping, preliminary_drafting
from tests.test_evidence_mapping_pipeline import fixture


class PreliminaryReportGapTests(unittest.TestCase):
    def test_korean_label_is_removed_when_current_field_was_recovered(self):
        case = fixture()
        case['extraction_candidates'].extend([
            {'id': 'old-income', 'key': 'monthly_income', 'label': '급여 실수령액', 'value': 99,
             'quote': '예전 판독', 'document_id': 'salary'},
            {'id': 'unknown-cash', 'key': 'cash_balance', 'label': '확인하지 못한 현금', 'value': 123,
             'quote': '근거 없는 판독', 'document_id': 'bank'}])
        before = copy.deepcopy(case)
        safe, omitted = preliminary_drafting.render_case(case)
        self.assertNotIn('급여 실수령액', omitted)
        self.assertIn('확인하지 못한 현금', omitted)
        self.assertEqual(next(row['value'] for row in safe['extraction_candidates'] if row['key'] == 'monthly_income'), 3200000)
        self.assertEqual(case, before)

    def test_detailed_annex_observation_requires_matching_source_value_and_period(self):
        case = fixture()
        packet = evidence_mapping.build(case)
        fact = next(row for row in packet['facts'] if row['key'] == 'income_gross' and row.get('frequency') == 'monthly')
        self.assertNotIn(fact['key'], {row['key'] for row in evidence_mapping.candidate_rows(packet)})
        candidate = {key: copy.deepcopy(fact[key]) for key in
                     ('key', 'value', 'quote', 'document_id', 'page', 'basis', 'frequency') if key in fact}
        candidate.update(id='old-gross', label='공제 전 월 급여')
        case['extraction_candidates'] = [candidate]
        safe, omitted = preliminary_drafting.render_case(case)
        self.assertNotIn(candidate['label'], omitted)
        self.assertTrue(any(row['id'] == fact['id'] for row in safe['evidence_mapping']['facts']))
        for changes in ({'document_id': 'annual'}, {'value': fact['value'] + 1},
                        {'quote': '원문에 없는 문장'}):
            changed = copy.deepcopy(case)
            changed['extraction_candidates'][0].update(changes)
            _, omitted = preliminary_drafting.render_case(changed)
            self.assertIn(candidate['label'], omitted, changes)
        changed = copy.deepcopy(case)
        changed['extraction_candidates'][0]['frequency'] = 'annual'
        safe, omitted = preliminary_drafting.render_case(changed)
        self.assertNotIn(candidate['label'], omitted)
        history = safe['source_reading_exclusions'][0]
        self.assertTrue(history['requires_review'])
        self.assertEqual(history['status'], 'changed_scope')
        self.assertEqual(safe['evidence_mapping']['facts'], packet['facts'])

    def test_old_consultation_candidate_is_history_not_a_missing_document(self):
        case = fixture()
        case['extraction_candidates'] = [{'id': 'old', 'key': 'marital_status', 'label': '혼인 상태',
            'value': '확인 전', 'document_id': None, 'source_id': 'consultation', 'quote': '과거 상담'}]
        safe, omitted = preliminary_drafting.render_case(case)
        self.assertEqual(omitted, [])
        self.assertEqual(safe['source_reading_exclusions'][0]['status'], 'historical_source')
        draft = {'sections': [], 'missing_fields': []}
        preliminary_drafting.attach_source_reading_history(draft, safe)
        self.assertTrue(draft['source_reading_exclusions'])
        self.assertEqual(draft['missing_fields'], [])
        self.assertIn('혼인 상태', draft['sections'][0]['content'])

    def test_typed_resolution_removes_old_aggregate_conflict_without_clearing_unresolved_evidence(self):
        case = fixture()
        case['extraction_candidates'] = [
            {'id': 'correct', 'key': 'monthly_income', 'label': '월 소득', 'value': 3200000,
             'document_id': 'salary', 'quote': '월 실수령액: 3,200,000원'},
            {'id': 'annual-wrong-key', 'key': 'monthly_income', 'label': '월 소득', 'value': 38400000,
             'document_id': 'annual', 'quote': '연간 실수령액: 38,400,000원'}]
        safe, omitted = preliminary_drafting.render_case(case, {'passed': True,
            'checked_item_ids': ['correct', 'annual-wrong-key']})
        self.assertFalse(any('출처 간 값' in row for row in omitted))
        self.assertEqual(next(row['value'] for row in safe['extraction_candidates'] if row['key'] == 'monthly_income'), 3200000)
        # The former AI verdict does not overwrite the original period-aware mapping.
        self.assertFalse(safe['evidence_mapping']['legal_approval'])


if __name__ == '__main__':
    unittest.main()
