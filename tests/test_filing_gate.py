"""Final filing gate: pure local fixtures, no model calls or court transport."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from apps.api import auto_documents, court_forms, domain, filing, legal_calculator, store


STAFF = {'id': 'staff', 'name': '직원 테스트', 'role': 'staff'}
LAWYER = {'id': 'lawyer', 'name': '변호사 테스트', 'role': 'lawyer'}


class FilingGateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        location = patch.object(store, 'DATA_DIR', Path(folder.name)); location.start(); self.addCleanup(location.stop)
        law = patch.object(filing.legal_watch, 'case_policy_status', return_value={'signature': 'law-reviewed', 'pending_changes': []})
        law.start(); self.addCleanup(law.stop)
        self.case = domain.new_case('가상 시험인', 'CT01', '서울회생법원', '격리된 제출 검사', True)
        source = '격리된 테스트 원문'.encode('utf-8')
        evidence = {'id': 'evidence', 'status': 'verified', 'sha256': hashlib.sha256(source).hexdigest(),
                    'text': source.decode(), 'storage_name': 'evidence.txt', 'uploader': 'staff'}
        source_path = filing._file(self.case, 'uploads', 'evidence.txt')
        source_path.parent.mkdir(parents=True, exist_ok=True); source_path.write_bytes(source)
        self.case['documents'] = [evidence]
        for fact in self.case['facts']:
            fact.update(status='confirmed', value=2800000, evidence_ids=['evidence'])
        inputs = legal_calculator.example_payload()
        self.case['facts'][1]['value'] = sum(row['principal'] + row['interest'] for row in inputs['creditors'])
        for row in [inputs['income']] + inputs['assets'] + inputs['creditors']:
            row['evidence_ids'] = ['evidence']
        calc = legal_calculator.calculate_legal(self.case, inputs)
        self.assertEqual(calc['status'], 'ready_for_review', calc['blockers'])
        calc.update(status='approved', approval={key: calc[key] for key in ('input_hash', 'policy_hash', 'result_hash')})
        self.case['legal_calculations'] = [calc]
        self.case['court_documents'] = []
        for template_id in filing.REQUIRED_TEMPLATES:
            fields = {f['key']: '확인' for f in court_forms.TEMPLATES[template_id]['fields'] if f.get('required') and not f.get('read_only')}
            fields.update({key: '1' for key in fields if key.endswith(('_month', '_day'))})
            preview = court_forms.preview(self.case, template_id, fields, calc)
            self.assertTrue(preview['ready_for_review'], (template_id, preview['missing_fields'], preview['overflow_fields']))
            raw = ('%PDF-1.4\nfixture ' + template_id).encode()
            doc = {'id': 'document-' + template_id, 'template_id': template_id, 'input_revision': 1,
                   'stale': False, 'status': 'automatically_verified', 'fields': fields,
                   'preview': preview, 'calculation_id': calc['id'], 'original_sha256': preview['original_sha256'],
                   'sha256': hashlib.sha256(raw).hexdigest()}
            doc['content_hash'] = store.digest({'fields': fields, 'preview': preview, 'input_revision': 1, 'calculation_id': calc['id']})
            doc['ai_review'] = {'status': 'passed', 'passed': True, 'artifact_sha256': doc['sha256'],
                                'input_revision': 1, 'verification_version': auto_documents.VERIFICATION_VERSION,
                                'self_source_used': False, 'deterministic_issues': [], 'whole_document_verified': False,
                                'legal_dependency_signature': 'law-reviewed'}
            path = filing._file(self.case, 'generated', doc['id'] + '.pdf')
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
            self.case['court_documents'].append(doc)

    def package(self, approve=False):
        result = filing.prepare(self.case, [], STAFF)
        if approve:
            filing.approve(self.case, result['id'], '전체 서식·서명·별지 및 최종 제출요건 확인', True, LAWYER)
        return result

    def receipt(self, package):
        raw = '전자소송 접수증\n가상 시험인\n2026개회12345 접수완료'.encode('utf-8')
        record = {'id': 'receipt-test', 'filename': 'receipt.txt', 'storage_name': 'receipt-test.txt',
                  'sha256': hashlib.sha256(raw).hexdigest(), 'text': raw.decode(), 'court_case_number': '2026개회12345',
                  'review': {'person_confirmed': True, 'case_number_confirmed': True, 'receipt_confirmed': True, 'reason': '원문 접수 내용 확인'}}
        path = filing._file(self.case, 'uploads', record['storage_name'])
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        return filing.add_receipt(self.case, package['id'], record, STAFF)

    def assertBlocked(self, code, function):
        with self.assertRaises(domain.DomainError) as raised:
            function()
        self.assertEqual(raised.exception.code, code)

    def test_complete_verified_set_can_prepare_but_requires_explicit_lawyer_final_review(self):
        self.case['ax_pipeline'] = {'human_review_required': True, 'review_required_stages': ['review'],
                                    'submission_ready': False}
        self.assertTrue(filing.readiness(self.case)['ready'])
        package = self.package()
        self.assertEqual(package['status'], 'prepared')
        self.assertBlocked('LAWYER_ONLY', lambda: filing.approve(self.case, package['id'], '전체 서류 확인함', True, STAFF))
        self.assertBlocked('FILING_FINAL_CHECK_REQUIRED', lambda: filing.approve(self.case, package['id'], '전체 서류 확인함', False, LAWYER))
        filing.approve(self.case, package['id'], '전체 서류와 서명 별지 확인함', True, LAWYER)
        self.assertEqual(self.case['ax_pipeline']['stage'], 'submission_ready')
        self.assertFalse(self.case['ax_pipeline']['human_review_required'])
        self.assertEqual(self.case['ax_pipeline']['review_required_stages'], [])
        self.assertTrue(self.case['ax_pipeline']['submission_ready'])
        self.assertFalse(package['external_transmission'])

    def test_receipt_does_not_invalidate_evidence_or_approval_and_records_only_external_filing(self):
        package = self.package(approve=True)
        before = copy.deepcopy(self.case['documents'])
        signature = filing.source_signature(self.case)
        receipt = self.receipt(package)
        self.assertEqual(self.case['input_revision'], 1)
        self.assertEqual(self.case['documents'], before)
        self.assertEqual(filing.source_signature(self.case), signature)
        filing.record_submission(self.case, {'bundle_id': package['id'], 'receipt_document_id': receipt['id'],
                                             'court_case_number': '2026개회12345'}, STAFF)
        saved = self.case['submissions'][-1]
        self.assertEqual(saved['status'], 'recorded')
        self.assertFalse(saved['external_transmission'])
        self.assertFalse(saved['court_approval_confirmed'])
        self.assertEqual(self.case['ax_pipeline']['stage'], 'submitted')
        self.assertFalse(self.case['ax_pipeline']['submission_ready'])
        self.assertEqual(self.case['stage'], '법원 제출')

    def test_missing_form_and_unresolved_fact_never_prepare(self):
        self.case['court_documents'].pop()
        self.assertFalse(filing.readiness(self.case)['ready'])
        self.assertBlocked('FILING_DOCUMENT_SET', self.package)
        self.case['facts'][0]['status'] = 'unknown'
        self.assertBlocked('FILING_UNRESOLVED_INPUTS', self.package)

    def test_current_pdf_needs_its_own_ai_review(self):
        doc = self.case['court_documents'][0]
        doc['ai_review']['artifact_sha256'] = 'other-pdf'
        self.assertBlocked('FILING_AI_REVIEW_REQUIRED', self.package)

    def test_tampered_pdf_and_ai_review_cannot_inherit_final_approval(self):
        package = self.package(approve=True)
        self.case['court_documents'][0]['ai_review']['extra'] = 'changed after approval'
        self.assertBlocked('FILING_PACKAGE_CHANGED', lambda: filing.current_package(self.case, package['id'], True))
        self.case['court_documents'][0]['ai_review'].pop('extra')
        doc = self.case['court_documents'][0]
        filing._file(self.case, 'generated', doc['id'] + '.pdf').write_bytes(b'tampered')
        self.assertBlocked('ARTIFACT_CHANGED', lambda: filing.current_package(self.case, package['id'], True))

    def test_changed_evidence_invalidates_filing_package(self):
        package = self.package(approve=True)
        domain.invalidate(self.case, '보완 증빙 도착')
        self.assertTrue(package['stale'])
        self.assertBlocked('FILING_PACKAGE_STALE', lambda: filing.current_package(self.case, package['id'], True))

    def test_same_revision_replacement_form_requires_new_package(self):
        package = self.package(approve=True)
        replacement = copy.deepcopy(self.case['court_documents'][0]); replacement['id'] = 'newer-document'
        self.case['court_documents'].append(replacement)
        self.assertBlocked('FILING_DOCUMENT_REPLACED', lambda: filing.current_package(self.case, package['id'], True))

    def test_receipt_cannot_record_unapproved_package_wrong_number_or_modified_file(self):
        package = self.package()
        self.assertBlocked('FILING_APPROVAL_REQUIRED', lambda: self.receipt(package))
        filing.approve(self.case, package['id'], '전체 서명 별지 제출항목 확인', True, LAWYER)
        receipt = self.receipt(package)
        payload = {'bundle_id': package['id'], 'receipt_document_id': receipt['id'], 'court_case_number': '2026개회98765'}
        self.assertBlocked('RECEIPT_EVIDENCE_REQUIRED', lambda: filing.record_submission(self.case, payload, STAFF))
        payload['court_case_number'] = '2026개회12345'
        filing._file(self.case, 'uploads', receipt['storage_name']).write_bytes(b'changed receipt')
        self.assertBlocked('ARTIFACT_CHANGED', lambda: filing.record_submission(self.case, payload, STAFF))

    def test_old_review_bundle_is_not_a_filing_package(self):
        domain.make_bundle(self.case)
        self.assertBlocked('FILING_TEMPLATE_NOT_APPROVED', lambda: filing.current_package(self.case, self.case['bundles'][0]['id'], True))

    def test_receipt_number_must_match_exactly_and_failed_receipt_is_not_submission(self):
        for text in ('접수증 2026개회123456', '접수확인 2026개회12345 접수취소'):
            with self.subTest(text=text):
                self.assertBlocked('RECEIPT_EVIDENCE_REQUIRED', lambda: filing.validate_receipt_text(text, '2026개회12345'))

    def test_unregistered_court_is_explicitly_unsupported(self):
        self.case['court_id'] = 'unknown'
        self.assertBlocked('FILING_COURT_UNSUPPORTED', self.package)

    def test_changed_law_invalidates_final_approval(self):
        package = self.package(approve=True)
        with patch.object(filing.legal_watch, 'case_policy_status', return_value={'signature': 'changed-law', 'pending_changes': []}):
            self.assertBlocked('FILING_LEGAL_REVIEW_CHANGED', lambda: filing.current_package(self.case, package['id'], True))

    def test_human_review_waiting_is_allowed_but_unresolved_review_items_are_not(self):
        doc = self.case['court_documents'][0]
        doc.update(stage_status='preliminary', human_review_required=True, review_pending=[])
        self.assertTrue(filing.readiness(self.case)['ready'])
        doc['review_pending'] = [{'code': 'STRATEGY_VERIFICATION', 'reason': '쟁점 근거 대조 미완료'}]
        self.assertBlocked('FILING_REVIEW_PENDING', self.package)

    def test_receipt_and_package_survive_backup_and_missing_receipt_is_rejected(self):
        from scripts.backup import create_backup, restore_backup
        import json
        import zipfile
        package = self.package(approve=True)
        receipt = self.receipt(package)
        store.initialize(); store.insert_case(self.case)
        backup = store.DATA_DIR / 'filing-backup.zip'
        create_backup(backup)
        restored = store.DATA_DIR / 'restored'
        restore_backup(backup, restored)
        self.assertEqual((restored / 'uploads' / self.case['id'] / receipt['storage_name']).read_bytes(),
                         filing._file(self.case, 'uploads', receipt['storage_name']).read_bytes())
        broken = store.DATA_DIR / 'missing-receipt.zip'
        with zipfile.ZipFile(backup) as archive, zipfile.ZipFile(broken, 'w') as target:
            manifest = json.loads(archive.read('manifest.json'))
            manifest['files'] = [r for r in manifest['files'] if not r['name'].endswith(receipt['storage_name'])]
            for row in manifest['files']:
                target.writestr(row['name'], archive.read(row['name']))
            target.writestr('manifest.json', json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, '누락'):
            restore_backup(broken, store.DATA_DIR / 'broken-restore')


if __name__ == '__main__':
    unittest.main()
