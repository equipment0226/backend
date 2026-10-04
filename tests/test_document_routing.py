"""Content-based routing never consumes fixture answer keys or filenames."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from apps.api import court_rules, document_routing, domain

ROOT = Path(__file__).resolve().parents[1]


def request(id='req', catalog_id='D07', **kwargs):
    return {'id': id, 'catalog_id': catalog_id, 'status': 'requested', **kwargs}


def entry(text, id='file', **kwargs):
    return {'id': id, 'filename': '28750a.pdf', 'text': text, **kwargs}


def route(text, requests=None, **kwargs):
    case = {'client_name': '검증고객', 'requests': requests or [request()], **kwargs}
    return document_routing.route_documents(case, [entry(text)])[0]


class DocumentRoutingTests(unittest.TestCase):
    def test_random_filename_does_not_affect_content_and_inputs_unchanged(self):
        case = {'client_name': '검증고객', 'requests': [request()]}
        entries = [entry('급여명세서\n성명: 검증고객\n실지급액: 300만원')]
        original = deepcopy((case, entries))
        result = document_routing.route_documents(case, entries)[0]
        self.assertEqual(result['request_id'], 'req')
        self.assertEqual(result['confidence'], 'high')
        self.assertEqual((case, entries), original)
        entries[0]['filename'] = '주민등록표등본_다른명의.pdf'
        self.assertEqual(document_routing.route_documents(case, entries)[0], result)

    def test_filename_only_and_document_checklist_are_not_evidence(self):
        for body in ['', '상담기록\n필요서류: 급여명세서\n성명: 검증고객',
                     '급여명세서 제출 필요\n성명: 검증고객']:
            self.assertEqual(route(body)['status'], 'unmatched')

    def test_wrong_or_missing_named_subject_is_held(self):
        for body in ['급여명세서\n성명: 다른사람', '급여명세서\n실수령: 100원',
                     '급여명세서\n성명: 검증고객\n성명: 다른사람']:
            self.assertEqual(route(body)['status'], 'identity_review')

    def test_missing_name_is_not_reported_as_a_confirmed_identity_conflict(self):
        missing = route('급여명세서\n월 실수령액: 300만원')
        different = route('급여명세서\n성명: 다른사람\n월 실수령액: 300만원')
        self.assertEqual(missing['identity_status'], 'unresolved')
        self.assertEqual(different['identity_status'], 'conflict')
        self.assertIsNone(missing['request_id'])
        self.assertIsNone(different['request_id'])

    def test_family_members_are_not_mistaken_for_the_subject(self):
        result = route('가족관계증명서\n성명: 검증고객\n부: 검증부모\n모: 다른가족',
                       [request(catalog_id='D03')])
        self.assertEqual(result['status'], 'matched')

    def test_inactive_and_no_longer_required_requests_are_never_linked(self):
        for updates in [{'status': 'withdrawn'}, {'status': 'cancelled'}, {'status': 'superseded'},
                        {'status': 'fulfilled', 'no_longer_required': True}]:
            result = route('급여명세서\n성명: 검증고객', [request(**updates)])
            self.assertIsNone(result['request_id'])
            self.assertEqual(result['candidate_request_ids'], [])

    def test_duplicate_requests_are_not_resolved_by_list_order(self):
        result = route('급여명세서\n성명: 검증고객', [request('first'), request('second')])
        self.assertEqual(result['status'], 'ambiguous')
        self.assertIsNone(result['request_id'])

    def test_same_bank_accounts_require_complete_matching_number(self):
        requests = [request('a', 'D36', institution='검증은행', account_key='account-a'),
                    request('b', 'D36', institution='검증은행', account_key='account-b')]
        accounts = [{'id': 'account-a', 'institution': '검증은행', 'number': '111-123-456'},
                    {'id': 'account-b', 'institution': '검증은행', 'number': '222-123-456'}]
        for number, expected in [('111123456', 'a'), ('222-123-456', 'b'), ('***-123-456', None), ('999-123-456', None)]:
            result = route('입출금거래내역\n성명: 검증고객\n금융기관: 검증은행 / 계좌번호: '+number,
                           requests, financial_accounts=accounts)
            self.assertEqual(result['request_id'], expected)

    def test_bank_counterparty_is_not_the_issuing_bank(self):
        result = route('입출금거래내역\n성명: 검증고객\n금융기관: 다른은행\n계좌번호: 11112222\n검증은행 이체',
                       [request(catalog_id='D36', institution='검증은행', account_key='one')],
                       financial_accounts=[{'id': 'one', 'number': '11112222'}])
        self.assertIsNone(result['request_id'])

    def test_short_bank_name_does_not_match_part_of_another_issuer(self):
        result = route('부채증명서\n다른검증은행 여신센터\n성명: 검증고객',
                       [request(catalog_id='D38', institution='검증은행')])
        self.assertIsNone(result['request_id'])

    def test_document_with_multiple_accounts_is_not_assigned_to_the_first(self):
        result = route('입출금거래내역\n성명: 검증고객\n금융기관: 검증은행\n계좌번호: 11112222\n계좌번호: 33334444',
                       [request(catalog_id='D36', institution='검증은행', account_key='one')],
                       financial_accounts=[{'id': 'one', 'number': '11112222'}])
        self.assertEqual(result['status'], 'ambiguous')

    def test_same_creditor_two_loans_are_resolved_by_loan_number(self):
        requests = [request('a', 'D38', institution='검증은행', account_key='loan-a'),
                    request('b', 'D38', institution='검증은행', account_key='loan-b')]
        creditors = [{'id': 'loan-a', 'loan_number': 'TEST-LOAN-A'}, {'id': 'loan-b', 'loan_number': 'TEST-LOAN-B'}]
        result = route('부채증명서\n성명: 검증고객\n채권자: 검증은행\n대출번호: TEST-LOAN-B', requests, creditors=creditors)
        self.assertEqual(result['request_id'], 'b')
        result = route('부채증명서\n성명: 검증고객\n채권자: 검증은행', requests, creditors=creditors)
        self.assertIsNone(result['request_id'])

    def test_multi_creditor_combined_certificate_is_held(self):
        result = route('부채증명서\n성명: 검증고객\n채권자: 검증은행\n채권자: 다른은행',
                       [request(catalog_id='D38', institution='검증은행')])
        self.assertEqual(result['status'], 'ambiguous')

    def test_partial_period_can_join_a_collection_without_claiming_complete(self):
        requests = [request('aug', period_start='2026-08-01', period_end='2026-08-31'),
                    request('sep', period_start='2026-09-01', period_end='2026-09-30')]
        result = route('급여명세서\n성명: 검증고객\n급여월: 2026-09', requests)
        self.assertEqual(result['request_id'], 'sep')
        self.assertEqual(result['matched_scope']['period_start'], '2026-09-01')
        self.assertNotIn('fulfilled', str(result))
        result = route('급여명세서\n성명: 검증고객\n발급일: 2026-09-30', requests)
        self.assertIsNone(result['request_id'])

    def test_one_file_spanning_two_period_requests_needs_review(self):
        result = route('급여명세서\n성명: 검증고객\n급여기간: 2026-08-01 ~ 2026-09-30',
                       [request('aug', period_start='2026-08-01', period_end='2026-08-31'),
                        request('sep', period_start='2026-09-01', period_end='2026-09-30')])
        self.assertEqual(result['status'], 'ambiguous')

    def test_different_document_kinds_on_later_pages_are_held(self):
        result = document_routing.route_documents({'client_name': '검증고객', 'requests': [request()]},
            [entry('', page_texts=[{'page': 1, 'text': '급여명세서\n성명: 검증고객'},
                                   {'page': 2, 'text': '부채증명서\n성명: 검증고객'}])])[0]
        self.assertEqual(result['status'], 'ambiguous')

    def test_single_unresolved_request_accepts_collection_but_retains_scope_pending(self):
        result = route('부채증명서\n성명: 검증고객\n채권자: 검증은행',
                       [request(catalog_id='D38', scope_unresolved=True)])
        self.assertEqual(result['request_id'], 'req')
        self.assertTrue(result['scope_pending'])
        self.assertTrue(result['matched_scope']['scope_pending'])
        result = route('부채증명서\n성명: 검증고객\n채권자: 검증은행',
                       [request('a', 'D38', scope_unresolved=True), request('b', 'D38', scope_unresolved=True)])
        self.assertIsNone(result['request_id'])

    def test_exact_scoped_request_wins_over_unresolved_collection(self):
        result = route('부채증명서\n성명: 검증고객\n채권자: 검증은행',
                       [request('generic', 'D38', scope_unresolved=True), request('exact', 'D38', institution='검증은행')])
        self.assertEqual(result['request_id'], 'exact')

    def test_supporting_receipts_use_explicit_accepted_catalogs_only(self):
        body = '보증금 및 차임 납부확인\n성명: 검증고객'
        self.assertIsNone(route(body, [request(catalog_id='D33')])['request_id'])
        self.assertEqual(route(body, [request(catalog_id='D33', accepted_catalog_ids=['SUPPORT-HOUSING'])])['request_id'], 'req')

    def test_no_network_or_models_are_used(self):
        with patch('socket.socket', side_effect=AssertionError('Routing must stay local')):
            self.assertEqual(route('급여명세서\n성명: 검증고객')['status'], 'matched')


class RandomFilenameFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pymupdf
        folder = ROOT / 'examples/court_ready_fixture'
        cls.manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        cls.case = domain.new_case(cls.manifest['name'], 'CT01', '서울회생법원', '', True)
        cls.case['consultation'] = {'notes': (folder / 'staff_consultation.txt').read_text(encoding='utf-8')}
        # Independent planning: answer-key catalog IDs and amounts are only
        # inspected AFTER routing. Neither filenames nor answers enter parsing.
        cls.entries = []
        for index, original in enumerate(cls.manifest['documents']):
            with pymupdf.open(folder / original['file']) as pdf:
                pages = [{'page': number, 'text': page.get_text()} for number, page in enumerate(pdf, 1)]
            cls.entries.append({'id': f'random-{index}', 'filename': f'{index * 8731:012x}.pdf',
                                'text': '\n'.join(page['text'] for page in pages), 'page_texts': pages})

    def test_all_29_random_named_originals_route_to_matching_kind_in_generic_collections(self):
        case = deepcopy(self.case)
        court_rules.reconcile(case, as_of='2026-10-04')
        results = document_routing.route_documents(case, self.entries)
        by_id = {row['id']: row for row in case['requests']}
        self.assertEqual(len(results), 29)
        for original, result in zip(self.manifest['documents'], results):
            with self.subTest(original=original['id'], reason=result['reason']):
                self.assertEqual(result['status'], 'matched')
                target = by_id[result['request_id']]
                self.assertIn(original['catalog_id'], {target['catalog_id'], *target.get('accepted_catalog_ids', [])})
                self.assertEqual(result['matched_scope']['catalog_id'], original['catalog_id'])
                if target.get('scope_unresolved'):
                    self.assertTrue(result['scope_pending'])

    def test_resolved_scope_connects_banks_creditors_and_employer_without_filename(self):
        case = deepcopy(self.case)
        case['financial_accounts'] = [
            {'id': 'bank-a', 'institution': '가상새봄은행', 'number': 'TEST-JIWOO-A-001'},
            {'id': 'bank-b', 'institution': '가상온유은행', 'number': 'TEST-JIWOO-B-002'}]
        case['creditors'] = [{'id': 'loan-a', 'name': '가상새봄은행'},
                             {'id': 'loan-b', 'name': '가상한결저축은행'},
                             {'id': 'loan-c', 'name': '가상디딤캐피탈'}]
        case['employers'] = [{'id': 'employer', 'name': '가상다온물류 주식회사'}]
        case['insurance_policies'] = [{'id': 'policy', 'name': '가상온기생명', 'policy_number': 'TEST-POLICY-JW-001'}]
        court_rules.reconcile(case, as_of='2026-10-04')
        results = document_routing.route_documents(case, self.entries)
        by_id = {row['id']: row for row in case['requests']}
        for original, result in zip(self.manifest['documents'], results):
            with self.subTest(original=original['id'], reason=result['reason']):
                self.assertEqual(result['status'], 'matched')
                target = by_id[result['request_id']]
                self.assertIn(original['catalog_id'], {target['catalog_id'], *target.get('accepted_catalog_ids', [])})
        self.assertEqual(by_id[results[14]['request_id']]['account_key'], 'bank-a')
        self.assertEqual(by_id[results[15]['request_id']]['account_key'], 'bank-b')
        self.assertEqual([by_id[row['request_id']]['account_key'] for row in results[5:8]], ['loan-a', 'loan-b', 'loan-c'])


if __name__ == '__main__':
    unittest.main()
