"""Authenticated internal filing hand-off and isolated receipt attachment routes."""
import asyncio
import hashlib
import io
from pathlib import Path
import zipfile

from fastapi import Depends, File, Form, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import Field, StrictBool

from . import domain, store, filing, download_names
from .extended_routes import Versioned, Review


class FilingPackageCreate(Versioned):
    document_ids: list[str] = Field(default_factory=list, max_length=7)


class FilingApproval(Review):
    final_checks_confirmed: StrictBool = False


def attach(app, staff, authorize, change, extract_file):
    @app.get('/api/cases/{case_id}/filing-readiness')
    def readiness(case_id: str, user=Depends(staff)):
        return filing.readiness(authorize(case_id, user))

    @app.post('/api/cases/{case_id}/filing-packages')
    def prepare(case_id: str, data: FilingPackageCreate, user=Depends(staff)):
        return change(case_id, user, data.expected_version, 'filing_package.prepared',
                      lambda case: filing.prepare(case, data.document_ids, user))

    @app.post('/api/cases/{case_id}/filing-packages/{package_id}/approve')
    def approve(case_id: str, package_id: str, data: FilingApproval, user=Depends(staff)):
        return change(case_id, user, data.expected_version, 'filing_package.approved',
                      lambda case: filing.approve(case, package_id, data.reason, data.final_checks_confirmed, user))

    @app.get('/api/cases/{case_id}/filing-packages/{package_id}/download')
    def download_package(case_id: str, package_id: str, user=Depends(staff)):
        case = authorize(case_id, user)
        package = filing.current_package(case, package_id)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            for document_id in package['document_ids']:
                doc = domain.item(case, 'court_documents', document_id)
                path = filing._hash_file(case, 'generated', document_id + '.pdf', doc['sha256'])
                title = filing.court_forms.TEMPLATES[doc['template_id']]['title']
                archive.writestr(download_names.filename(case, title, 'pdf'), path.read_bytes())
        store.access(user, case_id, 'filing_package.download:' + package_id)
        filename = download_names.filename(case, '제출검토패키지', 'zip')
        from urllib.parse import quote
        return Response(stream.getvalue(), media_type='application/zip',
                        headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(filename)})

    @app.post('/api/cases/{case_id}/filing-packages/{package_id}/receipts')
    async def upload_receipt(case_id: str, package_id: str, file: UploadFile = File(...),
                             expected_version: int = Form(...), court_case_number: str = Form(...),
                             reason: str = Form(...), person_confirmed: bool = Form(False),
                             case_number_confirmed: bool = Form(False), receipt_confirmed: bool = Form(False),
                             user=Depends(staff)):
        case = authorize(case_id, user)
        if case['version'] != expected_version:
            raise store.VersionConflict('사건이 변경되었습니다. 새로고침 후 접수증을 등록해 주세요.')
        filing.current_package(case, package_id, approved=True)
        domain.require(person_confirmed and case_number_confirmed and receipt_confirmed and 5 <= len(reason.strip()) <= 4000,
                       'RECEIPT_REVIEW_REQUIRED', '접수증의 명의·사건번호·실제 접수 여부를 확인하고 사유를 기록해 주세요.')
        raw = await file.read(10_000_001)
        domain.require(0 < len(raw) <= 10_000_000, 'FILE_SIZE', '접수증 파일은 10MB 이하여야 합니다.')
        filename = Path((file.filename or 'receipt').replace('\\', '/')).name[:160]
        extension = Path(filename).suffix.lower()
        domain.require(extension in {'.pdf', '.txt', '.png', '.jpg', '.jpeg'}, 'FILE_TYPE',
                       '접수증은 PDF, TXT 또는 이미지 파일로 올려 주세요.')
        pages = await asyncio.to_thread(extract_file, raw, extension)
        domain.require(bool(pages) and all(page.get('text', '').strip() for page in pages),
                       'RECEIPT_UNREADABLE', '접수증 전체 내용을 읽을 수 없습니다. 선명한 원본을 다시 올려 주세요.')
        text = '\n'.join(page['text'] for page in pages)[:60000]
        number = filing.validate_receipt_text(text, court_case_number)
        receipt_id = store.uid('receipt')
        path = filing._file(case, 'uploads', receipt_id + extension)
        record = {'id': receipt_id, 'filename': filename, 'storage_name': path.name,
                  'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw), 'text': text,
                  'court_case_number': number, 'created_at': store.now(), 'uploader': user['id'],
                  'review': {'person_confirmed': True, 'case_number_confirmed': True,
                             'receipt_confirmed': True, 'reason': reason.strip()},
                  'external_transmission': False}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        try:
            # Deliberately does not call domain.invalidate or enqueue OCR/AX.
            return change(case_id, user, expected_version, 'submission_receipt.verified',
                          lambda current: filing.add_receipt(current, package_id, record, user))
        except Exception:
            path.unlink(missing_ok=True)
            raise

    @app.get('/api/cases/{case_id}/submission-receipts/{receipt_id}/download')
    def download_receipt(case_id: str, receipt_id: str, user=Depends(staff)):
        case = authorize(case_id, user)
        receipt = domain.item(case, 'submission_receipts', receipt_id)
        path = filing._hash_file(case, 'uploads', receipt['storage_name'], receipt['sha256'])
        store.access(user, case_id, 'submission_receipt.download:' + receipt_id)
        return FileResponse(path, filename=receipt['filename'], media_type='application/octet-stream')
