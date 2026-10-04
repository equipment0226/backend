"""Independent local AI checks of corrected, versioned court PDFs."""
import copy
import hashlib

from fastapi import Depends

from . import auto_documents, ax_service, domain, store
from .extended_routes import Review, _approved_calculation, _artifact_path


def _reviewable(case, document_id):
    record = domain.item(case, 'court_documents', document_id)
    domain.require(not record.get('stale') and record['input_revision'] == case.get('input_revision'),
                   'FORM_STALE', '사건 자료가 바뀌었습니다. 새 버전을 작성한 뒤 재검토하세요.')
    approved_package = any(package.get('status') in {'approved', 'submitted'} and
                           document_id in package.get('document_ids', [])
                           for package in case.get('filing_packages', []))
    domain.require(not record.get('approval') and not approved_package,
                   'FORM_ALREADY_APPROVED', '승인된 문서는 보존됩니다. 수정할 새 버전을 작성하세요.')
    path = _artifact_path(case['id'], record['id'])
    domain.require(path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == record.get('sha256'),
                   'ARTIFACT_CHANGED', '저장된 PDF가 작성 당시와 다릅니다. 새 버전을 작성하세요.')
    return record


def attach(app, staff, authorize, change):
    @app.post('/api/cases/{case_id}/court-documents/{document_id}/verify')
    async def verify_document(case_id: str, document_id: str, data: Review, user=Depends(staff)):
        snapshot = authorize(case_id, user)
        if snapshot['version'] != data.expected_version:
            raise store.VersionConflict('사건이 변경되었습니다. 새로고침 후 재검토하세요.')
        record = copy.deepcopy(_reviewable(snapshot, document_id))
        calculation = _approved_calculation(snapshot, record.get('calculation_id'))
        signature, knowledge = ax_service.fingerprint(snapshot), ax_service.knowledge_signature(snapshot)
        original_review = copy.deepcopy(record.get('ai_review'))
        original_digest = store.digest(record)
        await ax_service._while_current(
            auto_documents.verify_artifacts(snapshot, [record], calculation), case_id, signature, knowledge)

        def apply(case):
            current = _reviewable(case, document_id)
            domain.require(store.digest(current) == original_digest and ax_service.fingerprint(case) == signature
                           and ax_service.knowledge_signature(case) == knowledge,
                           'FORM_REVIEW_CHANGED', '검토 중 문서나 근거가 바뀌었습니다. 현재 버전으로 다시 검토하세요.')
            current.setdefault('review_history', []).append({
                'id': store.uid('doc-review'), 'at': store.now(), 'actor': user['id'],
                'actor_name': user['name'], 'reason': data.reason,
                'previous_review': original_review, 'result': copy.deepcopy(record['ai_review']),
                'artifact_sha256': current['sha256'], 'input_revision': case['input_revision']})
            current['ai_review'] = record['ai_review']
            current['status'] = record['status']

        return change(case_id, user, data.expected_version, 'court_document.ai_reviewed', apply)
