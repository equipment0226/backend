"""Final human filing gate and external-filing receipt records.

There is deliberately no court transport here. A prepared package and a lawyer's
approval authorize internal hand-off only; an independently uploaded receipt is
required to record a filing performed outside this application.
"""
from __future__ import annotations

import copy
import hashlib
import re

from . import domain, store, court_forms, legal_watch, workflow_contract, auto_documents

VERSION = 'filing-handoff-v1'
# Product-supported complete set, not a claim that every court has no other
# attachments. Final signatures, appendices and additional court requirements
# are expressly included in the lawyer's final attestation.
REQUIRED_TEMPLATES = ('D5100', 'D5101', 'D5103', 'D5105', 'D5110', 'D5115', 'D5106')
FINANCIAL_TEMPLATES = {'D5101', 'D5103', 'D5110', 'D5106'}
INACTIVE = {'withdrawn', 'cancelled', 'superseded'}


def source_signature(case):
    # Receipt files, notifications and read-state do not alter debtor evidence.
    return store.digest({key: case.get(key) for key in (
        'input_revision', 'client_name', 'court_id', 'case_type', 'documents', 'facts',
        'issues', 'consultation', 'intake', 'requests', 'corrections', 'extraction_candidates')})


def _file(case, folder, filename):
    base = (store.DATA_DIR / folder / case['id']).resolve()
    path = (base / filename).resolve()
    domain.require(path.is_relative_to(base), 'ARTIFACT_PATH', '잘못된 문서 경로입니다.')
    return path


def _hash_file(case, folder, filename, expected):
    path = _file(case, folder, filename)
    domain.require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected,
                   'ARTIFACT_CHANGED', '보관된 파일이 작성·검증 당시와 다릅니다. 원본을 확인해 주세요.')
    return path


def _base_gate(case):
    from . import intake_workflow
    intake_workflow.require_completed(case)
    domain.require(not domain.blockers(case), 'FILING_UNRESOLVED_INPUTS',
                   '미확정 사실·미결 쟁점·미완료 서류를 보완한 뒤 최종 제출 검토를 진행해 주세요.')
    domain.require(not any(c.get('status') not in {'approved', 'closed', 'cancelled'} for c in case.get('corrections', [])),
                   'FILING_CORRECTIONS_OPEN', '남아 있는 보정 요구를 검토한 뒤 제출 패키지를 준비해 주세요.')
    law = legal_watch.case_policy_status(case)
    domain.require(not law.get('pending_changes'), 'FILING_LEGAL_CHANGE', '적용 법률 변경 검토를 먼저 완료해 주세요.')
    catalog = court_forms.catalog(case.get('court_id'))
    available = {t['id'] for t in catalog['templates'] if t.get('fields')}
    domain.require(case.get('court_id') in {court['id'] for court in catalog['courts']}
                   and set(REQUIRED_TEMPLATES) <= available, 'FILING_COURT_UNSUPPORTED',
                   '이 관할에는 제출 패키지에 필요한 공식 서식 전체가 연결되어 있지 않습니다.')
    return law


def latest_documents(case):
    result = {}
    for doc in case.get('court_documents', []):
        if doc.get('template_id') in REQUIRED_TEMPLATES:
            result[doc['template_id']] = doc
    return result


def _document_gate(case, doc, legal_signature=None):
    from .extended_routes import _approved_calculation, _form_preview
    domain.require(not doc.get('stale') and doc.get('input_revision') == case['input_revision'],
                   'FILING_DOCUMENT_STALE', '사건 자료가 변경된 작성본입니다. 최신 자료로 다시 작성해 주세요.')
    domain.require(latest_documents(case).get(doc.get('template_id'), {}).get('id') == doc['id'],
                   'FILING_DOCUMENT_REPLACED', '수정한 새 작성본이 있습니다. 최신 문서를 검토해 주세요.')
    domain.require(not doc.get('preliminary') and not doc.get('review_only'),
                   'FILING_PRELIMINARY_DOCUMENT', '확인할 항목이 남은 1차 초안은 최종 제출본으로 사용할 수 없습니다.')
    domain.require(not doc.get('review_pending'), 'FILING_REVIEW_PENDING',
                   '작성본의 미해결 보완 항목을 수정하고 새 버전으로 재검토해 주세요.')
    domain.require(doc.get('template_id') not in FINANCIAL_TEMPLATES or bool(doc.get('calculation_id')),
                   'FILING_CALCULATION_REQUIRED', '금액이 포함된 서식에 검증된 현재 법률계산을 연결해 주세요.')
    calc = _approved_calculation(case, doc.get('calculation_id'))
    fresh = _form_preview(case, doc['template_id'], doc.get('fields', {}), calc)
    domain.require(fresh.get('ready_for_review') and not fresh.get('missing_fields') and not fresh.get('overflow_fields'),
                   'FILING_FORM_INCOMPLETE', '서식의 필수 항목과 출력 배치를 보완해 주세요.')
    domain.require(fresh.get('original_sha256') == doc.get('original_sha256'),
                   'TEMPLATE_CHANGED', '공식 서식 원본이 변경되었습니다. 최신 원본으로 다시 작성해 주세요.')
    content_hash = store.digest({'fields': doc.get('fields', {}), 'preview': fresh,
                                'input_revision': doc['input_revision'], 'calculation_id': doc.get('calculation_id')})
    domain.require(content_hash == doc.get('content_hash'), 'FORM_CHANGED', '작성 내용이 저장된 검토본과 다릅니다.')
    _hash_file(case, 'generated', doc['id'] + '.pdf', doc.get('sha256'))
    review = doc.get('ai_review') or {}
    domain.require(review.get('passed') is True and review.get('status') == 'passed'
                   and review.get('artifact_sha256') == doc.get('sha256')
                   and review.get('input_revision') == case['input_revision']
                   and review.get('verification_version') == auto_documents.VERIFICATION_VERSION
                   and review.get('self_source_used') is False
                   and not review.get('deterministic_issues'),
                   'FILING_AI_REVIEW_REQUIRED', '현재 PDF의 AI 원문·수치 대조를 완료한 뒤 제출 검토를 진행해 주세요.')
    legal_signature = legal_signature or legal_watch.case_policy_status(case)['signature']
    domain.require(review.get('legal_dependency_signature') == legal_signature, 'FILING_LEGAL_REVIEW_CHANGED',
                   '문서 검토 이후 적용 법률 근거가 변경되었습니다. 현재 근거로 문서를 다시 대조해 주세요.')
    return {'document_id': doc['id'], 'template_id': doc['template_id'], 'sha256': doc['sha256'],
            'content_hash': content_hash, 'original_sha256': doc['original_sha256'],
            'ai_review_hash': store.digest(review), 'calculation_id': doc.get('calculation_id'),
            'calculation_hashes': {k: calc.get(k) for k in ('input_hash', 'policy_hash', 'result_hash')} if calc else None}


def _snapshot(case, document_ids):
    law = _base_gate(case)
    domain.require(isinstance(document_ids, list) and len(document_ids) == len(REQUIRED_TEMPLATES)
                   and len(set(document_ids)) == len(document_ids), 'FILING_DOCUMENT_SET',
                   '지원하는 공식 서식 7종의 최신 작성본을 각각 하나씩 선택해 주세요.')
    docs = [domain.item(case, 'court_documents', value) for value in document_ids]
    domain.require({d.get('template_id') for d in docs} == set(REQUIRED_TEMPLATES), 'FILING_DOCUMENT_SET',
                   '개시신청서·재산목록·수입지출목록·진술서·변제계획안·소득증명서·채권자목록이 모두 필요합니다.')
    records = [_document_gate(case, doc, law['signature']) for doc in docs]
    calc_ids = {r['calculation_id'] for r in records if r.get('calculation_id')}
    domain.require(len(calc_ids) == 1, 'FILING_CALCULATION_MISMATCH', '동일한 최신 계산을 반영한 작성본으로 묶어 주세요.')
    return {'input_revision': case['input_revision'], 'source_signature': source_signature(case),
            'legal_signature': law['signature'], 'documents': sorted(records, key=lambda row: row['template_id'])}


def readiness(case):
    latest = latest_documents(case)
    rows = [{'template_id': key, 'title': court_forms.TEMPLATES[key]['title'],
             'document_id': latest.get(key, {}).get('id'), 'status': latest.get(key, {}).get('status', 'missing'),
             'ai_review_passed': latest.get(key, {}).get('ai_review', {}).get('passed') is True}
            for key in REQUIRED_TEMPLATES]
    ids = [row['document_id'] for row in rows if row['document_id']]
    problems = []
    try:
        _base_gate(case)
    except domain.DomainError as exc:
        problems.append({'code': exc.code, 'message': exc.message})
    for row in rows:
        if not row['document_id']:
            problems.append({'code': 'FILING_DOCUMENT_MISSING', 'message': row['title'] + ' 작성본이 필요합니다.', 'template_id': row['template_id']})
            continue
        try:
            _document_gate(case, latest[row['template_id']])
        except domain.DomainError as exc:
            problems.append({'code': exc.code, 'message': row['title'] + ' · ' + exc.message, 'template_id': row['template_id']})
    if not problems:
        try:
            _snapshot(case, ids)
        except domain.DomainError as exc:
            problems.append({'code': exc.code, 'message': exc.message})
    return {'ready': not problems, 'required_documents': rows, 'document_ids': ids,
            'blockers': problems, 'external_transmission': False,
            'scope': '내부 제출 준비·최종 검토. 실제 법원 전송은 지원하지 않습니다.'}


def prepare(case, document_ids, user):
    if not document_ids:
        document_ids = [doc['id'] for doc in latest_documents(case).values()]
    snapshot = _snapshot(case, document_ids)
    signature = store.digest(snapshot)
    existing = next((p for p in case.get('filing_packages', []) if p.get('snapshot_hash') == signature and not p.get('stale')), None)
    if existing:
        return existing
    package = {'id': store.uid('filing'), 'title': '개인회생 제출 검토 패키지', 'kind': 'court_filing_handoff',
               'status': 'prepared', 'created_at': store.now(), 'created_by': user['id'],
               'input_revision': case['input_revision'], 'document_ids': list(document_ids),
               'snapshot': snapshot, 'snapshot_hash': signature, 'approval': None, 'stale': False,
               'external_transmission': False, 'version': VERSION}
    case.setdefault('filing_packages', []).append(package)
    return package


def current_package(case, package_id, approved=False):
    package = next((p for p in case.get('filing_packages', []) if p['id'] == package_id), None)
    domain.require(package is not None, 'FILING_TEMPLATE_NOT_APPROVED',
                   '검토 보고서 대신 공식 서식을 묶어 최종 승인한 제출 패키지를 연결해 주세요.')
    domain.require(not package.get('stale') and package.get('input_revision') == case['input_revision'],
                   'FILING_PACKAGE_STALE', '자료가 변경되어 제출 승인이 만료되었습니다. 최신 작성본을 다시 검토해 주세요.')
    snapshot = _snapshot(case, package['document_ids'])
    domain.require(store.digest(snapshot) == package.get('snapshot_hash') and package.get('snapshot') == snapshot,
                   'FILING_PACKAGE_CHANGED', '최종 검토할 작성본·계산·근거가 패키지 구성 당시와 다릅니다.')
    if approved:
        approval = package.get('approval') or {}
        domain.require(package.get('status') == 'approved' and approval.get('role') == 'lawyer'
                       and approval.get('snapshot_hash') == package['snapshot_hash']
                       and approval.get('input_revision') == case['input_revision']
                       and approval.get('final_checks_confirmed') is True,
                       'FILING_APPROVAL_REQUIRED', '담당 변호사의 최종 제출 검토 승인이 필요합니다.')
    return package


def _pipeline(case, submitted=False):
    stage = 'submitted' if submitted else 'submission_ready'
    case['stage'] = '법원 제출' if submitted else '제출 준비'
    pipeline = copy.deepcopy(case.get('ax_pipeline') or {})
    pipeline.update(stage=stage, status='completed' if submitted else 'waiting', input_revision=case['input_revision'],
                    updated_at=store.now(), stale=False, reasons=[], strategies=[],
                    human_review_required=False, review_required_stages=[], submission_ready=not submitted,
                    steps=workflow_contract.project_steps(len(workflow_contract.STAGES) if submitted else len(workflow_contract.STAGES)-1,
                                                        'completed' if submitted else 'waiting',
                                                        '' if submitted else '외부에서 법원에 제출한 뒤 접수증을 등록해 주세요.'))
    case['ax_pipeline'] = pipeline
    case.setdefault('workflow_checkpoints', []).append(workflow_contract.checkpoint(pipeline))
    case['automation'] = {'stage': stage, 'label': case['stage'], 'generated_at': store.now()}


def approve(case, package_id, reason, final_checks_confirmed, user):
    domain.require(user.get('role') == 'lawyer', 'LAWYER_ONLY', '담당 변호사만 최종 제출 검토를 승인할 수 있습니다.')
    domain.require(final_checks_confirmed is True and len(reason.strip()) >= 5,
                   'FILING_FINAL_CHECK_REQUIRED', '서명·날인, 미매핑 항목·별지 전체, 사건별 제출요건 확인과 검토 사유를 기록해 주세요.')
    package = current_package(case, package_id)
    domain.require(package['status'] == 'prepared', 'ALREADY_REVIEWED', '최종 검토 대기 패키지만 승인할 수 있습니다.')
    package.update(status='approved', approval={'actor': user['id'], 'actor_name': user['name'], 'role': user['role'],
        'reason': reason, 'at': store.now(), 'input_revision': case['input_revision'],
        'snapshot_hash': package['snapshot_hash'], 'final_checks_confirmed': True,
        'scope': 'AI 작성 항목 대조 및 서명·날인·미매핑 항목·별지·계산·최종 제출요건의 변호사 검토'})
    _pipeline(case)
    return package


def validate_receipt_text(text, court_case_number):
    number = re.sub(r'\s+', '', court_case_number)
    domain.require(bool(re.fullmatch(r'20\d{2}개회\d{1,12}', number)), 'RECEIPT_CASE_NUMBER',
                   '개인회생 법원 사건번호를 확인해 주세요. 예: 2026개회12345')
    compact = re.sub(r'\s+', '', text)
    domain.require(bool(re.search(r'(?<!\d)' + re.escape(number) + r'(?!\d)', compact))
                   and bool(re.search(r'접수증|접수확인|접수완료|접수내역|접수통지', compact))
                   and not re.search(r'접수(?:취소|반려|실패|미완료|예정)|미접수|접수되지않', compact),
                   'RECEIPT_EVIDENCE_REQUIRED', '접수증 원문에서 사건번호와 접수 사실을 확인할 수 없습니다.')
    return number


def add_receipt(case, package_id, record, user):
    package = current_package(case, package_id, approved=True)
    domain.require(all(record.get('review', {}).get(key) is True for key in ('person_confirmed', 'case_number_confirmed', 'receipt_confirmed'))
                   and len(str(record.get('review', {}).get('reason', '')).strip()) >= 5,
                   'RECEIPT_REVIEW_REQUIRED', '접수증 명의·사건번호·실제 접수 확인과 검토 사유가 필요합니다.')
    number = validate_receipt_text(record.get('text', ''), record['court_case_number'])
    domain.require(not any(r.get('sha256') == record['sha256'] and r.get('package_id') == package_id
                           for r in case.get('submission_receipts', [])), 'DUPLICATE_RECEIPT', '같은 접수증이 이미 연결되어 있습니다.')
    record.update(package_id=package_id, filing_package_id=package_id, court_case_number=number, status='verified', verified_by=user['id'],
                  verified_at=store.now(), source_type='external_filing_receipt')
    record['verification_hash'] = store.digest([record['sha256'], number, package['snapshot_hash'], record['review']])
    case.setdefault('submission_receipts', []).append(record)
    return record


def record_submission(case, data, user):
    package = current_package(case, data['bundle_id'], approved=True)
    receipt = domain.item(case, 'submission_receipts', data['receipt_document_id'])
    number = validate_receipt_text(receipt.get('text', ''), data['court_case_number'])
    domain.require(receipt.get('status') == 'verified' and receipt.get('package_id') == package['id']
                   and receipt.get('court_case_number') == number and bool(receipt.get('verified_by'))
                   and all(receipt.get('review', {}).get(key) is True for key in ('person_confirmed', 'case_number_confirmed', 'receipt_confirmed'))
                   and receipt.get('verification_hash') == store.digest([receipt['sha256'], number, package['snapshot_hash'], receipt['review']]),
                   'RECEIPT_REVIEW_REQUIRED', '최종 승인본에 연결된 접수증의 명의·사건번호·접수 확인이 필요합니다.')
    _hash_file(case, 'uploads', receipt['storage_name'], receipt['sha256'])
    domain.require(not any(s.get('filing_package_id') == package['id'] for s in case.get('submissions', [])),
                   'SUBMISSION_ALREADY_RECORDED', '이 제출 패키지의 접수 기록이 이미 있습니다.')
    case.setdefault('submissions', []).append({'id': store.uid('submission'), 'filing_package_id': package['id'],
        'bundle_id': package['id'], 'receipt_document_id': receipt['id'], 'court_case_number': number,
        'created_at': store.now(), 'recorded_by': user['id'], 'status': 'recorded',
        'record_type': 'external_filing_receipt', 'external_transmission': False, 'court_approval_confirmed': False,
        'input_revision': case['input_revision'], 'package_snapshot_hash': package['snapshot_hash'],
        'receipt_sha256': receipt['sha256'], 'approval': copy.deepcopy(package['approval'])})
    case['court_case_number'] = number
    _pipeline(case, submitted=True)
