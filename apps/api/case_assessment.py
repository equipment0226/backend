"""An explainable snapshot of evidence coverage and executable legal checks.

This is deliberately not an approval model. A filled form field, a supported
fact and a satisfied quantitative requirement answer different questions. The
dashboard keeps their denominators separate and never converts their ratios
into a probability of a court decision. All work here is local and read-only;
GET requests neither run a model nor collect new law/case material.
"""
from __future__ import annotations

import copy
import re
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation

from . import court_forms, evidence_mapping, extraction_readiness, legal_calculator

VERSION = 'evidence-and-legal-assessment-v1'
INACTIVE = {'withdrawn', 'cancelled', 'superseded'}
LEGAL_FIELDS = court_forms.CALC_KEYS | {'assets_total', 'household_size'}
FORMAL_FIELDS = {'court_prefix', 'statement', 'start_year', 'start_month', 'start_day',
                 'end_year', 'end_month', 'end_day'}
ALIASES = {'income_manwon': 'monthly_income'}
RULES = (
    ('unsecured_debt_limit', '무담보채무 한도', 'statute-579', '원'),
    ('secured_debt_limit', '담보부채무 한도', 'statute-579', '원'),
    ('positive_capacity', '지속 가능한 월 변제 여력', 'statute-579', '원'),
    ('liquidation_floor', '청산가치 이상 변제', 'statute-614', '원'),
    ('priority_fully_paid', '우선권 채권 전액 변제', 'statute-611', '원'),
    ('minimum_repayment', '이의가 있는 경우의 최저변제액', 'statute-614', '원'),
    ('objector_liquidation_floor', '이의 채권자별 청산배당 보장', 'statute-614', '원'),
    ('repayment_period', '변제기간과 예외 사유', 'statute-611', '개월'),
    ('preapproval_costs', '인가 전 비용·수수료 납부', 'statute-614', None),
)


def _ratio(covered, total):
    return {'covered': covered, 'total': total,
            'percentage': round(covered * 100 / total, 1) if total else None}


def _present(value):
    # A source-backed zero/false is evidence. Missing information is not zero.
    return value is not None and value != '' and value != [] and value != {}


def _target(tab, anchor=None):
    return {'tab': tab, 'anchor': anchor}


def _active_requests(case):
    return [row for row in case.get('requests', []) if row.get('status') not in INACTIVE
            and not row.get('no_longer_required')]


def _document_scope(case):
    requests = _active_requests(case)
    return requests, [document for document in extraction_readiness.active_documents(case)
                      if document.get('automated_check', {}).get('coverage_status') != 'identity_conflict']


def _source_reference(case, document_id):
    document = next((row for row in case.get('documents', []) if row.get('id') == document_id), {})
    return {'id': document_id, 'kind': 'case_document', 'document_id': document_id,
            'title': document.get('title') or document.get('filename') or '제출서류',
            'url': None}


def _fresh_calculation(case, policy, complete_ids):
    """Reject stale, edited-in-place and old-policy results, even if marked approved."""
    candidates = list(reversed(case.get('legal_calculations', [])))
    reason = 'not_calculated'
    def unavailable(cause):
        return None, {'calculation': cause, 'calculation_id': None,
                      'ignored_calculations': len(candidates)}
    for stored in candidates:
        if stored.get('stale') or stored.get('input_revision') != case.get('input_revision'):
            reason = 'input_changed'; continue
        if stored.get('policy_hash') != policy['policy_hash']:
            return unavailable('law_changed')
        inputs = stored.get('inputs')
        if not isinstance(inputs, dict):
            return unavailable('unverifiable')
        try:
            as_of = date.fromisoformat(inputs.get('as_of', ''))
            if not date.fromisoformat(policy['effective_from']) <= as_of <= date.fromisoformat(policy['valid_through']):
                return unavailable('law_changed')
            fresh = legal_calculator.calculate_legal(case, inputs)
        except (ValueError, TypeError, KeyError, ArithmeticError):
            return unavailable('unverifiable')
        # Never trust the saved passed boolean or a ready label without
        # reproducing the numbers from exactly the same evidence and policy.
        if any(fresh.get(key) != stored.get(key) for key in
               ('input_hash', 'policy_hash', 'summary', 'checks', 'blockers')):
            return unavailable('evidence_changed')
        if any(row.get('id') not in complete_ids for row in fresh.get('evidence_snapshot', [])):
            return unavailable('evidence_incomplete')
        return fresh, {'calculation': 'current', 'calculation_id': stored.get('id'),
                       'ignored_calculations': len(candidates) - 1}
    return unavailable(reason)


def _field_category(key):
    if key in LEGAL_FIELDS or key.startswith('allocations.'):
        return 'legal'
    if key in FORMAL_FIELDS:
        return 'formal'
    return 'source'


def _legal_values(calculation):
    if not calculation:
        return {}
    summary, inputs = calculation.get('summary', {}), calculation.get('inputs', {})
    result = dict(summary)
    if all(type(summary.get(key)) is int for key in ('base_living_cost', 'additional_living_cost')):
        result['recognized_living_cost'] = summary['base_living_cost'] + summary['additional_living_cost']
    if type(summary.get('principal_total')) is int:
        result['allocation_principal_total'] = summary['principal_total']
    payments = [row.get('creditor_payment') for row in calculation.get('schedule', [])]
    if payments and all(type(value) is int for value in payments):
        result['allocation_monthly_total'] = payments[0] if len(set(payments)) == 1 else '회차별 산정표'
    if type(summary.get('total_principal_payment')) is int and summary.get('principal_total', 0) > 0:
        result['principal_repayment_percent'] = summary['total_principal_payment'] / summary['principal_total'] * 100
    if summary.get('net_monthly_income') is not None:
        result['household_size'] = inputs.get('recognized_household_size')
        result['assets_total'] = sum(row['liquidation_value'] for row in calculation.get('asset_calculations', []))
    return result


def _forms(case, packet, calculation, verified_packet=None):
    values, origins = packet['form_values'], packet['origins']
    verified_packet = verified_packet or packet
    legal_values = _legal_values(calculation)
    employment = values.get('employment_type')
    wage = employment == '급여소득자'
    templates, deferred, unique = [], [], {}
    for template in court_forms.TEMPLATES.values():
        if template['court_ids'] and case.get('court_id') not in template['court_ids']:
            continue
        if template['id'] in {'D5115', 'D5110'} and not wage:
            deferred.append({'id': template['id'], 'title': template['title'],
                'reason': '급여소득 여부 확인 후 적용' if not employment else '급여소득자용 서식으로 현재 소득 유형의 별도 서식 확인 필요'})
            continue
        seen, fields = set(), []
        definitions = list(template['fields'])
        creditor_count = len(packet['inputs'].get('creditors', []))
        # Printed blank rows after the first are optional only when there is
        # no additional creditor. For each observed entity, name and principal
        # are still required; overflow creditors are placed in the annex.
        if template['id'] == 'D5106':
            for index in range(4, creditor_count):
                for suffix, label in (('name', '명칭'), ('principal', '원금')):
                    definitions.append({'key': f'creditors.{index}.{suffix}',
                        'label': f'채권자{index + 1} {label}', 'required': True})
        for definition in definitions:
            key = definition['key']
            creditor_field = re.fullmatch(r'creditors\.(\d+)\.(name|principal)', key)
            applicable_creditor = creditor_field and int(creditor_field[1]) < creditor_count
            if (not definition['required'] and not applicable_creditor) or key in seen:
                continue
            seen.add(key)
            if key == 'employer' and not wage:
                continue
            if key == 'housing_deposit' and values.get('housing_type') == '무상거주':
                continue
            category, source_key = _field_category(key), ALIASES.get(key, key)
            refs, covered, extracted = [], False, False
            if category == 'source':
                origin = origins.get(source_key, {})
                extracted = (_present(values.get(source_key)) and bool(origin.get('fact_ids'))
                           and origin.get('type') in {'deterministic_evidence', 'human_review'})
                verified_origin = verified_packet['origins'].get(source_key, {})
                covered = (extracted and _present(verified_packet['form_values'].get(source_key))
                           and verified_packet['form_values'][source_key] == values[source_key]
                           and bool(verified_origin.get('fact_ids')))
                refs = [_source_reference(case, docid) for docid in origin.get('source_ids', [])]
            elif category == 'legal':
                covered = _present(legal_values.get(key))
                if covered:
                    refs = [{'id': calculation['id'], 'kind': 'legal_calculation', 'title': '현재 근거의 법률계산', 'url': None}]
            elif key == 'court_prefix':
                covered = bool(case.get('court_id') and case.get('court_name'))
            # Authored text, signatures and plan start/end dates are not OCR
            # extraction successes. They remain separate authoring/formality tasks.
            status = 'covered' if covered else 'extracted' if extracted else 'missing' if category == 'source' else 'pending'
            metric_key = source_key if category == 'source' else key
            field = {'key': key, 'metric_key': metric_key, 'label': definition['label'],
                'category': category, 'status': status, 'source_refs': refs,
                'extracted': extracted, 'verified': covered if category == 'source' else False,
                'target': _target('verify' if category == 'source' else 'calculate' if category == 'legal' else 'court_forms', template['id'])}
            fields.append(field)
            unique.setdefault((category, metric_key), field)
        if fields:
            templates.append({'id': template['id'], 'title': template['title'],
                **_ratio(sum(row['status'] == 'covered' for row in fields), len(fields)),
                'fields': fields, 'source_refs': [copy.deepcopy(template['source'])],
                'target': _target('court_forms', template['id'])})
    result = _ratio(sum(row['status'] == 'covered' for row in unique.values()), len(unique))
    for category in ('source', 'legal', 'formal'):
        group = [row for (kind, _), row in unique.items() if kind == category]
        result[category] = _ratio(sum(row['extracted'] if category == 'source' else row['status'] == 'covered' for row in group), len(group))
        if category == 'source':
            verified = sum(row['verified'] for row in group)
            result[category].update(verified=verified, verified_percentage=round(verified * 100 / len(group), 1) if group else None)
    result['method'] = '동일 항목은 서식·출력 위치가 여러 곳이어도 한 번만 집계합니다. 법률판단·작성 일정은 원문 추출률에서 제외합니다.'
    result['scope'] = '등록한 공식서식의 적용 가능한 필수 매핑 항목. 모든 체크칸·개별 보정명령을 포함하는 접수 적합성 지표는 아닙니다.'
    return templates, result, list(unique.values()), deferred


def _number(value):
    if type(value) is bool:
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _debt_inputs_complete(calculation):
    if not calculation:
        return False
    rows = calculation['inputs'].get('creditors')
    if not isinstance(rows, list) or not rows:
        return False
    return all(isinstance(row, dict) and row.get('kind') in {'secured', 'unsecured', 'priority'}
        and bool(row.get('name')) and type(row.get('principal')) is int and row['principal'] >= 0
        and type(row.get('interest')) is int and row['interest'] >= 0
        and bool(row.get('evidence_ids')) for row in rows) and not any(
            blocker.get('code') in {'EVIDENCE_REQUIRED', 'EVIDENCE_NOT_IN_CASE', 'EVIDENCE_NOT_VERIFIED'}
            and str(blocker.get('field', '')).startswith('creditors') for blocker in calculation.get('blockers', []))


def _rules(calculation, policy, packet=None, case=None, pending_changes=None):
    sources = {row['id']: row for row in policy['sources']}
    checks = calculation.get('checks', []) if calculation else []
    inputs = calculation.get('inputs', {}) if calculation else {}
    summary = calculation.get('summary', {}) if calculation else {}
    codes = {row['code'] for row in calculation.get('blockers', [])} if calculation else set()
    result = []
    for key, title, source, unit in RULES:
        row = {'id': key, 'title': title, 'status': 'unknown', 'actual': None,
               'threshold': policy.get(key), 'unit': unit,
               'summary': '현재 근거의 계산 입력을 확인한 뒤 대조합니다.',
               'source_refs': [copy.deepcopy(sources[source])] if source in sources else [],
               'target': _target('calculate', key)}
        matched = [check for check in checks if check.get('key') == key]
        if key.endswith('debt_limit') and not _debt_inputs_complete(calculation):
            matched = []
        if matched and all(type(check.get('passed')) is bool for check in matched):
            row.update(status='met' if all(check['passed'] for check in matched) else 'unmet',
                       actual=matched[0].get('actual'),
                       threshold=matched[0].get('limit', matched[0].get('threshold')))
            if len(matched) > 1:
                row.update(actual=None, threshold=None, evaluations=copy.deepcopy(matched))
        if key.endswith('debt_limit') and row['status'] == 'unknown' and packet:
            value_key = key.removesuffix('_limit')
            value = packet['form_values'].get(value_key)
            origin = packet['origins'].get(value_key, {})
            if type(value) is int and value >= 0 and origin.get('fact_ids'):
                unresolved_requests = [request for request in _active_requests(case or {})
                    if request.get('status') != 'fulfilled' and (request.get('catalog_id') == 'D38'
                    or re.search(r'부채|채무|대출|채권', request.get('title', '')))]
                row.update(actual=value, threshold=policy[key], basis='verified_source_arithmetic')
                if value > policy[key] or not unresolved_requests:
                    row.update(status='met' if value <= policy[key] else 'unmet',
                        summary='확인된 채권자의 원금·이자·담보 분류를 합산해 한도와 대조했습니다. 누락 채권 여부와 기준일 변동은 별도 확인합니다.')
        if key == 'priority_fully_paid' and _debt_inputs_complete(calculation) and not any(
                creditor['kind'] == 'priority' for creditor in inputs['creditors']):
            row.update(status='not_applicable', actual=0, threshold=None)
        if key == 'minimum_repayment' and inputs.get('objection') is False:
            row.update(status='not_applicable', actual=None, threshold=None)
        if key == 'objector_liquidation_floor':
            objectors = inputs.get('objecting_creditor_ids')
            if inputs.get('objection') is False or (inputs.get('objection') is True and objectors == []):
                row.update(status='not_applicable', actual=None, threshold=None)
            elif (not isinstance(objectors, list) or not objectors or
                  {check.get('creditor_id') for check in matched} != set(objectors)):
                row.update(status='unknown', actual=None, threshold=None)
        if key == 'repayment_period':
            months = inputs.get('months')
            row.update(actual=months, threshold=policy['normal_max_months'])
            if type(months) is int and 1 <= months <= policy['exception_max_months']:
                period_codes = {'EXTENDED_PERIOD_REASON_REQUIRED', 'SHORT_PERIOD_REASON_REQUIRED',
                    'COURT_ORDER_REQUIRED', 'SHORTENING_COURT_POLICY_UNVERIFIED', 'PRINCIPAL_NOT_FULLY_PAID',
                    'EXTENSION_NOT_NEEDED_FOR_LIQUIDATION'}
                if codes & period_codes:
                    row['status'] = 'unmet'
                elif months == policy['normal_max_months']:
                    row['status'] = 'met'
                elif summary.get('months') == months and not codes & {
                        'EVIDENCE_REQUIRED', 'EVIDENCE_NOT_VERIFIED', 'EVIDENCE_NOT_IN_CASE', 'LEGAL_INPUT_REASON_REQUIRED'}:
                    row['status'] = 'met'
                    row['summary'] = '계산에 기록된 기간 예외 사유와 근거가 있습니다. 예외 인정 자체는 최종 법률검토 대상입니다.'
            elif type(months) is int and months > policy['exception_max_months']:
                row.update(status='unmet', threshold=policy['exception_max_months'])
        if key == 'preapproval_costs' and type(inputs.get('preapproval_costs_paid')) is bool:
            row.update(status='met' if inputs['preapproval_costs_paid'] else 'unmet',
                       actual=inputs['preapproval_costs_paid'], threshold=True)
        if row['summary'].startswith('현재 근거'):
            row['summary'] = {'met': '현재 계산에 반영된 입력으로 이 정량 요건을 충족합니다.',
                'unmet': '현재 입력으로 기준에 미달하거나 별도 근거가 필요합니다.',
                'unknown': '판단에 필요한 입력·증빙 또는 현재 계산이 아직 갖춰지지 않았습니다.',
                'not_applicable': '현재 확인된 입력에서는 이 조건부 요건이 적용되지 않습니다.'}[row['status']]
        watched_sources = {'statute-579': {'LW579'}, 'statute-611': {'LW611'},
                           'statute-614': {'LW614'}}.get(source, set())
        affected = [change for change in pending_changes or [] if not change.get('affected_source_ids')
                    or watched_sources.intersection(change.get('affected_source_ids', []))
                    or change.get('source_id') in watched_sources]
        if affected:
            row.update(status='unknown', summary='관련 법령 변경사항의 적용 검토가 끝나지 않아 이 요건 판단을 보류합니다.',
                       policy_review_required=True)
        result.append(row)
    counts = Counter(row['status'] for row in result)
    applicable = len(result) - counts['not_applicable']
    metrics = {key: counts[key] for key in ('met', 'unmet', 'unknown', 'not_applicable')}
    metrics.update(total=applicable, percentage=round(counts['met'] * 100 / applicable, 1) if applicable else None,
        evaluated_percentage=round(counts['met'] * 100 / (counts['met'] + counts['unmet']), 1)
            if counts['met'] + counts['unmet'] else None,
        scope='코드로 대조 가능한 정량·절차 요건. 미확인 항목은 충족으로 세지 않고, 비적용 조건은 분모에서 제외합니다.')
    return result, metrics


def _actions(case, packet, fields, rules, document_metrics, calculation):
    actions = []

    def add(key, category, priority, title, summary, tab, refs=None, **extra):
        actions.append({'id': key, 'category': category, 'priority': priority,
            'title': title, 'summary': summary, 'source_refs': refs or [],
            'target': _target(tab, key), **extra})

    if case.get('intake', {}).get('status') != 'completed' and not case.get('documents'):
        add('detailed_consultation', 'documents', 'high', '세부 상담으로 요청서류를 정합니다',
            '소득 형태·채무·재산·거주 상황을 확인하면 해당 사건에 필요한 서류가 정리됩니다. 지금은 미제출 상태를 법률상 위험으로 판단하지 않습니다.', 'consultation')
        return actions
    if document_metrics['requests_total'] > document_metrics['requests_complete']:
        missing = document_metrics['requests_total'] - document_metrics['requests_complete']
        add('requested_documents', 'documents', 'high', f'요청서류 {missing}건의 제출·검토를 마쳐 주세요',
            '요청한 기관·계좌·기간과 제출본을 함께 확인합니다. 추출이 끝나지 않은 서류는 다음 분석의 확정 근거로 사용하지 않습니다.', 'verify')
    if document_metrics['received'] > document_metrics['verified'] and not document_metrics['requests_total']:
        add('received_review', 'documents', 'high', '받은 서류와 추출값을 함께 확인합니다',
            '읽기를 마친 항목은 추출률에 반영됩니다. 원문과 추출값을 한 번에 검토 완료하면 법률판단의 확인된 근거로 연결됩니다.', 'verify')
    missing_fields = [row for row in fields if row['category'] == 'source' and row['status'] == 'missing']
    groups = (
        ('identity', '신청인·주소·연락처', {'client_name', 'resident_id', 'registered_address', 'address', 'phone'}, '주민등록 자료의 현재 주소·등록상 주소와 본인 연락처를 구분해 확인합니다.'),
        ('income', '소득과 근무 내역', {'employer', 'employer_address', 'employment_period', 'monthly_income', 'income_manwon'}, '급여 산정기간·실수령액과 재직자료를 연결합니다. 연간 소득과 특정 월 급여가 섞이지 않았는지 확인합니다.'),
        ('assets', '재산·보험·거주 내역', {'bank_balance', 'insurance_surrender', 'housing_deposit'}, '계좌별 잔액, 보험 해약환급금과 거주 형태에 맞는 자료를 확인합니다. 자료가 없는 항목을 0원으로 처리하지 않습니다.'),
        ('debts', '채권자별 채무 내역', {'total_debt', 'principal_total', 'interest_total', 'unsecured_debt', 'creditors.0.name', 'creditors.0.principal'}, '모든 채권자의 기준일 원금·이자와 담보 여부를 대조합니다. 월 상환금액을 채무잔액으로 잘못 읽은 부분도 함께 확인합니다.'),
        ('prior', '과거 회생·파산 절차', {'prior_proceedings'}, '당사자 진술과 과거 결정문·사건조회 자료를 함께 확인합니다. 과거 절차가 없다는 진술만으로 법적 결격 여부를 확정하지 않습니다.'),
    )
    for key, title, keys, explanation in groups:
        missing = [row for row in missing_fields if row['key'] in keys]
        if missing:
            add('fields_' + key, 'documents', 'medium', title + ' 보완', explanation, 'verify',
                fields=[{'key': row['key'], 'label': row['label']} for row in missing])
    unmet = {row['id']: row for row in rules if row['status'] == 'unmet'}
    if 'liquidation_floor' in unmet:
        rule = unmet['liquidation_floor']
        actual, threshold = _number(rule['actual']), _number(rule['threshold'])
        gap = max(Decimal(0), threshold - actual) if actual is not None and threshold is not None else None
        text = f'현가 기준 부족액은 {int(gap):,}원입니다. ' if gap is not None else ''
        add('liquidation_gap', 'legal', 'high', '청산가치 부족분을 재검토합니다',
            text + '재산 평가일·소유지분·담보와 법률상 공제 증빙을 먼저 대조하고 적법한 추가변제·기간 조정 가능성을 재계산합니다. 매각대금도 재산이므로 처분만으로 부족액이 없어지지는 않습니다.', 'calculate', rule['source_refs'])
    debt_rules = [unmet[key] for key in ('unsecured_debt_limit', 'secured_debt_limit') if key in unmet]
    if debt_rules:
        add('debt_limit', 'legal', 'high', '채무한도와 절차 선택을 검토합니다',
            '원금·이자·담보구분과 중복채권을 최신 부채증명서로 확인합니다. 실제 한도 초과가 유지되면 다른 회생·파산 절차도 검토합니다. 재산 매각은 채무의 소멸과 별개이므로 거래대금·실제 변제내역을 확인한 뒤 다시 계산해야 합니다.', 'issues', debt_rules[0]['source_refs'])
    if 'positive_capacity' in unmet:
        add('capacity', 'legal', 'high', '지속 가능한 변제 여력을 확인합니다',
            '일시적인 급여 감소인지, 계속 발생하는 지출인지 구분합니다. 최신 급여·재직자료와 실제 부양·주거·의료 지출을 대조한 뒤 수행 가능한 계획인지 재계산합니다.', 'calculate', unmet['positive_capacity']['source_refs'])
    for key in ('minimum_repayment', 'objector_liquidation_floor', 'priority_fully_paid', 'repayment_period', 'preapproval_costs'):
        if key in unmet:
            rule = unmet[key]
            descriptions = {
                'minimum_repayment': '이의 여부와 채무총액을 확인한 뒤 최저변제액 부족분을 재계산합니다. 총 납입액과 현가를 혼동하지 않도록 확인합니다.',
                'objector_liquidation_floor': '이의 채권자별 파산 예상배당과 현가 변제액을 대조합니다. 총액 요건을 충족해도 개별 채권자 보장액이 부족할 수 있습니다.',
                'priority_fully_paid': '우선권 채권의 분류·잔액과 전액 변제 순서를 확인합니다. 일반채권 안분과 분리한 계획을 검토합니다.',
                'repayment_period': '기본기간을 벗어난 경우 청산가치 충족·원금 전액 변제·개별 법원 결정 등 선택한 예외 사유와 근거를 확인합니다.',
                'preapproval_costs': '미납 비용과 납부 안내를 확인하고 영수증을 보완합니다. 서류를 제출했다는 사실만으로 납부 완료로 처리하지 않습니다.'}
            add(key, 'legal', 'high', rule['title'] + ' 보완', descriptions[key], 'calculate', rule['source_refs'])
    if any(row['status'] == 'unknown' for row in rules):
        add('legal_inputs', 'legal', 'medium', '법률판단에 필요한 입력을 확인합니다',
            '법률상 부양인원, 인정 생계비, 재산 공제, 변제기간과 비용·이의 여부를 사건별로 확인하면 정량 요건을 대조할 수 있습니다. 미확인 입력은 반려 사유로 세지 않습니다.', 'calculate')
    values = packet['form_values']
    if _present(values.get('housing_cost')) or _present(values.get('living_expenses')):
        sources = [row for row in legal_calculator.policy()['sources'] if row['id'] in
                   ({'seoul-living-2026', 'statute-579'} if case.get('court_id') == 'CT01' else {'statute-579'})]
        add('additional_living_evidence', 'supporting', 'low', '실제 필요한 생활지출의 근거를 정리합니다',
            '임대차계약·월세 이체, 계속 지출되는 치료비, 실제 부양자료가 있으면 추가생계비 판단에 참고할 수 있습니다. 제출만으로 인정되지는 않으며 관할 기준과 중복 공제를 확인합니다.', 'issues', sources)
    if values.get('retirement_expected') is not None or any(row.get('key', '').startswith('retirement_') for row in packet['facts']):
        add('retirement_evidence', 'supporting', 'medium', '퇴직급여 종류와 공제 근거를 구분합니다',
            '일반 퇴직금과 DB·DC·IRP를 구분하고 기준일 예상액·제도 확인자료를 연결합니다. 원문 총액을 곧바로 처분가능액이나 전액 청산가치로 보지 않습니다.', 'issues')
    return actions


def build(case):
    """Return a reproducible dashboard view without modifying the supplied case."""
    policy = legal_calculator.policy()
    from . import legal_watch
    law_status = legal_watch.case_policy_status(case)
    requests, documents = _document_scope(case)
    states = {doc['id']: extraction_readiness.document_state(case, doc) for doc in documents}
    extracted_ids = {doc['id'] for doc in documents if states[doc['id']]['can_review']}
    complete_ids = {doc['id'] for doc in documents if doc.get('status') == 'verified' and states[doc['id']]['can_review']}
    working = dict(case, documents=[doc for doc in documents if doc['id'] in complete_ids])
    packet = evidence_mapping.build(working)
    # The same parser can show extraction progress before human review. This
    # private projection is never persisted or passed to legal calculation;
    # changing the temporary status only selects pages for the pure mapper.
    extracted_working = dict(case, documents=[dict(doc, status='verified') for doc in documents if doc['id'] in extracted_ids])
    extracted_packet = evidence_mapping.build(extracted_working) if extracted_ids != complete_ids else packet
    calculation, freshness = _fresh_calculation(case, policy, complete_ids)
    forms, field_metrics, unique_fields, deferred = _forms(case, extracted_packet, calculation, packet)
    rule_rows, rule_metrics = _rules(calculation, policy, packet, case, law_status['pending_changes'])
    if case.get('case_type', 'personal_rehabilitation') != 'personal_rehabilitation':
        forms, unique_fields, deferred = [], [], []
        field_metrics = {**_ratio(0, 0), **{kind: _ratio(0, 0) for kind in ('source', 'legal', 'formal')}}
        field_metrics['source'].update(verified=0, verified_percentage=None)
        for row in rule_rows:
            row.update(status='not_applicable', actual=None, threshold=None,
                       summary='현재 사건 유형에 적용하는 개인회생 요건이 아닙니다.')
        rule_metrics = {'met': 0, 'unmet': 0, 'unknown': 0, 'not_applicable': len(rule_rows),
                        'total': 0, 'percentage': None, 'evaluated_percentage': None}
    requests_complete = 0
    for request in requests:
        ids = [doc['id'] for doc in documents if doc.get('request_id') == request.get('id')
               or doc['id'] in request.get('document_ids', [])]
        if request.get('status') == 'fulfilled' and ids and set(ids) <= complete_ids:
            requests_complete += 1
    document_metrics = {**_ratio(len(complete_ids), len(documents)), 'verified': len(complete_ids),
        'received': len(documents), 'requests_total': len(requests), 'requests_complete': requests_complete,
        'extraction_pending': sum(not states[doc['id']]['can_review'] for doc in documents),
        'request_percentage': round(requests_complete * 100 / len(requests), 1) if requests else None}
    actions = _actions(case, packet, unique_fields, rule_rows, document_metrics, calculation)
    if law_status['pending_changes']:
        actions.insert(0, {'id': 'law_changes', 'category': 'legal', 'priority': 'high',
            'title': '변경된 법령의 사건 적용 여부를 확인합니다',
            'summary': '저장된 법령 변경사항과 시행일·경과규정을 대조한 뒤 영향을 받는 요건을 다시 평가합니다. 검토 전 결과를 현재 충족 상태로 표시하지 않습니다.',
            'source_refs': [{'id': row.get('source_id'), 'title': row.get('title') or '법령 변경자료', 'url': row.get('url')} for row in law_status['pending_changes']],
            'target': _target('issues', 'law_changes')})
    pending = rule_metrics['unknown']
    confirmed = rule_metrics['unmet']
    if not documents:
        title, description = '상담에서 서류 준비로 이어집니다', '아직 검토를 마친 제출자료가 없습니다. 요청서류와 주요 확인 항목을 먼저 정리합니다.'
    elif confirmed:
        title, description = f'정량 요건 {confirmed}개를 보완해야 합니다', '확인된 미충족 요건과 자료가 부족해 판단을 보류한 항목을 나누어 표시합니다.'
    elif pending:
        title, description = '확인된 자료로 다음 검토를 준비합니다', '추출된 사실을 기준으로 법률상 인정액과 조건을 확인하면 계산·문서 작성으로 이어집니다.'
    else:
        title, description = '현재 계산의 정량 요건을 대조했습니다', '충족한 정량 요건과 최종 법률검토·서류 작성 상태는 별도로 확인합니다.'
    from . import strategy_context
    features = strategy_context.graph_features(strategy_context.build(working, calculation or {}, packet), calculation or {})
    return {'version': VERSION, 'input_revision': case.get('input_revision'),
        'summary': {'title': title, 'description': description},
        'metrics': {'required_fields': field_metrics, 'documents': document_metrics, 'rules': rule_metrics,
            'risks': {'confirmed': confirmed, 'pending': pending,
                      'document_gaps': field_metrics['source']['total'] - field_metrics['source']['covered'],
                      'scope': '확인된 미충족 정량 요건만 위험요소로 집계합니다. 미확인 자료를 기각 사유로 보지 않습니다.'}},
        'forms': forms, 'deferred_forms': deferred, 'rules': rule_rows, 'actions': actions,
        'prediction': {'status': 'not_validated', 'percentage': None,
            'reason': '추출률과 요건 충족률은 인가 확률이 아닙니다. 실제 종결결과를 독립 사건으로 검증·보정한 예측모델이 없어 개인 인가 확률은 산출하지 않습니다.'},
        'freshness': {**freshness, 'evidence_source_signature': packet['source_signature'],
                      'policy_version': policy['version'], 'policy_review_required': bool(law_status['pending_changes'])},
        'reasoning_features': features,
        'method': {'deduplicate_fields': True, 'unknown_is_failure': False,
                   'extraction_uses_complete_active_documents': True,
                   'legal_rules_use_current_verified_complete_documents': True,
                   'lawful_disposal_does_not_erase_asset_value': True,
                   'external_processing': False, 'model_called': False}}
