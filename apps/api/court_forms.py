"""Fill version-pinned public court PDF forms without reconstructing their layout.

This is a draft renderer. It never signs declarations, selects consent boxes, or
asserts that a court will accept the snapshot or the selected jurisdiction.
"""
from __future__ import annotations

from copy import deepcopy
import calendar
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import hashlib
from functools import lru_cache
import json
import os
from pathlib import Path
import re

import pymupdf

ROOT=Path(__file__).resolve().parents[2]
DATA=ROOT/'data'/'court_forms'
SOURCES=json.loads((DATA/'sources.json').read_text(encoding='utf-8'))
RENDERER_VERSION='court-evidence-fields-v4'
# Original national forms use a subset of 휴먼명조 and Type3 glyphs. A subset
# cannot render arbitrary new names. Prefer a locally licensed full face;
# HMFMOLD.TTF is 휴먼옛체 (Yet R), not 휴먼명조, and is intentionally excluded.
FONT_CANDIDATES=[os.environ.get('DEBTOFF_KOREAN_FONT',''), 'C:/Windows/Fonts/HMKMM.TTF',
    'C:/Windows/Fonts/HANBatang.ttf','C:/Windows/Fonts/batang.ttc',
    '/usr/share/fonts/truetype/nanum/NanumMyeongjo.ttf',
    '/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc']
CALC_KEYS={'months','monthly_deposit','base_living_cost','recognized_living_cost','monthly_disposable_income','monthly_trustee_fee','monthly_creditor_capacity','total_creditor_payment','liquidation_value','present_value','median_income','median_percent','allocation_principal_total','allocation_monthly_total','principal_repayment_percent'}


def _field(key,label,page,rect,required=False,size=9):
    return {'key':key,'label':label,'page':page,'rect':rect,'required':required,'font_size':size}


def _definitions():
    f=_field
    defs={
      'D5100':{'title':'개인회생절차 개시신청서','pages':[0,1], 'fields':[
        f('client_name','신청인 성명',0,[180,154,307,172],True),f('resident_id','주민등록번호',0,[396,154,522,172],True),
        f('registered_address','주민등록상 주소',0,[180,176,438,194],True),f('address','현주소',0,[180,198,438,216],True),
        f('service_address','송달장소',0,[180,220,330,238]),f('phone','휴대전화',0,[396,242,522,260],True),
        f('lawyer_name','대리인 성명',0,[185,279,522,297]),f('lawyer_address','대리인 주소',0,[185,300,435,318]),
        f('lawyer_phone','대리인 전화',0,[185,322,522,342]),f('lawyer_email','대리인 이메일',0,[185,346,314,362]),
        f('months','변제기간(개월)',0,[370,778,400,796],True,10),f('monthly_deposit','월 변제예정액',0,[453,778,488,796],True,8),
        f('refund_bank','환급은행',1,[427,181,514,198]),f('refund_account','환급계좌',1,[68,201,179,218]),
        f('court_prefix','제출법원(법원 앞 부분)',1,[350,718,471,737],True,12)]},
      'D5101':{'title':'재산목록','pages':[0], 'fields':[
        f('cash','현금',0,[157,202,218,221]),f('bank_balance','예금 총액',0,[157,225,218,264],True),
        f('bank_name','금융기관명',0,[336,225,359.5,241]),f('bank_account','계좌번호',0,[318,246,387,259],False,7),
        f('insurance_surrender','보험해약환급금',0,[157,272,218,311],True),f('insurance_name','보험회사',0,[336,272,361,289],False,7),
        f('vehicle_value','자동차 가액',0,[157,319,218,340]),f('housing_deposit','임차보증금 반환예상액',0,[157,348,218,406],True),
        f('address','임차물건',0,[318,347,537,362],True,7),f('lease_terms','보증금 및 월세',0,[318,364,537,385],False,8),
        f('real_estate_value','부동산 순가액',0,[157,413,218,498]),f('retirement_value','예상퇴직금 가액',0,[157,655,218,674]),
        f('assets_total','재산 합계',0,[157,706,218,727],True),f('exempt_value','면제재산 신청금액',0,[157,734,218,753]),
        f('liquidation_value','청산가치',0,[157,760,218,780],True)]},
      'D5103':{'title':'수입 및 지출에 관한 목록','pages':[0,1], 'fields':[
        f('employer','직장명',0,[362,212,530,230],True),f('job_title','직위',0,[362,236,530,254]),
        f('income_label','수입 명목',0,[60,310,112,333]),f('income_period_label','수입 기간구분',0,[118,310,184,333]),
        f('monthly_income','월 수입 금액',0,[190,310,269,333],True),f('annual_income','연간 환산 금액',0,[275,310,355,333]),
        f('income_seizure','압류·가압류 여부',0,[362,310,530,333]),f('annual_income','연 수입',0,[275,394,355,412]),
        f('monthly_income','월평균 수입',0,[456,394,503,412],True,8),f('household_size','생계비 가구수',0,[155,479,175,497],True),
        f('median_income','기준 중위소득',0,[307,479,350,497],False,7),f('median_percent','중위소득 적용비율',0,[417,479,438,497]),
        f('recognized_living_cost','예상 생계비',0,[477,479,522,497],True,7),
        f('additional_cost_reason','추가생계비 사유',1,[63,487,523,766],False,11)]},
      'D5105':{'title':'진술서','pages':[0,1,2], 'fields':[
        f('employer','최근 직장명',0,[247,269,334,287]),f('job_title','최근 직위',0,[416,269,518,287]),
        f('housing_deposit','임차보증금',0,[321,604,399,622]),f('housing_cost','월 임대료',0,[314,623,359,641],False,8),
        f('tenant_name','임차인 성명',0,[326,641,415,659]),
        f('statement','부채 경위 및 신청에 이른 사정',1,[62,470,528,745],True,11),
        f('prior_proceedings','과거 절차 상세',2,[61,365,525,640],True,10)]},
      'D5110':{'title':'변제계획안(가용소득만으로 변제)','pages':list(range(7)), 'fields':[
        f('client_name','채무자 성명',0,[161,334,350,353],True,12),
        f('case_year_suffix','사건번호 연도(20 이후)',0,[173,305,192,322]),f('case_sequence','사건 일련번호',0,[220,305,259,322]),
        f('lawyer_name','대리인 성명',0,[161,364,350,383],False,12),f('court_prefix','제출법원',0,[350,672,474,692],True,12),
        f('client_name','채무자 성명',1,[210,109,296,127],True),f('employer','급여 지급처',1,[192,327,280,345],True),
        f('start_year','변제 시작 연도',1,[78,224,108,242],True,8),f('start_month','변제 시작 월',1,[135,224,148,242],True,8),
        f('start_day','변제 시작 일',1,[176,224,194,242],True,8),f('end_year','변제 종료 연도',1,[244,224,274,242],True,8),
        f('end_month','변제 종료 월',1,[300,224,313,242],True,8),f('end_day','변제 종료 일',1,[340,224,358,242],True,8),
        f('months','변제기간 개월',1,[408,224,426,242],True,8),
        f('monthly_income','월평균 수입',1,[422,327,472,345],True,8),f('household_size','채무자·피부양자 수',1,[255,386,294,404],True),
        f('base_living_cost','기준 중위소득 60%',1,[437,406,500,424],True,8),f('recognized_living_cost','조정된 생계비',1,[442,425,511,443],True,8),
        f('monthly_income','월평균 수입',1,[60,525,123,549],True,8),f('recognized_living_cost','월평균 생계비',1,[128,525,189,549],True,8),
        f('monthly_disposable_income','월평균 가용소득',1,[196,525,258,549],True,8),f('monthly_trustee_fee','회생위원 보수',1,[264,525,324,549],True,8),
        f('monthly_creditor_capacity','월 실제 가용소득',1,[329,525,391,549],True,8),f('months','변제횟수',1,[398,525,458,549],True),
        f('total_creditor_payment','총 실제 가용소득',1,[464,525,525,549],True,8),
        f('monthly_creditor_capacity','월 변제예정액',3,[393,287,446,305],True,9),f('total_creditor_payment','총 변제예정액',3,[168,307,232,325],True,10),
        f('months','변제개월',3,[450,406,468,424],True),f('months','변제횟수',3,[145,426,163,444],True),
        f('client_name','채무자 성명',6,[212,109,274,127],True),
        f('monthly_disposable_income','월 가용소득',6,[139,189,207,225],True),f('monthly_trustee_fee','월 보수',6,[307,189,368,225],True),
        f('monthly_creditor_capacity','월 실제 가용소득',6,[138,239,207,284],True),f('months','변제횟수',6,[299,239,326,284],True),
        f('total_creditor_payment','총 실제 가용소득',6,[462,239,533,284],True),
        f('allocation_principal_total','확정 원금 합계',6,[153,593,211,617],True,7),
        f('allocation_monthly_total','월 변제예정액 합계',6,[282,593,341,617],True,7),
        f('total_creditor_payment','총 변제예정액 합계',6,[411,593,469,617],True,7),
        f('allocation_principal_total','채권 원금 총계 G',6,[179,626,273,651],True,8),
        f('allocation_monthly_total','월 변제예정액 총계 H',6,[304,626,404,651],True,8),
        f('total_creditor_payment','총 변제예정액 총계 I',6,[431,626,531,651],True,8),
        f('principal_repayment_percent','원금 변제율(소수 둘째자리 반올림)',6,[156,660,176,676],True,6),
        f('liquidation_value','청산가치',6,[101,724,171,773],True,8),f('total_creditor_payment','총 변제예정액',6,[284,718,360,747],True,8),
        f('present_value','총 변제액 현재가치',6,[284,751,360,777],True,8)]},
      'D5115':{'title':'소득증명서(급여소득자용)','pages':[0], 'fields':[
        f('client_name','성명',0,[136,180,277,220],True),f('resident_id','주민등록번호',0,[373,180,520,220],True),
        f('address','주소',0,[136,229,520,264],True),f('employer','직장명',0,[136,271,280,311],True),
        f('employer_phone','직장전화',0,[373,271,520,311]),f('employer_address','직장주소',0,[136,321,520,348],True),
        f('employment_period','근무기간',0,[136,357,520,384],True),f('income_manwon','월평균 소득(만원)',0,[365,421,421,443],True)]},
      'D5106':{'title':'개인회생채권자목록','pages':[0,1], 'fields':[
        f('total_debt','채권현재액 총합계',0,[152,181,216,203],True,8),f('principal_total','원금합계',0,[152,209,216,222],True,8),
        f('interest_total','이자합계',0,[152,226,216,240],True,8),f('secured_debt','담보부채권액 합계',0,[295,181,359,240]),
        f('unsecured_debt','무담보채권액 합계',0,[438,181,520,240],True,8)]},
      'BUSAN-ATTACHMENTS':{'title':'부산회생법원 자료제출목록 (2026-07-01 시행)','pages':[0,1,2,3], 'court_ids':['CT03'],'fields':[]},
    }
    # The first two D5106 pages are blank creditor rows. Pages 3+ contain instructions
    # or worked examples, so they are never included in generated applications.
    for i in range(4):
        y=351+i*109.5
        defs['D5106']['fields'] += [f(f'creditors.{i}.number',f'채권자{i+1} 번호',0,[70,y+3,86,y+25]),
          f(f'creditors.{i}.name',f'채권자{i+1} 명칭',0,[90,y+3,124,y+106],i==0,8),
          f(f'creditors.{i}.cause',f'채권자{i+1} 발생원인',0,[130,y+2,281,y+30]),
          f(f'creditors.{i}.address',f'채권자{i+1} 주소',0,[314,y+2,516,y+15],False,7),
          f(f'creditors.{i}.principal',f'채권자{i+1} 원금',0,[131,y+67,207,y+85],i==0,8),
          f(f'creditors.{i}.interest',f'채권자{i+1} 이자',0,[131,y+90,207,y+105],False,8),
          # The dotted separator lies between principal and interest rows. Keep
          # this single-line basis wholly inside the upper row, not centred on it.
          f(f'creditors.{i}.basis',f'채권자{i+1} 산정근거',0,[214,y+66,514,y+84],False,8)]
    for i in range(5):
        y=420+i*34
        defs['D5110']['fields'] += [f(f'allocations.{i}.number',f'배분{i+1} 채권번호',6,[60,y,80,y+27],False,8),
          f(f'allocations.{i}.name',f'배분{i+1} 채권자',6,[85,y,147,y+27],False,8),
          f(f'allocations.{i}.principal',f'배분{i+1} 확정원금',6,[152,y,211,y+27],False,7),
          f(f'allocations.{i}.monthly_payment',f'배분{i+1} 월예정액',6,[281,y,341,y+27],False,7),
          f(f'allocations.{i}.total_payment',f'배분{i+1} 총예정액',6,[410,y,469,y+27],False,7)]
    for key,value in defs.items():
        source=next(s for s in SOURCES if s['id']==key+'-pdf')
        value.update(id=key,format='pdf_overlay',source=source,court_ids=value.get('court_ids',[]),layout='original_pdf_preserved',version=source['sha256'][:16],required_manual_actions=['서명·날인','법률상 진술 및 동의 체크','접수일 현재 서식·관할·추가 요구 확인'])
        for field in value['fields']:
            field['read_only']=field['key'] in CALC_KEYS or field['key'].startswith('allocations.') or field['key'].endswith('.number')
            field['source_kind']='approved_calculation' if field['read_only'] else 'evidence_or_staff'
    return defs


TEMPLATES=_definitions()


def catalog(court_id=None):
    registry=json.loads((ROOT/'data'/'registry.json').read_text(encoding='utf-8'))
    courts=[{'id':c['id'],'name':c['name'],'coverage':'national_common_with_local_review',
      'common_templates':[k for k,v in TEMPLATES.items() if not v['court_ids']],
      'local_templates':[k for k,v in TEMPLATES.items() if c['id'] in v['court_ids']],
      'local_rule_sources':c.get('source_urls','').splitlines(),
      'independently_verified_local_forms':c['id']=='CT03'} for c in registry['courts']]
    return {'templates':[deepcopy(v) for v in TEMPLATES.values() if not court_id or not v['court_ids'] or court_id in v['court_ids']],
      'courts':courts,'coverage':{'national_common_templates':7,'mapped_courts':len(courts),'local_pdf_templates':1,'original_files':len(SOURCES),
        'note':'15개 법원에는 대법원 공통서식을 연결했습니다. 부산 자료제출목록은 별도 원본입니다. 법원별 최신 접수요건 승인과 동일하지 않습니다.'},'sources':SOURCES}


def original_path(template_id):
    template=TEMPLATES.get(template_id)
    if not template:raise ValueError('등록되지 않은 서식입니다.')
    path=ROOT/template['source']['path']
    if hashlib.sha256(path.read_bytes()).hexdigest()!=template['source']['sha256']:raise ValueError('원본 서식 해시가 달라졌습니다. 새 버전 매핑이 필요합니다.')
    return path


def _flatten(value,prefix=''):
    result={}
    if isinstance(value,dict):
        for key,item in value.items():result.update(_flatten(item,f'{prefix}.{key}' if prefix else str(key)))
    elif isinstance(value,list):
        for key,item in enumerate(value):result.update(_flatten(item,f'{prefix}.{key}'))
    else:result[prefix]=value
    return result


def _values(case,fields,calculation):
    values={};origins={};confirmed=set()
    from . import evidence_mapping
    packet=evidence_mapping.build(case)
    excluded={d.get('id') for d in case.get('documents',[]) if d.get('status') in ('rejected','quarantined','superseded') or d.get('automated_check',{}).get('coverage_status')=='identity_conflict'}
    for c in case.get('extraction_candidates',[]):
        if c.get('status') in ('rejected','quarantined','superseded') or c.get('document_id') in excluded:continue
        if c.get('value') is not None:
            values[c['key']]=c['value'];origins[c['key']]={'type':'extraction','id':c.get('id'),'status':c.get('status','candidate'),'document_id':c.get('document_id')}
    for fact in case.get('facts',[]):
        if fact.get('status')=='confirmed' and fact.get('value') is not None:
            values[fact['key']]=fact['value'];origins[fact['key']]={'type':'confirmed_fact','id':fact.get('id'),'status':'confirmed'};confirmed.add(fact['key'])
    # Original typed observations and reproducible arithmetic remain useful
    # when a semantic model is unavailable. This never authorizes legal inputs.
    for key,value in packet['form_values'].items():
        if key not in confirmed:
            values[key]=value;origins[key]=deepcopy(packet['origins'][key])
    for key,value in {'client_name':case.get('client_name'),'court_prefix':re.sub('법원$','',case.get('court_name',''))}.items():
        if value and key not in values:values[key]=value;origins[key]={'type':'case_record','status':'candidate'}
    # Current residence does not establish the registered address. Only a
    # separately typed resident-register observation or deliberate staff input
    # may fill that field; 등록기준지 is a different fact again.
    # Consultation is source material for local authorship, never PDF body text.
    # The manual D5105 action prepares and independently verifies a statement
    # when no current verified narrative is available.
    values.pop('statement',None);origins.pop('statement',None)
    confirmed.discard('statement')
    from .statement_authoring import current_verified_draft
    current_draft = current_verified_draft(case, require_semantic=False) if case.get('drafts') else None
    if current_draft:
        written = next((section for section in current_draft['sections']
                        if section['id'] == 'statement' and section.get('grounded_paragraphs')), None)
        if written:
            values['statement'] = written['content']
            origins['statement'] = {'type': 'grounded_narrative', 'id': current_draft['id'],
                'status': 'automatically_verified' if current_draft.get('ai_review',{}).get('passed') else 'semantic_review_required', 'source_ids': list(dict.fromkeys(
                    ref for paragraph in written['grounded_paragraphs'] for ref in paragraph['source_ids']))}
    if case.get('statement_runs'):
        from .statement_authoring import current_run
        statement_run=current_run(case)
        if statement_run:
            values['statement']=statement_run['statement']
            origins['statement']={'type':'grounded_narrative','id':statement_run['id'],
                'status':'automatically_verified','source_ids':list(dict.fromkeys(
                    ref for paragraph in statement_run['paragraphs'] for ref in paragraph['source_ids']))}
    explicit=_flatten(fields or {})
    for key,value in explicit.items():
        if key in CALC_KEYS or key in confirmed or key.startswith('allocations.'):continue
        values[key]=value;origins[key]={'type':'staff_input','status':'review_required'}
    # A candidate cannot supply legally computed columns; only a pinned approved run.
    for key in CALC_KEYS:values.pop(key,None)
    if not calculation and packet['facts']:
        for proposal in packet['proposed_decisions']:
            if proposal['key'] in CALC_KEYS:
                values[proposal['key']]=proposal['value']
                origins[proposal['key']]={'type':'proposed_legal_input','status':'review_required',
                    'reason':proposal['reason'],'source_ids':proposal['source_ids']}
    if calculation:
        manual = calculation.get('status') == 'approved' and bool(calculation.get('approval'))
        automatic = calculation.get('status') == 'ready_for_review' and calculation.get('auto_preparation', {}).get('passed') is True
        if automatic:
            from .legal_calculator import calculate_legal
            fresh = calculate_legal(case, calculation.get('inputs'))
            automatic = fresh['status'] == 'ready_for_review' and all(
                fresh.get(k) == calculation.get(k) == calculation['auto_preparation'].get(k)
                for k in ('input_hash', 'policy_hash', 'result_hash')) and all(
                fresh.get(k) == calculation.get(k) for k in ('summary', 'schedule', 'creditor_allocations'))
        if not (manual or automatic) or calculation.get('stale') or calculation.get('input_revision')!=case.get('input_revision'):
            raise ValueError('현재 근거의 계산 검증 또는 변호사 계산 검토가 필요합니다.')
        calculation_origin = 'automatically_verified' if automatic else 'approved'
        summary=calculation.get('summary',{})
        inputs=calculation.get('inputs',{})
        values.update(summary)
        values['recognized_living_cost']=summary.get('base_living_cost',0)+summary.get('additional_living_cost',0)
        values['monthly_income']=summary.get('net_monthly_income')
        values['household_size']=inputs.get('recognized_household_size')
        values['months']=inputs.get('months',summary.get('months'))
        values['total_debt']=summary.get('unsecured_debt',0)+summary.get('secured_debt',0)
        values['median_percent']=60 if inputs.get('living_cost_mode')=='seoul_median_60' else None
        values['allocation_principal_total']=summary.get('principal_total')
        principal=summary.get('principal_total')
        paid=summary.get('total_principal_payment')
        if principal and paid is not None:
            values['principal_repayment_percent']=format((Decimal(paid)*100/Decimal(principal)).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP),'f')
        payments=[step.get('creditor_payment',0) for step in calculation.get('schedule',[])]
        values['allocation_monthly_total']=payments[0] if payments and len(set(payments))==1 else '회차별 별지' if payments else None
        for key in summary:origins[key]={'type':'legal_calculation','id':calculation.get('id'),'status':calculation.get('status','review_required')}
        for key in CALC_KEYS|{'monthly_income','household_size','total_debt'}:
            if key in values:origins[key]={'type':'legal_calculation','id':calculation.get('id'),'status':calculation_origin}
        for i,row in enumerate(calculation.get('creditor_allocations',[])):
            for key,value in row.items():values[f'allocations.{i}.{key}']=value
            values[f'allocations.{i}.number']=i+1
            scheduled=[next((v.get('total',0) for v in step.get('allocations',[]) if v.get('creditor_id')==row.get('creditor_id')),0) for step in calculation.get('schedule',[])]
            values[f'allocations.{i}.monthly_payment']=scheduled[0] if scheduled and len(set(scheduled))==1 else '회차별 별지'
            for key in list(values):
                if key.startswith(f'allocations.{i}.'):origins[key]={'type':'legal_calculation','id':calculation.get('id'),'status':calculation_origin}
        for i,row in enumerate(inputs.get('creditors',[])):
            for key,value in row.items():values[f'creditors.{i}.{key}']=value
            for key in row:origins[f'creditors.{i}.{key}']={'type':'legal_calculation_input','id':calculation.get('id'),'status':calculation_origin}
    for key,value in list(values.items()):
        if isinstance(value,(dict,list)):
            values.update(_flatten(value,key))
    for key in list(values):
        match=re.fullmatch(r'creditors\.(\d+)\.name',key)
        if match and values[key]:
            number_key=f'creditors.{match[1]}.number';values[number_key]=int(match[1])+1
            origins[number_key]={'type':'creditor_list_order','status':calculation_origin if calculation else 'review_required'}
    if values.get('monthly_income') is not None:
        try:values.setdefault('annual_income',int(values['monthly_income'])*12);values.setdefault('income_manwon',int(values['monthly_income'])/10000)
        except (ValueError,TypeError):pass
    values.setdefault('income_label','급여' if values.get('employer') else None)
    values.setdefault('income_period_label','월평균' if values.get('monthly_income') else None)
    if values.get('housing_deposit') is not None and values.get('housing_cost') is not None:values.setdefault('lease_terms','보증금 '+_display(values['housing_deposit'])+'원 / 월세 '+_display(values['housing_cost'])+'원')
    match=re.fullmatch(r'20(\d{2})\s*개회\s*(\d+)',str(values.get('case_number','')))
    if match:
        for key,value in zip(('case_year_suffix','case_sequence'),match.groups()):
            values[key]=value;origins[key]=origins.get('case_number',{'type':'case_record','status':'review_required'})
    return values,origins


def preview(case,template_id='D5100',fields=None,calculation=None):
    if case.get('case_type','personal_rehabilitation')!='personal_rehabilitation':raise ValueError('개인회생 사건에만 사용하는 서식입니다.')
    template=TEMPLATES.get(template_id)
    if not template:raise ValueError('등록되지 않은 서식입니다.')
    if template['court_ids'] and case.get('court_id') not in template['court_ids']:raise ValueError('선택한 법원의 전용 서식이 아닙니다.')
    original=original_path(template_id)
    values,origins=_values(case,fields,calculation)
    output=[];missing=[]
    measure=pymupdf.open(original);overflow=[]
    original_chars={page:_original_characters(measure[page]) for page in template['pages']}
    for item in template['fields']:
        value=values.get(item['key'])
        entry=dict(item,value=value,source=origins.get(item['key'],{'type':'derived' if value is not None else 'missing','status':'review_required'}))
        entry['status']=entry['source']['status'];output.append(entry)
        if item['key']=='statement':entry['authoring_required']=value is None or value==''
        if item['required'] and (value is None or value==''):missing.append(item['label'])
        if value is not None and value!='':
            layout=_field_layout(item,_display(value,item['key']),original_chars[item['page']])
            fits=layout is not None
            entry['fits_original_field']=fits
            entry['render_layout']=layout
            if not fits:overflow.append(item['key'])
    measure.close()
    warnings=['공식 원본의 빈칸에 값을 배치한 검토용 초안입니다. 서명·동의·기각사유 부존재 진술은 자동 확정하지 않습니다.',
      '서식에 포함된 과거 작성요령·비용 안내의 현재 효력은 별도로 대조해야 합니다.']
    if not calculation and any(f['key'] in CALC_KEYS for f in output):warnings.append('원문에서 확인한 금액과 코드 산술은 기재합니다. 법률 판단 제안은 미승인 상태이며 미확정 비용·생계비·면제재산을 0원으로 간주하지 않습니다.')
    if template_id=='D5106':warnings.append('첫 4개 채권자는 원본 표에 배치하며 추가 채권자는 별지에 전부 보존합니다. 담보·다툼·전부명령 부속서류는 별도 검토합니다.')
    if template_id=='BUSAN-ATTACHMENTS':warnings.append('제출 여부 체크는 원본 증빙과 대조 후 담당자가 표시합니다. 업로드만으로 제출 완료를 표시하지 않습니다.')
    if overflow:warnings.append('원본 칸에 들어가지 않는 값은 별지에 보존됩니다. 배치 검토 필요: '+', '.join(overflow))
    known={f['key'] for f in output}
    extras=[{'key':k,'value':v} for k,v in _flatten(fields or {}).items() if k not in known and v is not None]
    from . import evidence_mapping
    packet=evidence_mapping.build(case)
    source_facts=evidence_mapping.form_facts(packet,template_id)
    return {'template':deepcopy(template),'fields':output,'missing_fields':list(dict.fromkeys(missing)),
      'unmapped_fields':extras,'overflow_fields':overflow,'warnings':warnings,'ready_for_review':not missing and not overflow,'submission_ready':False,'original_sha256':template['source']['sha256'],
      'case_id':case.get('id'),'input_revision':case.get('input_revision'),'calculation_id':calculation.get('id') if calculation else None,
      'source_facts':source_facts,'proposed_decisions':packet['proposed_decisions'] if not calculation else [],
      'evidence_mapping_version':packet['version'],'evidence_source_signature':packet['source_signature'],
      'renderer_version':RENDERER_VERSION,'typography':{**typography(),'original_font_families':original_font_inventory(template_id)}}


@lru_cache(maxsize=4)
def _font_details(candidates):
    path=next((Path(p) for p in candidates if p and Path(p).is_file()),None)
    if path is None:
        raise ValueError('한글 명조체 글꼴이 필요합니다. 함초롬바탕·바탕·나눔명조를 설치하거나 DEBTOFF_KOREAN_FONT를 지정하세요.')
    font=pymupdf.Font(fontfile=str(path))
    return path,font,hashlib.sha256(path.read_bytes()).hexdigest()


def typography():
    path,font,digest=_font_details(tuple(FONT_CANDIDATES))
    return {'original_body_family':'휴먼명조 (공식 원본 내 부분 포함 글꼴)',
            'generated_body_family':font.name,'font_file':path.name,'font_sha256':digest,
            'color':'black','alignment':'original_row_baseline','minimum_font_size':7,
            'layout_revision':RENDERER_VERSION}


@lru_cache(maxsize=16)
def original_font_inventory(template_id):
    families=set()
    with pymupdf.open(original_path(template_id)) as document:
        for index in TEMPLATES[template_id]['pages']:
            for font in document[index].get_fonts():
                name=font[3].split('+')[-1]
                try:name=name.encode('latin-1').decode('cp949')
                except (UnicodeError,LookupError):pass
                families.add('Type3 윤곽 글꼴' if font[2]=='Type3' else name)
    return sorted(families)


def _font(page):
    path,_,_=_font_details(tuple(FONT_CANDIDATES))
    page.insert_font(fontname='DebtoffCourtSerif',fontfile=str(path))
    return 'DebtoffCourtSerif'


def _original_characters(page):
    return [char for block in page.get_text('rawdict')['blocks'] for line in block.get('lines',[])
            for span in line['spans'] for char in span['chars'] if char['c'].strip()]


def _field_layout(field,text,original_chars):
    """Measure before drawing; preserve every original glyph and table rule."""
    _,font,_=_font_details(tuple(FONT_CANDIDATES))
    rect=pymupdf.Rect(field['rect']);padding=1 if rect.width<30 else 1.5;available=rect.width-2*padding
    if any(not font.has_glyph(ord(char)) for char in text if not char.isspace()):return None
    narrative=field['key'] in {'statement','prior_proceedings','additional_cost_reason'}
    numeric=bool(re.fullmatch(r'[\d,.:+()\-/\s]+',text))
    centered=field['key'].endswith(('_year','_month','_day','.number')) or field['key'] in {'months','household_size','median_percent','principal_repayment_percent'}
    alignment='center' if centered else 'right' if numeric or field['key']=='court_prefix' else 'left'
    requested=max(10,float(field['font_size'])) if not narrative else max(11,float(field['font_size']))
    sizes=list(dict.fromkeys([requested,*range(int(requested)-1,6,-1)]))
    for size in sizes:
        lines=[];fits=True
        for paragraph in text.split('\n'):
            if not paragraph:lines.append('');continue
            if numeric and font.text_length(paragraph,fontsize=size)>available:fits=False;break
            line=''
            for char in paragraph:
                if font.text_length(line+char,fontsize=size)>available:
                    if not line:fits=False;break
                    lines.append(line.rstrip());line=char.lstrip()
                else:line+=char
            lines.append(line.rstrip())
        if not fits:continue
        line_height=size*1.5 if narrative else size*1.35
        glyph_height=(font.ascender-font.descender)*size
        total_height=glyph_height+max(0,len(lines)-1)*line_height
        if total_height>rect.height-2:continue
        baseline=rect.y0+font.ascender*size+2 if narrative else (rect.y0+rect.y1-total_height)/2+font.ascender*size
        if len(lines)==1 and rect.height<=26:
            neighbors=[char['origin'][1] for char in original_chars
                if rect.y0+font.ascender*size<=char['origin'][1]<=rect.y1+font.descender*size
                and (0<=rect.x0-char['bbox'][2]<=160 or 0<=char['bbox'][0]-rect.x1<=160)]
            if neighbors:baseline=min(neighbors,key=lambda value:abs(value-baseline))
        layout=[];collision=False
        for index,line in enumerate(lines):
            width=font.text_length(line,fontsize=size)
            x=(rect.x0+rect.x1-width)/2 if alignment=='center' else rect.x1-padding-width if alignment=='right' else rect.x0+padding
            y=baseline+index*line_height
            bounds=pymupdf.Rect(x,y-font.ascender*size,x+width,y-font.descender*size)
            if any((bounds&pymupdf.Rect(char['bbox'])).get_area()>0.75 for char in original_chars):collision=True;break
            layout.append({'text':line,'x':round(x,4),'baseline':round(y,4),'font_size':size,'align':alignment})
        if not collision:return layout
    return None


def _display(value,key=None):
    if key in {'bank_name','refund_bank'} and isinstance(value,str) and value.endswith('은행'):return value[:-2]
    if isinstance(value,bool):return '예' if value else '아니오'
    if isinstance(value,(int,float)):
        if key and (key.endswith(('_year','_month','_day')) or key in ('months','household_size','median_percent')):return str(value)
        return f'{value:,}'
    if isinstance(value,(dict,list)):return json.dumps(value,ensure_ascii=False)
    return str(value)


def _schedule_date(fields,month):
    """Label only an explicitly entered draft start date; never infer a filing date."""
    try:
        first=date(int(fields['start_year']),int(fields['start_month']),int(fields['start_day']))
        index=first.year*12+first.month-1+int(month)-1
        year,zero_month=divmod(index,12)
        day=min(first.day,calendar.monthrange(year,zero_month+1)[1])
        return date(year,zero_month+1,day).isoformat()
    except (KeyError,ValueError,TypeError,OverflowError):return None


def _calculation_appendix(template_id,calculation,fields):
    """Export the complete approved ledger, including rows beyond original capacity."""
    if not calculation or template_id not in ('D5106','D5110'):return []
    creditors=calculation.get('inputs',{}).get('creditors',[])
    names={row.get('id'):str(row.get('name') or '채권자명 미확인') for row in creditors}
    lines=['[별지] 검증 계산에 연결된 전체 채권자 목록',
      '채권자 수: '+str(len(creditors))+'명. 원본 표의 행 수와 관계없이 전부 표시합니다.']
    for i,row in enumerate(creditors,1):
        kind={'unsecured':'무담보','secured':'담보부','priority':'우선권'}.get(row.get('kind'),'분류 미확인')
        lines.extend([f"채권자 {i}: {row.get('name','미확인')} / {kind}",
          f"원금 {_display(row.get('principal','미확인'))}원 / 이자 {_display(row.get('interest','미확인'))}원",
          '발생 원인: '+str(row.get('cause') or '별도 확인'),
          '주소: '+str(row.get('address') or '별도 확인')])
    if template_id!='D5110':return lines
    allocations=calculation.get('creditor_allocations',[])
    lines.append('[별지] 채권자별 전체 변제 합계')
    for row in allocations:
        lines.extend([f"채권자 {row.get('name') or names.get(row.get('creditor_id'),'명칭 미확인')}",
          f"원금 변제 {_display(row.get('principal_payment',0))}원 / 이자 변제 {_display(row.get('interest_payment',0))}원 / 합계 {_display(row.get('total_payment',0))}원",
          f"잔여 원금 {_display(row.get('unpaid_principal',0))}원 / 잔여 이자 {_display(row.get('unpaid_interest',0))}원"])
    schedule=calculation.get('schedule',[])
    lines+=['[별지] 회차별 채권자 변제예정액 전체 내역',
      '원 단위 배분 결과를 생략 없이 표시합니다. 표의 ‘회차별 별지’는 아래 내역을 뜻합니다.',
      '날짜는 직원이 입력한 시작일을 월 단위로 이동한 계획일이며 법원의 확정 납입일이 아닙니다.']
    for step in schedule:
        month=step.get('month');planned=_schedule_date(fields or {},month)
        lines.append(f"제{month}회차"+(f" / 계획일 {planned}" if planned else ' / 계획일 미입력'))
        lines.append(f"입금액 {_display(step.get('deposit',0))}원 / 회생위원 보수 {_display(step.get('trustee_fee',0))}원 / 채권자 변제액 {_display(step.get('creditor_payment',0))}원")
        for payment in step.get('allocations',[]):
            cid=payment.get('creditor_id')
            lines.append(names.get(cid,'채권자명 미확인'))
            lines.append(f"원금 {_display(payment.get('principal',0))}원 + 이자 {_display(payment.get('interest',0))}원 = 합계 {_display(payment.get('total',0))}원")
    lines+=['[별지] 회차 합계 대조',
      f"총 {_display(len(schedule))}회 / 입금액 합계 {_display(sum(s.get('deposit',0) for s in schedule))}원",
      f"회생위원 보수 합계 {_display(sum(s.get('trustee_fee',0) for s in schedule))}원 / 채권자 변제 합계 {_display(sum(s.get('creditor_payment',0) for s in schedule))}원"]
    return lines


def _annex_lines(text,width=55):
    """Lay out physical source lines without letting embedded newlines overlap."""
    for physical_line in str(text).splitlines() or ['']:
        if not physical_line:
            yield ''
        else:
            for start in range(0,len(physical_line),width):
                yield physical_line[start:start+width]


def render_pdf(template_id,case,fields=None,calculation=None):
    result=preview(case,template_id,fields,calculation)
    template=result['template'];source=pymupdf.open(original_path(template_id));out=pymupdf.open()
    for page in template['pages']:out.insert_pdf(source,from_page=page,to_page=page)
    page_map={source_page:i for i,source_page in enumerate(template['pages'])}
    overflow=[]
    for p in out:
        # Some court PDFs leave a body-area clipping path active. Isolate their
        # graphics state so our draft banner and values are not clipped away.
        p.wrap_contents()
        font=_font(p)
        p.draw_rect((30,28,p.rect.width-30,52),color=(.75,.2,.1),fill=(1,.97,.93))
        synthetic=bool(case.get('synthetic') or str((fields or {}).get('resident_id','')).startswith('TEST-'))
        banner='DRAFT · 가상자료 · 효력없음 · 테스트 전용' if synthetic else 'DRAFT · 검토용 작성본 · 미확인 항목/서명/선택란은 담당자 확인'
        p.insert_text((38,44),banner,fontname=font,fontsize=9,color=(.7,.15,.1))
    for field in result['fields']:
        value=field['value']
        if value is None or value=='':continue
        p=out[page_map[field['page']]];font=_font(p)
        layout=field.get('render_layout')
        if layout is not None:
            for line in layout:
                if line['text']:p.insert_text((line['x'],line['baseline']),line['text'],fontname=font,fontsize=line['font_size'],color=(0,0,0))
        else:overflow.append({'key':field['key'],'label':field['label'],'value':value})
    # Review annex keeps every unmapped or overflowing value; no silent truncation.
    lines=['원본: '+template['source']['title'],
      '원본 URL: '+template['source']['url'],'사건: '+str(case.get('client_name','')),'관할 후보: '+str(case.get('court_name','')),
      '확인 필요: '+(', '.join(result['missing_fields']) or '필수 매핑 값 존재; 증빙·법률 검토 별도')]+result['warnings']
    for field in result['fields']:
        if field['value'] is not None and field['value']!='':
            status={'source_checked':'원문 확인 · 의미 검토 필요','approved':'검토 승인',
                'automatically_verified':'자동 검증 완료','semantic_review_required':'본문 의미 검토 필요',
                'review_required':'담당자 검토 필요','candidate':'후보 · 검토 필요'}.get(field['status'],'근거 및 내용 검토 필요')
            lines.append(field['label']+': '+_display(field['value'])+' / '+status)
    for field in overflow+result['unmapped_fields']:lines.append('별지 ['+field.get('label',field['key'])+']: '+_display(field['value']))
    if result.get('source_facts'):
        lines.append('[별지] 제출 원문에서 확인한 세부 항목 · 법률 인정 및 AI 의미 검토와 구별')
        for fact in result['source_facts']:
            location=f"{fact.get('page') or 1}쪽 {fact.get('line_start') or 1}행"
            document=next((doc for doc in case.get('documents',[]) if doc['id']==fact['document_id']),{})
            title=document.get('filename') or document.get('name') or document.get('title') or '제출 자료'
            value='없음' if fact['value'] is False else '있음' if fact['value'] is True else _display(fact['value'])
            lines.append(f"{fact['label']}: {value} / {title} {location}")
            lines.append('원문: '+fact['quote'])
    for proposal in result.get('proposed_decisions',[]):
        lines.append('미승인 법률 판단 제안: '+proposal['reason']+' / '+_display(proposal['value']))
    lines.extend(_calculation_appendix(template_id,calculation,fields))
    current=out.new_page(width=595,height=842);font=_font(current);y=70
    current.insert_text((40,44),'작성값·근거·미확인 항목 대조표 (제출 전 분리 검토)',fontname=font,fontsize=13)
    for line in lines:
        # Each original newline consumes its own baseline and page-break check.
        # Never pass multiline text to insert_text while advancing only one row.
        for physical_line in _annex_lines(line):
            if y>790:current=out.new_page(width=595,height=842);font=_font(current);y=55
            if physical_line:current.insert_text((40,y),physical_line,fontname=font,fontsize=9)
            y+=15
        y+=6
    out.set_metadata({'title':template['title']+' · 검토용 초안','author':'Debtoff local draft renderer','subject':'Original court form '+result['original_sha256']})
    out.subset_fonts();data=out.tobytes(garbage=4,deflate=True);out.close();source.close();return data
