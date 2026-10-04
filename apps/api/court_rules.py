"""Court document selection with auditable, dated official-source policies.

This module does not widen consultation extraction. It refines the existing
rulebook's document needs using explicitly supplied account/institution scopes.
The JSON distinguishes mandatory attachments, alternatives, and documents a
court *may* additionally order; a preparatory request is never called a statute.
Raw account numbers are not copied into request titles or external prompts.
"""
from __future__ import annotations

import calendar
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from .store import now, uid

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / 'data/court_request_rules.json'
MANAGED_BY = 'court_request_rules'
TERMINAL = {'withdrawn', 'cancelled', 'superseded'}


def definition():
    return json.loads(POLICY_PATH.read_text(encoding='utf-8'))


def _date(value):
    if isinstance(value, date):
        return value.date() if isinstance(value, datetime) else value
    return date.fromisoformat(str(value)[:10])


def _months_before(day, count):
    month_index = day.year * 12 + day.month - 1 - count
    year, month = divmod(month_index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _sources(policy, source_ids, locator=None):
    return [{**policy['sources'][key], 'id': key, **({'locator': locator} if locator else {})}
            for key in dict.fromkeys(source_ids) if key in policy['sources']]


def _accounts(case):
    rows = case.get('financial_accounts', case.get('accounts', []))
    if not isinstance(rows, list):
        return []
    result, seen = [], set()
    for account in rows:
        if not isinstance(account, dict) or account.get('status') in {'excluded', 'rejected'}:
            continue
        number = str(account.get('account_number', account.get('number', '')))
        institution = str(account.get('institution', account.get('bank', account.get('issuer', '')))).strip()
        key = str(account.get('id') or account.get('account_key') or _hash([institution, number]))
        if not institution or (not number and not account.get('id') and not account.get('account_key')):
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append({'institution': institution, 'account_key': key,
                       'account_label': '끝자리 ' + number[-4:] if number else str(account.get('label', '계좌')),
                       'purpose': account.get('purpose', ''), 'salary_account': account.get('salary_account', False)})
    return result


def _entities(case, catalog_id, profile):
    split = profile.get('split', {}).get(catalog_id)
    if split == 'account':
        rows = _accounts(case)
    elif split in {'creditor', 'insurer', 'employer'}:
        collection = {'creditor': 'creditors', 'insurer': 'insurance_policies', 'employer': 'employers'}[split]
        rows = []
        for item in case.get(collection, []):
            if not isinstance(item, dict) or item.get('status') in {'excluded', 'rejected'}:
                continue
            institution = item.get('institution') or item.get('name') or item.get('issuer')
            if institution:
                rows.append({'institution': str(institution), 'account_key': str(item.get('id', '')),
                             'account_label': '', 'purpose': ''})
    else:
        rows = []
    return rows or [{'institution': '', 'account_key': '', 'account_label': '', 'purpose': '',
                     'scope_unresolved': bool(split)}]


def _salary(case, needs):
    from .rulebook import income_profile
    return income_profile(case)['salary'] or any(
        n['catalog_id'] in {'D06', 'D07', 'D08', 'D50'} or 'WF02' in n.get('rule_ids', []) for n in needs)


def _business(case, needs):
    from .rulebook import income_profile
    # An obsolete WF03 request must not keep its own business branch alive.
    return income_profile(case)['business']


def _policy_rows(catalog_id, need, profile, is_salary):
    rule = deepcopy(profile.get('documents', {}).get(catalog_id, {}))
    if catalog_id == 'D36':
        if profile.get('bank_statement_months'):
            rows = [{'purpose': 'account_activity', 'months': profile['bank_statement_months'],
                     'requirement_kind': profile.get('bank_statement_requirement', 'preparation'),
                     'locator': profile.get('bank_statement_locator', ''), 'account_scope': 'all'}]
            if is_salary and profile.get('salary_statement_months'):
                rows.append({'purpose': 'salary_income', 'months': profile['salary_statement_months'],
                             'requirement_kind': 'mandatory', 'locator': profile['salary_statement_locator'],
                             'account_scope': 'salary'})
            return [{**rule, **row} for row in rows]
        salary_only = is_salary and set(need.get('rule_ids', [])) <= {'WF02'}
        if profile.get('salary_alternative_months') and salary_only:
            return [{**rule, 'purpose': 'salary_income', 'months': profile['salary_alternative_months'],
                     'requirement_kind': 'alternative', 'account_scope': 'salary',
                     'alternatives': ['D06', 'D07', 'D50'], 'locator': '제402호 제2조 제3호 가목'}]
        if profile.get('additional_bank_months'):
            return [{**rule, 'purpose': 'account_activity', 'months': profile['additional_bank_months'],
                     'requirement_kind': 'preparation', 'account_scope': 'all',
                     'locator': '제402호 제3조 제1호 나목 (법원이 추가로 명할 수 있는 서류)',
                     'qualification': '법원 추가요구 가능 범위에 맞춘 사전 준비이며, 모든 사건의 법정 필수서류라는 뜻은 아닙니다.'}]
    return [rule]


def plan(case, required_documents=None, as_of=None):
    """Return desired request scopes, without changing extraction or case state.

    Explicit ``case.document_scopes`` may supply multiple institution/account/
    date ranges for the same catalog ID. ``financial_accounts`` / ``creditors`` /
    ``insurance_policies`` expand existing known entities; no inference is made.
    """
    policy = definition()
    if required_documents is None:
        from .rulebook import evaluate_case
        required_documents = evaluate_case(case)['required_documents']
    needs = deepcopy(required_documents)
    court_id = case.get('court_id', '')
    profile = policy['courts'].get(court_id, {'coverage': 'unverified', 'source_ids': []})
    profile = {**profile, 'split': policy['split']}
    reference = _date(as_of or case.get('application_date') or case.get('filing_date') or
                      case.get('request_reference_date') or case.get('created_at') or
                      datetime.now(timezone(timedelta(hours=9))).date())
    if profile.get('effective_from') and reference < _date(profile['effective_from']):
        profile = {'coverage': 'unverified', 'source_ids': [], 'split': policy['split']}
    catalog = {d['id']: d for d in json.loads((ROOT / 'data/registry.json').read_text(encoding='utf-8'))['documents']}
    salary, business = _salary(case, needs), _business(case, needs)
    business_only = set(policy.get('income_eligibility', {}).get('business_only_document_ids', []))
    excluded_income_documents = [n['catalog_id'] for n in needs if n['catalog_id'] in business_only and not business]
    # This is an automatic request guard, not a ban on a staff/court request.
    # Reconcile below preserves manual_override and court_order_id requests.
    needs = [n for n in needs if n['catalog_id'] not in excluded_income_documents]
    if needs:
        additions = profile.get('mandatory_base', []) + (profile.get('salary_additions', []) if salary else []) + (
            profile.get('business_additions', []) if business else [])
        for document_id in additions:
            if not any(n['catalog_id'] == document_id for n in needs):
                needs.append({'catalog_id': document_id, 'reason': '관할 법원 자료제출목록에 따른 증빙',
                              'rule_ids': ['COURT_REQUIRED']})
    # An explicit empty list means recomputation removed all needs, not baseline refill.
    warnings, requests = [], []
    from .rulebook import income_profile
    income = income_profile(case)
    if required_documents and (income['kind'] == 'unknown' or income['uncertain']):
        warnings.append({'code': 'INCOME_TYPE_UNCONFIRMED',
                         'message': '세부상담에서 현재 급여·사업소득 및 겸업 여부를 확인해 주세요. 사업 여부가 확인되기 전에는 사업자 전용 서류를 자동 요청하지 않습니다.'})
    if profile.get('coverage') == 'unverified':
        warnings.append({'code': 'COURT_RULE_UNVERIFIED', 'court_id': court_id,
                         'message': '해당 법원의 세부 기간·발급옵션 원문이 확인되지 않아 전국 공통 기준과 사건별 지정범위만 적용합니다.'})
    scopes = case.get('document_scopes', [])
    for need in needs:
        document_id = need['catalog_id']
        if document_id not in catalog:
            continue
        entry = catalog[document_id]
        explicit_scopes = [x for x in scopes if x.get('catalog_id') == document_id and x.get('status') not in TERMINAL]
        variants = _policy_rows(document_id, need, profile, salary)
        pairs = [(scope, {**variants[0], **scope}) for scope in explicit_scopes] if explicit_scopes else [
            (entity, variant) for variant in variants for entity in _entities(case, document_id, profile)]
        for entity, settings in pairs:
            # Known payroll accounts narrow payroll requests, never all-account requests.
            if settings.get('account_scope') == 'salary':
                payroll_known = any(a.get('salary_account') or a.get('purpose') in {'salary', 'salary_income'} for a in _accounts(case))
                if payroll_known and not (entity.get('salary_account') or entity.get('purpose') in {'salary', 'salary_income'}):
                    continue
            options = deepcopy(settings.get('issuance_options', {}))
            source_ids = settings.get('source_ids', profile.get('source_ids', []))
            if document_id in policy['government_document_ids']:
                options.setdefault('freshness_months', 2)
                options.setdefault('freshness_exception', '특별한 사정이 있는 경우 예외 검토')
                source_ids = list(source_ids) + ['NATIONAL_FRESHNESS']
            if profile.get('all_certificates_freshness_months') and document_id not in {'D33', 'D43', 'D44', 'D45', 'D46', 'D47', 'D48'}:
                options['freshness_months'] = profile['all_certificates_freshness_months']
            start = settings.get('period_start')
            end = settings.get('period_end')
            months = settings.get('months')
            if months and not (start and end):
                start, end = _months_before(reference, months).isoformat(), reference.isoformat()
            if start and end:
                if _date(start) > _date(end):
                    raise ValueError('서류 요청 시작일은 종료일보다 늦을 수 없습니다.')
                period = f'{start} ~ {end}'
            elif settings.get('period_label'):
                period = settings['period_label']
            else:
                period = need.get('period') or '신청일 기준 현황 · 증명 대상 기간은 사건별 확인'
            if options.get('freshness_months'):
                period += f" · 발급일 {_months_before(reference, options['freshness_months']).isoformat()} 이후"
            institution = str(entity.get('institution', ''))
            account_key = str(entity.get('account_key', ''))
            account_label = str(entity.get('account_label', ''))
            purpose = settings.get('purpose', 'evidence')
            key_data = [court_id, document_id, institution, account_key, start, end, purpose, options,
                        period if not start else None]
            source_refs = _sources(policy, source_ids, settings.get('locator'))
            # Use an alternative only after its required scope has been verified.
            # A random verified bank file must never cover another account.
            verified = [d for d in case.get('documents', []) if d.get('status') == 'verified']
            if document_id == 'D36' and purpose == 'account_activity' and court_id == 'CT03':
                inventories = [d.get('document_metadata', d.get('metadata', {})) for d in verified if d.get('catalog_id') == 'D35']
                if any(meta.get('covers_all_accounts') is True and
                       meta.get('institution') in {institution, '전체 금융기관'} and bool(meta.get('institution')) and
                       (not meta.get('account_key') or meta.get('account_key') == account_key) for meta in inventories):
                    continue
            if settings.get('requirement_kind') == 'alternative' and document_id in {'D07', 'D50', 'D36'} and purpose != 'account_activity':
                if any(d.get('catalog_id') == 'D06' and (not institution or
                       d.get('document_metadata', d.get('metadata', {})).get('institution') == institution) for d in verified):
                    continue
            suffix = ' · '.join(x for x in [institution, account_label] if x)
            request = {'catalog_id': document_id, 'title': entry['name'] + (' · ' + suffix if suffix else ''),
                       'issuer': institution or entry['issuer'], 'institution': institution,
                       'account_key': account_key, 'account_label': account_label,
                       'period': period, 'period_start': start, 'period_end': end, 'purpose': purpose,
                       'reference_date': reference.isoformat(), 'issuance_options': options,
                       'requirement_kind': settings.get('requirement_kind', 'preparation'),
                       'reason': need.get('reason', '상담 내용에 따른 자료 확인'),
                       'qualification': settings.get('qualification', ''),
                       'source_refs': source_refs, 'rule_ids': need.get('rule_ids', []),
                       'alternatives': settings.get('alternatives', []),
                       'scope_key': _hash(key_data), 'scope_unresolved': entity.get('scope_unresolved', False),
                       'issuance_url': entry.get('issuance_url', ''), 'steps': entry.get('steps', []),
                       'not_proven': entry.get('not_proven', ''), 'policy_version': policy['version'],
                       'court_id': court_id, 'managed_by': MANAGED_BY}
            if request['scope_unresolved']:
                warnings.append({'code': 'REQUEST_SCOPE_UNRESOLVED', 'catalog_id': document_id,
                                 'message': '기관·계좌를 확인하면 요청이 기관·계좌별로 자동 분리됩니다.'})
            requests.append(request)
    deduplicated = {r['scope_key']: r for r in requests}
    return {'version': policy['version'], 'court_id': court_id, 'coverage': profile.get('coverage', 'unverified'),
            'reference_date': reference.isoformat(), 'requests': list(deduplicated.values()), 'warnings': warnings,
            'income_profile': income, 'excluded_income_document_ids': sorted(set(excluded_income_documents))}


def reconcile(case, required_documents=None, as_of=None):
    """Idempotently create/withdraw rule-managed requests, preserving every upload.

    Fulfilled obsolete requests remain fulfilled with ``no_longer_required``;
    reopening a former scope creates a new request with a predecessor link.
    Staff-created requests and case-specific court orders are never withdrawn.
    """
    result = plan(case, required_documents, as_of)
    case.setdefault('request_reference_date', result['reference_date'])
    existing = case.setdefault('requests', [])
    history = case.setdefault('request_history', [])
    # An intentional staff withdrawal remains effective for that exact scope.
    # A different account/period/court has a different key and is reconsidered.
    suppressed = {r.get('scope_key'): r for r in existing
                  if r.get('manual_override') and r.get('status') in TERMINAL and r.get('scope_key')}
    result['suppressed_requests'] = [{'scope_key': r['scope_key'], 'request_id': suppressed[r['scope_key']]['id'],
                                     'reason': suppressed[r['scope_key']].get('withdrawal_reason', '')}
                                    for r in result['requests'] if r['scope_key'] in suppressed]
    result['requests'] = [r for r in result['requests'] if r['scope_key'] not in suppressed]
    desired = {r['scope_key']: r for r in result['requests']}
    created, withdrawn = [], []
    timestamp = now()
    # Migrate legacy automatic requests without inventing scope equivalence.
    # Known identical scope keeps its request ID and all upload associations.
    # Unscoped submitted rows become historical review material, never copied
    # onto each newly split account or period and marked fulfilled en masse.
    for legacy in existing:
        if legacy.get('generated_by') != 'workflow_rulebook' or legacy.get('managed_by') == MANAGED_BY or legacy.get('migration_at') or legacy.get('manual_override'):
            continue
        specification = desired.get(legacy.get('scope_key'))
        if specification and legacy.get('status') not in TERMINAL:
            legacy.update(specification, migrated_from='workflow_rulebook', migration_at=timestamp)
            action = 'legacy_scope_migrated'
        else:
            uploaded = bool(legacy.get('document_ids')) or any(d.get('request_id') == legacy['id'] for d in case.get('documents', []))
            previous = legacy.get('status')
            legacy.update(status='withdrawn', no_longer_required=True, migration_at=timestamp,
                          requires_scope_review=uploaded,
                          withdrawal_reason='법원별 기관·계좌·기간 요청으로 전환. 기존 제출파일은 보존하며 범위 일치 확인 후 연결합니다.' if uploaded else
                                            '법원별 기관·계좌·기간 요청으로 대체됨')
            action = 'legacy_scope_review' if uploaded else 'legacy_withdrawn'
            withdrawn.append(legacy['id'])
            legacy['previous_status'] = previous
        history.append({'id': uid('reqevt'), 'request_id': legacy['id'], 'action': action, 'at': timestamp,
                        'previous_status': legacy.get('previous_status'), 'policy_version': result['version']})
    for request in existing:
        if request.get('managed_by') != MANAGED_BY or request.get('court_order_id') or request.get('manual_override'):
            continue
        if request.get('scope_key') in desired:
            continue
        if request.get('status') in TERMINAL or request.get('no_longer_required'):
            continue
        previous = request.get('status')
        request.update(no_longer_required=True, withdrawn_at=timestamp,
                       withdrawal_reason='상담·관할·기관·계좌·기간 변경으로 현재 요청 범위에서 제외됨')
        if previous != 'fulfilled':
            request['status'] = 'withdrawn'
        request['version'] = request.get('version', 1) + 1
        history.append({'id': uid('reqevt'), 'request_id': request['id'], 'action': 'withdrawn',
                        'previous_status': previous, 'reason': request['withdrawal_reason'], 'at': timestamp,
                        'scope_key': request.get('scope_key'), 'policy_version': result['version']})
        withdrawn.append(request['id'])
    for scope_key, specification in desired.items():
        active = next((r for r in existing if r.get('managed_by') == MANAGED_BY and r.get('scope_key') == scope_key
                       and r.get('status') not in TERMINAL and not r.get('no_longer_required')), None)
        if active:
            active.update(source_refs=specification['source_refs'], rule_ids=specification['rule_ids'],
                          policy_version=result['version'])
            continue
        predecessor = next((r for r in reversed(existing) if r.get('scope_key') == scope_key), None)
        request = {**specification, 'id': uid('req'), 'version': 1, 'status': 'requested',
                   'document_ids': [], 'due_date': None, 'created_at': timestamp,
                   'predecessor_id': predecessor['id'] if predecessor else None}
        existing.append(request)
        history.append({'id': uid('reqevt'), 'request_id': request['id'], 'action': 'requested',
                        'at': timestamp, 'scope_key': scope_key, 'policy_version': result['version'],
                        'source_ids': [s['id'] for s in request['source_refs']]})
        created.append(request['id'])
    case['court_request_plan'] = {k: v for k, v in result.items() if k != 'requests'}
    return {**result, 'created_ids': created, 'withdrawn_ids': withdrawn}


def validate_metadata(request, document, as_of=None):
    """Validate declared/OCR metadata; missing metadata remains unresolved.

    Returns reasons for a re-request, but never changes a document's verified
    state by itself. Consumer must verify identity/content and confidence too.
    """
    metadata = document.get('document_metadata', document.get('metadata', {}))
    failures, unknown = [], []
    for key in ('institution', 'account_key'):
        expected = request.get(key)
        if expected and not metadata.get(key):
            unknown.append(key)
        elif expected and metadata.get(key) != expected:
            failures.append({'code': key.upper() + '_MISMATCH', 'message': '요청한 기관 또는 계좌와 다른 자료입니다.'})
    for key, compare in [('period_start', lambda actual, expected: actual <= expected),
                         ('period_end', lambda actual, expected: actual >= expected)]:
        if request.get(key):
            if not metadata.get(key):
                unknown.append(key)
            else:
                try:
                    if not compare(_date(metadata[key]), _date(request[key])):
                        failures.append({'code': 'PERIOD_INCOMPLETE', 'message': '요청기간 전체가 포함되지 않았습니다.'})
                except (ValueError, TypeError):
                    failures.append({'code': 'INVALID_DATE', 'message': '문서 기간 날짜를 확인할 수 없습니다.'})
    options = request.get('issuance_options', {})
    if options.get('freshness_months'):
        if not metadata.get('issued_at'):
            unknown.append('issued_at')
        else:
            try:
                today = _date(as_of or datetime.now(timezone(timedelta(hours=9))).date())
                reference = max(_date(request['reference_date']), today)
                issued = _date(metadata['issued_at'])
                # The planning anchor stabilizes request IDs, not certificate age.
                # Reissued valid documents after that anchor remain acceptable.
                if issued > today or issued < _months_before(reference, options['freshness_months']):
                    failures.append({'code': 'ISSUANCE_DATE_OUT_OF_RANGE', 'message': '요청 기준 발급일 범위를 확인해 재발급하거나 예외 사유를 남겨주세요.'})
            except (ValueError, TypeError):
                failures.append({'code': 'INVALID_DATE', 'message': '발급일을 확인할 수 없습니다.'})
    for key in ('certificate_type', 'address_history', 'tax_scope', 'jurisdiction_scope', 'person_number_display'):
        if options.get(key):
            if not metadata.get(key):
                unknown.append(key)
            elif metadata[key] != options[key]:
                failures.append({'code': 'ISSUANCE_OPTION_MISMATCH', 'field': key, 'message': '필요한 발급 옵션과 일치하지 않습니다.'})
    return {'status': 're_request' if failures else 'metadata_required' if unknown else 'matched',
            'failures': failures, 'unknown_fields': unknown}
