"""Independent aggregation boundaries: originals, identities, conflicts, duplicates."""
import copy
from pathlib import Path
import unittest

import pymupdf

from apps.api import evidence_mapping, store

ROOT = Path(__file__).resolve().parents[1]


def document(identifier, text=None, pages=None):
    pages = pages or [{'page': 1, 'text': text}]
    return {'id': identifier, 'status': 'verified', 'page_texts': pages,
            'sha256': store.digest(pages), 'text': '\n'.join(page['text'] for page in pages)}


def build(*documents):
    return evidence_mapping.build({'documents': list(documents), 'court_id': 'CT01', 'input_revision': 1})


def bank(account, amount, institution='검증은행', as_of='2026-10-01'):
    return f'예금계좌 조회\n금융기관: {institution}\n계좌번호: {account}\n잔액 기준일: {as_of}\n예금잔액: {amount:,}원'


def debt(name, principal, interest, loan=None):
    return f'부채증명서\n채권자: {name}\n' + (f'대출번호: {loan}\n' if loan else '') + f'원금: {principal:,}원 / 이자: {interest:,}원\n담보 없음\n기준일: 2026-10-01'


class EvidenceAggregationIntegrityTests(unittest.TestCase):
    def test_original_three_creditors_in_one_pdf_have_correct_separate_amounts(self):
        path = next((ROOT / 'examples/synthetic_case/documents').glob('03_*.pdf'))
        with pymupdf.open(path) as pdf:
            pages = [{'page': i+1, 'text': page.get_text(sort=True)} for i, page in enumerate(pdf)]
        packet = build(document('original-debts', pages=pages))
        creditors = packet['inputs']['creditors']
        self.assertEqual(len(creditors), 3)
        self.assertEqual(sorted((row['principal'], row['interest']) for row in creditors),
                         [(15000000, 600000), (25000000, 1000000), (35000000, 1400000)])
        self.assertEqual(packet['form_values']['total_debt'], 78000000)
        self.assertTrue(all(len(row['evidence_ids']) == 1 for row in creditors))

    def test_two_loans_of_one_bank_in_one_document_are_not_merged(self):
        packet = build(document('two-loans', debt('검증은행', 23000000, 700000, 'TEST-LOAN-A') + '\n' +
                                debt('검증은행', 8500000, 210000, 'TEST-LOAN-B')))
        self.assertEqual(len(packet['inputs']['creditors']), 2)
        self.assertEqual(packet['form_values']['total_debt'], 32410000)

    def test_duplicate_loan_files_do_not_double_the_debt(self):
        text = debt('검증은행', 23000000, 700000, 'TEST-LOAN-A')
        packet = build(document('first', text), document('duplicate', text))
        self.assertEqual(len(packet['inputs']['creditors']), 1)
        self.assertEqual(packet['form_values']['total_debt'], 23700000)

    def test_duplicate_account_and_unidentified_narrative_are_not_additional_assets(self):
        text = bank('TEST-ACCOUNT-A', 1250000)
        packet = build(document('first', text), document('duplicate', text),
                       document('second', bank('TEST-ACCOUNT-B', 650000)),
                       document('history', '생활비 진술\n예금: 1,900,000원'))
        self.assertEqual(packet['form_values']['bank_balance'], 1900000)
        self.assertEqual(packet['form_values']['assets_total'], 1900000)

    def test_same_date_account_conflict_cannot_choose_one_or_publish_partial_total(self):
        packet = build(document('first', bank('TEST-ACCOUNT-A', 1200000)),
                       document('conflict', bank('TEST-ACCOUNT-A', 900000)),
                       document('second', bank('TEST-ACCOUNT-B', 650000)))
        self.assertNotIn('bank_balance', packet['form_values'])
        self.assertTrue(any(row['code'] == 'CONFLICTING_TYPED_VALUES' for row in packet['errors']))

    def test_latest_dated_balance_replaces_older_snapshot_without_addition(self):
        packet = build(document('old', bank('TEST-ACCOUNT-A', 1100000, as_of='2026-08-31')),
                       document('new', bank('TEST-ACCOUNT-A', 950000, as_of='2026-09-30')))
        self.assertEqual(packet['form_values']['bank_balance'], 950000)

    def test_reissued_old_balance_does_not_override_later_reference_balance(self):
        old = bank('TEST-ACCOUNT-A', 1100000, as_of='2026-08-31') + '\n발급일: 2026-10-04'
        current = bank('TEST-ACCOUNT-A', 950000, as_of='2026-09-30') + '\n발급일: 2026-10-01'
        packet = build(document('old-reissued', old), document('new-balance', current))
        self.assertEqual(packet['form_values']['bank_balance'], 950000)

    def test_family_reimbursement_does_not_silently_replace_or_join_financial_debt(self):
        paths = list((ROOT / 'examples/synthetic_case/documents').glob('03_*.pdf')) + list((ROOT / 'examples/synthetic_case/documents').glob('07_*.pdf'))
        docs = []
        for path in paths:
            with pymupdf.open(path) as pdf:
                pages = [{'page': i+1, 'text': page.get_text(sort=True)} for i, page in enumerate(pdf)]
            docs.append(document(path.stem, pages=pages))
        packet = build(*docs)
        self.assertEqual(packet['form_values']['total_debt'], 78000000)
        self.assertNotEqual(packet['form_values']['total_debt'], 80000000)
        self.assertEqual(len(packet['inputs']['creditors']), 3)

    def test_rejected_identity_conflicting_documents_do_not_authorize_values(self):
        bad = document('other-person', bank('OTHER-ACCOUNT', 90000000))
        bad['automated_check'] = {'coverage_status': 'identity_conflict'}
        packet = build(document('mine', bank('TEST-ACCOUNT-A', 600000)), bad)
        self.assertEqual(packet['form_values']['bank_balance'], 600000)
        self.assertNotIn('other-person', {r['document_id'] for r in packet['facts']})

    def test_unidentified_loans_do_not_publish_other_creditors_as_complete_total(self):
        packet = build(document('unknown-a', debt('검증은행', 1000000, 10000)),
                       document('unknown-b', debt('검증은행', 2000000, 20000)),
                       document('identified', debt('다른은행', 3000000, 30000, 'OTHER-LOAN')))
        self.assertNotIn('total_debt', packet['form_values'])
        self.assertTrue(any(row['code'] == 'LOAN_IDENTITY_REQUIRED' for row in packet['errors']))

    def test_conflicting_one_loan_prevents_partial_total(self):
        packet = build(document('old', debt('검증은행', 1000000, 10000, 'SAME-LOAN')),
                       document('conflict', debt('검증은행', 2000000, 10000, 'SAME-LOAN')),
                       document('other', debt('다른은행', 3000000, 30000, 'OTHER-LOAN')))
        self.assertNotIn('total_debt', packet['form_values'])

    def test_account_without_bank_cannot_be_added_as_a_different_account(self):
        without_bank = '계좌번호: TEST-ACCOUNT-A\n잔액 기준일: 2026-10-01\n예금잔액: 1,200,000원'
        packet = build(document('identified', bank('TEST-ACCOUNT-A', 1200000)),
                       document('unidentified', without_bank))
        # The second source may reconcile the first; it cannot prove a second
        # distinct account at an unknown institution and double the assets.
        self.assertNotEqual(packet['form_values'].get('bank_balance'), 2400000)
