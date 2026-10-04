"""Observed local court outcomes, never a language-model approval probability.

The 30-case floor is a conservative product display policy, not a claim of
statistical sufficiency or validation. Wilson intervals describe sampling
uncertainty conditional on the recorded cohort; they do not remove selection
bias or measure an individual applicant's chance of approval.
Formula: NIST/SEMATECH, 7.2.4.1,
https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm
"""
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import re
import sqlite3

from . import store

VERSION = 'verified-local-outcomes-v1'
PROFILE_VERSION = 'predecision-financial-cohort-v1'
MINIMUM_SAMPLE_SIZE = 30
LOOKBACK_DAYS = 730
MAX_RECORDS = 10000
SEOUL = timezone(timedelta(hours=9))
DECISION_TYPES = ('initial_plan_approval', 'initial_plan_denial', 'application_dismissal',
                  'commencement', 'correction_order', 'discharge', 'other')
TERMINAL = {'initial_plan_approval', 'initial_plan_denial', 'application_dismissal'}
OUTCOME_DEFINITION = '최초 변제계획 인가 / (최초 변제계획 인가 + 최초 불인가·개시신청 기각). 사건당 첫 종결결정 1건; 개시·보정·면책·항고 파기환송은 인가로 세지 않습니다.'


def _instant(value):
    if not isinstance(value, str):
        raise ValueError('TIMESTAMP_REQUIRED')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return parsed.astimezone(timezone.utc)


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('DECISION_DATE_REQUIRED')
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('DECISION_DATE_INVALID') from exc


def _compact(value):
    return re.sub(r'\s+', '', str(value))


def verify_decision(data, document, *, as_of=None):
    """Check a lawyer-classified operative quote and its date against evidence.

    Legacy broad 'approved' labels remain usable history, but are unclassified
    for this estimator. No classifier invents a court stage from those labels.
    """
    kind = data.get('decision_type')
    if not kind:
        if data.get('decision_date') or data.get('decision_quote'):
            raise ValueError('DECISION_TYPE_REQUIRED')
        return {'verified': False, 'code': 'DECISION_UNCLASSIFIED'}
    if kind not in DECISION_TYPES:
        raise ValueError('DECISION_TYPE_INVALID')
    day = _day(data.get('decision_date'))
    cutoff = _instant(as_of or store.now())
    if day > cutoff.astimezone(SEOUL).date():
        raise ValueError('DECISION_DATE_FUTURE')
    quote = data.get('decision_quote')
    text = document.get('text', '')
    if (not isinstance(quote, str) or not 5 <= len(quote) <= 1500
            or not isinstance(text, str) or quote not in text):
        raise ValueError('DECISION_QUOTE_NOT_IN_SOURCE')
    compact = _compact(text)
    dates = (day.isoformat(), f'{day.year}.{day.month}.{day.day}.',
             f'{day.year}.{day.month:02}.{day.day:02}.', f'{day.year}년{day.month}월{day.day}일')
    if not any(candidate in compact for candidate in dates):
        raise ValueError('DECISION_DATE_NOT_IN_SOURCE')
    expected = {'initial_plan_approval': 'approved', 'initial_plan_denial': 'rejected',
                'application_dismissal': 'rejected', 'commencement': 'approved',
                'correction_order': 'correction', 'discharge': 'approved'}.get(kind)
    if expected and data.get('outcome') != expected:
        raise ValueError('DECISION_OUTCOME_MISMATCH')
    q = _compact(quote)
    patterns = {
        'initial_plan_approval': r'변제계획(?:안)?(?:을|를)?인가한다',
        'initial_plan_denial': r'변제계획(?:안)?(?:을|를)?(?:불인가한다|인가하지아니한다)',
        'application_dismissal': r'(?:개인회생절차)?개시신청(?:을|를)?기각한다',
        'commencement': r'개인회생절차(?:를)?개시한다',
        'correction_order': r'보정(?:을|하여|하라|명령|권고|할)',
        'discharge': r'면책(?:한다|을허가한다)',
    }
    if kind in patterns and not re.search(patterns[kind], q):
        raise ValueError('DECISION_OPERATIVE_TEXT_UNCONFIRMED')
    if re.search(r'예시|가상|샘플|검토용|모의|파기.*환송', q):
        raise ValueError('DECISION_NOT_INITIAL_ADJUDICATION')
    # A discharge/remand decision may quote an earlier plan's approval. It is
    # not an initial approval order merely because the substring is present.
    if kind in TERMINAL and re.search(r'면책한다|면책을허가한다|파기하고.{0,80}환송', compact):
        raise ValueError('DECISION_STAGE_CONFLICT')
    return {'verified': kind != 'other', 'version': VERSION,
            'decision_type': kind, 'decision_date': day.isoformat(),
            'quote_sha256': hashlib.sha256(quote.encode()).hexdigest(),
            'source_text_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'method': 'lawyer_classification_exact_operative_quote_and_date'}


def profile(case, calculation=None):
    """Freeze pre-decision matching variables at document generation time."""
    calculation = calculation or {}
    if not isinstance(calculation, dict):
        return None
    summary, inputs = calculation.get('summary', {}), calculation.get('inputs', {})
    if not isinstance(summary, dict) or not isinstance(inputs, dict) or not isinstance(inputs.get('income'), dict):
        return None
    monetary = [summary.get(key) for key in ('unsecured_debt', 'secured_debt', 'liquidation_shortfall', 'monthly_creditor_capacity')]
    kind, months = inputs.get('income', {}).get('kind'), summary.get('months')
    if (case.get('case_type', 'personal_rehabilitation') != 'personal_rehabilitation'
            or any(type(value) is not int or value < 0 for value in monetary)
            or kind not in {'wage', 'business'} or type(months) is not int or months < 1
            or not calculation.get('policy_hash')):
        return None
    debt = monetary[0] + monetary[1]
    return {'version': PROFILE_VERSION, 'case_type': case.get('case_type', 'personal_rehabilitation'),
            'policy_hash': calculation['policy_hash'], 'income_kind': kind,
            'debt_band': 'up_to_50m' if debt <= 50000000 else 'up_to_100m' if debt <= 100000000 else 'up_to_300m' if debt <= 300000000 else 'over_300m',
            'has_secured_debt': monetary[1] > 0, 'liquidation_met': monetary[2] == 0,
            'positive_capacity': monetary[3] > 0,
            'months_band': 'under_36' if months < 36 else '36' if months == 36 else 'over_36'}


def wilson_interval(approved, total):
    if type(total) is not int or type(approved) is not int or not 0 <= approved <= total or total < 1:
        raise ValueError('BINOMIAL_COUNTS_INVALID')
    z, fraction = 1.959963984540054, approved / total
    denominator = 1 + z * z / total
    center = (fraction + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(fraction * (1 - fraction) / total + z * z / (4 * total * total)) / denominator
    return {'method': 'wilson', 'level': 0.95,
            'lower': round(max(0, center - radius) * 100, 2),
            'upper': round(min(1, center + radius) * 100, 2)}


def estimate(case, calculation=None, *, as_of=None):
    cutoff = _instant(as_of or store.now())
    # A requested future cutoff must never disclose outcomes unavailable now.
    if cutoff > _instant(store.now()):
        raise ValueError('ESTIMATION_DATE_FUTURE')
    start = cutoff - timedelta(days=LOOKBACK_DAYS)
    if calculation is None:
        calculation = next((row for row in reversed(case.get('legal_calculations', []))
                            if not row.get('stale') and row.get('input_revision') == case.get('input_revision')), None)
    target = profile(case, calculation)
    result = {'version': VERSION, 'status': 'insufficient_evidence', 'percentage': None,
        'prediction': {'status': 'not_validated', 'percentage': None,
                       'reason': '개별 사건 예측에 필요한 독립 시계열 검증·확률 보정 자료가 없습니다.'},
        'observed_rate': {'label': '유사사건 관측 인가비율', 'percentage': None, 'confidence_interval': None,
                          'scope': '같은 사무실·법원·사건유형·생성 당시 재무분류의 기록된 종결 빈도이며 개인 사건의 승인 확률이 아닙니다.'},
        'evidence_count': 0, 'minimum_sample_size': MINIMUM_SAMPLE_SIZE,
        'distribution': {'approved': 0, 'rejected': 0, 'correction': 0},
        'outcome_definition': OUTCOME_DEFINITION,
        'cohort': {'court_id': case.get('court_id'), 'case_type': case.get('case_type', 'personal_rehabilitation'),
                   'lookback_start': start.isoformat(), 'as_of': cutoff.isoformat(),
                   'matching_fields': target, 'correction_excluded_from_denominator': True},
        'as_of': cutoff.isoformat(), 'reasons': [], 'exclusions': {}, 'evidence_refs': [],
        'limitations': ['결과가 등록된 사건만 관찰하여 선택편향이 남습니다. 미종결 사건은 분모에서 제외됩니다.',
                       '95% 구간은 관측표본의 통계적 불확실성이며 개별 인가 보장이나 모델 정확도가 아닙니다.',
                       '최소 30건은 표시 정책이며 예측모델의 검증 통과 기준이 아닙니다.'],
        'prediction_validation': {'status': 'not_available', 'independent_temporal_holdout': False,
                                  'calibration_verified': False, 'model_version': None},
        'method_source': 'https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm'}
    if not case.get('org_id') or not case.get('court_id'):
        result['reasons'].append({'code': 'SCOPE_REQUIRED', 'message': '소속 사무실과 관할이 확인되어야 합니다.'})
        return result
    if not target:
        result['reasons'].append({'code': 'MATCHING_FEATURES_REQUIRED', 'message': '현재 계산의 소득·채무·청산가치·변제기간을 확인해야 유사집단을 비교할 수 있습니다.'})
        return result
    try:
        if _instant(calculation.get('created_at')) > cutoff:
            raise ValueError('TARGET_FEATURES_AFTER_ASOF')
    except (ValueError, TypeError, AttributeError):
        result['reasons'].append({'code': 'TARGET_FEATURE_TIME_UNCONFIRMED',
                                 'message': '평가시각 이전에 생성된 계산 이력이 확인되어야 집계할 수 있습니다.'})
        return result
    excluded, candidates = Counter(), []
    try:
        with store.db() as con:
            rows = con.execute('''SELECT o.*, c.body AS case_body FROM court_outcomes o
                JOIN cases c ON c.id=o.case_id AND c.org_id=o.org_id
                WHERE o.org_id=? AND o.court_id=? ORDER BY o.created_at DESC LIMIT ?''',
                (case['org_id'], case['court_id'], MAX_RECORDS + 1)).fetchall()
            if len(rows) > MAX_RECORDS:
                result['reasons'].append({'code': 'COHORT_QUERY_LIMIT', 'message': '표본 전체를 확인할 수 있는 범위에서 다시 집계해야 합니다.'})
                return result
            for dbrow in rows:
                try:
                    row, historical = json.loads(dbrow['body']), json.loads(dbrow['case_body'])
                    def reject(code):
                        excluded[code] += 1
                    if dbrow['case_id'] == case['id']:
                        reject('CURRENT_CASE_EXCLUDED'); continue
                    if row.get('synthetic') or historical.get('synthetic'):
                        reject('SYNTHETIC'); continue
                    if historical.get('org_id') != case['org_id'] or historical.get('court_id') != case['court_id']:
                        reject('SCOPE_CHANGED'); continue
                    if not start <= _instant(row['created_at']) <= cutoff:
                        reject('OUTSIDE_KNOWLEDGE_WINDOW'); continue
                    decision_day = _day(row.get('decision_date'))
                    decision_start = datetime.combine(decision_day, time.min, SEOUL)
                    if not start.astimezone(SEOUL).date() <= decision_day <= cutoff.astimezone(SEOUL).date():
                        reject('OUTSIDE_DECISION_WINDOW'); continue
                    if row.get('evidence_verified') is not True or row.get('decision_verified') is not True:
                        reject('UNVERIFIED_OR_UNCLASSIFIED'); continue
                    generated = row.get('generated_snapshot', {})
                    features = generated.get('generation_features', {})
                    if features.get('approval_profile') != target or row.get('features') != features:
                        reject('COHORT_MISMATCH'); continue
                    # Same-day generation has ambiguous ordering; require a
                    # strictly earlier dated snapshot instead of leaking a decision.
                    if _instant(generated['created_at']) >= decision_start:
                        reject('POST_DECISION_FEATURES'); continue
                    doc_hash = row.get('document_hash')
                    if not doc_hash or doc_hash != (generated.get('content_hash') or generated.get('snapshot_hash')):
                        reject('GENERATION_HASH_MISMATCH'); continue
                    ledger = con.execute('SELECT body FROM generated_document_versions WHERE id=? AND case_id=? AND content_hash=?',
                                         (generated['id'] + ':' + doc_hash, dbrow['case_id'], doc_hash)).fetchone()
                    if not ledger or json.loads(ledger['body']).get('generation_features') != features:
                        reject('GENERATION_LEDGER_MISSING'); continue
                    evidence = next((doc for doc in historical.get('documents', []) if doc.get('id') == row.get('source_document_id')), None)
                    if (not evidence or evidence.get('status') != 'verified' or evidence.get('synthetic')
                            or not row.get('source_hash') or row['source_hash'] != evidence.get('sha256')
                            or row.get('source_version') != evidence.get('version')):
                        reject('DECISION_EVIDENCE_CHANGED'); continue
                    validation = verify_decision(row, evidence, as_of=cutoff.isoformat())
                    stored_validation = row.get('decision_verification', {})
                    if not validation['verified'] or validation != stored_validation:
                        reject('DECISION_VERIFICATION_MISMATCH'); continue
                    if row['decision_type'] not in TERMINAL | {'correction_order'}:
                        reject('DIFFERENT_DECISION_STAGE'); continue
                    candidates.append({**row, 'case_id': dbrow['case_id']})
                except (ValueError, TypeError, KeyError, AttributeError):
                    excluded['INVALID_OR_LEGACY_RECORD'] += 1
    except (sqlite3.Error, OSError):
        result['reasons'].append({'code': 'OUTCOME_STORE_UNAVAILABLE', 'message': '법원 결과 기록을 읽지 못해 비율을 보류했습니다.'})
        return result
    by_case = {}
    for row in sorted(candidates, key=lambda row: (row['decision_date'], row['created_at'], row['id'])):
        by_case.setdefault(row['case_id'], []).append(row)
    terminal, seen_sources, correction_cases = [], set(), set()
    for case_id, events in by_case.items():
        if any(row['decision_type'] == 'correction_order' for row in events):
            correction_cases.add(case_id)
        decisions = [row for row in events if row['decision_type'] in TERMINAL]
        if not decisions:
            continue
        first = decisions[0]
        if len({row['decision_type'] for row in decisions if row['decision_date'] == first['decision_date']}) > 1:
            excluded['CONFLICTING_FIRST_DECISION'] += 1; continue
        excluded['REPEATED_CASE_DECISION'] += len(decisions) - 1
        if first['source_hash'] in seen_sources:
            excluded['DUPLICATE_DECISION_SOURCE'] += 1; continue
        seen_sources.add(first['source_hash'])
        terminal.append(first)
    approved = sum(row['decision_type'] == 'initial_plan_approval' for row in terminal)
    total = len(terminal)
    result.update(evidence_count=total, exclusions=dict(excluded),
                  distribution={'approved': approved, 'rejected': total - approved, 'correction': len(correction_cases)})
    # Same-office staff may only access their assigned cases. Do not return
    # another case's IDs, exact decision date, document hash, quote or URL.
    # Opaque composite digests support aggregate reproducibility only.
    result['evidence_refs'] = [{'evidence_digest': store.digest([VERSION, row['id'], row['source_hash'], row['document_hash']])}
                               for row in terminal[:100]]
    result['cohort_signature'] = store.digest([VERSION, target, cutoff.isoformat(), sorted((row['id'], row['source_hash'], row['document_hash']) for row in terminal)])
    if total < MINIMUM_SAMPLE_SIZE:
        result['reasons'].append({'code': 'INSUFFICIENT_MATCHED_OUTCOMES',
                                 'message': f'확인된 유사 사건 종결결과 {total}건으로, 관측 비율 표시 기준 {MINIMUM_SAMPLE_SIZE}건에 미달합니다.'})
    else:
        result['status'] = 'observed_rate_available'
        result['observed_rate'].update(percentage=round(approved / total * 100, 2), confidence_interval=wilson_interval(approved, total))
    result['reasons'].append({'code': 'INDIVIDUAL_PREDICTION_NOT_VALIDATED',
                             'message': '별도 사건으로 검증·보정한 예측모델이 없어 개인 승인 확률은 표시하지 않습니다.'})
    return result
