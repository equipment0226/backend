from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from apps.api import approval_estimator as estimator, automation, domain, store


class ApprovalEstimatorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for target, name, value in [(store, 'DATA_DIR', Path(directory.name)),
                                    (store, 'now', lambda: '2026-10-03T09:00:00+00:00')]:
            mocked = patch.object(target, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        store.initialize()
        self.case = domain.new_case('평가 대상', 'CT01', '서울회생법원', '', False)
        self.calc = {'id': 'calc-target', 'input_revision': 1, 'policy_hash': 'verified-policy-1',
                     'created_at': '2026-08-01T00:00:00+00:00',
                     'inputs': {'income': {'kind': 'wage'}},
                     'summary': {'unsecured_debt': 80000000, 'secured_debt': 0,
                                 'liquidation_shortfall': 0, 'monthly_creditor_capacity': 1000000, 'months': 36}}
        self.case['legal_calculations'] = [self.calc]
        self.user = {'id': 'lawyer', 'role': 'lawyer', 'name': '변호사'}
        self.as_of = '2026-09-30T23:00:00+00:00'
        self.counter = 0

    def add_result(self, decision_type='initial_plan_approval', *, case_overrides=None,
                   record_overrides=None, profile_overrides=None, document_overrides=None,
                   generated_at='2026-08-01T00:00:00+00:00', source_suffix=None):
        """Nonproduction fixture in a temporary DB; production demo cases remain excluded."""
        self.counter += 1
        case = domain.new_case('격리 테스트 자료', 'CT01', '서울회생법원', '', False)
        case.update(case_overrides or {})
        phrases = {'initial_plan_approval': '채무자의 변제계획을 인가한다.',
                   'initial_plan_denial': '채무자의 변제계획을 불인가한다.',
                   'application_dismissal': '채무자의 개인회생절차 개시신청을 기각한다.',
                   'commencement': '채무자의 개인회생절차를 개시한다.',
                   'correction_order': '서류를 보정하라.', 'discharge': '채무자를 면책한다.'}
        phrase = phrases.get(decision_type, '결정 내용을 별도로 확인한다.')
        text = f'2026. 9. 1.\n주문\n{phrase}\n서류 구별: {source_suffix or self.counter}'
        doc = {'id': 'decision-source', 'status': 'verified', 'text': text, 'version': 1,
               'sha256': hashlib.sha256(text.encode()).hexdigest()}
        doc.update(document_overrides or {})
        case['documents'] = [doc]
        bundle = {'id': f'bundle-{self.counter}', 'created_at': generated_at,
                  'content_hash': store.digest([case['id'], self.counter]), 'template_id': 'D5110'}
        bundle['generation_features'] = automation._features(case, self.calc, bundle)
        if profile_overrides:
            bundle['generation_features']['approval_profile'].update(profile_overrides)
        case['court_documents'] = [bundle]
        outcome = 'correction' if decision_type == 'correction_order' else 'rejected' if decision_type in {'initial_plan_denial', 'application_dismissal'} else 'approved'
        data = {'outcome': outcome, 'reason': '해당 법원 결정 주문의 원문 확인', 'bundle_id': bundle['id'],
                'source_document_id': doc['id']}
        if decision_type:
            data.update(decision_type=decision_type, decision_date='2026-09-01', decision_quote=phrase)
        with patch.object(store, 'now', lambda: '2026-09-02T01:00:00+00:00'):
            record = automation.record_outcome(case, data, self.user)
        record.update(record_overrides or {})
        store.insert_case(case)
        return case, record

    def estimate(self):
        return estimator.estimate(self.case, self.calc, as_of=self.as_of)

    def test_no_data_returns_explicit_null_individual_and_observed_rates(self):
        result = self.estimate()
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertIsNone(result['percentage'])
        self.assertIsNone(result['prediction']['percentage'])
        self.assertIsNone(result['observed_rate']['percentage'])
        self.assertEqual(result['evidence_count'], 0)
        self.assertEqual(result['minimum_sample_size'], 30)
        self.assertIn('INSUFFICIENT_MATCHED_OUTCOMES', {r['code'] for r in result['reasons']})

    def test_thirty_verified_matching_cases_enable_only_observed_rate_and_wilson_bounds(self):
        for index in range(30):
            self.add_result('initial_plan_approval' if index < 24 else 'initial_plan_denial')
        result = self.estimate()
        self.assertEqual(result['status'], 'observed_rate_available', result)
        self.assertEqual(result['evidence_count'], 30)
        self.assertEqual(result['observed_rate']['percentage'], 80.0)
        self.assertAlmostEqual(result['observed_rate']['confidence_interval']['lower'], 62.69, places=2)
        self.assertAlmostEqual(result['observed_rate']['confidence_interval']['upper'], 90.49, places=2)
        self.assertIsNone(result['percentage'])
        self.assertIsNone(result['prediction']['percentage'])
        self.assertFalse(result['prediction_validation']['calibration_verified'])
        self.assertEqual(result['distribution'], {'approved': 24, 'rejected': 6, 'correction': 0})
        self.assertNotIn('격리 테스트', store.dumps(result))
        self.assertTrue(all(set(reference) == {'evidence_digest'} for reference in result['evidence_refs']))
        self.assertNotIn('source_document_id', store.dumps(result))
        self.assertNotIn('case_id', store.dumps(result))

    def test_twenty_nine_cases_do_not_publish_percentage(self):
        for _ in range(29):
            self.add_result()
        result = self.estimate()
        self.assertEqual(result['evidence_count'], 29)
        self.assertIsNone(result['observed_rate']['percentage'])
        self.assertIsNone(result['observed_rate']['confidence_interval'])

    def test_other_court_org_synthetic_and_changed_profile_do_not_enter_cohort(self):
        self.add_result(case_overrides={'org_id': 'different-org'})
        self.add_result(case_overrides={'court_id': 'CT02'})
        self.add_result(case_overrides={'synthetic': True})
        self.add_result(profile_overrides={'income_kind': 'business'})
        self.add_result(profile_overrides={'policy_hash': 'superseded-policy'})
        self.add_result(profile_overrides={'debt_band': 'over_300m'})
        self.add_result()
        result = self.estimate()
        self.assertEqual(result['evidence_count'], 1)
        self.assertEqual(result['exclusions']['SYNTHETIC'], 1)
        self.assertEqual(result['exclusions']['COHORT_MISMATCH'], 3)

    def test_old_approved_discharge_and_commencement_are_not_initial_approval(self):
        self.add_result(None)
        self.add_result('discharge')
        self.add_result('commencement')
        self.add_result('correction_order')
        self.add_result('application_dismissal')
        result = self.estimate()
        self.assertEqual(result['evidence_count'], 1)
        self.assertEqual(result['distribution'], {'approved': 0, 'rejected': 1, 'correction': 1})

    def test_future_records_and_post_decision_features_cannot_leak_into_fixed_asof(self):
        self.add_result(record_overrides={'created_at': '2026-10-01T01:00:00+00:00'})
        self.add_result(record_overrides={'decision_date': '2026-10-02'})
        self.add_result(generated_at='2026-09-01T01:00:00+00:00')
        self.add_result(record_overrides={'created_at': '2020-01-01T00:00:00+00:00'})
        result = self.estimate()
        self.assertEqual(result['evidence_count'], 0)
        self.assertEqual(result['exclusions']['OUTSIDE_KNOWLEDGE_WINDOW'], 2)
        self.assertEqual(result['exclusions']['POST_DECISION_FEATURES'], 1)
        with self.assertRaisesRegex(ValueError, 'ESTIMATION_DATE_FUTURE'):
            estimator.estimate(self.case, self.calc, as_of='2030-01-01T00:00:00+00:00')

    def test_same_case_multiple_documents_and_shared_source_do_not_inflate_sample(self):
        first, record = self.add_result(source_suffix='same-order')
        duplicate = {**deepcopy(record), 'id': 'duplicate-order', 'created_at': '2026-09-03T01:00:00+00:00'}
        with store.db() as con:
            con.execute('INSERT INTO court_outcomes VALUES (?,?,?,?,?,?,?)',
                        (duplicate['id'], first['id'], first['org_id'], first['court_id'], duplicate['outcome'], store.dumps(duplicate), duplicate['created_at']))
        self.add_result(source_suffix='same-order')
        result = self.estimate()
        self.assertEqual(result['evidence_count'], 1)
        self.assertEqual(result['exclusions']['REPEATED_CASE_DECISION'], 1)
        self.assertEqual(result['exclusions']['DUPLICATE_DECISION_SOURCE'], 1)

    def test_current_case_is_excluded_even_if_an_outcome_already_exists(self):
        self.case, _ = self.add_result()
        self.assertEqual(self.estimate()['evidence_count'], 0)

    def test_tampered_source_or_generation_snapshot_fails_evidence_link(self):
        self.add_result(record_overrides={'source_hash': 'different-source'})
        self.add_result(record_overrides={'document_hash': 'different-version'})
        self.add_result(record_overrides={'decision_verified': False})
        case, record = self.add_result()
        with store.db() as con:
            con.execute('DELETE FROM generated_document_versions WHERE case_id=?', (case['id'],))
        result = self.estimate()
        self.assertEqual(result['evidence_count'], 0)
        self.assertEqual(result['exclusions']['DECISION_EVIDENCE_CHANGED'], 1)
        self.assertEqual(result['exclusions']['GENERATION_HASH_MISMATCH'], 1)
        self.assertEqual(result['exclusions']['GENERATION_LEDGER_MISSING'], 1)

    def test_classification_requires_correct_quote_date_and_outcome_stage(self):
        doc = {'text': '2026. 9. 1.\n채무자의 변제계획을 인가한다.'}
        good = {'decision_type': 'initial_plan_approval', 'decision_date': '2026-09-01',
                'decision_quote': '채무자의 변제계획을 인가한다.', 'outcome': 'approved'}
        self.assertTrue(estimator.verify_decision(good, doc)['verified'])
        for changes, error in [({'decision_quote': '원문에 없는 인가 문장'}, 'DECISION_QUOTE_NOT_IN_SOURCE'),
                               ({'decision_date': '2026-08-31'}, 'DECISION_DATE_NOT_IN_SOURCE'),
                               ({'decision_date': '2030-01-01'}, 'DECISION_DATE_FUTURE'),
                               ({'outcome': 'rejected'}, 'DECISION_OUTCOME_MISMATCH'),
                               ({'decision_type': 'discharge'}, 'DECISION_OPERATIVE_TEXT_UNCONFIRMED')]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, error):
                estimator.verify_decision({**good, **changes}, doc)
        with self.assertRaisesRegex(ValueError, 'DECISION_STAGE_CONFLICT'):
            estimator.verify_decision(good, {'text': doc['text'] + '\n주문: 채무자를 면책한다.'})

    def test_wilson_extremes_do_not_report_false_certainty(self):
        self.assertGreater(estimator.wilson_interval(0, 30)['upper'], 0)
        self.assertLess(estimator.wilson_interval(30, 30)['lower'], 100)
        self.assertEqual(estimator.wilson_interval(0, 30)['lower'], 0)
        self.assertEqual(estimator.wilson_interval(30, 30)['upper'], 100)
        for numerator, total in [(1, 0), (-1, 30), (31, 30), (True, 30)]:
            with self.assertRaises(ValueError):
                estimator.wilson_interval(numerator, total)

    def test_no_calculation_has_a_clear_scope_reason(self):
        self.case['legal_calculations'] = []
        result = estimator.estimate(self.case, as_of=self.as_of)
        self.assertIsNone(result['observed_rate']['percentage'])
        self.assertEqual(result['reasons'][0]['code'], 'MATCHING_FEATURES_REQUIRED')

    def test_historical_evaluation_never_uses_later_target_features(self):
        self.calc['created_at'] = '2026-10-02T00:00:00+00:00'
        result = self.estimate()
        self.assertIsNone(result['observed_rate']['percentage'])
        self.assertEqual(result['reasons'][0]['code'], 'TARGET_FEATURE_TIME_UNCONFIRMED')


if __name__ == '__main__':
    unittest.main()
