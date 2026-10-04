"""Executable evidence-preparation rules, distinct from approved legal determinations."""
import json
import re
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]

FACTORS=[
 ('client_name','내담자 성명',['D01','D03'],'application'),
 ('address','실제 거주지·주소',['D01','D02','D33'],'application'),
 ('monthly_income','월 소득·실수령액',['D07','D36','D50'],'income_expense'),
 ('income_period','소득 산정 기간',['D07','D36'],'income_expense'),
 ('employer','근무처',['D08','D09'],'statement'),
 ('employment_type','근로·소득 유형',['D08','D15'],'application'),
 ('total_debt','채무 총액',['D38','D45'],'creditors'),
 ('creditors','채권자별 잔액·원금·이자·기준일',['D38','D45'],'creditors'),
 ('assets_total','재산 가액',['D27','D30','D37','D39'],'assets'),
 ('living_expenses','실제 월 생활지출',['D33','D36','D42'],'income_expense'),
 ('household_size','가구원 수·관계',['D03'],'application'),
 ('dependents','부양대상·소득·실제 부양',['D03','D41','D42'],'income_expense'),
 ('housing_cost','실제 월 주거비',['D33','D36'],'income_expense'),
 ('housing_deposit','임차보증금',['D33'],'assets'),
 ('business_revenue','사업 수입',['D21','D49'],'income_expense'),
 ('business_expenses','사업 필요경비',['D22','D49'],'income_expense'),
 ('repayment_date','제3자 완납일',['D38','D36'],'creditors'),
 ('payer','제3자 지급자',['D36','D46'],'statement'),
 ('fund_source','완납·송금 자금출처',['D36','D46'],'statement'),
 ('family_legal_relation','가족간 대여·증여·대위 관계',['D45','D46'],'creditors'),
 ('remaining_debt','완납 후 잔존 채무',['D38'],'creditors'),
 ('recent_borrowing','최근 차입일·금액',['D38','D44'],'statement'),
 ('fund_usage','대출·처분대금 최종 사용처',['D36','D44'],'statement'),
 ('disposal_proceeds','재산 처분일·대금·잔액',['D27','D31','D44'],'assets'),
 ('tax_arrears','세목·체납액·기준일',['D29','D40'],'creditors'),
 ('insurance_surrender','보험 해약환급금',['D39'],'assets'),
 ('prior_proceedings','과거 신청·종결·면책 이력',['D43'],'application'),
 ('service_date','실제 송달일 근거',['D48'],'correction'),
 ('account_scope','본인 계좌 전체 범위',['D35','D36'],'attachments'),
]


def _rule(id,title,keywords,docs,factors,targets,reason,refs,exclude=None):
    return {'id':id,'title':title,'condition':{'any_keywords':keywords,'exclude_keywords':exclude or [],'always':not keywords},'required_documents':docs,'required_factors':factors,'draft_targets':targets,'reason':reason,'source_refs':refs,'scope':'자료 준비·초안 구성 규칙','legal_determination':False,'version':1}


RULES=[
 _rule('WF01','기본 신원·계좌·채권 범위',[],['D01','D03','D35','D38'],['client_name','address','total_debt','creditors','account_scope'],['application','creditors','attachments'],'신원과 관할 후보, 빠진 계좌·채권 범위를 확인합니다.',['R01','R03','R29']),
 _rule('WF02','급여소득의 금액·기간·지속성',['급여','직장','월급','근로','회사원'],['D07','D08','D36'],['monthly_income','income_period','employer','employment_type'],['income_expense','statement'],'급여명세·실제 입금과 재직자료를 대조합니다.',['R05','R09']),
 _rule('WF03','사업소득의 수입과 경비 분리',['자영업','사업소득','사업자','프리랜서','가게 운영'],['D15','D21','D22','D49'],['business_revenue','business_expenses','monthly_income'],['income_expense','statement'],'매출을 순소득으로 간주하지 않고 수입과 필요경비를 분리합니다.',['R10','R25']),
 _rule('WF04','임차 주거비와 보증금',['월세','전세','임차','보증금'],['D33','D36'],['housing_cost','housing_deposit'],['assets','income_expense'],'계약상 금액과 실제 납부를 대조합니다. 법률상 인정액으로 자동 승격하지 않습니다.',['R11','R14']),
 _rule('WF05','가족·제3자의 채무 완납',['가족이 갚','가족이 대신','가족 완납','부모님이 갚','부모가 대신','대신 갚','완납'],['D38','D36','D45','D46'],['repayment_date','payer','fund_source','family_legal_relation','remaining_debt'],['creditors','statement'],'완납증명·지급자·송금원천·약정을 확인하고 잔존채무와 가족간 법률관계를 판단할 자료를 준비합니다.',['R18','R19','N027'],['완납한 적 없','대신 갚은 적 없']),
 _rule('WF06','최근 차입금 사용처',['최근 대출','최근에 대출','신규 대출','카드론','현금서비스','최근 차입'],['D38','D36','D44'],['recent_borrowing','fund_usage'],['statement','creditors'],'대출 실행에서 계좌 이동과 최종 사용처까지 증빙을 연결합니다.',['R18','N026']),
 _rule('WF07','처분 재산의 대금 추적',['매각','처분','팔았','매도','폐업'],['D27','D31','D44','D36'],['disposal_proceeds','fund_usage','assets_total'],['assets','statement'],'처분대금과 사용처를 확인하며 처분을 재산 소멸로 처리하지 않습니다.',['R18']),
 _rule('WF08','조세채무 및 과세 범위',['세금 체납','국세','지방세','체납'],['D29','D40'],['tax_arrears'],['creditors','attachments'],'세목·과세/미과세·조회 지역과 기간을 확인하고 조세채권을 별도로 표시합니다.',['R19','N028']),
 _rule('WF09','실제 부양관계',['자녀','부양','미성년','양육'],['D03'],['household_size','dependents'],['application','income_expense'],'가구원 수와 인정 부양가족을 구분할 자료를 준비합니다.',['R11']),
 _rule('WF10','의료·특별 지출',['치료','병원','질병','의료비','장애'],['D42'],['living_expenses','dependents'],['income_expense','statement'],'질병과 비용은 진료·납부 근거가 있는 범위에서만 서술합니다.',['R11','N011']),
 _rule('WF11','혼인 변경 및 재산관계',['이혼','별거','재혼','혼인'],['D04'],['household_size','assets_total'],['application','assets'],'혼인 및 재산관계를 확인하며 배우자 재산을 자동 합산하지 않습니다.',['R17']),
 _rule('WF12','보험 환급금',['보험','해약환급'],['D39'],['insurance_surrender'],['assets'],'보험료 납부액과 환급가능액을 구분합니다.',['R14']),
 _rule('WF13','과거 절차와 면책 이력',['과거 회생','회생 경험','파산 경험','면책','재신청'],['D43'],['prior_proceedings'],['application'],'접수·기각·종결·면책 확정일을 구분하며 자료 없이 재신청 가능성을 판정하지 않습니다.',['R20']),
 _rule('WF14','소득 공백의 추가 확인',['무직','실직','퇴사','소득이 없','소득 없음'],['D09','D24'],['monthly_income','employment_type'],['application','statement'],'자료 부재와 실제 소득 부재를 구분하고 절차 방향을 변호사에게 넘깁니다.',['R06','R13','R21']),
 _rule('WF15','자동차 재산',['자동차','차량','승용차'],['D30','D32'],['assets_total'],['assets'],'등록·소유·담보와 현재 시가를 확인합니다.',['R14']),
 _rule('WF16','본인 계좌 간 이체',['계좌이체','계좌 간','통장 이동','본인계좌'],['D35','D36'],['account_scope','fund_usage'],['income_expense','statement'],'같은 자금을 계좌 이동마다 소득으로 중복 합산하지 않습니다.',['R09','N025']),
 _rule('WF17','연금소득의 지급내역',['연금'],['D25'],['monthly_income','income_period'],['income_expense'],'지급기간과 실제 수급액을 대조합니다.',['R05']),
 _rule('WF18','보정 요구별 범위와 송달근거',['보정권고','보정명령','송달'],['D48'],['service_date'],['correction','attachments'],'요구별 대상·기간·설명·실제 송달 증거를 연결합니다.',['R26','N020'])
]


def definition():
    registry=json.loads((ROOT/'data/registry.json').read_text(encoding='utf-8'))
    catalog={d['id']:d for d in registry['documents']}
    factors=[{'key':k,'label':label,'evidence_catalog_ids':docs,'draft_target':target,'missing_policy':'미확인으로 표시; 초안 생성은 계속하고 변호사 질문에 추가'} for k,label,docs,target in FACTORS]
    rules=[]
    for r in RULES:
        docs=[{'catalog_id':d,'name':catalog[d]['name'],'reason':r['reason'],'period':'상담·서류에 기재된 기준기간 확인','factor_keys':[f['key'] for f in factors if d in f['evidence_catalog_ids'] and f['key'] in r['required_factors']]} for d in r['required_documents']]
        rules.append({**r,'required_documents':docs})
    return {'version':'workflow-v2','rules':rules,'factors':factors,'registry_factors':registry['factors'],'document_targets':{'application':'개인회생 신청서 초안','creditors':'채권자목록 초안','assets':'재산목록 초안','income_expense':'수입·지출목록 초안','statement':'진술서 초안','repayment_plan':'변제계획 검토 초안','attachments':'첨부서류목록','correction':'보정 답변 초안'},'limitations':['서류 준비 및 초안 구성용 실행 규칙입니다. 법률상 결론·부양 인정·변제액 확정 규칙이 아닙니다.','조건 키워드는 근거가 있는 후보를 찾는 용도이며 부정문·복잡한 문맥은 추가 확인합니다.']}


def sources(case):
    from .extraction_readiness import active_documents
    result=[]
    for d in active_documents(case):
        if d.get('text') and d.get('status') not in ('rejected','quarantined','superseded'):
            result.append({'source_id':'doc:'+d['id'],'document_id':d['id'],'text':d['text'],'source_type':d.get('source_type','case_document')})
    c=case.get('consultation',{})
    if c and c.get('status')!='quarantined':
        result.append({'source_id':'consultation','text':c.get('notes','')+'\n'+'\n'.join(c.get('answers',{}).values()),'source_type':'party_statement'})
    for m in case.get('messages',[]):
        if m.get('role')=='client' and m.get('status')!='quarantined':
            result.append({'source_id':'message:'+m['id'],'text':m['text'],'source_type':'party_statement'})
    if not result and case.get('summary'):
        result.append({'source_id':'case:summary','text':case['summary'],'source_type':'case_note'})
    return result


_NEGATIVE = re.compile(r'없(?:습니다|어요|다|음|고|으며|는|었|을|지만|으나|는데)|아니(?:다|고|며|라|에요|지만)|아닙니다|아님|안\s*함|하지\s*않|받지\s*않|해당\s*없|미해당')
_UNCERTAIN = re.compile(r'미확인|확인\s*(?:필요|예정|중)|확인해야|모르|모름|알\s*수\s*없|여부|있을\s*수|가능성|인지|있나요|있습니까')
_POSITIVE = re.compile(r'있(?:습니다|어요|다|음|고|으며|지만)|받(?:습니다|아요|고|으며)|발생(?:합니다|하고)|운영(?:합니다|하고)|입니다|합니다')
_DECLARED_INCOME = {
    'salary': {'salary', 'employee', 'wage', '급여소득자', '근로소득자'},
    'business': {'business', 'self_employed', 'freelancer', '영업소득자', '사업소득자', '자영업', '프리랜서'},
    'mixed': {'mixed', 'salary_and_business', '급여·사업소득', '근로·사업소득', '복합소득'},
}


def _trigger_context(text, match):
    """Keep a listed subject and its predicate together, without crossing sentences.

    A fixed character window misread '사업소득, 세금 체납, 과거 회생 이력은
    없습니다' as positive business income. Commas/list separators do not end a
    predicate's scope; a completed positive predicate or contrast does.
    This filters rule triggers only and does not create extraction facts.
    """
    left = max(text.rfind(token, 0, match.start()) for token in ('\n', '.', '!', '?', ';')) + 1
    endings = [index for token in ('\n', '.', '!', '?', ';')
               if (index := text.find(token, match.end())) >= 0]
    right = min(endings) if endings else len(text)
    sentence = text[left:right]
    after = text[match.end():right]
    # The second clause's absence must not negate the first clause's presence.
    after = re.split(r'하지만|그러나|반면|다만|그런데|(?<=지만)\s*', after, maxsplit=1)[0]
    uncertainty = _UNCERTAIN.search(after)
    negative = _NEGATIVE.search(after)
    positive = _POSITIVE.search(after)
    stop = min([m.start() for m in (uncertainty, negative) if m] or [len(after) + 1])
    if positive and positive.start() < stop:
        return sentence, 'positive'
    if uncertainty or (right < len(text) and text[right] == '?'):
        return sentence, 'uncertain'
    if negative:
        return sentence, 'negative'
    return sentence, 'positive'


def _rule_refs(rule, source_rows):
    refs, uncertain = [], False
    for source in source_rows:
        for word in rule['condition']['any_keywords']:
            for match in re.finditer(re.escape(word), source['text']):
                sentence, polarity = _trigger_context(source['text'], match)
                if any(x in sentence for x in rule['condition']['exclude_keywords']):
                    continue
                if polarity != 'positive':
                    uncertain |= polarity == 'uncertain'
                    continue
                tail = source['text'][match.end():]
                if rule['id'] in {'WF03', 'WF08', 'WF17'} and re.match(r'(?:[은는이가액금:：\s]|금액|수입|소득)*0\s*(?:원|만원|만\s*원)(?!\d)', tail):
                    continue
                if rule['id'] == 'WF03':
                    # An employer's registration number or a blank form title is
                    # not evidence that this debtor runs a business.
                    if word == '사업자' and re.match(r'\s*등록(?:번호|증|내역)', source['text'][match.end():]):
                        continue
                    if re.search(r'폐업|과거에|예전에|이전에', sentence) and not re.search(r'현재.*(?:사업|자영업|프리랜서)|다시\s*(?:사업|운영)', sentence):
                        continue
                    if source.get('document_id') and not re.search(r'(?:사업|영업|프리랜서|자영업).{0,25}(?:소득.{0,8}\d|운영|종사|수입.{0,8}\d)', sentence):
                        continue
                if rule['id'] == 'WF12' and word == '보험':
                    around = source['text'][max(0, match.start()-3):match.end()+1]
                    if re.search(r'(?:사회|건강|고용|산재|국민)보험', around):
                        continue
                if rule['id'] == 'WF08':
                    if word in {'국세', '지방세'} and not re.search(r'체납|미납', sentence):
                        continue
                    if source.get('document_id') and not re.search(r'(?:체납|미납)(?:액|금액|금)?\s*[:：은는이가]?\s*[1-9][\d,]*\s*(?:원|만)|(?:체납|미납).{0,12}(?:있|했|하였|중)', sentence):
                        continue
                if rule['id'] == 'WF17':
                    if re.search(r'퇴직\s*$',source['text'][max(0,match.start()-6):match.start()]):
                        # A retirement-plan balance/absence is not National
                        # Pension income and has its own preparation request.
                        continue
                    if re.match(r'\s*(?:미가입|가입\s*없|수급\s*없)',tail):
                        continue
                    if re.match(r'\s*(?:산정|가입|보험료|공제)', tail):
                        continue
                    if source.get('document_id') and not re.search(r'연금.{0,20}(?:수급|지급|수령|받|월\s*[1-9])', sentence):
                        continue
                if rule['id'] == 'WF15' and source.get('document_id'):
                    # An inventory certificate headed "자동차 소유 조회" may
                    # explicitly say no vehicle is owned. Its title is not a
                    # reason to request a valuation/registration of a car.
                    if not re.search(r'(?:자동차|차량|승용차).{0,30}(?:보유|소유|등록).{0,12}(?:있|1\s*대|[2-9]\s*대|본인|채무자)|(?:보유|소유)\s*(?:자동차|차량)\s*[:：]\s*(?!없|해당\s*없|미보유|0\s*대)[가-힣A-Za-z0-9]', sentence):
                        continue
                if rule['id'] == 'WF18' and word == '송달':
                    if not re.search(r'보정|명령|결정|법원|송달일',sentence):
                        # A requested service address is an application field,
                        # not evidence of an already-issued correction order.
                        continue
                quote = source['text'][max(0, match.start()-25):min(len(source['text']), match.end()+90)]
                refs.append({k:v for k,v in source.items() if k!='text'} | {'quote':quote,'keyword':word})
                return refs, uncertain
    return refs, uncertain


def income_profile(case, source_rows=None):
    """Income routing uses affirmative context, never a previously requested file."""
    source_rows = sources(case) if source_rows is None else source_rows
    declared = case.get('employment_type') or case.get('consultation', {}).get('employment_type', '')
    declared = str(declared).strip()
    result = {'salary': False, 'business': False, 'uncertain': False}
    for kind, rule_id in (('salary', 'WF02'), ('business', 'WF03')):
        rule = next(r for r in RULES if r['id'] == rule_id)
        refs, uncertain = _rule_refs(rule, source_rows)
        result[kind] = declared in _DECLARED_INCOME[kind] | _DECLARED_INCOME['mixed'] or bool(refs)
        result['uncertain'] |= uncertain and not result[kind]
    result['kind'] = ('mixed' if result['salary'] and result['business'] else
                      'salary' if result['salary'] else 'business' if result['business'] else 'unknown')
    return result


def evaluate_case(case):
    defs=definition()
    source_rows=sources(case)
    matched=[]
    from .extraction_readiness import active_documents
    current_documents=active_documents(case)
    excluded={d['id'] for d in case.get('documents',[])}-{d['id'] for d in current_documents}
    available={f['key'] for f in case.get('extraction_candidates',[]) if f.get('value') is not None and f.get('status') not in ('rejected','quarantined','superseded') and f.get('document_id') not in excluded}
    available|={f['key'] for f in case.get('facts',[]) if f.get('value') is not None and f.get('status')=='confirmed' and f.get('evidence_ids') and all(any(d['id']==eid and d.get('status')=='verified' for d in current_documents) for eid in f['evidence_ids'])}
    needs={}
    income = income_profile(case, source_rows)
    for rule in defs['rules']:
        condition=rule['condition']
        refs, _ = _rule_refs(rule, source_rows)
        declared_income_match = (rule['id'] == 'WF02' and income['salary']) or (rule['id'] == 'WF03' and income['business'])
        if not condition['always'] and not refs and not declared_income_match:
            continue
        row={**rule,'trigger_refs':refs,'missing_factors':[f for f in rule['required_factors'] if f not in available]}
        matched.append(row)
        for doc in rule['required_documents']:
            if doc['catalog_id'] not in needs:
                needs[doc['catalog_id']]={**doc,'rule_ids':[]}
            needs[doc['catalog_id']]['rule_ids'].append(rule['id'])
    questions = ([{'code':'INCOME_TYPE_UNCONFIRMED','factor_key':'employment_type',
                   'message':'세부상담에서 현재 급여·사업소득 및 겸업 여부를 확인해 주세요.'}]
                 if income['kind']=='unknown' or income['uncertain'] else [])
    return {'version':defs['version'],'matched_rules':matched,'required_documents':list(needs.values()),'missing_factors':sorted({f for r in matched for f in r['missing_factors']}),'factor_definitions':defs['factors'],'draft_targets':defs['document_targets'],'source_count':len(source_rows),'income_profile':income,'review_questions':questions}
