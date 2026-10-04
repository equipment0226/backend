"""HTTP/SQLite integration of calculation approval and original court PDFs.

All cases and files are synthetic and isolated in a per-suite store. No model
or network calls are made. Official PDFs are the checked-in versioned originals.
"""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

os.environ.setdefault('DEBTOFF_DEMO_MODE', '1')
from fastapi.testclient import TestClient
import pymupdf
from apps.api.main import app
from apps.api import domain, legal_calculator, store


class DocumentWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1] / '.work/tests'
        root.mkdir(parents=True, exist_ok=True)
        cls.data_dir = Path(tempfile.mkdtemp(prefix='document-workflow-', dir=root))
        cls.store_patch = mock.patch.object(store, 'DATA_DIR', cls.data_dir)
        cls.schedule_patch = mock.patch('apps.api.ax_service.maybe_schedule', return_value=None)
        cls.store_patch.start()
        cls.schedule_patch.start()
        cls.context = TestClient(app, raise_server_exceptions=False)
        cls.client = cls.context.__enter__()
        cls.headers = {}
        for role in ('staff', 'lawyer', 'client'):
            response = cls.client.post('/api/auth/login', json={
                'username': 'demo-' + role, 'password': 'debtoff-demo'})
            if response.status_code != 200:
                raise AssertionError(response.text)
            cls.headers[role] = {'Authorization': 'Bearer ' + response.json()['token']}

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)
        cls.schedule_patch.stop()
        cls.store_patch.stop()

    def setUp(self):
        self.case = domain.new_case('김가온', 'CT01', '서울회생법원',
                                   '통합 검증 전용 합성 급여소득 사건', True)
        self.case['case_type'] = 'personal_rehabilitation'
        self.case['client_user_id'] = 'client'
        store.insert_case(self.case)
        self.base = '/api/cases/' + self.case['id']
        self.inputs = legal_calculator.example_payload()

    def read(self):
        response = self.client.get(self.base, headers=self.headers['staff'])
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def post(self, path, data=None, role='staff', version=None):
        return self.client.post(self.base + path, headers=self.headers[role], json={
            'expected_version': version if version is not None else self.read()['version'],
            **(data or {})})

    def evidence(self, verify=True):
        content = ('가상자료 / 김가온 / 2026년 7~9월 급여명세 및 잔액 확인\n'
                   '세전 3100000원 공제 300000원 세후 월 2800000원.\n'
                   '임차보증금 10000000원 예금 1200000원 보험해약환급금 800000원.\n'
                   '예시은행A 원금35000000원 이자1400000원. '
                   '예시카드B 원금25000000원 이자1000000원. '
                   '예시저축C 원금15000000원 이자600000원.').encode()
        response = self.client.post(self.base + '/documents', headers=self.headers['staff'],
            files={'file': ('synthetic-financial-statement.txt', content, 'text/plain')},
            data={'expected_version': self.read()['version'], 'request_id': ''})
        self.assertEqual(response.status_code, 200, response.text)
        docid = response.json()['documents'][-1]['id']
        if verify:
            response = self.post('/documents/' + docid + '/verify', {
                'scope_confirmed': True, 'content_confirmed': True,
                'person_confirmed': True, 'reason': '합성 원문의 명의·기간·각 금액을 전수 대조'})
            self.assertEqual(response.status_code, 200, response.text)
        for row in [self.inputs['income']] + self.inputs['assets'] + self.inputs['creditors']:
            row['evidence_ids'] = [docid]
        return docid

    def calculation(self, approve=False):
        response = self.post('/legal-calculations', {'inputs': self.inputs})
        self.assertEqual(response.status_code, 200, response.text)
        calc = response.json()['legal_calculations'][-1]
        if approve:
            response = self.post('/legal-calculations/' + calc['id'] + '/approve',
                                 {'reason': '합성 증빙과 소득·생계비·원금·현가·배분 대조 완료'}, role='lawyer')
            self.assertEqual(response.status_code, 200, response.text)
            calc = response.json()['legal_calculations'][-1]
        return calc

    def tamper(self, fn):
        case = store.get_case(self.case['id'])
        fn(case)
        with store.db() as con:
            con.execute('UPDATE cases SET body=? WHERE id=?', (store.dumps(case), case['id']))

    def form(self, template='D5105', fields=None, calc=None):
        # These tests exercise explicit staff drafting/approval, not model
        # authorship. Consultation is no longer copied into a statement.
        fields=dict(fields or {})
        if template=='D5105':fields.setdefault('statement','급여소득으로 채무를 변제하고자 개인회생을 신청합니다.')
        response = self.post('/court-documents', {
            'template_id': template, 'fields': fields, 'calculation_id': calc})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['court_documents'][-1]

    def approve_form(self, form, role='lawyer'):
        return self.post('/court-documents/' + form['id'] + '/approve',
                         {'reason': '공식 원본 필드와 근거를 대조한 검토 승인'}, role=role)

    def test_read_endpoints_require_staff_authentication(self):
        for url in ('/api/legal-calculation/schema', '/api/court-forms',
                    '/api/court-forms/D5100/original', '/api/testing/sample-bundle'):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 401)
                self.assertEqual(self.client.get(url, headers=self.headers['client']).status_code, 403)
        response = self.client.get('/api/legal-calculation/schema', headers=self.headers['staff'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['policy']['id'], 'kr-rehab-2026-v1')

    def test_calculation_approval_is_lawyer_only_and_snapshot_bound(self):
        self.evidence()
        calc = self.calculation()
        self.assertEqual(calc['status'], 'ready_for_review')
        path = '/legal-calculations/' + calc['id'] + '/approve'
        payload = {'reason': '직원 또는 고객이 승인하면 안 됩니다.'}
        self.assertEqual(self.post(path, payload).status_code, 403)
        self.assertEqual(self.post(path, payload, role='client').status_code, 403)
        response = self.post(path, {'reason': '변호사 원문 대조 및 계산 판단 확인'}, role='lawyer')
        self.assertEqual(response.status_code, 200, response.text)
        calc = response.json()['legal_calculations'][-1]
        self.assertEqual(calc['status'], 'approved')
        self.assertEqual(calc['approval']['actor'], 'lawyer')
        for key in ('input_hash', 'policy_hash', 'result_hash'):
            self.assertEqual(calc['approval'][key], calc[key])
        self.assertEqual(self.post(path, payload, role='lawyer').status_code, 422)

    def test_unverified_evidence_previews_but_cannot_approve(self):
        self.evidence(verify=False)
        calc = self.calculation()
        self.assertEqual(calc['status'], 'blocked')
        self.assertEqual(calc['summary']['total_creditor_payment'], 45412452)
        response = self.post('/legal-calculations/' + calc['id'] + '/approve',
                             {'reason': '미검토 증빙은 승인되어서는 안 됨'}, role='lawyer')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'CALCULATION_BLOCKED')

    def test_cross_case_evidence_not_accepted(self):
        self.evidence()
        self.inputs['income']['evidence_ids'] = ['doc-of-another-case']
        calc = self.calculation()
        self.assertEqual(calc['status'], 'blocked')
        self.assertIn('EVIDENCE_NOT_IN_CASE', {b['code'] for b in calc['blockers']})

    def test_source_status_change_invalidates_saved_calculation_even_without_revision_change(self):
        self.evidence()
        calc = self.calculation()
        self.tamper(lambda case: case['documents'][0].update(status='rejected'))
        response = self.post('/legal-calculations/' + calc['id'] + '/approve',
                             {'reason': '근거 검증상태 변경 후 승인 시도'}, role='lawyer')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'CALCULATION_BLOCKED')

    def test_saved_result_tamper_rejected_even_if_hash_field_unchanged(self):
        self.evidence()
        calc = self.calculation()
        self.tamper(lambda case: case['legal_calculations'][-1]['summary'].update(monthly_deposit=1))
        response = self.post('/legal-calculations/' + calc['id'] + '/approve',
                             {'reason': '저장 숫자 변조를 탐지해야 함'}, role='lawyer')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'CALCULATION_CHANGED')

    def test_in_place_source_text_tamper_changes_calculation_snapshot(self):
        self.evidence()
        calc = self.calculation()
        self.tamper(lambda case: case['documents'][0].update(text='원문이 외부에서 변조되었습니다.'))
        response = self.post('/legal-calculations/' + calc['id'] + '/approve',
                             {'reason': '리비전 변경 없는 원문 변조 검출'}, role='lawyer')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'CALCULATION_CHANGED')

    def test_policy_snapshot_change_requires_new_calculation(self):
        self.evidence()
        calc = self.calculation()
        changed = legal_calculator.policy()
        changed['policy_hash'] = 'new-policy-hash'
        with mock.patch.object(legal_calculator, 'policy', return_value=changed):
            response = self.post('/legal-calculations/' + calc['id'] + '/approve',
                                 {'reason': '기준 버전 변경 이후 승인 시도'}, role='lawyer')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'CALCULATION_CHANGED')

    def test_input_mutation_stales_calculation_and_form(self):
        self.evidence()
        calc = self.calculation(approve=True)
        form = self.form(fields={'prior_proceedings': '합성 사례: 이전 개인회생·파산 없음'})
        response = self.client.post(self.base + '/documents', headers=self.headers['staff'],
            files={'file': ('changed.txt', '새 급여 2900000원'.encode(), 'text/plain')},
            data={'expected_version': self.read()['version'], 'request_id': ''})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['legal_calculations'][-1]['stale'])
        self.assertTrue(response.json()['court_documents'][-1]['stale'])
        self.assertEqual(self.approve_form(form).json()['code'], 'FORM_STALE')
        response = self.post('/legal-calculations/' + calc['id'] + '/approve',
                             {'reason': '구 계산 승인 재시도는 실패해야 함'}, role='lawyer')
        self.assertEqual(response.json()['code'], 'CALCULATION_STALE')

    def test_original_pdf_is_versioned_and_actual_render_preserves_original(self):
        from apps.api.court_forms import TEMPLATES
        original = self.client.get('/api/court-forms/D5105/original', headers=self.headers['staff'])
        self.assertEqual(original.status_code, 200, original.text[:100] if not original.content.startswith(b'%PDF') else '')
        self.assertEqual(hashlib.sha256(original.content).hexdigest(), TEMPLATES['D5105']['source']['sha256'])
        form = self.form(fields={'prior_proceedings': '가상 사례: 이전 절차 없음'})
        response = self.client.get(self.base + '/court-documents/' + form['id'] + '/download', headers=self.headers['staff'])
        self.assertEqual(response.status_code, 200, response.text[:100] if not response.content.startswith(b'%PDF') else '')
        with pymupdf.open(stream=response.content, filetype='pdf') as document:
            text = ''.join(p.get_text() for p in document)
            self.assertGreaterEqual(document.page_count, 4)
            self.assertIn('진술서', text.replace(' ', ''))
            self.assertIn('급여소득으로 채무를 변제하고자 개인회생을 신청합니다.', text)
            self.assertNotIn('통합 검증 전용 합성 급여소득 사건', text)
            self.assertIn('DRAFT', text)
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), form['sha256'])

    def test_required_fields_missing_cannot_approve(self):
        form = self.form(template='D5100')
        self.assertFalse(form['preview']['ready_for_review'])
        response = self.approve_form(form)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'FORM_INCOMPLETE')

    def test_form_approval_role_and_pdf_tamper(self):
        form = self.form(fields={'prior_proceedings': '가상 사례: 이전 절차 없음'})
        self.assertEqual(self.approve_form(form, role='staff').status_code, 403)
        self.assertEqual(self.approve_form(form, role='client').status_code, 403)
        path = self.data_dir / 'generated' / self.case['id'] / (form['id'] + '.pdf')
        path.write_bytes(path.read_bytes() + b'\nTAMPERED')
        response = self.approve_form(form)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'ARTIFACT_CHANGED')
        response = self.client.get(self.base + '/court-documents/' + form['id'] + '/download', headers=self.headers['staff'])
        self.assertEqual(response.status_code, 422)

    def test_complete_form_can_be_review_approved_but_not_submission_ready(self):
        form = self.form(fields={'prior_proceedings': '가상 사례: 이전 절차 없음'})
        response = self.approve_form(form)
        self.assertEqual(response.status_code, 200, response.text)
        approved = response.json()['court_documents'][-1]
        self.assertEqual(approved['status'], 'review_approved')
        self.assertFalse(approved['preview']['submission_ready'])
        self.assertEqual(approved['approval']['sha256'], approved['sha256'])

    def test_approved_calculation_cannot_be_overridden_by_form_fields(self):
        self.evidence()
        calc = self.calculation(approve=True)
        response = self.post('/court-documents', {'template_id': 'D5110', 'calculation_id': calc['id'],
                            'fields': {'monthly_deposit': 1, 'total_creditor_payment': 1}})
        self.assertEqual(response.status_code, 200, response.text[:300])
        fields = response.json()['court_documents'][-1]['preview']['fields']
        for field in fields:
            if field['key'] == 'total_creditor_payment':
                self.assertEqual(field['value'], 45412452)
                self.assertEqual(field['source']['type'], 'legal_calculation')

    def test_confirmed_fact_cannot_be_overridden_by_form_fields(self):
        doc = self.evidence()
        response = self.post('/facts/income/confirm', {'value': 2800000, 'evidence_ids': [doc],
                             'reason': '합성 급여 원문 수령금액 확인'})
        self.assertEqual(response.status_code, 200, response.text)
        response = self.post('/court-documents', {'template_id': 'D5103', 'fields': {'monthly_income': 1}})
        self.assertEqual(response.status_code, 200, response.text[:300])
        for field in response.json()['court_documents'][-1]['preview']['fields']:
            if field['key'] == 'monthly_income':
                self.assertEqual(field['value'], 2800000)
                self.assertEqual(field['source']['type'], 'confirmed_fact')

    def test_form_requires_approved_calculation_if_linked(self):
        self.evidence()
        calc = self.calculation()
        response = self.post('/court-documents', {'template_id': 'D5110', 'calculation_id': calc['id'], 'fields': {}})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'CALCULATION_NOT_APPROVED')

    def test_other_court_private_and_unknown_templates_are_rejected(self):
        for template in ('BUSAN-ATTACHMENTS', '../../secret', 'not-a-template'):
            response = self.post('/court-documents', {'template_id': template, 'fields': {}})
            self.assertEqual(response.status_code, 422, response.text)

    def test_unknown_collection_items_are_structured_errors_not_500(self):
        for path in ('/legal-calculations/missing/approve', '/court-documents/missing/approve'):
            response = self.post(path, {'reason': '존재하지 않는 항목 승인 시도'}, role='lawyer')
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()['code'], 'ITEM_NOT_FOUND')

    def test_malformed_types_never_crash_calculator_or_form_routes(self):
        for value in ([], None, 'not-an-object'):
            response = self.post('/legal-calculations', {'inputs': value})
            self.assertEqual(response.status_code, 422)
        for key, value in (('income', []), ('creditors', [None]), ('assets', [{}]),
                           ('recognized_household_size', True), ('as_of', {}),
                           ('months', 100000000), ('monthly_trustee_fee', -1)):
            invalid = copy.deepcopy(self.inputs)
            invalid[key] = value
            response = self.post('/legal-calculations', {'inputs': invalid})
            self.assertEqual(response.status_code, 200, (key, response.text))
            self.assertEqual(response.json()['legal_calculations'][-1]['status'], 'blocked')
        for value in ({'monthly_income': {'nested': 'bad'}}, {'monthly_income': True}, []):
            response = self.post('/court-documents', {'template_id': 'D5103', 'fields': value})
            self.assertEqual(response.status_code, 422, response.text)

    def test_cross_case_generated_records_are_hidden(self):
        other = domain.new_case('별도 가상인', 'CT01', '서울회생법원', '다른 사건', True)
        store.insert_case(other)
        self.evidence()
        calc = self.calculation()
        url = '/api/cases/' + other['id'] + '/legal-calculations/' + calc['id'] + '/download'
        self.assertEqual(self.client.get(url, headers=self.headers['staff']).status_code, 422)
        self.assertEqual(self.client.get(url, headers=self.headers['client']).status_code, 403)
        projection = self.client.get(self.base, headers=self.headers['client']).json()
        self.assertNotIn('legal_calculations', projection)
        self.assertNotIn('court_documents', projection)

    def test_non_finite_numbers_return_validation_error(self):
        for endpoint, body in (
            ('/legal-calculations', {'inputs': {'monthly_amount': float('nan')}}),
            ('/court-documents', {'template_id': 'D5103', 'fields': {'monthly_income': float('inf')}})):
            body['expected_version'] = store.get_case(self.case['id'])['version']
            response = self.client.post(self.base + endpoint, headers={**self.headers['staff'], 'Content-Type': 'application/json'},
                                        content=json.dumps(body))
            self.assertEqual(response.status_code, 422, response.text)

    def test_official_hwp_original_is_downloaded_with_pinned_hash(self):
        from apps.api.court_forms import SOURCES
        source = next(s for s in SOURCES if s['id'] == 'D5100-hwp')
        response = self.client.get('/api/court-forms/D5100/original?format=hwp', headers=self.headers['staff'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), source['sha256'])
        self.assertTrue(response.content.startswith(bytes.fromhex('d0cf11e0a1b11ae1')))
        self.assertIn('.hwp', response.headers['content-disposition'])
        response = self.client.get('/api/court-forms/D5100/original?format=exe', headers=self.headers['staff'])
        self.assertEqual(response.status_code, 422)

    def test_sample_bundle_is_real_zip_and_csv_is_formula_safe(self):
        response = self.client.get('/api/testing/sample-bundle', headers=self.headers['staff'])
        self.assertEqual(response.status_code, 200, response.text[:100] if response.status_code != 200 else '')
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            self.assertTrue(any(name.endswith('.pdf') for name in z.namelist()))
            self.assertTrue(any(name.endswith('.png') for name in z.namelist()))
        self.evidence()
        self.inputs['creditors'][0]['name'] = '=HYPERLINK("https://example.invalid")'
        calc = self.calculation()
        response = self.client.get(self.base + '/legal-calculations/' + calc['id'] + '/download', headers=self.headers['staff'])
        self.assertEqual(response.status_code, 200)
        self.assertIn("'=HYPERLINK", response.content.decode('utf-8-sig'))


if __name__ == '__main__':
    unittest.main()
