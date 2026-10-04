"""Typed, attributed observations from Korean evidence; no legal conclusions.

Amounts retain their document, period, account and gross/net meaning. Absence
of an observation never means zero. Public rules and model prose are not facts.
"""
from __future__ import annotations

import calendar
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

VERSION = 'document-facts-v1'
LABELS = {
    'income_gross': '공제 전 급여', 'income_net': '실수령 소득', 'income_deductions': '급여 공제 합계',
    'income_deduction': '급여 공제 항목', 'income_period': '소득 확인기간',
    'creditor_principal': '채권자별 원금', 'creditor_interest': '채권자별 이자', 'creditor_total': '채권자별 채무 합계',
    'cash_balance': '계좌별 예금잔액', 'housing_deposit': '임차보증금', 'housing_cost': '월 주거비',
    'living_expenses': '실제 월 생활지출', 'household_size': '실제 가구원 수', 'dependent_count': '부양가족 진술 인원',
    'employment_type': '소득 형태', 'real_estate_ownership': '부동산 보유 기재',
    'vehicle_ownership': '자동차 보유 기재', 'insurance_contracts': '보험계약 보유 기재',
    'housing_ownership': '거주 주택 소유 기재', 'bank_payroll_deposit': '기간별 급여 입금',
    'bank_living_expense': '기간별 생활비 출금', 'bank_debt_repayment': '기간별 상환 출금',
    'bank_inflow': '거래 입금', 'bank_outflow': '거래 출금', 'insurance_surrender': '보험 해약환급금',
    'tax_arrears': '세금 체납액', 'employer': '근무처', 'account_scope': '계좌 범위', 'query_scope': '자료 조회 범위',
    'client_name': '자료 대상자 성명', 'resident_id': '주민등록번호 기재', 'address': '주소',
    'employer_address': '근무처 주소', 'employer_phone': '근무처 연락처', 'employment_period': '재직 기간',
    'job_title': '직위·담당 업무',
    'employment_start': '입사일', 'housing_type': '거주 형태', 'housing_owner': '주택 소유자 기재',
    'housing_tenant': '임차인 기재', 'creditor_name': '채권자', 'creditor_cause': '차입 원인 기재',
    'creditor_address': '채권자 주소', 'creditor_kind': '담보 기재', 'institution': '금융기관',
    'account_key': '계좌 식별', 'loan_key': '대출 식별', 'pension_assessed_income': '연금 기준소득',
    'health_qualification': '건강보험 자격', 'pension_membership': '연금 가입 기재',
    'marital_status': '혼인 상태 기재', 'children_count': '자녀 수 기재', 'debt_history': '채무 발생 경위 진술',
    'family_debt_balance': '가족 차용금 잔액 진술', 'family_legal_relation': '가족 간 상환약정 진술',
    'monthly_income': '월 소득', 'annual_income': '월소득 연간 환산', 'income_manwon': '월 소득(만원)',
    'income_label': '수입 명목', 'income_period_label': '수입 기간구분', 'bank_balance': '예금 합계',
    'bank_name': '예금 금융기관', 'bank_account': '예금 계좌', 'assets_total': '관측 재산 합계',
    'principal_total': '채무 원금 합계', 'interest_total': '채무 이자 합계', 'total_debt': '채무 합계',
    'secured_debt': '담보부 채무 합계', 'unsecured_debt': '무담보 채무 합계',
    'real_estate_value': '부동산 가액', 'vehicle_value': '자동차 가액', 'tenant_name': '임차인 성명',
}
DATE = r'(?:19|20)\d{2}[-./년]\s*\d{1,2}(?:[-./월]\s*\d{1,2}일?)?'
RANGE = re.compile(r'(' + DATE + r')\s*(?:~|∼|〜|부터|–|—)\s*(' + DATE + r')(?:까지)?')
MONTH = re.compile(r'(?<!\d)((?:19|20)\d{2})[-./년]\s*(0?[1-9]|1[0-2])(?:월)?(?!\d)')
NUMBER = r'[-−]?\s*\d[\d,]*(?:\.\d+)?\s*(?:억\s*)?(?:\d[\d,]*(?:\.\d+)?\s*)?(?:천\s*만|백\s*만|만)?\s*(?:원)?'
NEGATIVE = re.compile(r'없(?:음|습니다|어요|다|고|으며|는)|아니(?:오|다|고|며|라)|아님|아닙니다|하지\s*않|해당\s*없|미해당')
UNKNOWN = re.compile(r'미확인|미상|모름|확인\s*(?:필요|예정|중)|불명|여부|알\s*수\s*없|인지|있나요|입니까')


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _date(value, end=False):
    numbers = [int(n) for n in re.findall(r'\d+', value)]
    try:
        year, month = numbers[:2]
        day = numbers[2] if len(numbers) > 2 else calendar.monthrange(year, month)[1] if end else 1
        from datetime import date
        return date(year, month, day).isoformat()
    except (ValueError, IndexError):
        return None


def _money(text, unit_multiplier=1):
    compact = re.sub(r'[\s,원]', '', text)
    match = re.fullmatch(r'(?:(\d+(?:\.\d+)?)억)?(?:(\d+(?:\.\d+)?)(천만|백만|만)?)?', compact)
    if not match or not any(match.groups()):
        return None
    try:
        value = Decimal(match.group(1) or 0) * 100000000
        value += Decimal(match.group(2) or 0) * {None: 1, '만': 10000, '백만': 1000000, '천만': 10000000}[match.group(3)]
        if not re.search(r'원|억|만', text):
            value *= unit_multiplier
        return int(value) if value == int(value) and 0 <= value <= 10**13 else None
    except (InvalidOperation, ValueError):
        return None


def _context(text):
    meta = {}
    # A date printed as issue-date is not itself the income earning period.
    ranges = [(m, text[max(0, text.rfind('\n', 0, m.start()) + 1):m.start()]) for m in RANGE.finditer(text)]
    ranges = [(m, prefix) for m, prefix in ranges if re.search(r'기간|귀속|대상|급여|소득|조회', prefix)]
    if ranges:
        match, _ = ranges[0]
        meta.update(period_start=_date(match.group(1)), period_end=_date(match.group(2), True))
    for key, pattern in (
        ('institution', r'(?:금융기관|채권자|은행명)\s*[:：]\s*([^\n/;|]+)'),
        ('account_key', r'(?:계좌번호|계좌)\s*[:：]\s*([A-Za-z0-9*＊Xx●•-]{3,})'),
        ('loan_key', r'(?:대출번호|채권번호|약정번호|계약번호)\s*[:：]\s*([A-Za-z0-9*＊Xx●•-]{3,})'),
        ('employer', r'(?:근무처|회사명|직장명|사업장명|사업장)\s*[:：]\s*([^\n/;|]+)'),
    ):
        matches = list(re.finditer(pattern, text))
        if matches:
            match = matches[-1]
            meta[key] = match.group(1).strip()
    # Issuance is an administrative event, not the date the stated balance
    # existed. It can never make an old balance outrank a newer observation.
    for key, pattern in (
        ('balance_date', r'(?:잔액\s*기준일|기준일)\s*[:：]?\s*(' + DATE + ')'),
        ('query_date', r'조회일\s*[:：]?\s*(' + DATE + ')'),
        ('issued_at', r'발급일\s*[:：]?\s*(' + DATE + ')'),
    ):
        matches = list(re.finditer(pattern, text))
        if matches:
            meta[key] = _date(matches[-1].group(1))
            meta[key + '_quote'] = matches[-1].group()
    for key in ('balance_date', 'query_date'):
        if meta.get(key):
            meta.update(as_of=meta[key], as_of_kind=key, as_of_quote=meta[key + '_quote'])
            break
    return meta


def _clauses(text):
    # Keep commas together: a list of subjects can share a negative predicate.
    for match in re.finditer(r'[^\n.!?;]+', text):
        yield match.start(), match.end(), match.group()


def employment_observations(text):
    """Require a complete affirmative/negative employment clause, not a substring."""
    found = []
    salary = r'급여소득자|근로소득자|직장인|회사원|현재\s*재직\s*중|정규직(?:으로)?\s*(?:근무|재직|취업)|직장가입자'
    business = r'사업소득자|영업소득자|자영업(?:자)?|개인사업(?:자)?|프리랜서'
    unemployed = r'(?:현재\s*)?(?:무직|실직)(?:\s*(?:상태|중))?|(?:현재\s*)?(?:모든\s*|전체\s*)?소득(?:이|은)?\s*없(?:음|습니다|어요|다)'
    for start, end, clause in _clauses(text):
        for value, pattern in (('급여소득자', salary), ('사업소득자', business), ('무직 진술', unemployed)):
            for match in re.finditer(pattern, clause):
                prefix, tail = clause[:match.start()], clause[match.end():]
                if UNKNOWN.search(clause) or re.search(r'과거|예전|당시|이전|\d{4}\s*[~∼-]\s*\d{4}', prefix):
                    continue
                if re.search(r'취업\s*후|재직\s*당시|퇴직\s*후|퇴사\s*후', clause) and not re.search(r'현재', clause):
                    continue
                if value == '무직 진술':
                    # '급여외/사업/기타/연금 소득 없음' does not negate employment.
                    if '소득' in match.group() and re.search(r'급여\s*외|근로\s*외|사업|기타|이자|배당|연금|임대|추가|외의', prefix):
                        continue
                    if re.search(r'아님|아니|아닙|아닌|없지\s*않|없는\s*것은\s*아', tail):
                        continue
                elif NEGATIVE.search(tail.split('/')[0]):
                    # A positive predicate or a contrast ends the negative scope.
                    first = re.split(r'하지만|그러나|다만|반면|[,/]', tail, maxsplit=1)[0]
                    if NEGATIVE.search(first) or not re.search(r'입니다|근무|재직|종사|운영', first):
                        continue
                if value == '사업소득자' and re.match(r'\s*등록(?:번호|증)', tail):
                    continue
                found.append({'value': value, 'start': start, 'end': end, 'quote': clause.strip()})
    return found


def classification(text):
    patterns = [
        ('급여명세서', 'D07', r'급여\s*명세|급료\s*명세'),
        ('근로소득원천징수영수증', 'D06', r'근로\s*소득\s*원천\s*징수|원천\s*징수\s*영수증'),
        ('재직증명서', 'D08', r'재직\s*증명'), ('건강보험자격득실확인서', 'D09', r'건강\s*보험\s*자격\s*득실'),
        ('연금산정용가입내역확인서', 'D11', r'연금\s*산정용?\s*가입|연금.*가입\s*내역'),
        ('계좌통합조회결과', 'D35', r'계좌\s*통합\s*조회'), ('지적전산자료조회결과', 'D28', r'지적\s*전산\s*자료'),
        ('지방세세목별과세증명서', 'D29', r'지방세\s*세목별\s*과세'),
        ('보험가입·해약환급금확인', 'D39', r'보험\s*가입.*해약\s*환급|보험\s*가입\s*확인'),
        ('무상거주확인', None, r'무상\s*거주\s*확인'),
        ('채무발생·생활비 진술', None, r'채무\s*발생.*(?:생활비|진술)|채무\s*경위\s*진술'),
    ]
    for title, catalog_id, pattern in patterns:
        match = re.search(pattern, text[:300])
        if match:
            line = text[text.rfind('\n', 0, match.start()) + 1:text.find('\n', match.end()) if '\n' in text[match.end():] else len(text)]
            if not re.search(r'미제출|제출\s*전|제출\s*필요|요청|미확보', line):
                return {'classification': title, 'catalog_id': catalog_id, 'quote': match.group()}
    return None


# The nearest *typed* label owns an amount. Generic '소득' never swallows annual
# gross pay, a deduction, an account balance or a creditor-specific amount.
MONEY_LABELS = [
    ('income_gross', r'(?:총\s*급여|지급\s*총액|지급액\s*합계|세전\s*(?:급여|소득)?|연봉|과세\s*급여)', 'gross'),
    ('income_net', r'(?:실수령\s*(?:소득|액|합계)?|실지급\s*(?:액|합계)?|차인\s*지급\s*액|세후\s*(?:급여|소득)?|월\s*평균)', 'net'),
    ('income_deductions', r'(?:공제\s*(?:액|총액|합계)|총\s*공제(?:액)?|공제액\s*계)', 'deductions'),
    ('income_deduction', r'(?:소득세|지방소득세|국민연금|건강보험료?|고용보험료?|장기요양보험료?)', 'deductions'),
    ('creditor_principal', r'(?<!상환\s)원금', None), ('creditor_interest', r'이자(?:액)?', None),
    ('creditor_total', r'(?:채무|부채|대출)\s*(?:합계|총액|잔액)', None),
    ('cash_balance', r'예금\s*잔액|예금(?=\s*[:：])|(?:조회\s*)?(?:시작|종료|기말|현재)\s*잔액', None),
    ('housing_deposit', r'(?:임차|임대차|전세)\s*보증금', None),
    ('housing_cost', r'월\s*(?:세|차임|임대료|주거비)', None),
    ('living_expenses', r'(?:실제\s*)?월\s*(?:생활\s*(?:비|지출)|지출)', None),
    ('insurance_surrender', r'해약\s*환급금|해지\s*환급금', None),
    ('tax_arrears', r'(?:국세|지방세|세금)?\s*체납\s*(?:액|금액)|조세\s*채무', None),
    ('pension_assessed_income', r'(?:월\s*)?기준\s*소득(?:월액)?', 'assessment'),
]


def extract(sources):
    """Return typed source observations, preserving exact quotes and page lines."""
    result = []
    contexts = {}
    for source in sources:
        if source.get('kind') not in {'case_document', 'party_statement'}:
            continue
        group = source.get('document_id') or source['id']
        contexts.setdefault(group, []).append(source.get('text', ''))
    group_texts = {key: '\n'.join(rows) for key, rows in contexts.items()}
    contexts = {key: _context(body) for key, body in group_texts.items()}
    page_units = {}
    for source in sources:
        match = re.search(r'단위\s*[:：]?\s*(천원|만원|원)', source.get('text', ''))
        if match:
            page_units[(source.get('document_id') or source['id'], source.get('page'))] = (match.group(1), match.group(), source['id'])
    for source in sources:
        if source.get('kind') not in {'case_document', 'party_statement'}:
            continue
        text = source.get('text', '')
        meta = {**contexts.get(source.get('document_id') or source['id'], {}), **_context(text)}
        unit_name, unit_quote, unit_source_id = page_units.get((source.get('document_id') or source['id'], source.get('page')), ('원', None, None))
        multiplier = {'천원': 1000, '만원': 10000, '원': 1}[unit_name]
        def add(key, value, start, end, **details):
            quote = text[start:end].strip()
            if value is None or not quote:
                return
            row = {'key': key, 'label': LABELS.get(key, key), 'value': value, 'quote': quote,
                   'document_id': source.get('document_id'), 'source_id': source['id'], 'page': source.get('page'),
                   'source_version': source.get('version', 1), 'source_type': source['kind'],
                   'line_start': source.get('line_offset', 0) + text[:start].count('\n') + 1,
                   'line_end': source.get('line_offset', 0) + text[:end].count('\n') + 1,
                   'status': 'candidate', 'origin': VERSION, 'unit': 'KRW' if type(value) is int and key not in {'household_size', 'dependent_count', 'children_count'} else None,
                   **meta, **details}
            if value is False or (key in {'dependent_count', 'children_count'} and value == 0):
                row['explicit_absence'] = True
            qualifiers = {key: row.get(key) for key in ('unit', 'basis', 'frequency', 'period_start', 'period_end',
                'period_months', 'as_of', 'institution', 'account_key', 'loan_key', 'balance_kind', 'deduction_kind', 'scope')}
            row['id'] = 'typed-' + _hash([key, value, source.get('document_id') or source['id'], row['page'], quote, qualifiers])[:16]
            result.append(row)
        lines = list(re.finditer(r'[^\n]+', text))
        for line in lines:
            segment = line.group()
            local_meta = _context(segment)
            if meta.get('as_of_kind') == 'balance_date' and local_meta.get('as_of_kind') == 'query_date':
                local_meta = {key: value for key, value in local_meta.items() if key not in {'as_of', 'as_of_kind', 'as_of_quote'}}
            meta.update(local_meta)
            matches = [(key, basis, match) for key, pattern, basis in MONEY_LABELS for match in re.finditer(pattern, segment)]
            for key, basis, label in matches:
                tail = segment[label.end():]
                number = re.match(r'\s*(?:은|는|이|가|:|：)?\s*(' + NUMBER + ')', tail)
                if not number:
                    continue
                amount = _money(number.group(1), multiplier)
                if amount is None:
                    continue
                # Never create the same amount under a nested earlier label.
                amount_start = label.end() + number.start(1)
                if any(other.start() > label.start() and other.end() <= amount_start for _, _, other in matches):
                    continue
                before = segment[:label.start()]
                frequency, months = None, None
                monthly = re.search(r'매월|(?<![가-힣\d])월\s*(?:총\s*)?(?:평균|실수령|급여|공제|소득|차임|세|주거|생활|지출)', segment[max(0, label.start()-12):label.end()])
                explicit_months = re.search(r'(?:직전|최근)?\s*(\d{1,2})\s*개월', before)
                if monthly:
                    frequency = 'monthly'
                elif explicit_months:
                    months = int(explicit_months.group(1))
                    frequency = 'annual' if months == 12 else 'period'
                elif re.search(r'연봉|연간|연\s*소득|귀속\s*연도', segment) or re.search(r'귀속\s*연도', text):
                    frequency = 'annual'
                elif key in {'income_gross', 'income_net', 'income_deductions', 'income_deduction'}:
                    # A titled monthly payslip or explicit pay-month supports a
                    # monthly interpretation; a tax certificate alone does not.
                    if re.search(r'급여\s*명세|급료\s*명세|급여월|지급월', text):
                        frequency = 'monthly'
                    elif meta.get('period_start'):
                        frequency = 'period'
                actual_multiplier = 1 if re.search(r'원|억|만', number.group(1)) else multiplier
                details = {'basis': basis, 'amount_basis': basis, 'frequency': frequency,
                           'unit_multiplier': actual_multiplier}
                if actual_multiplier != 1:
                    details.update(source_unit_quote=unit_quote, unit_source_id=unit_source_id)
                if months:
                    details['period_months'] = months
                if key == 'income_deduction':
                    details['deduction_kind'] = label.group()
                if key == 'cash_balance':
                    details['balance_kind'] = 'opening' if re.search(r'시작', label.group()) else 'closing'
                if key.startswith('creditor_'):
                    details['scope'] = 'creditor' if meta.get('institution') or re.search(r'부채\s*증명|채무\s*확인', text) else 'unspecified'
                add(key, amount, line.start(), line.end(), **details)
            family_balance = re.search(r'가족\s*(?:채무|차용금)\s*잔액\s*[:：]?\s*(' + NUMBER + ')', segment)
            if family_balance:
                add('family_debt_balance', _money(family_balance.group(1), multiplier), line.start(), line.end(),
                    scope='family_claim_requires_review', source_assertion='party_statement')
            for match in re.finditer(r'(?:가구원|세대원)\s*(?:수|:|：)?\s*(?:[가-힣]{2,5}\s*)?(\d{1,2})\s*명|(?<!\d)(\d{1,2})\s*인\s*가구', segment):
                size = int(match.group(1) or match.group(2))
                if 1 <= size <= 30:
                    add('household_size', size, line.start(), line.end(), limitation='실제 동거 인원이며 법률상 인정 부양인원과 다릅니다.')
            if re.search(r'부양가족\s*(?:은|:|：)?\s*없', segment):
                add('dependent_count', 0, line.start(), line.end(), limitation='부양가족이 없다는 문서 기재이며 법률상 생계비 판단이 아닙니다.')
            if re.search(r'자녀\s*(?:는|:|：)?\s*없', segment):
                add('children_count', 0, line.start(), line.end())
            if re.search(r'미혼', segment):
                add('marital_status', '미혼', line.start(), line.end())
            if re.search(r'직장\s*가입자', segment):
                add('health_qualification', '직장가입자', line.start(), line.end())
            if re.search(r'연금.*가입|가입.*연금', text[:180]) and re.search(r'현재\s*가입\s*유지', segment):
                add('pension_membership', '현재 가입 유지', line.start(), line.end())
            if re.search(r'무상\s*거주|무료\s*거주', segment) and not NEGATIVE.search(segment):
                add('housing_type', '무상거주', line.start(), line.end())
            if re.search(r'담보\s*없|무담보', segment):
                add('creditor_kind', '무담보', line.start(), line.end(), scope='document_statement')
            if re.search(r'부족한\s*생활비.*차용|원리금\s*상환\s*부담|채무가\s*늘어난|차입\s*경위', segment):
                add('debt_history', segment.strip(), line.start(), line.end(), source_assertion='party_statement')
            if re.search(r'(?:법률관계\s*진술|가족\s*(?:간\s*)?약정)\s*[:：]', segment):
                add('family_legal_relation', segment.strip(), line.start(), line.end(), source_assertion='party_statement',
                    limitation='상환약정에 관한 원문 진술이며 별도 채권 인정·합산 판단은 미확정입니다.')
            for key, pattern in (
                ('real_estate_ownership', r'(?:소유\s*)?부동산\s*(?:검색\s*결과|소유)?\s*[:：]?\s*없'),
                ('vehicle_ownership', r'(?:소유\s*)?(?:자동차|차량)\s*(?:검색\s*결과|소유)?\s*[:：]?\s*없'),
                ('insurance_contracts', r'(?:보유\s*)?보험\s*계약\s*[:：]?\s*없'),
                ('housing_ownership', r'소유자\s*본인\s*여부\s*[:：]?\s*(?:아니오|아님)'),
            ):
                if re.search(pattern, segment):
                    add(key, False, line.start(), line.end())
            period = RANGE.search(segment)
            if period and re.search(r'소득|급여|지급|확인기간', segment):
                add('income_period', period.group(), line.start(), line.end(),
                    period_start=_date(period.group(1)), period_end=_date(period.group(2), True))
            for key, pattern in [
                ('job_title', r'(?:담당\s*업무|직무|직위|직책)\s*[:：]\s*([^\n/;|]+)'),
                ('client_name', r'(?:성명|채무자|예금주|조회대상|가입자|본인)\s*[:：]\s*([가-힣]{2,5})(?=\s|[/;.,]|$)'),
                ('resident_id', r'주민\s*등록\s*번호\s*[:：]\s*(\d{6}\s*-?\s*[\d*＊●]{7})'),
                ('address', r'(?<!처\s)(?<!자\s)(?:거주지|거주\s*주소|^주소)\s*[:：]\s*([^\n/;|]+)'),
                ('employer', r'(?:근무처|회사명|직장명|사업장명|사업장)\s*[:：]\s*([^\n/;|]+)'),
                ('employer_address', r'(?:근무처|회사|사업장)\s*(?:주소|소재지)\s*[:：]\s*([^\n/;|]+)'),
                ('employer_phone', r'(?:근무처|회사|사업장)\s*(?:전화|연락처)\s*[:：]\s*([\d()+ -]+)'),
                ('employment_period', r'(?:재직\s*기간|근무\s*기간)\s*[:：]\s*([^\n/;|]+)'),
                ('employment_start', r'(?:입사일|입사\s*일자)\s*[:：]\s*(' + DATE + ')'),
                ('housing_owner', r'(?:주택\s*소유자|임대인|거주지\s*소유자)\s*[:：]\s*([^\n/;|]+)'),
                ('housing_tenant', r'임차인\s*[:：]\s*([^\n/;|]+)'),
                ('creditor_name', r'채권자\s*[:：]\s*([^\n/;|]+)'),
                ('creditor_cause', r'(?:차입\s*원인|대출\s*목적|차입금\s*용도|차용\s*용도)\s*[:：]\s*([^\n/;|]+)'),
                ('creditor_address', r'채권자\s*(?:주소|소재지)\s*[:：]\s*([^\n/;|]+)'),
                ('institution', r'(?:금융기관|은행명)\s*[:：]\s*([^\n/;|]+)'),
                ('account_key', r'(?:계좌번호|계좌)\s*[:：]\s*([A-Za-z0-9*＊Xx●•-]{3,})'),
                ('loan_key', r'(?:대출번호|채권번호|약정번호|계약번호)\s*[:：]\s*([A-Za-z0-9*＊Xx●•-]{3,})'),
                ('account_scope', r'(?:전체\s*계좌|본인\s*계좌|계좌\s*목록)\s*[:：]\s*([^\n;]+)'),
                ('query_scope', r'조회\s*범위\s*[:：]\s*([^\n;]+)'),
            ]:
                match = re.search(pattern, segment)
                if match and not UNKNOWN.search(match.group(1)):
                    add(key, match.group(1).strip(), line.start(), line.end())
        for employment in employment_observations(text):
            add('employment_type', employment['value'], employment['start'], employment['end'], unit='category')
        salary_title = re.search(r'급여\s*명세서|급료\s*명세서|근로\s*소득\s*원천\s*징수', text[:300])
        income = next((row for row in result if row['source_id'] == source['id'] and row['key'] in {'income_gross', 'income_net'} and row['value'] > 0), None)
        if salary_title and income:
            end = text.find(income['quote']) + len(income['quote'])
            add('employment_type', '급여소득자', salary_title.start(), end, unit='category',
                employment_assertion='document_period', limitation='해당 증빙 기간의 근로소득이며 현재 재직의 별도 판단과 구분합니다.')
        _table_rows(text, add, group_texts.get(source.get('document_id') or source['id'], text),
                    (multiplier, unit_quote, unit_source_id))
    seen, unique = set(), []
    for row in result:
        identity = row['id']
        if identity not in seen:
            seen.add(identity); unique.append(row)
    return unique


def _table_rows(text, add, context_text=None, unit_info=(1, None, None)):
    """Read aligned text or one-cell-per-line monthly bank summaries."""
    headers = [(r'급여\s*입금', 'bank_payroll_deposit'), (r'생활비\s*출금', 'bank_living_expense'),
               (r'상환\s*출금', 'bank_debt_repayment'), (r'입금(?:액)?', 'bank_inflow'), (r'출금(?:액)?', 'bank_outflow')]
    lines = list(re.finditer(r'[^\n]+', text))
    columns, header_seen = [], False
    # Retrieval windows may begin after the header. Recover only its column
    # labels from the same document; row values/quotes still come from text.
    for context_line in (context_text or text).splitlines():
        if re.search(r'\d', context_line) or len(context_line.strip()) >= 100:
            continue
        contextual = [(match.start(), match.end(), key) for pattern, key in headers[:3]
                      for match in re.finditer(pattern, context_line)]
        for _, _, key in sorted(contextual):
            if key not in columns:
                columns.append(key)
    header_seen = bool(columns)
    header_run = False
    for index, line in enumerate(lines):
        segment = line.group()
        labels = []
        for pattern, key in headers:
            for match in re.finditer(pattern, segment):
                if not any(begin <= match.start() and end >= match.end() for begin, end, _ in labels):
                    labels.append((match.start(), match.end(), key))
        if labels and not re.search(r'\d', segment) and len(segment.strip()) < 100:
            if not header_run:
                columns = []
            columns.extend(key for _, _, key in sorted(labels))
            header_seen = True
            header_run = True
            continue
        header_run = False
        month = MONTH.match(segment.strip())
        if not month or not columns or not header_seen:
            continue
        # An ISO full date is a daily transaction, not a monthly summary.
        if re.match(r'[-./]\s*\d', segment.strip()[month.end():]):
            continue
        row_start, row_end = line.start(), line.end()
        rest = segment.strip()[month.end():]
        amounts = re.findall(r'(?<![\d.])[-−]?\d[\d,]*(?:\.\d+)?(?:원)?', rest)
        for following in lines[index+1:index+1+len(columns)]:
            if len(amounts) >= len(columns):
                break
            if not re.fullmatch(r'\s*[-−]?\d[\d,]*(?:\.\d+)?\s*원?\s*', following.group()):
                break
            amounts.append(following.group().strip()); row_end = following.end()
        if len(amounts) != len(columns):
            continue
        year, month_no = int(month.group(1)), int(month.group(2))
        row_meta = _context(text[:row_start])
        row_meta = {key: value for key, value in row_meta.items() if key in {'institution', 'account_key', 'loan_key'}}
        for key, value in zip(columns, amounts):
            multiplier, unit_quote, unit_source_id = unit_info
            unit_meta = {'unit_multiplier': 1 if '원' in value else multiplier}
            if unit_meta['unit_multiplier'] != 1:
                unit_meta.update(source_unit_quote=unit_quote, unit_source_id=unit_source_id)
            add(key, _money(value, multiplier), row_start, row_end, frequency='monthly', basis='transaction',
                amount_basis='transaction', period_start=f'{year:04d}-{month_no:02d}-01',
                period_end=f'{year:04d}-{month_no:02d}-{calendar.monthrange(year, month_no)[1]:02d}', **row_meta, **unit_meta)
