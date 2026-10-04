"""Multi-original requests: one atomic upload, complete scope and complete review."""
import asyncio
import copy
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from apps.api import (automation, ax_engine, ax_service, court_rules, domain,
                      extraction_readiness, main, request_collection, store)


def collection_case():
    case = domain.new_case('검증고객', 'CT01', '서울회생법원', '가상 묶음 제출', True)
    case['intake'] = {'status': 'completed'}
    case['requests'] = [{'id': 'req', 'catalog_id': 'D07', 'title': '월별 급여명세서',
        'status': 'received', 'period': '2026년 8~9월', 'document_ids': ['first', 'second'], 'version': 1}]
    for docid, month in [('first', '08'), ('second', '09')]:
        text = f'급여명세서\n성명: 검증고객\n급여월: 2026-{month}\n월 실수령액: 2,800,000원'
        case['documents'].append({'id': docid, 'request_id': 'req', 'version': 1, 'sha256': 'test-' + docid,
            'status': 'received', 'text': text, 'page_texts': [{'page': 1, 'text': text}]})
    case['extraction_candidates'] = ax_engine.extract_factor_candidates(ax_engine.case_sources(case))
    for doc in case['documents']:
        extraction_readiness.capture(case, doc)
    return case


async def supported(kind, payload, **kwargs):
    return {'status': 'passed', 'passed': True,
        'checked_item_ids': [item['id'] for item in payload['items']],
        'findings': [{'item_id': item['id'], 'status': 'supported'} for item in payload['items']]}


class CollectionValidationTests(unittest.TestCase):
    def test_scope_verification_receives_all_active_originals(self):
        case = collection_case()
        with patch('apps.api.verification.run_local_verification_batched', side_effect=supported) as verify:
            complete, _ = asyncio.run(automation._validate_requests(case, []))
        self.assertTrue(complete)
        payload = verify.call_args.args[1]
        self.assertEqual(payload['items'][0]['source_ids'], ['first', 'second'])
        self.assertEqual({source['id'] for source in payload['sources']}, {'first', 'second'})
        self.assertTrue(all(doc['status'] == 'verified' for doc in case['documents']))
        self.assertEqual(case['requests'][0]['collection_progress']['verified'], 2)

    def test_older_incomplete_file_blocks_even_when_latest_file_is_complete(self):
        case = collection_case()
        case['documents'][0]['extraction_status'] = 'running'
        with patch('apps.api.verification.run_local_verification_batched', side_effect=supported) as verify:
            complete, unavailable = asyncio.run(automation._validate_requests(case, []))
        self.assertFalse(complete)
        self.assertTrue(unavailable)
        verify.assert_not_called()
        self.assertEqual(case['requests'][0]['status'], 'received')

    def test_failed_additional_file_does_not_erase_previous_review(self):
        case = collection_case()
        case['documents'][0]['status'] = 'verified'
        failed = {'status': 'needs_review', 'passed': False, 'checked_item_ids': ['req'],
                  'findings': [{'item_id': 'req', 'status': 'unsupported'}]}
        with patch('apps.api.verification.run_local_verification_batched', new=AsyncMock(return_value=failed)):
            complete, _ = asyncio.run(automation._validate_requests(case, []))
        self.assertFalse(complete)
        self.assertEqual(case['documents'][0]['status'], 'verified')
        self.assertEqual(case['documents'][1]['status'], 'needs_more')

    def test_additional_file_invalidates_the_collection_scope_cache(self):
        case = collection_case()
        case['documents'] = case['documents'][:1]
        with patch('apps.api.verification.run_local_verification_batched', side_effect=supported) as verify:
            self.assertTrue(asyncio.run(automation._validate_requests(case, []))[0])
            self.assertTrue(asyncio.run(automation._validate_requests(case, []))[0])
            self.assertEqual(verify.call_count, 1)
            other = collection_case()['documents'][1]
            case['documents'].append(other)
            self.assertTrue(asyncio.run(automation._validate_requests(case, []))[0])
            self.assertEqual(verify.call_count, 2)

    def test_adjacent_explicit_issuer_periods_merge_but_gaps_do_not(self):
        request = {'period_start': '2026-08-01', 'period_end': '2026-09-30'}
        documents = [{'document_metadata': {'period_start': '2026-08-01', 'period_end': '2026-08-31'}},
                     {'document_metadata': {'period_start': '2026-09-01', 'period_end': '2026-09-30'}}]
        metadata = request_collection.combined_metadata(request, documents)
        self.assertEqual(court_rules.validate_metadata(request, {'document_metadata': metadata})['status'], 'matched')
        documents[1]['document_metadata']['period_start'] = '2026-09-02'
        metadata = request_collection.combined_metadata(request, documents)
        self.assertEqual(court_rules.validate_metadata(request, {'document_metadata': metadata})['status'], 're_request')

    def test_missing_scope_metadata_cannot_be_made_complete_by_ai(self):
        case = collection_case()
        case['requests'][0].update(managed_by=court_rules.MANAGED_BY, institution='확인되지 않은 기관')
        with patch('apps.api.verification.run_local_verification_batched', side_effect=supported):
            complete, unavailable = asyncio.run(automation._validate_requests(case, []))
        self.assertFalse(complete)
        self.assertTrue(unavailable)
        self.assertEqual(case['requests'][0]['status'], 'received')


class BatchUploadTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(store, 'DATA_DIR', Path(folder)))
        self.stack.enter_context(patch.dict(os.environ, {'DEBTOFF_AUTO_AX': '0'}))
        self.schedule = self.stack.enter_context(patch.object(ax_service, 'maybe_schedule'))
        self.user = {'id': 'staff', 'name': '검증직원', 'role': 'staff', 'org_id': 'office-1'}
        self.stack.enter_context(patch.dict(main.app.dependency_overrides, {main.current_user: lambda: self.user}))
        store.initialize()
        self.case = domain.new_case('검증고객', 'CT01', '서울회생법원', '합성 다중 업로드', True)
        self.case['intake'] = {'status': 'completed'}
        self.case['requests'] = [{'id': 'req', 'catalog_id': 'D07', 'title': '월별 급여명세서',
            'status': 'requested', 'document_ids': [], 'version': 1}]
        store.insert_case(self.case)
        self.client = self.stack.enter_context(TestClient(main.app, raise_server_exceptions=False))
        self.base = '/api/cases/' + self.case['id']

    def upload(self, contents, **extra):
        return self.client.post(self.base + '/documents/batch',
            data={'expected_version': store.get_case(self.case['id'])['version'], 'request_id': 'req', **extra},
            files=[('files', (name, content, 'text/plain')) for name, content in contents])

    def test_batch_stores_all_files_with_one_revision_and_one_scheduling_call(self):
        response = self.upload([('8월.txt', b'August original'), ('9월.txt', b'September original'), ('10월.txt', b'October original')])
        self.assertEqual(response.status_code, 200, response.text)
        case = store.get_case(self.case['id'])
        self.assertEqual(len(case['documents']), 3)
        self.assertEqual(case['version'], self.case['version'] + 1)
        self.assertEqual(case['input_revision'], self.case['input_revision'] + 1)
        self.assertEqual(len(case['requests'][0]['document_ids']), 3)
        self.schedule.assert_called_once()
        self.assertEqual(len(list((store.DATA_DIR / 'uploads' / case['id']).iterdir())), 3)

    def test_parse_failure_saves_none_and_preserves_case_version(self):
        response = self.upload([('valid.txt', b'Original'), ('invalid.pdf', b'Not a PDF')])
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(store.get_case(self.case['id']), self.case)
        self.schedule.assert_not_called()
        self.assertFalse((store.DATA_DIR / 'uploads' / self.case['id']).exists())

    def test_duplicate_or_stale_batch_cleans_every_new_file(self):
        self.assertEqual(self.upload([('existing.txt', b'Existing')]).status_code, 200)
        before = store.get_case(self.case['id'])
        self.schedule.reset_mock()
        response = self.upload([('same.txt', b'Existing'), ('new.txt', b'New')])
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(store.get_case(self.case['id']), before)
        self.assertEqual(len(list((store.DATA_DIR / 'uploads' / before['id']).iterdir())), 1)
        self.schedule.assert_not_called()

    def test_additional_upload_preserves_verified_original_and_reopens_collection(self):
        self.assertEqual(self.upload([('existing.txt', b'Existing')]).status_code, 200)
        original = store.get_case(self.case['id'])['documents'][0]
        reviewed = self.client.post(self.base + '/documents/' + original['id'] + '/review', json={
            'expected_version': store.get_case(self.case['id'])['version'], 'scope_confirmed': True,
            'content_confirmed': True, 'person_confirmed': True, 'reason': '합성 원문을 함께 대조했습니다.'})
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(self.upload([('additional.txt', b'Additional')]).status_code, 200)
        current = store.get_case(self.case['id'])
        self.assertEqual(current['documents'][0]['status'], 'verified')
        self.assertEqual(current['requests'][0]['status'], 'received')
        self.assertEqual(len(current['documents']), 2)

    def test_each_document_review_is_needed_to_complete_a_collection(self):
        self.assertEqual(self.upload([('first.txt', b'First'), ('second.txt', b'Second')]).status_code, 200)
        documents = store.get_case(self.case['id'])['documents']
        for index, document in enumerate(documents):
            response = self.client.post(self.base + '/documents/' + document['id'] + '/review', json={
                'expected_version': store.get_case(self.case['id'])['version'], 'scope_confirmed': True,
                'content_confirmed': True, 'person_confirmed': True, 'reason': '원문과 추출값을 모두 확인했습니다.'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(store.get_case(self.case['id'])['requests'][0]['status'], 'fulfilled' if index == 1 else 'received')

    def test_only_explicit_replacement_supersedes_an_original(self):
        self.assertEqual(self.upload([('first.txt', b'First')]).status_code, 200)
        original = store.get_case(self.case['id'])['documents'][0]
        response = self.upload([('corrected.txt', b'Corrected')], replaces_document_id=original['id'])
        self.assertEqual(response.status_code, 200, response.text)
        current = store.get_case(self.case['id'])
        self.assertEqual(current['documents'][0]['status'], 'superseded')
        self.assertEqual(current['documents'][1]['replaces_document_id'], original['id'])
        self.assertTrue((store.DATA_DIR / 'uploads' / current['id'] / original['storage_name']).is_file())

    def test_batch_limit_fails_before_reading_or_storing_any_file(self):
        response = self.upload([(str(index) + '.txt', str(index).encode()) for index in range(21)])
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(store.get_case(self.case['id']), self.case)

    def test_post_commit_scheduling_error_never_deletes_committed_originals(self):
        self.schedule.side_effect = RuntimeError('Scheduling failed after commit')
        response = self.upload([('first.txt', b'First'), ('second.txt', b'Second')])
        self.assertEqual(response.status_code, 500)
        saved = store.get_case(self.case['id'])
        self.assertEqual(len(saved['documents']), 2)
        for document in saved['documents']:
            self.assertTrue((store.DATA_DIR / 'uploads' / saved['id'] / document['storage_name']).is_file())

    def test_fulfilled_but_no_longer_required_request_rejects_upload(self):
        def retire(case):
            case['requests'][0].update(status='fulfilled', no_longer_required=True)
        main.change(self.case['id'], self.user, self.case['version'], 'test.request_retired', retire)
        before = store.get_case(self.case['id'])
        response = self.upload([('unneeded.txt', b'No longer requested')])
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()['code'], 'REQUEST_WITHDRAWN')
        self.assertEqual(store.get_case(self.case['id']), before)
        self.assertEqual(list((store.DATA_DIR / 'uploads' / before['id']).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
