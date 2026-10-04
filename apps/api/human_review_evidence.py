"""Bind explicit staff corrections to independently re-parsed originals.

An audit record is a human statement about an original, not replacement OCR or
an AI pass. Unchanged acceptance alone never authorizes a different value.
"""
from __future__ import annotations

import copy

from . import document_facts, store

VERSION = 'human-source-correction-v1'
AGGREGATES = {'total_debt', 'assets_total', 'creditors', 'bank_balance', 'principal_total',
              'interest_total', 'annual_income', 'income_manwon', 'secured_debt', 'unsecured_debt'}
CONTEXT_KEYS = ('basis', 'frequency', 'unit', 'source_unit_quote', 'unit_multiplier',
                'period_start', 'period_end', 'as_of', 'institution', 'account_key', 'page')


def _same(left, right):
    return type(left) is type(right) and left == right


def apply(case, observed):
    """Return effective observations, separate audit sources and explicit gaps."""
    from .automation import document_review_signature

    documents = {doc['id']: doc for doc in case.get('documents', [])}
    originals = {row['id']: row for row in observed}
    edits, sources, errors, grouped = [], [], [], {}

    def issue(candidate, code, reason):
        errors.append({'code': code, 'key': candidate.get('key'), 'candidate_id': candidate.get('id'),
                       'document_id': candidate.get('document_id'), 'reason': reason})

    # A preliminary render can replace candidates with mapped projections. Its
    # audit-only copies retain the original signed edits, not a second decision.
    candidates = {}
    for candidate in [*case.get('extraction_candidates', []), *case.get('source_review_candidates', [])]:
        if candidate.get('id'):
            candidates.setdefault(candidate['id'], candidate)
    for candidate in candidates.values():
        if candidate.get('status') in {'superseded', 'quarantined'}:
            continue
        edit = candidate.get('source_edit')
        if not isinstance(edit, dict):
            if candidate.get('origin') == 'human_correction' and candidate.get('status') == 'accepted':
                issue(candidate, 'HUMAN_REVIEW_BINDING_REQUIRED', '수정값을 현재 원문과 연결한 담당자 검토 기록이 필요합니다.')
            continue
        doc = documents.get(candidate.get('document_id'))
        if not doc or doc.get('status') != 'verified' or doc.get('automated_check', {}).get('coverage_status') == 'identity_conflict':
            continue
        decision = edit.get('decision')
        valid_state = ((decision == 'correct' and candidate.get('status') == 'accepted'
                        and _same(candidate.get('value'), edit.get('corrected_value')))
                       or (decision == 'reject' and candidate.get('status') == 'rejected'))
        binding = (valid_state and edit.get('verification_type') == 'human_review'
                   and bool(edit.get('actor_id')) and bool(edit.get('at'))
                   and isinstance(edit.get('reason'), str) and len(edit['reason'].strip()) >= 5
                   and bool(doc.get('sha256')) and edit.get('document_id') == doc['id']
                   and edit.get('source_sha256') == doc['sha256']
                   and edit.get('source_version') == doc.get('version', 1)
                   and edit.get('source_text_sha256') == store.digest(doc.get('text', ''))
                   and edit.get('source_document_signature') == document_review_signature(case, doc))
        if not binding:
            issue(candidate, 'HUMAN_REVIEW_SOURCE_CHANGED', '담당자 수정·반려 기록과 현재 원문 또는 확인 범위가 달라 다시 확인해야 합니다.')
            continue
        basis = edit.get('reviewed_basis') or {}
        ids = basis.get('typed_fact_ids') or ([basis['typed_fact_id']] if basis.get('typed_fact_id') else [])
        if candidate.get('key') in AGGREGATES or not isinstance(ids, list) or len(ids) != 1:
            issue(candidate, 'HUMAN_REVIEW_DETAIL_REQUIRED', '합계나 여러 항목의 수정값을 개별 채무·계좌에 임의 배분하지 않습니다. 원문에 연결된 세부 항목을 수정해 주세요.')
            continue
        original = originals.get(ids[0])
        alias = (candidate.get('key') == 'monthly_income' and original
                 and original['key'] in {'income_net', 'income_gross'} and original.get('frequency') == 'monthly')
        identified = (original and original['document_id'] == doc['id']
                      and basis.get('document_id') == doc['id'] and basis.get('key') == candidate.get('key')
                      and (original['key'] == candidate.get('key') or alias)
                      and _same(basis.get('original_value'), original['value'])
                      and basis.get('quote') == original['quote'])
        if identified:
            identified = (basis.get('source_id') == original['source_id'] if basis.get('source_id')
                          else bool({doc['id'], original['source_id']} & set(basis.get('source_ids') or [])))
        if identified:
            for key in CONTEXT_KEYS:
                value = basis.get('amount_basis') if key == 'basis' and basis.get('basis') is None else basis.get(key)
                if value is not None and not _same(value, original.get(key)):
                    identified = False
                    break
        if not identified:
            issue(candidate, 'HUMAN_REVIEW_FACT_CHANGED', '수정 대상으로 지정한 항목의 원래 값·기간·계좌·인용을 현재 원문에서 확인하지 못했습니다.')
            continue
        # Changing an identity used by other observations needs a separate entity
        # review; changing one label must not silently move every account/loan.
        if original['key'] in {'account_key', 'institution', 'loan_key'}:
            issue(candidate, 'HUMAN_REVIEW_DETAIL_REQUIRED', '기관·계좌·대출 식별을 변경하려면 해당 금액 항목의 귀속도 함께 확인해야 합니다.')
            continue
        value = edit.get('corrected_value')
        if decision == 'correct' and not (
                type(value) is type(original['value']) and (
                    type(value) is bool or type(value) is int and value >= 0
                    or isinstance(value, str) and bool(value.strip()) and len(value) <= 10000)):
            issue(candidate, 'HUMAN_REVIEW_VALUE_TYPE', '수정값의 금액·인원·문자 형식을 원래 항목과 맞춰 주세요.')
            continue
        record = {'candidate_id': candidate['id'], 'original_fact_id': original['id'],
                  **copy.deepcopy(edit)}
        review_id = 'human-review:' + store.digest(record)[:24]
        label = document_facts.LABELS.get(original['key'], original['key'])
        if original['key'] == 'income_gross':
            label = '세전 급여'
        period = {'monthly': '월 ', 'annual': '연간 '}.get(original.get('frequency'), '')
        unit = '원' if original.get('unit') == 'KRW' else ''
        action = (f'수정 후 {period}{label}: {store.dumps(value)}{unit}' if decision == 'correct'
                  else '해당 판독값은 반려되어 사용할 수 없습니다.')
        quote = f'담당자 원문 검토 기록\n{action}\n기존 판독값: {store.dumps(original["value"])}\n검토 사유: {edit["reason"]}'
        source = {'id': review_id, 'kind': 'human_review', 'text': quote, 'version': VERSION,
                  'document_id': doc['id'], 'original_source_id': original['source_id'],
                  'original_fact_id': original['id'], 'source_document_signature': edit['source_document_signature'],
                  'actor_id': edit['actor_id'], 'reason': edit['reason'], 'decision': decision,
                  'original_value': copy.deepcopy(original['value']), 'corrected_value': copy.deepcopy(value),
                  'ai_verified': False}
        sources.append(source)
        edits.append(record)
        grouped.setdefault(original['id'], []).append((record, source))

    effective = []
    for original in observed:
        matches = grouped.get(original['id'], [])
        if not matches:
            effective.append(copy.deepcopy(original))
            continue
        outcomes = {store.dumps([record['decision'], record.get('corrected_value')]) for record, _ in matches}
        if len(outcomes) > 1:
            errors.append({'code': 'HUMAN_REVIEW_CONFLICT', 'key': original['key'],
                           'document_id': original['document_id'], 'fact_ids': [original['id']],
                           'reason': '같은 원문 항목에 서로 다른 담당자 수정·반려 기록이 있어 하나로 확정할 수 없습니다.'})
            effective.append({**copy.deepcopy(original), 'value': None, 'status': 'review_required'})
            continue
        record, source = matches[-1]
        rejected = record['decision'] == 'reject'
        if rejected:
            errors.append({'code': 'HUMAN_REVIEW_REJECTED', 'key': original['key'],
                           'document_id': original['document_id'], 'fact_ids': [original['id']],
                           'reason': '담당자가 이 판독값을 반려했습니다. 해당 항목을 보완하기 전에는 계산·서식 값으로 사용하지 않습니다.'})
        row = {**copy.deepcopy(original), 'id': 'reviewed-' + store.digest([original['id'], source['id']])[:20],
               'value': None if rejected else copy.deepcopy(record['corrected_value']),
               'status': 'rejected' if rejected else 'human_reviewed', 'origin': VERSION,
               'source_type': 'human_review', 'source_id': source['id'], 'quote': source['text'],
               'original_fact_id': original['id'], 'original_source_id': original['source_id'],
               'original_quote': original['quote'], 'original_value': copy.deepcopy(original['value']),
               'original_unit_multiplier': original.get('unit_multiplier', 1),
               'original_source_unit_quote': original.get('source_unit_quote'),
               'unit_multiplier': 1, 'source_unit_quote': None,
               'review_source_ids': [item['id'] for _, item in matches],
               'source_edit': copy.deepcopy(record), 'ai_verified': False}
        row.pop('explicit_absence', None)
        effective.append(row)
    return {'facts': effective, 'sources': sources, 'edits': edits, 'errors': errors}
