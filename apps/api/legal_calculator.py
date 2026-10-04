"""Reproducible KR personal-rehabilitation calculations; no model calls.

This module computes a reviewed-input plan, not a court's decision. Monetary
inputs are integer KRW. Unknown values are errors, never zero defaults.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import date
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP, localcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICY_FILE = ROOT / 'data/legal_calculation_rules.json'
CODE_VERSION = 'kr-rehab-calculator-1.0'


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def policy(as_of=None):
    value = json.loads(POLICY_FILE.read_text(encoding='utf-8'))
    from . import legal_watch
    overlay = legal_watch.active_overlays(as_of=as_of)
    for key in ('unsecured_debt_limit', 'secured_debt_limit', 'normal_max_months', 'exception_max_months'):
        candidate = overlay.get('calculator', {}).get(key)
        if type(candidate) is int and candidate > 0:
            value[key] = candidate
    if overlay.get('calculator'):
        value['legal_update'] = {key: overlay.get(key) for key in ('version', 'effective_date', 'sources')}
    value['policy_hash'] = _digest(value)
    return value


def example_payload():
    """Synthetic values, explicitly unconfirmed; bind IDs to uploaded documents."""
    return {
        'as_of': '2026-10-03', 'policy_id': 'kr-rehab-2026-v1',
        'income': {'kind': 'wage', 'basis': 'net', 'monthly_amount': 2800000,
                   'taxes_and_social_insurance': 0, 'business_expenses': 0,
                   'period': '2026-07~2026-09', 'evidence_ids': ['fixture-payroll']},
        'recognized_household_size': 1, 'additional_living_cost': 0,
        'living_cost_mode': 'seoul_median_60', 'months': 36, 'prepaid_months': 0,
        'monthly_trustee_fee': 0, 'preapproval_costs_paid': True,
        'objection': False,
        'assets': [
            {'id': 'deposit', 'label': '임차보증금', 'owned_value': 10000000,
             'secured_deduction': 0, 'exempt_deduction': 0, 'disposal_cost': 0,
             'evidence_ids': ['fixture-lease']},
            {'id': 'bank', 'label': '예금', 'owned_value': 1200000,
             'secured_deduction': 0, 'exempt_deduction': 0, 'disposal_cost': 0,
             'evidence_ids': ['fixture-bank']},
            {'id': 'insurance', 'label': '보험해약환급금', 'owned_value': 800000,
             'secured_deduction': 0, 'exempt_deduction': 0, 'disposal_cost': 0,
             'evidence_ids': ['fixture-insurance']}],
        'creditors': [
            {'id': 'creditor-a', 'name': '예시은행A', 'kind': 'unsecured',
             'principal': 35000000, 'interest': 1400000, 'evidence_ids': ['fixture-debt-a']},
            {'id': 'creditor-b', 'name': '예시카드B', 'kind': 'unsecured',
             'principal': 25000000, 'interest': 1000000, 'evidence_ids': ['fixture-debt-b']},
            {'id': 'creditor-c', 'name': '예시저축C', 'kind': 'unsecured',
             'principal': 15000000, 'interest': 600000, 'evidence_ids': ['fixture-debt-c']}],
        'decisions': {
            'household_reason': '단독 가구이며 실제 부양가족 없음. 관계자료 대조 필요.',
            'living_cost_reason': '서울 2026 기본생계비만 적용. 추가주거비 인정 여부는 검토.',
            'asset_reason': '소유지분 반영 평가액. 면제재산 공제는 아직 청구하지 않은 안.',
            'debt_reason': '부채증명서의 기준일 원금·이자. 가족 변제·양도·대위 없음 확인 필요.',
            'fee_reason': '합성 사례의 월 회생위원 보수 0원 가정. 실제 보수명령 확인 필요.',
            'objection_reason': '현재 이의 여부 확인 필요.',
            'period_reason': '일반 36개월 안. 인가 전 적립회차 0회 가정.',
            'discount_reason': '서울 실무편람 연 5% 월복리 현가법 적용.'}}


def schema():
    return {'version': CODE_VERSION, 'policy': policy(), 'example': example_payload(),
            'required': ['as_of', 'policy_id', 'income', 'recognized_household_size',
                         'additional_living_cost', 'living_cost_mode', 'months',
                         'prepaid_months', 'monthly_trustee_fee', 'preapproval_costs_paid',
                         'objection', 'assets', 'creditors', 'decisions'],
            'notes': ['금액은 정수 원. 미상은 null이며 0원과 다릅니다.',
                      '세후 소득에는 공제액을 다시 빼지 않습니다.',
                      'assets=[]는 무재산 확인 진술을 뜻하므로 평가 사유와 증빙이 필요합니다.',
                      '서울 외 관할은 living_cost_mode=case_specific와 base_living_cost, '
                      'annual_discount_rate, discount_reason을 별도 입력합니다.',
                      '사건 원문 증빙이 모두 verified여야 검토 가능한 계산으로 승격됩니다.',
                      'ready_for_review는 확정이 아닙니다. 별도 변호사 승인으로 해시를 고정합니다.']}


def _allocate(total, claims):
    """Exact Hamilton allocation. Stable creditor ID resolves equal remainders.

    This is an explicit engine rounding policy, not an assertion that every
    court prescribes Hamilton rounding. A reviewer approves the exported plan.
    """
    denominator = sum(amount for _, amount in claims)
    if not denominator or not total:
        return {key: 0 for key, _ in claims}
    total = min(total, denominator)
    allocation = {key: total * amount // denominator for key, amount in claims}
    residual = total - sum(allocation.values())
    order = sorted(claims, key=lambda row: (-(total * row[1] % denominator), row[0]))
    for key, _ in order[:residual]:
        allocation[key] += 1
    return allocation


def minimum_repayment(claim_total):
    raw = Decimal(claim_total) * (Decimal('0.05') if claim_total < 50000000 else Decimal('0.03'))
    if claim_total >= 50000000:
        raw += 1000000
    return min(Decimal(30000000), raw)


def present_value(payments, prepaid_months, annual_rate=Decimal('0.05')):
    with localcontext() as ctx:
        ctx.prec = 40
        monthly = Decimal(annual_rate) / 12
        return sum((Decimal(amount) / ((1 + monthly) ** max(0, index + 1 - prepaid_months))
                    for index, amount in enumerate(payments)), Decimal(0))


def calculate_legal(case, payload, user=None):
    """Pure calculation. Does not mutate case and cannot grant legal approval.

    `user` is intentionally not used as an authorization mechanism. The API
    must authenticate the separate review operation and pin input/policy hashes.
    """
    p = copy.deepcopy(payload) if isinstance(payload, dict) else {}
    try:
        policy_date = date.fromisoformat(p.get('as_of', ''))
    except (ValueError, TypeError):
        policy_date = None
    rules = policy(as_of=policy_date)
    # Evidence contents are part of the snapshot, not merely the case revision.
    # This also detects accidental in-place edits that bypass invalidation.
    evidence_ids = set()
    def collect_evidence(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.endswith('evidence_ids') and isinstance(child, list):
                    evidence_ids.update(i for i in child if isinstance(i, str))
                else:
                    collect_evidence(child)
        elif isinstance(value, list):
            for child in value:
                collect_evidence(child)
    collect_evidence(p)
    evidence_snapshot = [
        {'id': d.get('id'), 'sha256': d.get('sha256'), 'status': d.get('status'),
         'text_hash': _digest({'text': d.get('text'), 'pages': d.get('pages')})}
        for d in case.get('documents', []) if d.get('id') in evidence_ids]
    evidence_snapshot.sort(key=lambda d: d['id'])
    result = {'id': 'legal-' + _digest({'case_id': case.get('id'), 'payload': p,
                                      'revision': case.get('input_revision'),
                                      'policy': rules['policy_hash']})[:16],
              'status': 'blocked', 'code_version': CODE_VERSION,
              'input_revision': case.get('input_revision'), 'inputs': p,
              'input_hash': _digest({'case_id': case.get('id'), 'court_id': case.get('court_id'),
                                     'input_revision': case.get('input_revision'), 'inputs': p,
                                     'evidence_snapshot': evidence_snapshot}),
              'evidence_snapshot': evidence_snapshot,
              'policy_version': rules['version'], 'policy_hash': rules['policy_hash'],
              'summary': {}, 'creditor_allocations': [], 'schedule': [], 'checks': [],
              'blockers': [], 'warnings': [], 'formulas': [], 'source_refs': rules['sources'],
              'approval': None, 'stale': False,
              'scope': '금액 계산 및 정량 요건 대조. 법원의 인가·면책 판단과 구별됩니다.'}
    blockers = result['blockers']
    numeric_errors = []

    def block(code, message, field=None, numeric=False):
        item = {'code': code, 'message': message}
        if field:
            item['field'] = field
        if item not in blockers:
            blockers.append(item)
        if numeric:
            numeric_errors.append(code)

    def money(obj, key, prefix='', maximum=10**13):
        value = obj.get(key)
        if type(value) is not int or not 0 <= value <= maximum:
            block('INVALID_MONEY', '확인된 0 이상의 정수 원 금액이 필요합니다.', prefix + key, True)
            return 0
        return value

    def count(key, low, high):
        value = p.get(key)
        if type(value) is not int or not low <= value <= high:
            block('INVALID_INTEGER', f'{low}~{high} 정수가 필요합니다.', key, True)
            return low
        return value

    if p.get('policy_id') != rules['id']:
        block('POLICY_MISMATCH', '현재 계산 기준 버전을 선택하세요.', 'policy_id', True)
    try:
        day = date.fromisoformat(p.get('as_of', ''))
        if not date.fromisoformat(rules['effective_from']) <= day <= date.fromisoformat(rules['valid_through']):
            block('POLICY_DATE_OUTSIDE_SCOPE', '고정된 기준의 적용 기간 밖입니다.', 'as_of', True)
    except (ValueError, TypeError):
        block('INVALID_DATE', '기준일 YYYY-MM-DD가 필요합니다.', 'as_of', True)
    if case.get('case_type', 'personal_rehabilitation') != 'personal_rehabilitation':
        block('PROCEDURE_NOT_SUPPORTED', '개인회생 사건의 계산 엔진입니다.', numeric=True)
    if any(issue.get('status') not in ('resolved', 'closed', 'decided') for issue in case.get('issues', [])):
        block('UNRESOLVED_LEGAL_ISSUE', '완납·대위 등 미결 쟁점의 판단을 먼저 기록하세요.')
    income = p.get('income') if isinstance(p.get('income'), dict) else {}
    if income.get('kind') != 'wage':
        block('INCOME_TYPE_NOT_SUPPORTED', '이 버전은 급여소득의 고정 월평균 안을 지원합니다. 영업·변동소득은 별도 산정표가 필요합니다.', 'income.kind', True)
    gross_or_net = money(income, 'monthly_amount', 'income.')
    deductions = money(income, 'taxes_and_social_insurance', 'income.')
    business_expenses = money(income, 'business_expenses', 'income.')
    if business_expenses:
        block('WAGE_BUSINESS_EXPENSE', '급여소득에서 영업비용을 공제할 수 없습니다.', 'income.business_expenses', True)
    if income.get('basis') not in ('gross', 'net'):
        block('INCOME_BASIS_REQUIRED', '세전/세후 기준이 필요합니다.', 'income.basis', True)
    if income.get('basis') == 'net' and deductions:
        block('DOUBLE_DEDUCTION', '세후소득에서 이미 공제한 세금·보험을 다시 공제하지 마세요.', 'income.taxes_and_social_insurance', True)
    if income.get('basis') == 'gross' and deductions > gross_or_net:
        block('DEDUCTIONS_EXCEED_INCOME', '공제액이 세전소득보다 큽니다.', 'income.taxes_and_social_insurance', True)
    net = gross_or_net - deductions if income.get('basis') == 'gross' else gross_or_net
    if not isinstance(income.get('period'), str) or not income['period'].strip():
        block('INCOME_PERIOD_REQUIRED', '월평균 산정기간을 입력하세요.', 'income.period')
    household = count('recognized_household_size', 1, 20)
    months = count('months', 1, 60)
    prepaid = count('prepaid_months', 0, 60)
    if prepaid > months:
        block('PREPAID_EXCEEDS_TERM', '적립회차가 전체 회차를 초과합니다.', 'prepaid_months', True)
    extra = money(p, 'additional_living_cost')
    fee = money(p, 'monthly_trustee_fee')
    for key in ('objection', 'preapproval_costs_paid'):
        if type(p.get(key)) is not bool:
            block('BOOLEAN_REQUIRED', '확인된 예/아니오 입력이 필요합니다.', key, True)
    if p.get('preapproval_costs_paid') is False:
        block('PREAPPROVAL_COSTS_UNPAID', '인가 전 비용·수수료 납부가 미완료입니다.')
    decisions = p.get('decisions') if isinstance(p.get('decisions'), dict) else {}
    for key in ('household_reason', 'living_cost_reason', 'asset_reason', 'debt_reason',
                'fee_reason', 'objection_reason', 'period_reason', 'discount_reason'):
        if not isinstance(decisions.get(key), str) or not decisions[key].strip():
            block('LEGAL_INPUT_REASON_REQUIRED', '사건별 판단 근거가 필요합니다.', 'decisions.' + key)
    median = rules['median_income_2026'].get(str(household))
    if median is None:
        median = rules['median_income_2026']['7'] + (household - 7) * rules['median_increment_above_7']
    benchmark = int((Decimal(median) * Decimal('0.60')).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if p.get('living_cost_mode') == 'seoul_median_60':
        living = benchmark
        rate = Decimal('0.05')
        if case.get('court_id') != 'CT01':
            block('COURT_POLICY_MISMATCH', '서울 기준을 다른 관할에 자동 적용하지 않습니다. 사건별 인정 생계비와 할인율을 입력하세요.', 'living_cost_mode', True)
    elif p.get('living_cost_mode') == 'case_specific':
        living = money(p, 'base_living_cost')
        try:
            rate = Decimal(str(p.get('annual_discount_rate')))
            if not rate.is_finite() or not Decimal(0) <= rate <= Decimal('0.2'):
                raise ValueError()
        except Exception:
            block('DISCOUNT_RATE_REQUIRED', '검토할 연 할인율(0~0.2)이 필요합니다.', 'annual_discount_rate', True)
            rate = Decimal(0)
        result['warnings'].append('다른 관할/개별 결정의 생계비·현가율은 사건별 입력값입니다. 해당 관할 원문과 변호사 확인을 요구합니다.')
    else:
        living, rate = 0, Decimal(0)
        block('LIVING_COST_POLICY_REQUIRED', '인정 생계비 산정 방법을 선택하세요.', 'living_cost_mode', True)

    docs = {d.get('id'): d for d in case.get('documents', [])}
    evidence_rows = [('income', income)]
    if p.get('period_exception') == 'court_order':
        evidence_rows.append(('period_order', {'evidence_ids': p.get('period_order_evidence_ids')}))
    creditors = p.get('creditors')
    assets = p.get('assets')
    if not isinstance(creditors, list) or not creditors or len(creditors) > 500:
        block('CREDITORS_REQUIRED', '1~500개 채권자별 원금·이자·분류가 필요합니다.', 'creditors', True)
        creditors = []
    if not isinstance(assets, list) or len(assets) > 500:
        block('ASSETS_REQUIRED', '재산 목록을 입력하세요. 무재산 확인 시에만 빈 목록을 사용합니다.', 'assets', True)
        assets = []
    if not assets:
        evidence_rows.append(('asset_evidence_ids', {'evidence_ids': p.get('asset_evidence_ids')}))
    principal_total = interest_total = secured_total = priority_total = 0
    ids = set()
    clean_creditors = []
    for index, creditor in enumerate(creditors):
        prefix = f'creditors[{index}].'
        if not isinstance(creditor, dict):
            block('INVALID_CREDITOR', '채권자 구조를 확인하세요.', prefix, True)
            continue
        cid = creditor.get('id')
        if not isinstance(cid, str) or not cid.strip() or cid in ids:
            block('CREDITOR_ID_INVALID', '채권자 ID는 공백 없이 고유해야 합니다.', prefix + 'id', True)
        ids.add(cid if isinstance(cid, str) else str(index))
        principal = money(creditor, 'principal', prefix)
        interest = money(creditor, 'interest', prefix)
        if not creditor.get('name') or principal + interest <= 0:
            block('CREDITOR_IDENTITY_OR_BALANCE_REQUIRED', '채권자명과 양수 잔액이 필요합니다.', prefix, True)
        kind = creditor.get('kind')
        if kind not in ('unsecured', 'priority', 'secured'):
            block('CREDITOR_KIND_REQUIRED', '일반/우선/담보 채권 구분이 필요합니다.', prefix + 'kind', True)
        if kind == 'secured':
            secured_total += principal + interest
            block('SECURED_PLAN_REQUIRED', '담보권별 예정부족액·유보액·별도 변제는 별도 심사가 필요하여 일반채권 배분표에서 제외하고 확정을 차단합니다.', prefix, True)
        else:
            principal_total += principal
            interest_total += interest
            if kind == 'priority':
                priority_total += principal + interest
        clean_creditors.append({**creditor, 'principal': principal, 'interest': interest})
        evidence_rows.append((prefix, creditor))
    asset_rows = []
    asset_ids = set()
    for index, asset in enumerate(assets):
        prefix = f'assets[{index}].'
        if not isinstance(asset, dict):
            block('INVALID_ASSET', '재산 항목 구조를 확인하세요.', prefix, True)
            continue
        aid = asset.get('id')
        if not isinstance(aid, str) or not aid.strip() or aid in asset_ids:
            block('ASSET_ID_INVALID', '중복 없는 재산 ID가 필요합니다.', prefix + 'id', True)
        asset_ids.add(aid if isinstance(aid, str) else str(index))
        owned = money(asset, 'owned_value', prefix)
        lien = money(asset, 'secured_deduction', prefix)
        exempt = money(asset, 'exempt_deduction', prefix)
        disposal = money(asset, 'disposal_cost', prefix)
        if lien + exempt + disposal > owned:
            block('ASSET_DEDUCTIONS_EXCEED_VALUE', '동일 재산의 담보·면제·환가비용 공제가 소유지분 평가액을 초과합니다.', prefix, True)
        asset_rows.append({'id': aid, 'label': asset.get('label'), 'owned_value': owned,
                           'deductions': lien + exempt + disposal,
                           'liquidation_value': max(0, owned - lien - exempt - disposal)})
        evidence_rows.append((prefix, asset))
    for prefix, row in evidence_rows:
        evidence = row.get('evidence_ids')
        if not isinstance(evidence, list) or not evidence:
            block('EVIDENCE_REQUIRED', '사건 내 근거 서류를 연결하세요.', prefix + '.evidence_ids')
            continue
        for docid in evidence:
            document = docs.get(docid) if isinstance(docid, str) else None
            if document is None:
                block('EVIDENCE_NOT_IN_CASE', '근거 서류가 이 사건에 없습니다.', prefix + '.evidence_ids')
            elif document.get('status') != 'verified':
                block('EVIDENCE_NOT_VERIFIED', '원문과 추출값 대조가 완료되지 않은 근거입니다.', prefix + '.evidence_ids')
    unsecured_total = principal_total + interest_total
    for fact in case.get('facts', []):
        if fact.get('status') != 'confirmed':
            continue
        expected = {'monthly_income': net, 'total_debt': unsecured_total + secured_total}.get(fact.get('key'))
        if expected is not None and fact.get('value') is not None and fact['value'] != expected:
            block('CONFIRMED_FACT_CONFLICT', '확정 사실과 계산 입력 금액이 다릅니다. 먼저 불일치를 해소하세요.', fact.get('key'))
    for key, actual, limit in (('unsecured_debt_limit', unsecured_total, rules['unsecured_debt_limit']),
                               ('secured_debt_limit', secured_total, rules['secured_debt_limit'])):
        passed = actual <= limit
        result['checks'].append({'key': key, 'passed': passed, 'actual': actual, 'limit': limit,
                                 'source_id': 'statute-579'})
        if not passed:
            block('DEBT_LIMIT_EXCEEDED', '제579조 채무한도를 초과합니다.', key, True)
    if numeric_errors:
        result['summary'] = {'unsecured_debt': unsecured_total, 'secured_debt': secured_total,
                             'median_60_reference': benchmark}
        return result

    liquid = sum(row['liquidation_value'] for row in asset_rows)
    disposable = net - living - extra
    available = disposable - fee
    result['asset_calculations'] = asset_rows
    if available <= 0:
        block('NO_POSITIVE_REPAYMENT_CAPACITY', '인정 생계비와 비용을 차감한 월 변제 여력이 양수가 아닙니다.')
    if months > 36 and (p.get('period_exception') not in ('liquidation', 'court_order') or not decisions.get('period_exception_reason')):
        block('EXTENDED_PERIOD_REASON_REQUIRED', '36개월 초과는 청산가치 충족 필요와 사유를 별도 기록해야 합니다. 다른 특별사정은 개별 명령 입력이 필요합니다.', 'period_exception')
    if months < 36 and p.get('period_exception') not in ('full_principal', 'court_order'):
        block('SHORT_PERIOD_REASON_REQUIRED', '36개월 미만은 원금 전액 변제 또는 개별 법원 결정 근거가 필요합니다.', 'period_exception')
    if p.get('period_exception') == 'court_order' and not decisions.get('period_exception_reason'):
        block('COURT_ORDER_REQUIRED', '기간 변경 결정의 근거를 기록하세요.', 'decisions.period_exception_reason')
    if p.get('period_exception') == 'full_principal' and case.get('court_id') != 'CT01':
        block('SHORTENING_COURT_POLICY_UNVERIFIED', '다른 관할의 원금 전액 단축 적용은 법원 결정 근거를 연결하세요.')
    balances = {c['id']: {'principal': c['principal'], 'interest': c['interest']} for c in clean_creditors}
    totals = {c['id']: {'principal': 0, 'interest': 0, 'pv': Decimal(0)} for c in clean_creditors}
    schedule = []
    stop_on_principal = p.get('period_exception') == 'full_principal' and months < 36
    for month in range(1, months + 1):
        remaining = max(0, available)
        paid = {c['id']: {'principal': 0, 'interest': 0} for c in clean_creditors}
        # Priority debt must be paid in full before ordinary principal/interest.
        for kind, component in (('priority', 'principal'), ('priority', 'interest'),
                                ('unsecured', 'principal'), ('unsecured', 'interest')):
            if stop_on_principal and kind == 'unsecured' and component == 'interest':
                continue
            claims = [(c['id'], balances[c['id']][component]) for c in clean_creditors if c['kind'] == kind]
            paid_class = _allocate(min(remaining, sum(amount for _, amount in claims)), claims)
            for cid, amount in paid_class.items():
                paid[cid][component] += amount
                balances[cid][component] -= amount
                totals[cid][component] += amount
                remaining -= amount
        total_paid = sum(v['principal'] + v['interest'] for v in paid.values())
        with localcontext() as ctx:
            ctx.prec = 40
            factor = (1 + rate / 12) ** -max(0, month - prepaid)
            for cid, amounts in paid.items():
                totals[cid]['pv'] += Decimal(amounts['principal'] + amounts['interest']) * factor
        schedule.append({'month': month, 'trustee_fee': fee if total_paid else 0,
                         'creditor_payment': total_paid, 'deposit': total_paid + (fee if total_paid else 0),
                         'unused_capacity': max(0, available) - total_paid,
                         'allocations': [{'creditor_id': cid, **amounts, 'total': sum(amounts.values())}
                                         for cid, amounts in paid.items()]})
    total_paid = sum(row['creditor_payment'] for row in schedule)
    pv = present_value([row['creditor_payment'] for row in schedule], prepaid, rate)
    minimum = minimum_repayment(unsecured_total)
    principal_paid = sum(v['principal'] for v in totals.values())
    priority_paid = sum(v['principal'] + v['interest'] for c in clean_creditors if c['kind'] == 'priority'
                        for v in [totals[c['id']]])
    result['summary'] = {'net_monthly_income': net, 'median_income': median,
                         'median_60_reference': benchmark, 'base_living_cost': living,
                         'additional_living_cost': extra, 'monthly_disposable_income': disposable,
                         'monthly_trustee_fee': fee, 'monthly_creditor_capacity': max(0, available),
                         'monthly_deposit': max((row['deposit'] for row in schedule), default=0),
                         'months': months, 'prepaid_months': prepaid,
                         'unsecured_debt': unsecured_total, 'secured_debt': secured_total,
                         'principal_total': principal_total, 'interest_total': interest_total,
                         'liquidation_value': liquid, 'total_creditor_payment': total_paid,
                         'total_principal_payment': principal_paid,
                         'total_interest_payment': total_paid - principal_paid,
                         'present_value': int(pv.to_integral_value(rounding=ROUND_FLOOR)),
                         'present_value_exact': format(pv, '.12f'),
                         'minimum_repayment_if_objection': str(minimum),
                         'minimum_test_applied': p['objection'], 'priority_total': priority_total,
                         'annual_discount_rate': str(rate),
                         'liquidation_shortfall': max(0, liquid - int(pv.to_integral_value(rounding=ROUND_FLOOR)))}
    for key, passed, actual, threshold, source in (
            ('liquidation_floor', pv >= liquid, str(pv), liquid, 'statute-614'),
            ('priority_fully_paid', priority_paid == priority_total, priority_paid, priority_total, 'statute-611'),
            ('positive_capacity', available > 0, available, 1, 'statute-579'),
            ('allocation_conservation', total_paid == sum(v['principal'] + v['interest'] for v in totals.values()),
             total_paid, total_paid, 'engine-rounding')):
        result['checks'].append({'key': key, 'passed': passed, 'actual': actual, 'threshold': threshold,
                                 'source_id': source})
        if not passed:
            block('NUMERICAL_REQUIREMENT_NOT_MET', '정량 요건을 충족하지 못했습니다: ' + key, key)
    if p['objection']:
        passed = pv >= minimum
        result['checks'].append({'key': 'minimum_repayment', 'passed': passed, 'actual': str(pv),
                                 'threshold': str(minimum), 'source_id': 'statute-614'})
        if not passed:
            block('MINIMUM_REPAYMENT_NOT_MET', '이의 있는 경우의 최저변제액을 현가 기준으로 충족하지 못합니다.')
        objectors = p.get('objecting_creditor_ids')
        if not isinstance(objectors, list):
            block('OBJECTORS_REQUIRED', '이의 채권자 ID 목록이 필요합니다. 회생위원만 이의한 경우 빈 목록을 입력하세요.', 'objecting_creditor_ids')
        else:
            for cid in objectors:
                creditor = next((c for c in clean_creditors if c['id'] == cid), None)
                if creditor is None:
                    block('UNKNOWN_OBJECTOR', '이의 채권자가 현재 목록에 없습니다.', 'objecting_creditor_ids')
                    continue
                floor = creditor.get('bankruptcy_dividend')
                if type(floor) is not int or floor < 0:
                    block('OBJECTOR_LIQUIDATION_REQUIRED', '이의 채권자별 파산 예상배당액을 검토하여 입력하세요.', 'creditors.' + cid + '.bankruptcy_dividend')
                    continue
                passed = totals[cid]['pv'] >= floor
                result['checks'].append({'key': 'objector_liquidation_floor', 'creditor_id': cid,
                                         'passed': passed, 'actual': str(totals[cid]['pv']),
                                         'threshold': floor, 'source_id': 'statute-614'})
                if not passed:
                    block('OBJECTOR_LIQUIDATION_NOT_MET', '이의 채권자별 현가 변제액이 파산 예상배당액보다 적습니다.', 'creditors.' + cid)
    if months < 36 and stop_on_principal and principal_paid != principal_total:
        block('PRINCIPAL_NOT_FULLY_PAID', '단축기간 내 원금을 전액 변제하지 못합니다.')
    if months > 36 and p.get('period_exception') == 'liquidation':
        pv36 = present_value([row['creditor_payment'] for row in schedule[:36]], min(prepaid, 36), rate)
        if pv36 >= liquid:
            block('EXTENSION_NOT_NEEDED_FOR_LIQUIDATION', '36개월 안에 청산가치가 충족되어 선택한 연장 사유가 성립하지 않습니다.')
    result['schedule'] = schedule
    result['creditor_allocations'] = [
        {'creditor_id': c['id'], 'name': c['name'], 'kind': c['kind'],
         'principal': c['principal'], 'interest': c['interest'],
         'principal_payment': totals[c['id']]['principal'], 'interest_payment': totals[c['id']]['interest'],
         'total_payment': totals[c['id']]['principal'] + totals[c['id']]['interest'],
         'unpaid_principal': balances[c['id']]['principal'], 'unpaid_interest': balances[c['id']]['interest'],
         'present_value_exact': format(totals[c['id']]['pv'], '.12f')}
        for c in clean_creditors]
    result['formulas'] = [
        {'key': 'income', 'formula': '세전소득 - 세금·보험료 또는 검토된 세후소득', 'value': net},
        {'key': 'living', 'formula': '2026 기준중위소득 × 60%, 원 단위 반올림(서울) 또는 사건별 인정액', 'value': living},
        {'key': 'capacity', 'formula': '세후소득 - 기본생계비 - 추가생계비 - 월 회생위원 보수', 'value': available},
        {'key': 'liquidation', 'formula': '자산별 소유지분 평가액 - 담보 공제 - 면제재산 - 환가비용의 합', 'value': liquid},
        {'key': 'present_value', 'formula': 'Σ 회차별 채권자 변제액 / (1 + 연 할인율 / 12) ^ max(0, 회차 - 인가전 적립회차)', 'value': str(pv)},
        {'key': 'minimum', 'formula': '이의 있는 경우 min(30,000,000, 채권원리금<50,000,000 ? 원리금×5% : 원리금×3%+1,000,000)', 'value': str(minimum)},
        {'key': 'allocation', 'formula': '우선채권 전액 → 일반 원금 → 일반 이자. 각 단계 비율 안분; 1원 잔여는 소수 잔여 큰 순/ID순 배정.', 'value': total_paid}]
    result['warnings'].extend([
        '60% 생계비는 사건별 인정액 자체가 아닙니다. 실제 부양·추가비용·소득기간을 검토하세요.',
        '면제재산·담보·환가비용은 입력한 심사값입니다. 단순 재산 합계를 법정 청산가치로 확정하지 않습니다.',
        '1원 배분은 명시된 시스템 단수처리 방식입니다. 법원 서식의 단수처리와 최종 대조가 필요합니다.',
        '개인회생채권자별 이의·청산배당 보장은 개별 검토 대상이며 총액 계산만으로 인가를 확정하지 않습니다.'])
    result['status'] = 'ready_for_review' if not blockers else 'blocked'
    result['result_hash'] = _digest({'summary': result['summary'], 'schedule': schedule,
                                    'creditors': result['creditor_allocations'], 'input_hash': result['input_hash'],
                                    'policy_hash': result['policy_hash']})
    return result
