"""Parsing completion is distinct from a human decision or semantic AI review."""
import copy
import unittest
from unittest.mock import patch

from apps.api import ax_service, extraction_readiness as readiness


class ExtractionReadinessTests(unittest.TestCase):
    def case(self):
        document = {'id': 'source', 'filename': '합성급여.txt', 'status': 'received',
            'sha256': 'synthetic-file-hash', 'version': 1,
            'text': '월 실수령액 3,200,000원',
            'page_texts': [{'page': 1, 'text': '월 실수령액 3,200,000원'}],
            'extraction_status': 'extracted',
            'ocr_summary': {'page_count': 1, 'unreadable_pages': []}}
        return {'documents': [document], 'extraction_candidates': []}, document

    def complete(self, case, document, receipt=True):
        rows = readiness.candidates(document)
        ax_service.enrich_candidates(case, rows, document['id'])
        if receipt:
            readiness.capture(case, document, rows)

    def test_nonempty_text_alone_is_not_completion(self):
        case, doc = self.case()
        state = readiness.document_state(case, doc)
        self.assertFalse(state['can_review'])
        self.assertEqual(state['status'], 'pending')
        self.assertGreater(len(readiness.candidates(doc)), 0)

    def test_complete_legacy_candidates_allow_review_without_ai_approval(self):
        case, doc = self.case()
        self.complete(case, doc, receipt=False)
        doc['ai_review'] = {'status': 'needs_review', 'passed': False}
        state = readiness.document_state(case, doc)
        self.assertEqual(state['status'], 'completed')
        self.assertTrue(state['can_review'])
        self.assertFalse(doc['ai_review']['passed'])
        self.assertEqual(doc['status'], 'received')

    def test_missing_candidate_fails_legacy_and_manifest_completeness(self):
        for receipt in (False, True):
            with self.subTest(receipt=receipt):
                case, doc = self.case()
                self.complete(case, doc, receipt=receipt)
                case['extraction_candidates'].clear()
                self.assertFalse(readiness.document_state(case, doc)['can_review'])

    def test_empty_or_missing_pages_and_partial_status_fail_closed(self):
        for mutation in (
            {'page_texts': [{'page': 1, 'text': '월 실수령액 3,200,000원'}, {'page': 2, 'text': ''}]},
            {'ocr_summary': {'page_count': 2, 'unreadable_pages': []}},
            {'ocr_summary': {'page_count': 1, 'unreadable_pages': [1]}},
            {'extraction_status': 'partial'}, {'extraction_status': 'manual_review'},
            {'text': '', 'page_texts': []},
        ):
            with self.subTest(mutation=mutation):
                case, doc = self.case()
                self.complete(case, doc)
                doc.update(mutation)
                state = readiness.document_state(case, doc)
                self.assertFalse(state['can_review'])
                self.assertEqual(state['status'], 'failed')

    def test_original_or_page_or_parser_version_change_invalidates_completion_receipt(self):
        for key, value in (('text', '월 실수령액 3,300,000원'),
                           ('page_texts', [{'page': 1, 'text': '월 실수령액 3,300,000원'}]),
                           ('version', 2), ('sha256', 'different-file-hash')):
            with self.subTest(key=key):
                case, doc = self.case()
                self.complete(case, doc)
                doc[key] = value
                self.assertFalse(readiness.document_state(case, doc)['can_review'])
        case, doc = self.case()
        self.complete(case, doc)
        with patch('apps.api.ax_engine.HARNESS_VERSION', 'new-parser-test-version'):
            self.assertFalse(readiness.document_state(case, doc)['can_review'])

    def test_no_typed_values_can_be_reviewed_as_original_only(self):
        case, doc = self.case()
        doc.update(text='서류의 별도 설명 문장입니다.',
                   page_texts=[{'page': 1, 'text': '서류의 별도 설명 문장입니다.'}])
        self.assertEqual(readiness.candidates(doc), [])
        self.complete(case, doc)
        state = readiness.document_state(case, doc)
        self.assertTrue(state['can_review'])
        self.assertEqual(state['extracted_count'], 0)
        self.assertIn('원문', state['message'])

    def test_nonempty_page_with_excluded_lines_or_incomplete_ocr_is_not_complete(self):
        for details in ({'status': 'manual_review'}, {'text_truncated': True},
                        {'lines': [{'text': '읽지 못한 금액', 'requires_review': True}]}):
            with self.subTest(details=details):
                case, doc = self.case()
                doc['page_texts'][0].update(details)
                self.complete(case, doc)
                self.assertEqual(doc['extraction_manifest']['status'], 'failed')
                self.assertFalse(readiness.document_state(case, doc)['can_review'])

    def test_active_parser_job_has_progress_status_not_permission(self):
        case, doc = self.case()
        case['ax_runs'] = [{'status': 'running'}]
        self.assertEqual(readiness.document_state(case, doc)['status'], 'running')
        self.assertFalse(readiness.document_state(case, doc)['can_review'])
        doc['extraction_status'] = 'pending'
        self.assertEqual(readiness.document_state(case, doc)['status'], 'pending')

    def test_human_correction_retains_original_completion_identity_without_reparse_promotion(self):
        case, doc = self.case()
        self.complete(case, doc)
        original_signature = case['extraction_candidates'][0]['signature']
        candidate = case['extraction_candidates'][0]
        candidate.update(value=3300000, original_value=candidate['value'], origin='human_correction', status='accepted')
        self.assertEqual(candidate['signature'], original_signature)
        self.assertTrue(readiness.document_state(case, doc)['can_review'])
        self.assertEqual(doc['text'], '월 실수령액 3,200,000원')

    def test_projection_does_not_mutate_candidate_values(self):
        case, doc = self.case()
        self.complete(case, doc)
        prior = copy.deepcopy(case['extraction_candidates'])
        readiness.project(case)
        self.assertEqual(case['extraction_candidates'], prior)
        self.assertTrue(doc['extraction_state']['can_review'])


if __name__ == '__main__':
    unittest.main()
