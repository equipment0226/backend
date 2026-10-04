"""Browser-visible PDF download names without changing stored artifact bytes."""
import hashlib
import os
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote
from unittest.mock import patch

os.environ.setdefault('DEBTOFF_DEMO_MODE', '1')

import pymupdf
from fastapi.testclient import TestClient

from apps.api import corpus, court_forms, domain, download_names, store
from apps.api.extended_routes import _artifact_path
from apps.api.main import app


def response_filename(response):
    disposition = response.headers['content-disposition']
    encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
    if encoded:
        return unquote(encoded[1])
    fallback = re.search(r'filename="([^"]+)"', disposition)
    return fallback[1] if fallback else None


class DownloadNamesApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1] / '.work/tests'
        root.mkdir(parents=True, exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix='download-names-', dir=root)
        cls.root = Path(cls.temporary.name)
        cls.patches = [
            patch.object(store, 'DATA_DIR', cls.root / 'data'),
            patch.object(corpus, 'CORPUS_DIR', cls.root / 'corpus'),
            patch.dict(os.environ, {'DEBTOFF_DEMO_MODE': '1', 'DEBTOFF_AUTO_AX': '0',
                                    'DEBTOFF_LEGAL_WATCH': '0'}),
            patch('apps.api.ax_service.maybe_schedule', return_value=None),
        ]
        for item in cls.patches:
            item.start()
        cls.context = TestClient(app, raise_server_exceptions=False)
        cls.client = cls.context.__enter__()
        cls.headers = {}
        for role in ('staff', 'lawyer', 'client'):
            result = cls.client.post('/api/auth/login', json={
                'username': 'demo-' + role, 'password': 'debtoff-demo'})
            if result.status_code != 200:
                raise AssertionError(result.text)
            cls.headers[role] = {'Authorization': 'Bearer ' + result.json()['token']}
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), 'Synthetic PDF: immutable artifact bytes.')
            cls.pdf_bytes = document.tobytes()
        cls.pdf_hash = hashlib.sha256(cls.pdf_bytes).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)
        for item in reversed(cls.patches):
            item.stop()
        cls.temporary.cleanup()

    def make_case(self, **values):
        case = domain.new_case('가상다운로드고객', 'CT01', '서울회생법원', '합성 PDF 다운로드 검증', True)
        case.update(values)
        document_id = store.uid('court-doc')
        case['court_documents'] = [{'id': document_id, 'template_id': 'D5101',
                                   'sha256': self.pdf_hash, 'status': 'draft', 'stale': False}]
        store.insert_case(case)
        path = _artifact_path(case['id'], document_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.pdf_bytes)
        return case, document_id, path

    def download(self, case, document_id, role='staff', **headers):
        return self.client.get(f'/api/cases/{case["id"]}/court-documents/{document_id}/download',
                               headers={**self.headers[role], **headers})

    def test_generated_pdf_prefers_matter_number_and_preserves_pdf_bytes_and_hash(self):
        case, doc_id, path = self.make_case(matter_number='2026-수임-0042', engagement_number='후순위번호')
        result = self.download(case, doc_id)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(response_filename(result), '2026-수임-0042_재산목록.pdf')
        self.assertEqual(result.headers['content-type'], 'application/pdf')
        self.assertEqual(result.content, self.pdf_bytes)
        self.assertEqual(hashlib.sha256(result.content).hexdigest(), self.pdf_hash)
        self.assertEqual(path.read_bytes(), self.pdf_bytes)
        self.assertEqual(store.get_case(case['id'])['court_documents'][0]['sha256'], self.pdf_hash)

    def test_generated_pdf_falls_back_to_case_id(self):
        case, doc_id, _ = self.make_case()
        result = self.download(case, doc_id)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(response_filename(result), case['id'] + '_재산목록.pdf')

    def test_engagement_number_is_supported_when_matter_number_is_absent(self):
        case, doc_id, _ = self.make_case(engagement_number='수임별칭-0043')
        result = self.download(case, doc_id)
        self.assertEqual(response_filename(result), '수임별칭-0043_재산목록.pdf')

    def test_cross_origin_browser_can_read_korean_filename_header(self):
        case, doc_id, _ = self.make_case(matter_number='수임번호-한글')
        result = self.download(case, doc_id, Origin='http://localhost:5173')
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.headers['access-control-allow-origin'], 'http://localhost:5173')
        exposed = {name.strip().lower() for name in result.headers['access-control-expose-headers'].split(',')}
        self.assertIn('content-disposition', exposed)
        self.assertIn("filename*=utf-8''", result.headers['content-disposition'].lower())
        self.assertEqual(response_filename(result), '수임번호-한글_재산목록.pdf')

    def test_original_form_uses_case_reference_and_retains_official_source_hash(self):
        case, _, _ = self.make_case(matter_number='2026-원본-001')
        source_path = court_forms.original_path('D5101')
        original_bytes = source_path.read_bytes()
        original_hash = hashlib.sha256(original_bytes).hexdigest()
        result = self.client.get('/api/court-forms/D5101/original', params={'case_id': case['id']},
                                 headers={**self.headers['staff'], 'Origin': 'http://localhost:5173'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(response_filename(result), '2026-원본-001_재산목록_원본.pdf')
        self.assertEqual(hashlib.sha256(result.content).hexdigest(), original_hash)
        self.assertEqual(source_path.read_bytes(), original_bytes)
        self.assertIn('content-disposition', result.headers['access-control-expose-headers'].lower())

    def test_original_without_case_reference_has_explicit_general_form_filename(self):
        result = self.client.get('/api/court-forms/D5101/original', headers=self.headers['staff'])
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(response_filename(result), '법원서식_재산목록_원본.pdf')

    def test_original_case_reference_cannot_access_other_members_or_organization(self):
        for case_values in ({'members': ['lawyer']}, {'org_id': 'other-office'}):
            case, doc_id, _ = self.make_case(**case_values)
            with self.subTest(case_values=case_values), patch.object(court_forms, 'original_path') as original:
                response = self.client.get('/api/court-forms/D5101/original', params={'case_id': case['id']},
                                           headers=self.headers['staff'])
                self.assertEqual(response.status_code, 404, response.text)
                original.assert_not_called()
                self.assertEqual(self.download(case, doc_id).status_code, 404)

    def test_client_and_unauthenticated_callers_cannot_download_internal_forms(self):
        case, doc_id, _ = self.make_case(client_user_id='client')
        urls = [f'/api/court-forms/D5101/original?case_id={case["id"]}',
                f'/api/cases/{case["id"]}/court-documents/{doc_id}/download']
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 401)
                self.assertEqual(self.client.get(url, headers=self.headers['client']).status_code, 403)

    def test_unsafe_reference_characters_cannot_inject_headers_or_paths(self):
        case, doc_id, _ = self.make_case(matter_number='../수임/번호\\문서\r\nX-Injected: yes\x00')
        result = self.download(case, doc_id)
        self.assertEqual(result.status_code, 200, result.text)
        name = response_filename(result)
        self.assertIsNotNone(name)
        self.assertNotRegex(name, r'[\x00-\x1f\x7f<>:"/\\|?*]')
        self.assertFalse(name.startswith('.'))
        self.assertTrue(name.endswith('_재산목록.pdf'))
        self.assertNotIn('x-injected', result.headers)
        self.assertNotIn('%0d', result.headers['content-disposition'].lower())
        self.assertNotIn('%0a', result.headers['content-disposition'].lower())

    def test_unicode_title_and_reference_are_normalized_and_safe_for_header_encoding(self):
        name = download_names.filename({'matter_number': '\u1112\u1161\u11ab글수임'}, '재산/목록\r\n검증')
        self.assertEqual(name, '한글수임_재산_목록__검증.pdf')
        header = download_names.disposition(name)
        self.assertEqual(unquote(header.split("''", 1)[1]), name)
        self.assertNotIn('\r', header)
        self.assertNotIn('\n', header)

    def test_existing_artifact_hash_guard_is_not_bypassed_by_download_name_change(self):
        case, doc_id, path = self.make_case(matter_number='수임-무결성')
        path.write_bytes(self.pdf_bytes + b'corrupted')
        result = self.download(case, doc_id)
        self.assertEqual(result.status_code, 422, result.text)
        self.assertEqual(result.json()['code'], 'ARTIFACT_CHANGED')


if __name__ == '__main__':
    unittest.main()
