"""Deterministic workflow controls; no legal conclusions or amounts are delegated to an LLM."""
from datetime import datetime, timedelta, timezone
from .store import uid, now, digest


class DomainError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def require(condition, code, message):
    if not condition:
        raise DomainError(code, message)


def item(case, collection, item_id):
    found = next((x for x in case.get(collection, []) if x['id'] == item_id), None)
    require(found is not None, 'ITEM_NOT_FOUND', '이 사건에서 해당 항목을 찾지 못했습니다.')
    return found


def invalidate(case, reason):
    case['input_revision'] += 1
    if case.get('ax_pipeline'):
        case['ax_pipeline'].update(status='waiting', stage='collecting', stale=True, stale_reason=reason)
    for record in case.get('strategy_analyses', []):
        record.update(stale=True, stale_reason=reason)
    for key in ('calculations', 'bundles', 'approvals'):
        for value in case[key]:
            value['stale'] = True
            value['stale_reason'] = reason
    for issue in case['issues']:
        if issue['status'] == 'decided':
            issue['status'] = 'review_required'
            issue['stale_reason'] = reason
    for corr in case['corrections']:
        if corr['status'] in ('reviewed', 'approved'):
            corr['status'] = 'review_required'
            corr['stale_reason'] = reason
    for draft in case.get('drafts',[]):
        draft.update(stale=True,stale_reason=reason)
    for key in ('legal_calculations','court_documents'):
        for record in case.get(key,[]):
            record.update(stale=True,stale_reason=reason)
    for record in case.get('filing_packages', []):
        record.update(stale=True, stale_reason=reason)


def evidence(case, ids, verified=True):
    require(bool(ids), 'EVIDENCE_REQUIRED', '원문 근거를 한 개 이상 연결하세요.')
    docs = [item(case, 'documents', x) for x in ids]
    if verified:
        require(all(d['status'] == 'verified' for d in docs), 'EVIDENCE_UNVERIFIED', '원문 인물·범위·내용 검증이 먼저 필요합니다.')
    return docs


def blockers(case):
    result = []
    doc_index = {d['id']:d for d in case['documents']}
    for f in case['facts']:
        valid_evidence = bool(f.get('evidence_ids')) and all(doc_index.get(x,{}).get('status')=='verified' for x in f.get('evidence_ids',[]))
        if f['status'] != 'confirmed' or f['value'] is None or not valid_evidence:
            result.append({'code': 'FACT_UNKNOWN', 'title': f"{f['label']} 확인 필요", 'target': 'verify', 'id': f['id']})
    for i in case['issues']:
        valid_evidence = bool(i.get('evidence_ids')) and all(doc_index.get(x,{}).get('status')=='verified' for x in i.get('evidence_ids',[]))
        if i['status'] != 'decided' or not valid_evidence:
            result.append({'code': 'LEGAL_ISSUE_OPEN', 'title': i['title'], 'target': 'issues', 'id': i['id']})
    for r in case['requests']:
        if r['status'] not in ('fulfilled', 'withdrawn', 'superseded', 'cancelled'):
            result.append({'code': 'DOCUMENT_REQUIRED', 'title': f"{r['title']} · {r['status']}", 'target': 'documents', 'id': r['id']})
    return result


def deadline_alerts(case):
    today = datetime.now(timezone(timedelta(hours=9))).date()
    result = []
    for d in case['deadlines']:
        if not d.get('due_date'):
            result.append({**d, 'severity': 'urgent', 'message': '실제 송달 증거와 기한 확인 필요'})
        else:
            days = (datetime.fromisoformat(d['due_date']).date() - today).days
            result.append({**d, 'days_remaining': days, 'severity': 'overdue' if days < 0 else 'urgent' if days <= 3 else 'normal'})
    return result


def confirm_fact(case, fact_id, data):
    evidence(case, data['evidence_ids'])
    require(data.get('reason', '').strip(), 'REASON_REQUIRED', '원문 대조 결과와 확인 이유를 남기세요.')
    require(data.get('value') is not None, 'UNKNOWN_VALUE', '미확인은 0원으로 확정할 수 없습니다.')
    fact = item(case, 'facts', fact_id)
    require(isinstance(data['value'], int) and not isinstance(data['value'], bool) and 0 <= data['value'] <= 10**13, 'INVALID_AMOUNT', '금액은 0 이상의 정수 원 단위여야 합니다.')
    invalidate(case, f"{fact['label']} 변경")
    fact.update(value=data['value'], evidence_ids=data['evidence_ids'], reason=data['reason'], status='confirmed', confirmed_at=now())


def decide(case, issue_id, data, user):
    require(user['role'] == 'lawyer', 'LAWYER_ONLY', '담당 변호사의 판단이 필요합니다.')
    require(len(data.get('reason', '').strip()) >= 5, 'REASON_REQUIRED', '쟁점별 판단 이유를 5자 이상 기록하세요.')
    require(data.get('decision', '').strip(), 'DECISION_REQUIRED', '채택할 판단을 기록하세요.')
    evidence(case, data['evidence_ids'])
    issue = item(case, 'issues', issue_id)
    # Changing any legal input invalidates dependent outputs, but not unrelated decisions.
    case['input_revision'] += 1
    for key in ('calculations', 'bundles', 'approvals'):
        for value in case[key]:
            value.update(stale=True, stale_reason='법률 판단 변경')
    for key in ('legal_calculations', 'court_documents', 'drafts', 'strategy_analyses', 'filing_packages'):
        for value in case.get(key, []):
            value.update(stale=True, stale_reason='법률 판단 변경')
    if case.get('ax_pipeline'):
        case['ax_pipeline'].update(stage='collecting',status='waiting',stale=True)
    issue.update(status='decided', decision=data['decision'], reason=data['reason'], evidence_ids=data['evidence_ids'], decided_by=user['name'], decided_at=now())


def calculate(case, data):
    mode = data.get('mode', 'scenario')
    require(mode in ('scenario', 'operational'), 'INVALID_MODE', '계산 모드가 올바르지 않습니다.')
    if mode == 'operational':
        require(not blockers(case), 'UNRESOLVED_INPUTS', '미확정 사실·자료·법률 쟁점부터 해결하세요.')
        raise DomainError('LEGAL_RULESET_NOT_APPROVED', '관할·시행일·배분·비용·단수처리 정답 검증을 마친 법률 계산 규칙이 없습니다. 현재는 가정안만 계산합니다.')
    for key in ('income', 'expenses', 'months', 'liquidation_value'):
        require(isinstance(data.get(key), int) and not isinstance(data.get(key), bool), 'UNKNOWN_VALUE', '입력값은 정수여야 하며 빈칸을 0으로 취급하지 않습니다.')
        require(0 <= data[key] <= 10**13, 'INVALID_AMOUNT', '금액은 0 이상이어야 합니다.')
    require(1 <= data['months'] <= 120, 'INVALID_MONTHS', '시나리오 기간은 1~120개월로 입력하세요. 법률상 허용기간 판정이 아닙니다.')
    available = data['income'] - data['expenses']
    total = max(available, 0) * data['months']
    result = {'id': uid('calc'), 'mode': mode, 'input_revision': case['input_revision'], 'inputs': {k:data[k] for k in ('income','expenses','months','liquidation_value')}, 'monthly_available': available, 'total': total, 'liquidation_gap': max(0, data['liquidation_value'] - total), 'stale': False, 'created_at': now(), 'code_version': 'cashflow-v1', 'warnings': ['가정 현금흐름입니다. 법원 변제액·채권 배분·인가 가능성 계산이 아닙니다.']}
    if available < 0:
        result['warnings'].append('월 현금흐름이 부족합니다. 수행가능성 검토가 필요합니다.')
    case['calculations'].append(result)


def make_bundle(case):
    snapshot = {k:case[k] for k in ('facts','issues','requests','calculations','corrections')}
    case['bundles'].append({'id':uid('bundle'), 'title':'사건 검증 보고 및 보정 검토 패키지', 'kind':'review_only', 'status':'draft', 'input_revision':case['input_revision'], 'snapshot_hash':digest(snapshot), 'snapshot':snapshot, 'created_at':now(), 'stale':False, 'documents':['사건 검증 보고서','근거·첨부 목록','보정 요구 충족표','현금흐름 가정안'], 'limitations':['검토용 출력입니다. 승인된 법원 제출 서식 및 법률 계산 정답셋은 미확보입니다.']})


def approve_bundle(case, bundle_id, data, user):
    require(user['role'] == 'lawyer', 'LAWYER_ONLY', '담당 변호사만 승인할 수 있습니다.')
    bundle = item(case, 'bundles', bundle_id)
    require(not bundle['stale'] and bundle['input_revision'] == case['input_revision'], 'STALE_APPROVAL', '입력이 변경되었습니다. 문서를 다시 생성하세요.')
    require(not blockers(case), 'UNRESOLVED_INPUTS', '미결 쟁점과 미확인 사실·요구자료를 먼저 검토하세요.')
    require(len(data.get('reason','').strip()) >= 5, 'REASON_REQUIRED', '승인 이유가 필요합니다.')
    # Review approval never becomes a legal filing approval.
    bundle.update(status='review_approved', approved_by=user['name'], approved_at=now())
    case['approvals'].append({'id':uid('approval'), 'gate':'H-R', 'bundle_id':bundle_id, 'input_revision':case['input_revision'], 'hash':bundle['snapshot_hash'], 'reason':data['reason'], 'actor':user['name'], 'at':now(), 'stale':False, 'scope':'검토 보고서 승인 · 법원 제출 승인 아님'})


def new_case(name, court_id, court_name, summary, synthetic=False):
    return {'id':uid('case'), 'org_id':'office-1', 'version':1, 'input_revision':1, 'title':f'{name} · 개인회생', 'client_name':name, 'court_id':court_id, 'court_name':court_name, 'summary':summary, 'synthetic':synthetic, 'assigned_to':'담당 직원', 'members':['staff','lawyer'], 'client_user_id':None, 'stage':'자료 수집', 'court_case_number':None, 'created_at':now(), 'updated_at':now(), 'documents':[], 'requests':[], 'facts':[{'id':'income','key':'monthly_income','label':'월 소득','value':None,'status':'unknown','evidence_ids':[],'reason':''},{'id':'debt','key':'total_debt','label':'채무 총액','value':None,'status':'unknown','evidence_ids':[],'reason':''}], 'issues':[], 'calculations':[], 'bundles':[], 'corrections':[], 'deadlines':[], 'messages':[], 'approvals':[], 'submissions':[], 'audit':[]}


def request_document(case, catalog, period, reason):
    require(period.strip() and reason.strip(), 'SCOPE_REQUIRED', '요청 기간·범위와 사유가 필요합니다.')
    case['requests'].append({'id':uid('req'), 'version':1, 'catalog_id':catalog['id'], 'title':catalog['name'], 'issuer':catalog['issuer'], 'period':period, 'reason':reason, 'status':'requested', 'issuance_url':catalog['issuance_url'], 'steps':catalog['steps'], 'not_proven':catalog['not_proven'], 'document_ids':[], 'due_date':None})


def seed_cases(registry):
    first = new_case('김예시', 'CT01', '서울회생법원', '급여소득자 합성 사례. 소득 진술 불일치와 가족의 카드 완납 회신을 검토합니다.', True)
    first['id'] = 'demo-001'
    first['client_user_id'] = 'client'
    first['facts'][0]['claimed_value'] = 2600000
    first['issues'] = [
        {'id':'issue-income','title':'상담 소득과 급여 증빙 불일치','description':'상담 진술 260만원과 수신 자료의 280만원 후보값을 원문·기간·세전/세후 기준으로 비교해야 합니다.','status':'open','evidence_ids':[],'counter_evidence':['고객 진술과 증빙 후보가 다름'],'options':['기간 및 급여 변동 확인','근로계약·급여 입금 대조'],'decision':None,'reason':''},
        {'id':'issue-paid','title':'가족이 완납한 카드 채무의 법률관계','description':'완납 회신만으로 채권을 제외하지 않습니다. 지급자·자금출처·대위·가족간 채무 여부를 검토하세요.','status':'open','evidence_ids':[],'counter_evidence':['완납증명 및 송금 원문 미확보'],'options':['완납증명과 송금증빙 요청','최근 변제 및 대위 관계 검토'],'decision':None,'reason':''}]
    for catalog in registry['documents']:
        if catalog['id'] in ('D01','D03','D05'):
            request_document(first, catalog, '직원과 확인할 사건별 기간', '신원·관계·소득 확인을 위한 자료 요청 후보')
    first['deadlines']=[{'id':'deadline-demo','title':'보정권고 송달일 및 만료일 확인','due_date':None,'kind':'legal','status':'unconfirmed','evidence_id':None,'assigned_to':'담당 직원','backup':'담당 변호사'}]
    first['corrections']=[{'id':'corr-demo','round':1,'title':'최근 거래의 사용처 소명','requirement':'법원 원문에서 요구한 계좌·기간·거래 범위를 확인하고, 사용처와 증빙을 연결합니다.','period':'원문 대조 후 확정','source_document_id':None,'source_page':1,'source_type':'synthetic_example','status':'open','answer':'','evidence_ids':[],'due_date':None,'limitations':'합성 예시. 참조 보정권고의 특정 금액·요율은 적용하지 않음.'}]
    first['messages']=[{'id':uid('msg'),'text':'필요 자료를 올려주시면 담당자가 기간과 내용을 확인합니다. 가족이 갚아준 채무는 관련 증빙도 함께 알려주세요.','sender':'담당 직원','role':'staff','created_at':now()}]
    second = new_case('이예시', 'CT02', '수원회생법원', '상담 기록과 자료 범위를 준비 중인 합성 사례입니다.', True)
    second['id']='demo-002'
    return [first, second]
