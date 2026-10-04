"""Safe local ZIP intake: original bytes, atomic attachment, durable progress."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import time
import unittest
import zipfile
import zlib
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from unittest.mock import patch

from fastapi.testclient import TestClient
from apps.api import archive_import, ax_service, court_rules, domain, extraction_readiness, main, store


def make_zip(entries, compression=zipfile.ZIP_STORED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=compression) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def raw_zip(name, content=b'original', flags=0, extra=b''):
    """Legacy CP949/Unicode-extra and encryption-bit fixtures without decoders."""
    crc, size = zlib.crc32(content), len(content)
    local = struct.pack('<IHHHHHIIIHH', 0x04034b50, 20, flags, 0, 0, 0,
                        crc, size, size, len(name), len(extra)) + name + extra + content
    central = struct.pack('<IHHHHHHIIIHHHHHII', 0x02014b50, 20, 20, flags, 0, 0, 0,
                          crc, size, size, len(name), len(extra), 0, 0, 0, 0, 0) + name + extra
    return local + central + struct.pack('<IHHHHIIH', 0x06054b50, 0, 0, 1, 1, len(central), len(local), 0)


class ImmediatePool:
    def submit(self, fn, *args):
        future = Future()
        try:
            future.set_result(fn(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future


class HeldPool:
    def submit(self, *_args):
        return Future()


class ArchiveSafetyTests(unittest.TestCase):
    def assert_blocked(self, content, code, filename='original.zip'):
        with self.assertRaises(domain.DomainError) as raised:
            archive_import.inspect_archive(content, filename)
        self.assertEqual(raised.exception.code, code)

    def test_ordinary_zip_and_alzip_standard_zip_are_supported(self):
        result = archive_import.inspect_archive(make_zip([('폴더/급여.txt', '급여명세서')]), '알집에서생성.zip')
        self.assertEqual(result[0]['filename'], '급여.txt')
        self.assertEqual(result[0]['status'], 'pending')

    def test_special_alz_egg_are_not_falsely_claimed_to_be_supported(self):
        for name in ['서류.alz', '서류.egg']:
            self.assert_blocked(b'ALZ-or-EGG', 'ARCHIVE_FORMAT_UNSUPPORTED', name)

    def test_unsafe_member_paths_fail_before_any_reads(self):
        for name in ['../secret.txt', '/absolute.txt', 'C:/private.txt', 'safe/../../bad.txt',
                     'safe\\..\\bad.txt', '\\\\server\\share.txt', 'a/evil\x01.txt']:
            with self.subTest(name=name):
                self.assert_blocked(make_zip([(name, b'content')]), 'ARCHIVE_UNSAFE_PATH')

    def test_symlink_and_encrypted_members_are_rejected(self):
        member = zipfile.ZipInfo('link.txt')
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.assert_blocked(make_zip([(member, b'../target')]), 'ARCHIVE_SPECIAL_ENTRY')
        self.assert_blocked(raw_zip(b'file.txt', flags=1), 'ARCHIVE_ENCRYPTED')

    def test_nested_archives_duplicate_paths_and_bombs_are_rejected(self):
        self.assert_blocked(make_zip([('nested.zip', b'not opened')]), 'ARCHIVE_NESTED')
        self.assert_blocked(make_zip([('File.txt', b'one'), ('file.txt', b'two')]), 'ARCHIVE_DUPLICATE_PATH')
        self.assert_blocked(make_zip([('huge.txt', b'0' * 500_000)], zipfile.ZIP_DEFLATED), 'ARCHIVE_COMPRESSION_RATIO')

    def test_limits_are_checked_on_metadata_before_decompression(self):
        archive = make_zip([('a.txt', b'1234'), ('b.txt', b'5678')])
        for setting, value, expected in [('MAX_FILE_BYTES', 3, 'ARCHIVE_MEMBER_SIZE'),
                                         ('MAX_TOTAL_BYTES', 6, 'ARCHIVE_EXPANDED_SIZE'),
                                         ('MAX_FILES', 1, 'ARCHIVE_FILE_LIMIT'),
                                         ('MAX_ARCHIVE_BYTES', 1, 'ARCHIVE_SIZE')]:
            with self.subTest(setting=setting), patch.object(archive_import, setting, value):
                self.assert_blocked(archive, expected)

    def test_metadata_is_skipped_but_not_interpreted_as_document_facts(self):
        entries = archive_import.inspect_archive(make_zip([('a.txt', b'original'),
            ('manifest.json', b'{"approved":true}'), ('__MACOSX/x.txt', b'private'),
            ('.DS_Store', b'metadata'), ('Thumbs.db', b'thumbnail')]), 'docs.zip')
        self.assertEqual([entry['status'] for entry in entries], ['pending'] + ['skipped'] * 4)
        self.assert_blocked(make_zip([('manifest.json', b'{}')]), 'ARCHIVE_NO_DOCUMENTS')

    def test_legacy_korean_names_and_unicode_extra_are_validated(self):
        entries = archive_import.inspect_archive(raw_zip('급여명세서.txt'.encode('cp949')), 'old.zip')
        self.assertEqual(entries[0]['filename'], '급여명세서.txt')
        name = b'legacy.txt'
        for decoded in ['새이름.txt', '../outside.txt']:
            payload = b'\x01' + struct.pack('<I', zlib.crc32(name)) + decoded.encode('utf-8')
            extra = struct.pack('<HH', 0x7075, len(payload)) + payload
            content = raw_zip(name, extra=extra)
            if decoded.startswith('..'):
                self.assert_blocked(content, 'ARCHIVE_UNSAFE_PATH')
            else:
                self.assertEqual(archive_import.inspect_archive(content, 'x.zip')[0]['filename'], decoded)


class ArchiveApiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(store, 'DATA_DIR', Path(folder)))
        self.stack.enter_context(patch.dict(os.environ, {'DEBTOFF_AUTO_AX': '0'}))
        self.stack.enter_context(patch.object(archive_import, 'POOL', ImmediatePool()))
        self.stack.enter_context(patch.object(archive_import, 'FUTURES', {}))
        self.schedule = self.stack.enter_context(patch.object(ax_service, 'maybe_schedule'))
        self.user = {'id': 'staff', 'name': '검증직원', 'role': 'staff', 'org_id': 'office-1'}
        self.stack.enter_context(patch.dict(main.app.dependency_overrides, {main.current_user: lambda: self.user}))
        store.initialize()
        self.case = domain.new_case('검증고객', 'CT01', '서울회생법원', '합성 압축자료', True)
        self.case['client_user_id'] = 'client'
        self.case['intake'] = {'status': 'completed'}
        self.case['requests'] = [
            {'id': 'salary', 'catalog_id': 'D07', 'title': '급여명세서', 'status': 'requested', 'document_ids': [], 'version': 1},
            {'id': 'debt', 'catalog_id': 'D38', 'title': '부채증명서', 'status': 'requested', 'document_ids': [], 'version': 1, 'scope_unresolved': True}]
        store.insert_case(self.case)
        self.client = self.stack.enter_context(TestClient(main.app, raise_server_exceptions=False))
        self.base = '/api/cases/' + self.case['id'] + '/document-imports'

    def start(self, entries=None, *, content=None, token='', version=None):
        if content is None:
            content = make_zip(entries or [('random.txt', '급여명세서\n성명: 검증고객\n월 실수령액: 2,800,000원')])
        return self.client.post(self.base, data={'expected_version': version or store.get_case(self.case['id'])['version'],
            'client_token': token}, files={'archive': ('original.zip', content, 'application/zip')})

    def result(self, response):
        self.assertEqual(response.status_code, 202, response.text)
        response = self.client.get(self.base + '/' + response.json()['id'])
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_routing_commits_once_and_keeps_unmatched_originals(self):
        job = self.result(self.start([
            ('one/random.txt', '급여명세서\n성명: 검증고객\n월 실수령액: 2,800,000원'),
            ('two/random.txt', '부채증명서\n성명: 검증고객\n대출잔액: 78,000,000원'),
            ('unknown.txt', '이 서류는 종류 확인이 필요합니다.'), ('manifest.json', '{"income":999999}')]))
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual((job['imported_count'], job['matched_count'], job['review_count'], job['skipped_count']), (3, 2, 1, 1))
        case = store.get_case(self.case['id'])
        self.assertEqual(case['version'], self.case['version'] + 1)
        self.assertEqual(case['input_revision'], self.case['input_revision'] + 1)
        self.assertEqual([doc['request_id'] for doc in case['documents']], ['salary', 'debt', ''])
        self.assertTrue(case['documents'][2]['classification_review_required'])
        self.assertTrue(all(doc['status'] == 'received' and not doc['content_confirmed'] for doc in case['documents']))
        self.assertEqual(len({doc['storage_name'] for doc in case['documents']}), 3)
        self.assertTrue(all(req['status'] == 'received' for req in case['requests']))
        self.schedule.assert_called_once()
        public = json.dumps(job, ensure_ascii=False)
        self.assertNotIn('2,800,000', public)
        self.assertNotIn('archive_sha256', public)
        self.assertNotIn('runner_id', public)
        self.assertNotIn('archive_index', public)

    def test_wrong_identity_is_preserved_in_quarantine_not_linked(self):
        job = self.result(self.start([('x.txt', '급여명세서\n성명: 다른사람\n월 실수령액: 2,800,000원')]))
        self.assertEqual(job['status'], 'completed')
        doc = store.get_case(self.case['id'])['documents'][0]
        self.assertEqual(doc['status'], 'quarantined')
        self.assertFalse(doc['request_id'])
        self.assertEqual(doc['automated_check']['coverage_status'], 'identity_conflict')

    def test_bad_second_original_rolls_back_entire_import(self):
        job = self.result(self.start([('first.txt', b'Original'), ('second.pdf', b'Not a PDF')]))
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(store.get_case(self.case['id']), self.case)
        self.assertFalse((store.DATA_DIR / 'uploads' / self.case['id']).exists())
        self.schedule.assert_not_called()

    def test_unread_subject_name_can_be_reviewed_without_false_identity_conflict(self):
        job = self.result(self.start([('x.txt', '급여명세서\n월 실수령액: 2,800,000원')]))
        self.assertEqual(job['status'], 'completed')
        case = store.get_case(self.case['id'])
        document = case['documents'][0]
        self.assertEqual(document['status'], 'received')
        self.assertTrue(document['classification_review_required'])
        self.assertFalse(document['person_confirmed'])
        self.assertTrue(extraction_readiness.document_state(case, document)['can_review'])

    def test_identical_files_do_not_partially_append(self):
        job = self.result(self.start([('a.txt', b'Same original'), ('b.txt', b'Same original')]))
        self.assertEqual(job['error']['code'], 'DUPLICATE_FILE')
        self.assertEqual(store.get_case(self.case['id']), self.case)
        self.schedule.assert_not_called()

    def test_client_token_recovers_response_loss_without_second_commit(self):
        content = make_zip([('x.txt', b'Original archive bytes')])
        first = self.result(self.start(content=content, token='stable-client-token'))
        response = self.start(content=content, token='stable-client-token', version=1)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['id'], first['id'])
        self.assertEqual(len(store.get_case(self.case['id'])['documents']), 1)
        self.schedule.assert_called_once()
        listed = self.client.get(self.base).json()['jobs']
        self.assertEqual(listed[0]['client_token'], 'stable-client-token')
        response = self.start([('new.txt', b'New bytes')], token='stable-client-token')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'IMPORT_TOKEN_REUSED')

    def test_post_commit_scheduling_failure_preserves_all_originals(self):
        self.schedule.side_effect = RuntimeError('after commit')
        job = self.result(self.start([('a.txt', b'Original A'), ('b.txt', b'Original B')]))
        self.assertEqual(job['status'], 'completed', job)
        self.assertTrue(job['warning'])
        case = store.get_case(self.case['id'])
        self.assertEqual(len(case['documents']), 2)
        self.assertTrue(all((store.DATA_DIR / 'uploads' / case['id'] / doc['storage_name']).is_file() for doc in case['documents']))

    def test_version_change_during_reading_cannot_overwrite_staff_work(self):
        from apps.api import document_routing
        real_route = document_routing.route_documents
        def concurrent_edit(case, entries):
            store.mutate(case['id'], case['version'], self.user, 'test.concurrent', lambda current: current.update(summary='담당자 보완'))
            return real_route(case, entries)
        with patch.object(document_routing, 'route_documents', side_effect=concurrent_edit):
            job = self.result(self.start())
        self.assertEqual(job['error']['code'], 'IMPORT_VERSION_CONFLICT')
        case = store.get_case(self.case['id'])
        self.assertEqual(case['summary'], '담당자 보완')
        self.assertEqual(case['documents'], [])
        self.schedule.assert_not_called()

    def test_active_job_and_process_wide_queue_are_bounded(self):
        with patch.object(archive_import, 'POOL', HeldPool()):
            first = self.start()
            self.assertEqual(first.status_code, 202)
            second = self.start([('next.txt', b'Other')])
            self.assertEqual(second.status_code, 409)
            self.assertEqual(second.json()['detail']['code'], 'IMPORT_IN_PROGRESS')
            self.assertEqual(len(self.client.get(self.base).json()['jobs']), 1)
        archive_import.FUTURES.clear()
        for index in range(archive_import.MAX_PENDING_JOBS):
            archive_import.FUTURES[('other-case', str(index))] = Future()
        other = domain.new_case('다른고객', 'CT01', '서울회생법원', '', True)
        store.insert_case(other)
        response = self.client.post('/api/cases/' + other['id'] + '/document-imports',
            data={'expected_version': 1}, files={'archive': ('x.zip', make_zip([('a.txt', b'Original')]), 'application/zip')})
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json()['detail']['code'], 'IMPORT_QUEUE_FULL')
        self.assertFalse(archive_import._folder(other['id']).exists())

    def test_restart_marks_uncommitted_job_failed_and_recovers_committed_job(self):
        with patch.object(archive_import, 'POOL', HeldPool()):
            response = self.start()
        job_id = response.json()['id']
        path = archive_import._path(self.case['id'], job_id)
        saved = json.loads(path.read_text(encoding='utf-8'))
        saved['runner_id'] = 'old-process'
        archive_import._save(saved)
        job = self.client.get(self.base + '/' + job_id).json()
        self.assertEqual(job['error']['code'], 'IMPORT_INTERRUPTED')
        archive_import.FUTURES.clear()
        complete = self.result(self.start())
        path = archive_import._path(self.case['id'], complete['id'])
        saved = json.loads(path.read_text(encoding='utf-8'))
        saved.update(status='saving', runner_id='previous-process')
        saved['files'][0]['status'] = 'classified'
        archive_import._save(saved)
        recovered = self.client.get(self.base + '/' + complete['id']).json()
        self.assertEqual(recovered['status'], 'completed')
        self.assertEqual(recovered['imported_count'], 1)
        self.assertTrue(recovered['warning'])
        self.assertEqual(len(store.get_case(self.case['id'])['documents']), 1)

    def test_case_and_actor_authorization_apply_to_job_list_and_status(self):
        staff_job = self.result(self.start())
        self.user = {'id': 'client', 'role': 'client', 'name': '검증고객', 'org_id': 'office-1'}
        self.assertEqual(self.client.get(self.base).json()['jobs'], [])
        self.assertEqual(self.client.get(self.base + '/' + staff_job['id']).status_code, 404)
        self.user = {'id': 'other', 'role': 'client', 'name': '다른고객', 'org_id': 'office-1'}
        self.assertEqual(self.client.get(self.base).status_code, 404)
        self.assertEqual(self.client.get(self.base + '/' + staff_job['id']).status_code, 404)

    def test_deleted_case_stops_queued_work_without_recreating_case(self):
        with patch.object(archive_import, 'POOL', HeldPool()):
            response = self.start()
        job = archive_import._read(self.case['id'], response.json()['id'])
        with store.db() as con:
            con.execute('DELETE FROM cases WHERE id=?', (self.case['id'],))
        for path in archive_import._folder(self.case['id']).iterdir():
            path.unlink()
        archive_import._folder(self.case['id']).rmdir()
        archive_import._worker(job, make_zip([('a.txt', b'Original')]), self.user,
            main.authorize, main.extract_file, main._commit_prepared_documents)
        self.assertEqual(job['error']['code'], 'IMPORT_CASE_UNAVAILABLE')
        self.assertFalse(archive_import._folder(self.case['id']).exists())
        self.assertIsNone(store.get_case(self.case['id']))
        self.schedule.assert_not_called()

    def test_real_29_random_named_pdfs_async_job_preserves_every_byte_and_receipt(self):
        folder = store.ROOT / 'examples/court_ready_fixture'
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        case = domain.new_case(manifest['name'], 'CT01', '서울회생법원', '가상 ZIP 통합검사', True)
        case['intake'] = {'status': 'completed'}
        case['consultation'] = {'notes': (folder / 'staff_consultation.txt').read_text(encoding='utf-8')}
        court_rules.reconcile(case, as_of='2026-10-04')
        store.insert_case(case)
        self.case = case
        self.base = '/api/cases/' + case['id'] + '/document-imports'
        originals = [(f'{index * 91337:016x}.pdf', (folder / row['file']).read_bytes())
                     for index, row in enumerate(manifest['documents'], 1)]
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(archive_import, 'POOL', pool):
            response = self.start(originals, token='real-29-pdf-import')
            self.assertEqual(response.status_code, 202, response.text)
            job = response.json()
            self.assertEqual(job['status'], 'queued')
            deadline = time.monotonic() + 30
            while job['status'] in archive_import.ACTIVE and time.monotonic() < deadline:
                time.sleep(0.05)
                job = self.client.get(self.base + '/' + job['id']).json()
            self.assertEqual(job['status'], 'completed', job)
        self.assertEqual((job['imported_count'], job['matched_count'], job['review_count']), (29, 29, 0))
        saved = store.get_case(case['id'])
        self.assertEqual(len(saved['documents']), 29)
        self.assertEqual(saved['version'], case['version'] + 1)
        self.assertEqual(saved['input_revision'], case['input_revision'] + 1)
        self.schedule.assert_called_once()
        requested = {request['id']: request for request in saved['requests']}
        for original, document, answer in zip(originals, saved['documents'], manifest['documents']):
            with self.subTest(document=answer['id']):
                path = store.DATA_DIR / 'uploads' / case['id'] / document['storage_name']
                self.assertEqual(path.read_bytes(), original[1])
                self.assertEqual(document['sha256'], hashlib.sha256(original[1]).hexdigest())
                self.assertEqual(document['filename'], original[0])
                self.assertEqual(document['status'], 'received')
                self.assertFalse(document['content_confirmed'])
                self.assertEqual(document['extraction_manifest']['status'], 'completed')
                self.assertEqual(extraction_readiness.document_state(saved, document)['status'], 'completed')
                target = requested[document['request_id']]
                self.assertIn(answer['catalog_id'], {target['catalog_id'], *target.get('accepted_catalog_ids', [])})
                self.assertIn(document['id'], target['document_ids'])
                self.assertEqual(target['status'], 'received')
                if target.get('scope_unresolved'):
                    self.assertTrue(document['scope_review_required'])


if __name__ == '__main__':
    unittest.main()
