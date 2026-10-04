"""Conservative monetary-role checks for locally generated Korean prose.

This is a bounded check of labelled monetary claims, not a semantic approval of
the whole narrative. Only the paragraph's exact cited excerpts belong here.
Dates, counts, causal claims and legal conclusions need their existing checks.
No expected fixture answer, model or network service is used.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import re

VERSION = 'narrative-monetary-roles-v1'
ROLES = [
    ('income_net', r'공제\s*후\s*(?:급여|소득|금액)?|실수령\s*(?:소득|금액|합계|액)?|실지급\s*(?:합계|액)?|차인\s*지급액|세후\s*(?:급여|소득|수입)?'),
    ('income_gross', r'총\s*(?:급여|지급액|소득)|급여\s*총액|세전\s*(?:급여|소득|수입)?|연봉'),
    ('income_deductions', r'(?:세금\s*(?:및|·)\s*사회보험료?\s*)?공제\s*(?:액|총액|합계)?|총\s*공제액?'),
    ('income_tax', r'(?<!지방)소득세'), ('local_income_tax', r'지방소득세'),
    ('health_insurance', r'건강보험료'), ('pension_contribution', r'국민연금'),
    ('income', r'(?:월|연)\s*소득|급여|소득|수입'),
    ('recognized_living_cost', r'(?:인정|기본|추가)\s*생계비'),
    ('living_expenses', r'(?:실제\s*)?(?:월\s*)?생활\s*(?:비|지출)|생계비'),
    ('food_expense', r'식비'), ('transport_expense', r'교통비?'),
    ('communication_expense', r'통신비?'), ('utilities_expense', r'공과금'),
    ('other_expense', r'기타\s*(?:생활비|지출|비용)?'),
    ('assets_total', r'(?:자산|재산)\s*(?:합계|총액|가액)?'),
    ('cash_balance', r'예금\s*(?:잔액)?|현금\s*(?:잔액)?|계좌\s*잔액'),
    ('housing_deposit', r'(?:임차|임대차|전세)\s*보증금'),
    ('housing_cost', r'월\s*(?:차임|임대료|주거비|세)'),
    ('insurance_surrender', r'(?:해약|해지)\s*환급금'),
    ('creditor_principal', r'원금'), ('creditor_interest', r'이자(?:액)?'),
    ('debt_total', r'(?:금융\s*)?(?:채무|부채)\s*(?:합계|총액|잔액)?|대출\s*(?:잔액|금액)|차용금'),
    ('tax_arrears', r'(?:세금\s*)?체납\s*(?:액|금액)|조세\s*채무'),
    ('total_repayment', r'총\s*변제액|변제\s*총액|총\s*변제\s*예정액'),
    ('minimum_repayment', r'최저\s*변제액'), ('liquidation_value', r'청산\s*가치'),
    ('monthly_payment', r'(?:월\s*)?변제\s*(?:재원|금|액)|월\s*납입액'),
]
ROLE_LABELS = dict(income_net='실수령 소득', income_gross='공제 전 소득', income_deductions='급여 공제 합계',
    income='소득',living_expenses='생활비',recognized_living_cost='인정 생계비',assets_total='자산 합계',
    cash_balance='예금잔액',housing_deposit='임차보증금',housing_cost='주거비',debt_total='채무 합계',
    creditor_principal='채무 원금',creditor_interest='채무 이자',monthly_payment='월 변제액',
    total_repayment='변제 총액',minimum_repayment='최저 변제액',liquidation_value='청산가치')
ALIASES = {'monthly_income':'income','total_debt':'debt_total','creditor_total':'debt_total',
    'monthly_creditor_capacity':'monthly_payment','monthly_deposit':'monthly_payment','base_living_cost':'recognized_living_cost',
    'minimum_required_total_repayment':'minimum_repayment'}
FLOW_ROLES = {role for role,_ in ROLES if role.startswith('income') or role.endswith('_expense')} | {
    'living_expenses','recognized_living_cost','housing_cost','monthly_payment','health_insurance','pension_contribution','local_income_tax'}
NUM = r'\d[\d,]*(?:\.\d+)?'
UNIT = r'(?:억|천만|백만|십만|만|천|백|십)'
# Every repeated group needs a Korean scale separator. A nested repetition of
# bare digit runs causes exponential backtracking on long unlabelled numbers.
AMOUNT = NUM + r'(?:\s*' + UNIT + r'\s*' + NUM + r')*(?:\s*' + UNIT + r')?\s*'
MONEY = re.compile(r'(?<![\dA-Za-z])(?P<amount>'+AMOUNT+r')원')
AFTER_LABEL = re.compile(r'\s*(?:은|는|이|가|:|：|=)?\s*(?:약\s*|합계\s*|총\s*)?(?P<amount>'+AMOUNT+r')(?P<won>원)?')
PERIODS = re.compile(r'(?P<annual>연간|연봉|연평균|직전\s*12\s*개월|최근\s*12\s*개월|12\s*개월|연\s*(?=총급여|소득|실수령))|(?P<monthly>매월|월평균|월\s*(?=총|급여|소득|공제|실수령|생활|지출|변제|차임|세|\d[\d,.]*(?:억|천|백|십|만|원)))|(?P<period>(?P<months>\d{1,2})\s*개월)')


def money_value(value, multiplier=1):
    """Exact display-unit conversion; never annualise or divide an income."""
    compact=re.sub(r'[\s,원]','',value)
    tokens=list(re.finditer(r'(\d+(?:\.\d+)?)(억|천만|백만|십만|만|천|백|십)?',compact))
    if not tokens or ''.join(token.group() for token in tokens)!=compact:return None
    total=Decimal(0);section=Decimal(0)
    try:
        for token in tokens:
            number=Decimal(token.group(1));unit=token.group(2)
            if unit=='억' or unit and unit.endswith('만'):
                inner={'천만':1000,'백만':100,'십만':10}.get(unit,1)
                total+=(section+number*inner)*(100000000 if unit=='억' else 10000);section=Decimal(0)
            elif unit:section+=number*{'천':1000,'백':100,'십':10}[unit]
            else:section+=number
        amount=(total+section)*multiplier
        return int(amount) if amount==amount.to_integral_value() and 0<=amount<=10**15 else None
    except InvalidOperation:return None


def _period(text, label_start, amount_end, role):
    if role not in FLOW_ROLES:return None,None
    start=max(text.rfind('\n',0,label_start),text.rfind('。',0,label_start)) + 1
    # A decimal point inside a monetary value is not a sentence boundary.
    sentence=list(re.finditer(r'(?<!\d)[.!?]\s+|(?<=원)[.!?]\s+',text[start:label_start]))
    if sentence:start+=sentence[-1].end()
    found=list(PERIODS.finditer(text[start:amount_end]))
    if not found:return None,None
    match=found[-1]
    return ('annual',12) if match.group('annual') else ('monthly',1) if match.group('monthly') else ('period',int(match.group('months')))


def _claims(text, *, multiplier=1):
    labels=[(role,match) for role,pattern in ROLES for match in re.finditer(pattern,text)]
    found=[]
    for role,label in labels:
        tail=AFTER_LABEL.match(text,label.end())
        if not tail:continue
        start,end=tail.span('amount')
        # Nested generic labels cannot take ownership from '실수령 소득' etc.
        if any(other.start()<=label.start() and other.end()>=label.end() and
               other.end()-other.start()>label.end()-label.start() for _,other in labels):continue
        if any(label.end()<=other.start()<start for _,other in labels):continue
        if not tail.group('won') and re.match(r'\s*(?:년|월|일|개월|명|호|쪽|%)',text[end:]):continue
        raw=tail.group('amount')
        value=money_value(raw,1 if tail.group('won') or re.search('[억만천백십]',raw) else multiplier)
        if value is None:continue
        frequency,months=_period(text,label.start(),tail.end(),role)
        found.append({'role':role,'value':value,'frequency':frequency,'period_months':months,
            'start':start,'end':tail.end(),'label_start':label.start()})
    # Support the natural reversed phrase '320만원의 실수령 소득'.
    for amount in MONEY.finditer(text):
        if any(row['start']<=amount.start()<row['end'] for row in found):continue
        suffix=re.match(r'\s*(?:의\s*)?',text[amount.end():]);begin=amount.end()+suffix.end()
        matches=[(role,label) for role,label in labels if label.start()==begin]
        if matches:
            role,label=max(matches,key=lambda pair:pair[1].end()-pair[1].start())
            frequency,months=_period(text,amount.start(),label.end(),role)
            found.append({'role':role,'value':money_value(amount.group('amount')),'frequency':frequency,'period_months':months,
                'start':amount.start(),'end':amount.end(),'label_start':label.start()})
        else:
            found.append({'role':None,'value':money_value(amount.group('amount')),'frequency':None,'period_months':None,
                'start':amount.start(),'end':amount.end(),'label_start':amount.start()})
    unique={}
    for row in found:unique[(row['role'],row['value'],row['start'])]=row
    return list(unique.values())


def _evidence_claims(rows):
    found=[]
    for row in rows:
        if row.get('kind')=='public_legal_source':continue
        quote=row.get('quote',row.get('text',''))
        if not isinstance(quote,str):continue
        # A separately attributed table heading can declare a page-wide unit.
        # An unexplained multiplier alone is never trusted as such a heading.
        unit=re.search(r'단위\s*[:：]?\s*(천원|만원|원)',quote+'\n'+str(row.get('source_unit_quote') or ''))
        multiplier={'천원':1000,'만원':10000,'원':1}.get(unit.group(1),1) if unit else 1
        claims=_claims(quote,multiplier=multiplier)
        key=ALIASES.get(row.get('key'),row.get('key'))
        for claim in claims:
            if claim['role'] is None:continue
            # Metadata may add missing period for the same typed amount, but
            # never relabel quoted assets as income or map every number to key.
            if claim['role']==key and not claim['frequency']:
                claim['frequency']=row.get('frequency')
                claim['period_months']=row.get('period_months')
            found.append(claim)
        # A code calculation is structured data, not natural-language inference.
        # Its specific key and scalar witness must both be supplied by the caller.
        if row.get('kind')=='code_calculation':
            try:
                data=json.loads(quote if quote.lstrip().startswith('{') else '{'+quote+'}')
            except (ValueError,TypeError):data={}
            for name,value in data.items() if isinstance(data,dict) else []:
                calculation_role=ALIASES.get(name,name)
                if calculation_role not in ROLE_LABELS or type(value) not in {int,float}:continue
                found.append({'role':calculation_role,'value':value,
                    'frequency':'monthly' if name.startswith('monthly_') else None,'period_months':None})
            for value in row.get('verified_numeric_values',[]) if key in ROLE_LABELS else []:
                if type(value) in {int,float}:
                    found.append({'role':key,'value':value,'frequency':row.get('frequency') or ('monthly' if str(row.get('key','')).startswith('monthly_') else None),
                        'period_months':row.get('period_months')})
    return found


def validate_numeric_roles(text, evidence_rows):
    """Return review findings, never a whole-document semantic pass.

    ``evidence_rows`` must contain only exact excerpts actually cited by this
    paragraph. A flat bag of witnessed numbers does not support monetary roles.
    No monthly/annual calculation is inferred from source amounts.
    """
    evidence=_evidence_claims(evidence_rows);checks=[]
    for claim in _claims(text):
        role,value=claim['role'],claim['value']
        same=[item for item in evidence if item['role']==role and item['value']==value]
        valid=[item for item in same if item['frequency']==claim['frequency'] and
            (claim['frequency']!='period' or item.get('period_months')==claim.get('period_months'))]
        if role and valid:continue
        if not role:
            code='NARRATIVE_NUMERIC_ROLE_REQUIRED';reason='금액이 어떤 항목인지 원문과 연결해 확인해야 합니다.'
        elif same:
            code='NARRATIVE_NUMERIC_PERIOD_MISMATCH';reason='금액의 월간·연간 또는 대상 기간이 인용 원문과 다릅니다.'
        elif any(item['value']==value for item in evidence):
            code='NARRATIVE_NUMERIC_ROLE_MISMATCH';reason='같은 금액이 원문에 있으나 소득·공제·자산·생활비 등 항목의 의미가 다릅니다.'
        else:
            code='NARRATIVE_AMOUNT_NOT_SUPPORTED';reason='해당 항목과 금액을 함께 뒷받침하는 인용 원문이 없습니다.'
        checks.append({'code':code,'reason':reason,'amount':value,'claim_label':ROLE_LABELS.get(role,'금액 항목'),
            'frequency':claim['frequency']})
    return checks
