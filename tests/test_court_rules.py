import copy
from datetime import date
import hashlib
import json
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch

from apps.api import court_rules


class CourtRequestRulesTests(unittest.TestCase):
    def case(self, court='CT01'):
        return {'id': 'test', 'court_id': court, 'created_at': '2026-10-03T01:00:00Z',
                'requests': [], 'documents': [], 'financial_accounts': [
                    {'id': 'acc-a', 'bank': '가상은행', 'account_number': '12345678901', 'salary_account': True},
                    {'id': 'acc-b', 'bank': '가상은행', 'account_number': '98765432109'},
                    {'id': 'acc-c', 'bank': '다른은행', 'account_number': '00000123456'}]}

    def needs(self, catalog='D36', rules=None):
        return [{'catalog_id': catalog, 'rule_ids': rules or ['WF16'], 'reason': '검증용'}]

    def selected(self, plan, catalog):
        return [r for r in plan['requests'] if r['catalog_id'] == catalog]

    def test_busan_account_period_and_salary_period_are_distinct(self):
        case = self.case('CT03')
        case['employment_type'] = 'salary'
        rows = self.selected(court_rules.plan(case, self.needs()), 'D36')
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({r['scope_key'] for r in rows}), 4)
        salary = [r for r in rows if r['purpose'] == 'salary_income']
        self.assertEqual(salary[0]['period_start'], '2024-10-03')
        self.assertEqual(salary[0]['account_key'], 'acc-a')
        self.assertTrue(all(r['period_start'] == '2025-10-03' for r in rows if r['purpose'] == 'account_activity'))
        self.assertNotIn('12345678901', json.dumps(rows))

    def test_seoul_and_suwon_salary_fallback_is_six_months_not_every_account(self):
        for court in ['CT01', 'CT02', 'CT06']:
            case = self.case(court)
            rows = self.selected(court_rules.plan(case, self.needs(rules=['WF02'])), 'D36')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['period_start'], '2026-04-03')
            self.assertEqual(rows[0]['requirement_kind'], 'alternative')

    def test_unknown_court_never_inherits_local_period(self):
        result = court_rules.plan(self.case('CT08'), self.needs())
        self.assertEqual(result['coverage'], 'unverified')
        self.assertTrue(any(w['code'] == 'COURT_RULE_UNVERIFIED' for w in result['warnings']))
        self.assertTrue(all(r['period_start'] is None for r in result['requests']))

    def test_busan_issuance_options_and_tax_period_have_official_locators(self):
        result = court_rules.plan(self.case('CT03'), self.needs('D03'))
        family = self.selected(result, 'D03')[0]
        self.assertEqual(family['issuance_options']['certificate_type'], '상세')
        self.assertEqual(family['issuance_options']['freshness_months'], 2)
        tax = self.selected(result, 'D29')[0]
        self.assertEqual(tax['period_start'], '2021-10-03')
        self.assertEqual(tax['issuance_options']['jurisdiction_scope'], '전국')
        self.assertTrue(all(s.get('url') and s.get('locator') for s in tax['source_refs']))

    def test_business_tax_three_years_not_issuance_three_years(self):
        case = self.case('CT03')
        case['employment_type'] = 'business'
        result = court_rules.plan(case, self.needs('D21'))
        tax = self.selected(result, 'D05')[0]
        self.assertEqual(tax['period_start'], '2023-10-03')
        self.assertEqual(tax['issuance_options']['freshness_months'], 2)

    def test_reconcile_is_idempotent_and_retains_withdrawn_uploads(self):
        case = self.case()
        first = court_rules.reconcile(case, self.needs())
        self.assertTrue(first['created_ids'])
        count = len(case['requests'])
        self.assertEqual(court_rules.reconcile(case, self.needs())['created_ids'], [])
        self.assertEqual(count, len(case['requests']))
        victim = self.selected({'requests': case['requests']}, 'D36')[0]
        victim['document_ids'] = ['original-upload']
        case['financial_accounts'] = case['financial_accounts'][1:]
        result = court_rules.reconcile(case, self.needs())
        self.assertIn(victim['id'], result['withdrawn_ids'])
        self.assertEqual(victim['document_ids'], ['original-upload'])
        self.assertEqual(victim['status'], 'withdrawn')
        self.assertTrue(any(e['request_id'] == victim['id'] and e['action'] == 'withdrawn' for e in case['request_history']))

    def test_manual_and_court_order_requests_are_not_withdrawn(self):
        case = self.case()
        case['requests'] = [{'id': 'manual', 'status': 'requested'},
                            {'id': 'court', 'status': 'requested', 'managed_by': court_rules.MANAGED_BY,
                             'scope_key': 'other', 'court_order_id': 'order1'}]
        court_rules.reconcile(case, [])
        self.assertTrue(all(r['status'] == 'requested' for r in case['requests']))

    def test_manual_withdrawal_suppresses_identical_scope_but_not_new_period(self):
        case = self.case()
        court_rules.reconcile(case, self.needs())
        victim = self.selected({'requests': case['requests']}, 'D36')[0]
        victim.update(status='withdrawn', manual_override=True, withdrawal_reason='해당 계좌는 요청 불필요')
        result = court_rules.reconcile(case, self.needs())
        self.assertEqual(result['created_ids'], [])
        self.assertEqual(len(result['suppressed_requests']), 1)
        self.assertNotIn(victim['scope_key'], [r['scope_key'] for r in result['requests']])
        changed = court_rules.reconcile(case, self.needs(), as_of='2026-10-04')
        self.assertTrue(changed['created_ids'])

    def test_legacy_unscoped_submissions_are_preserved_for_scope_review(self):
        case = self.case()
        case['requests'] = [{'id': 'legacy', 'catalog_id': 'D36', 'generated_by': 'workflow_rulebook',
                             'period': '상담에서 기간 확인', 'status': 'fulfilled', 'document_ids': ['old-upload']}]
        case['documents'] = [{'id': 'old-upload', 'request_id': 'legacy', 'status': 'verified'}]
        result = court_rules.reconcile(case, self.needs())
        legacy = case['requests'][0]
        self.assertEqual(legacy['status'], 'withdrawn')
        self.assertTrue(legacy['requires_scope_review'])
        self.assertEqual(legacy['document_ids'], ['old-upload'])
        self.assertEqual(case['documents'][0]['request_id'], 'legacy')
        self.assertTrue(result['created_ids'])
        self.assertTrue(all(r['status'] == 'requested' and not r['document_ids'] for r in case['requests'][1:]))

    def test_legacy_identical_known_scope_keeps_upload_links(self):
        case = self.case()
        specification = court_rules.plan(case, self.needs())['requests'][0]
        legacy = {**specification, 'id': 'legacy', 'status': 'fulfilled', 'generated_by': 'workflow_rulebook',
                  'document_ids': ['old-upload']}
        legacy.pop('managed_by')
        case['requests'] = [legacy]
        court_rules.reconcile(case, self.needs())
        self.assertEqual(legacy['managed_by'], court_rules.MANAGED_BY)
        self.assertEqual(legacy['status'], 'fulfilled')
        self.assertEqual(legacy['document_ids'], ['old-upload'])
        self.assertEqual(sum(r['scope_key'] == legacy['scope_key'] for r in case['requests']), 1)

    def test_explicit_periods_and_institutions_create_separate_requests(self):
        case = self.case()
        case['document_scopes'] = [
            {'catalog_id': 'D36', 'institution': '가상은행', 'account_key': 'a', 'period_start': '2026-01-01', 'period_end': '2026-03-31'},
            {'catalog_id': 'D36', 'institution': '가상은행', 'account_key': 'a', 'period_start': '2026-04-01', 'period_end': '2026-06-30'}]
        rows = self.selected(court_rules.plan(case, self.needs()), 'D36')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['period_start'], '2026-01-01')
        self.assertNotEqual(rows[0]['scope_key'], rows[1]['scope_key'])

    def test_verified_account_inventory_retires_only_alternative_busan_scope(self):
        case = self.case('CT03')
        case['employment_type'] = 'salary'
        court_rules.reconcile(case, self.needs())
        case['documents'] = [{'id': 'inv', 'status': 'verified', 'catalog_id': 'D35',
                              'document_metadata': {'covers_all_accounts': True, 'institution': '전체 금융기관'}}]
        result = court_rules.reconcile(case, self.needs())
        rows = self.selected(result, 'D36')
        self.assertEqual([r['purpose'] for r in rows], ['salary_income'])
        self.assertEqual(len(result['withdrawn_ids']), 3)

    def test_one_bank_inventory_does_not_withdraw_another_bank_request(self):
        case = self.case('CT03')
        case['documents'] = [{'id': 'inv', 'status': 'verified', 'catalog_id': 'D35',
                              'document_metadata': {'covers_all_accounts': True, 'institution': '가상은행'}}]
        rows = self.selected(court_rules.plan(case, self.needs()), 'D36')
        self.assertEqual([(r['institution'], r['account_key']) for r in rows], [('다른은행', 'acc-c')])
        case['documents'][0]['document_metadata'].pop('institution')
        self.assertEqual(len(self.selected(court_rules.plan(case, self.needs()), 'D36')), 3)

    def test_metadata_rejects_other_account_incomplete_period_and_wrong_options(self):
        request = self.selected(court_rules.plan(self.case('CT03'), self.needs()), 'D36')[0]
        result = court_rules.validate_metadata(request, {'metadata': {
            'institution': '다른은행', 'account_key': 'other', 'period_start': '2026-09-01',
            'period_end': '2026-09-30', 'issued_at': '2026-01-01'}})
        self.assertEqual(result['status'], 're_request')
        codes = {x['code'] for x in result['failures']}
        self.assertIn('PERIOD_INCOMPLETE', codes)
        self.assertIn('ACCOUNT_KEY_MISMATCH', codes)
        self.assertIn('ISSUANCE_DATE_OUT_OF_RANGE', codes)
        self.assertEqual(court_rules.validate_metadata(request, {})['status'], 'metadata_required')

    def test_calendar_month_freshness_handles_leap_day(self):
        self.assertEqual(court_rules._months_before(date(2024, 4, 30), 2), date(2024, 2, 29))

    def test_freshness_uses_current_date_not_frozen_request_anchor(self):
        request = {'reference_date': '2026-07-01', 'issuance_options': {'freshness_months': 2}}
        fresh = {'metadata': {'issued_at': '2026-10-20'}}
        self.assertEqual(court_rules.validate_metadata(request, fresh, as_of='2026-10-20')['status'], 'matched')
        self.assertEqual(court_rules.validate_metadata(request, {'metadata': {'issued_at': '2026-10-21'}}, as_of='2026-10-20')['status'], 're_request')
        self.assertEqual(court_rules.validate_metadata(request, {'metadata': {'issued_at': '2026-07-01'}}, as_of='2026-10-20')['status'], 're_request')
        self.assertEqual(court_rules.validate_metadata(request, {'metadata': {'issued_at': '2026-08-20'}}, as_of='2026-10-20')['status'], 'matched')

    def test_dated_busan_rule_is_not_back_applied(self):
        result = court_rules.plan(self.case('CT03'), self.needs(), as_of='2026-06-01')
        self.assertEqual(result['coverage'], 'unverified')

    def test_local_scope_parser_binds_exact_account_and_dates_from_ocr(self):
        from apps.api.automation import _request_metadata
        case = self.case('CT03')
        request = self.selected(court_rules.plan(case, self.needs()), 'D36')[0]
        document = {'text': '가상은행\n계좌번호 123-456-78901\n거래기간: 2025.10.03 ~ 2026.10.03\n발급일: 2026년 10월 03일'}
        _request_metadata(case, request, document)
        self.assertEqual(court_rules.validate_metadata(request, document, as_of='2026-10-03')['status'], 'matched')
        self.assertEqual(document['document_metadata']['account_key'], 'acc-a')
        self.assertIn('거래기간:', document['metadata_source_quotes']['period_start'])

    def test_transaction_dates_do_not_prove_complete_statement_range(self):
        from apps.api.automation import _request_metadata
        case = self.case('CT03')
        request = self.selected(court_rules.plan(case, self.needs()), 'D36')[0]
        document = {'text': '가상은행\n계좌번호 123-456-78901\n2025.10.03 입금\n2026.10.03 출금\n발급일: 2026년 10월 03일'}
        _request_metadata(case, request, document)
        self.assertNotIn('period_start', document['document_metadata'])
        self.assertEqual(court_rules.validate_metadata(request, document)['status'], 'metadata_required')

    def test_corpus_merges_real_precedents_and_checks_saved_bytes(self):
        from apps.api import corpus
        seeds = {s['id']: s for s in corpus.seeds()}
        self.assertIn('AXP01', seeds)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = b'official-source-test'
            path = root / '.local/corpus/raw/test.html'
            path.parent.mkdir(parents=True)
            path.write_bytes(raw)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'sources': [{**seeds['AXP01'], 'status': 'downloaded',
                'raw_path': '.local/corpus/raw/test.html', 'sha256': hashlib.sha256(raw).hexdigest(),
                'retrieved_at': '2026-10-03'}]}), encoding='utf-8')
            with patch.object(corpus, 'ROOT', root), patch.object(corpus, 'RESEARCH_MANIFEST_PATH', manifest):
                self.assertEqual(corpus._saved_research_source(seeds['AXP01'])['raw'], raw)
                tampered = dict(seeds['AXP01'], url='https://www.law.go.kr/unrelated')
                self.assertIsNone(corpus._saved_research_source(tampered))
                path.write_bytes(b'tampered')
                with self.assertRaises(ValueError):
                    corpus._saved_research_source(seeds['AXP01'])

    def test_downloaded_precedents_have_hashed_original_evidence_and_are_not_success_labels(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / 'data/legal_research/manifest.json').read_text(encoding='utf-8'))
        for source in manifest['sources']:
            self.assertEqual(source['status'], 'downloaded')
            self.assertEqual(hashlib.sha256((root / source['text_path']).read_bytes()).hexdigest(), source['text_sha256'])
        cases = json.loads((root / 'data/ax_legal_precedents.json').read_text(encoding='utf-8'))['precedents']
        for precedent in cases:
            text = (root / precedent['text_path']).read_text(encoding='utf-8')
            self.assertIn(precedent['case_number'], text)
            self.assertTrue(precedent['not_an_approval_sample'])


if __name__ == '__main__':
    unittest.main()
