"""Atomic human review of an uploaded original and its extraction candidates.

This records a staff decision, never an AI verdict or a replacement source.
The uploaded bytes, OCR text, page text and previous authored files are immutable.
"""
import copy

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr
from typing import Literal

from . import automation, domain, store


NUMERIC_KEYS = {'monthly_income', 'total_debt', 'living_expenses', 'assets_total',
                'household_size', 'housing_cost', 'housing_deposit'}


class CandidateDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    candidate_id: str = Field(min_length=1, max_length=200)
    decision: Literal['accept', 'correct', 'reject']
    value: StrictStr | StrictInt | StrictFloat | StrictBool | list | dict | None = None
    reason: str | None = Field(default=None, min_length=5, max_length=1500)


class SourceReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: StrictInt = Field(ge=1)
    scope_confirmed: StrictBool
    content_confirmed: StrictBool
    person_confirmed: StrictBool
    reason: str = Field(min_length=5, max_length=1500)
    candidate_reviews: list[CandidateDecision] = Field(default_factory=list, max_length=1000)


def reviewable_document(case, doc_id):
    from .extraction_readiness import document_state
    document = domain.item(case, 'documents', doc_id)
    domain.require(document.get('status') not in {'superseded', 'quarantined', 'rejected'},
                   'INACTIVE_DOCUMENT', '대체되거나 격리·반려된 서류는 바로 검토 완료할 수 없습니다.')
    domain.require(document.get('automated_check', {}).get('coverage_status') != 'identity_conflict',
                   'DOCUMENT_IDENTITY_CONFLICT', '서류의 명의가 사건과 다릅니다. 자료 귀속을 먼저 확인하세요.')
    state = document_state(case, document)
    domain.require(state['can_review'], 'EXTRACTION_PENDING', state['message'])
    return document


def validate_candidate(candidate, decision, value, *, document=None):
    if document is not None:
        domain.require(candidate.get('document_id') == document['id'],
                       'CANDIDATE_DOCUMENT_MISMATCH', '이 서류에 속한 추출 항목만 함께 검토할 수 있습니다.')
    domain.require(candidate.get('status') not in ('superseded', 'quarantined'),
                   'INACTIVE_CANDIDATE', '대체되거나 격리된 값은 바로 채택할 수 없습니다. 원문을 재확인하고 다시 분석하세요.')
    domain.require(candidate.get('status') != 'rejected' or decision != 'accept',
                   'REJECTED_CANDIDATE', '반려한 값은 원문과 함께 수정 검토하세요.')
    if decision == 'correct':
        domain.require(value is not None, 'VALUE_REQUIRED', '수정할 값을 입력하세요.')
        domain.require(len(store.dumps(value)) <= 10000, 'VALUE_SIZE', '수정값이 너무 깁니다.')
        if candidate['key'] in NUMERIC_KEYS:
            domain.require(type(value) is int and value >= 0,
                           'VALUE_TYPE', '금액·인원은 0 이상의 정수로 입력하세요.')


def apply_candidate(candidate, decision, value, reason, user, *, document=None, case=None):
    """Apply an already validated decision; the caller invalidates exactly once."""
    candidate.setdefault('review_history', []).append({
        'at': store.now(), 'actor_id': user['id'],
        'previous': {key: copy.deepcopy(candidate.get(key))
                     for key in ('value', 'original_value', 'status', 'origin', 'review', 'source_edit')}})
    review = {'decision': decision, 'reason': reason, 'actor': user['name'],
              'actor_id': user['id'], 'at': store.now(), 'verification_type': 'human_review'}
    if document is not None:
        review.update(document_id=document['id'], source_sha256=document.get('sha256'),
                      source_version=document.get('version', 1),
                      source_text_sha256=store.digest(document.get('text', '')))
        if case is not None:
            review['source_document_signature'] = automation.document_review_signature(case, document)
    if decision in {'correct', 'reject'}:
        basis = {key: copy.deepcopy(candidate.get(key)) for key in (
            'key', 'document_id', 'source_id', 'source_ids', 'typed_fact_id', 'typed_fact_ids',
            'quote', 'basis', 'amount_basis', 'frequency', 'unit', 'source_unit_quote',
            'unit_multiplier', 'period_start', 'period_end', 'year', 'month', 'as_of',
            'institution', 'account_key', 'page', 'line_start', 'line_end')}
        basis.update(value_before=copy.deepcopy(candidate['value']),
                     original_value=copy.deepcopy(candidate.get('original_value', candidate['value'])))
        # A later unchanged confirmation must not erase the explicit correction.
        # Mapping may use this only after rechecking the bound original and scope;
        # the original quote is never rewritten to impersonate source evidence.
        candidate['source_edit'] = {**copy.deepcopy(review), 'reviewed_basis': basis,
                                    'corrected_value': copy.deepcopy(value) if decision == 'correct' else None}
    if decision == 'correct':
        candidate.update(original_value=candidate.get('original_value', candidate['value']),
                         value=value, origin='human_correction')
    candidate.update(status='rejected' if decision == 'reject' else 'accepted', review=review)


def apply_document(case, document, data, user):
    document.setdefault('review_history', []).append({
        'at': store.now(), 'actor_id': user['id'],
        'source_sha256': document.get('sha256'), 'source_version': document.get('version', 1),
        'previous': {key: copy.deepcopy(document.get(key)) for key in (
            'status', 'scope_confirmed', 'content_confirmed', 'person_confirmed',
            'reason', 'verified_by', 'verified_at', 'manual_verification', 'auto_verified', 'extraction_review')}})
    document.update(**{key: getattr(data, key) for key in (
        'scope_confirmed', 'content_confirmed', 'person_confirmed', 'reason')},
        verified_by=user['name'], verified_at=store.now(), auto_verified=False)
    document['status'] = 'verified' if all(getattr(data, key) for key in (
        'scope_confirmed', 'content_confirmed', 'person_confirmed')) else 'needs_more'
    document['public_status'] = '담당자 확인 완료' if document['status'] == 'verified' else '추가 확인 필요'
    if document.get('request_id'):
        request = domain.item(case, 'requests', document['request_id'])
        # A historical source can be reviewed without reopening a withdrawn request.
        if request.get('status') not in automation.INACTIVE and not request.get('no_longer_required'):
            request['status'] = 'fulfilled' if document['status'] == 'verified' else 'needs_more'
            if document['status'] == 'verified':
                request.pop('public_review_note', None)
    if document['status'] == 'verified':
        document['manual_verification'] = {
            'signature': automation.document_review_signature(case, document),
            'verified_at': document['verified_at'], 'verified_by': document['verified_by'],
            'verified_by_id': user['id']}
    else:
        document.pop('manual_verification', None)


def attach(app, staff, change):
    @app.post('/api/cases/{case_id}/documents/{doc_id}/review')
    def review_source(case_id: str, doc_id: str, data: SourceReview, user=Depends(staff)):
        def apply(case):
            document = reviewable_document(case, doc_id)
            ids = [row.candidate_id for row in data.candidate_reviews]
            domain.require(len(ids) == len(set(ids)), 'DUPLICATE_CANDIDATE', '같은 추출 항목을 두 번 검토할 수 없습니다.')
            candidates = []
            for row in data.candidate_reviews:
                candidate = domain.item(case, 'extraction_candidates', row.candidate_id)
                validate_candidate(candidate, row.decision, row.value, document=document)
                domain.require(candidate.get('status') != 'rejected', 'INACTIVE_CANDIDATE',
                               '반려한 항목은 통합 검토에서 되살릴 수 없습니다. 원문을 재확인하세요.')
                candidates.append((candidate, row))
            active = [candidate for candidate in case.get('extraction_candidates', [])
                      if candidate.get('document_id') == doc_id
                      and candidate.get('status') not in {'rejected', 'superseded', 'quarantined'}]
            for candidate in active:
                if candidate['id'] not in ids:
                    validate_candidate(candidate, 'accept', None, document=document)
            domain.invalidate(case, '서류 원문·추출값 통합 검토 반영')
            for candidate in active:
                if candidate['id'] not in ids:
                    apply_candidate(candidate, 'accept', None, data.reason, user, document=document, case=case)
            for candidate, row in candidates:
                apply_candidate(candidate, row.decision, row.value, row.reason or data.reason, user,
                                document=document, case=case)
            apply_document(case, document, data, user)
            document['extraction_review'] = {
                'at': document['verified_at'], 'actor_id': user['id'],
                'reason': data.reason, 'source_sha256': document.get('sha256'),
                'source_version': document.get('version', 1),
                'source_document_signature': automation.document_review_signature(case, document),
                'candidate_ids': [candidate['id'] for candidate in active],
                'edited_candidate_ids': ids, 'input_revision': case['input_revision'],
                'scope': 'human_source_and_all_active_candidate_review', 'ai_verified': False}
        # One case transaction, one audit event and one post-commit scheduling call.
        return change(case_id, user, data.expected_version, 'document.verified', apply)
