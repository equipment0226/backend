"""Rebuild the checked-in court policy from explicitly reviewed source locators.

No network and no legal inference: changes to mappings below require a source
review. This script also records the original PDF hashes used in that review.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
registry = json.loads((ROOT / 'data/registry.json').read_text(encoding='utf-8'))
source_registry = {s['id']: s for s in registry['sources']}
forms = json.loads((ROOT / 'data/court_forms/catalog.json').read_text(encoding='utf-8'))
busan = next(t for t in forms['templates'] if t['id'] == 'BUSAN-ATTACHMENTS')['source']
research = json.loads((ROOT / 'data/legal_research/manifest.json').read_text(encoding='utf-8'))
gwangju = next(s for s in research['sources'] if s['id'] == 'AXC06')

sources = {
    'NATIONAL_FRESHNESS': {'title': '개인회생사건 처리지침 제4조 제1항 (생활법령 공식 안내)',
        'url': source_registry['L08']['url'], 'locator': '제출서류의 발급기한', 'scope': 'national',
        'reviewed_at': '2026-10-03', 'evidence': '관공서 작성 서류는 원칙적으로 신청일로부터 2개월 내 발급; 특별한 사정 예외.',
        'authority_type': 'official_government_guide', 'effective_date': None},
    'BUSAN_20260701': {**{k: busan[k] for k in ('title', 'url', 'sha256', 'path')},
        'locator': '자료제출목록 1~4쪽', 'scope': 'CT03', 'effective_date': '2026-07-01',
        'reviewed_at': '2026-10-03', 'authority_type': 'court_document_checklist',
        'evidence': '공통10번 은행거래 1년 또는 계좌통합 상세조회; 급여16번 입금 2년; 영업19번 세무증빙 3년; 공통6번 지방세 전국 모든세목 5년.'},
    'GWANGJU_402': {'title': gwangju['title'], 'url': gwangju['url'], 'sha256': gwangju['sha256'],
        'path': gwangju['text_path'], 'locator': '제402호, 인쇄187~190쪽', 'scope': 'CT06',
        'effective_date': None, 'reviewed_at': '2026-10-03', 'authority_type': 'court_practice_rule',
        'evidence': '제2조: 급여증빙 대체 통장6개월, 월별 수입상황보고서1년. 제3조: 추가명령 가능한 예금1년·매각2년·주거등기5년.'}
}
for key, old, court, locator in [('SEOUL_402', 'C03', 'CT01', '제402호, PDF228~231쪽 / 인쇄208~211쪽'),
                                 ('SUWON_402', 'C02', 'CT02', '제402호, PDF99~100쪽 / 인쇄193~195쪽')]:
    raw = next((ROOT / '.local/corpus/raw').glob(old + '-*.pdf'))
    sources[key] = {'title': source_registry[old]['title'], 'url': source_registry[old]['url'],
        'sha256': hashlib.sha256(raw.read_bytes()).hexdigest(), 'locator': locator, 'scope': court,
        'reviewed_at': '2026-10-03', 'effective_date': None, 'authority_type': 'court_practice_rule',
        'evidence': '제2조: 급여증빙 대체 통장6개월, 월별 수입상황보고서1년. 제3조: 추가명령 가능한 예금1년·매각2년·주거등기5년.'}

common_court = {
    'coverage': 'verified_selected_requirements',
    'mandatory_base': ['D01', 'D09', 'D11', 'D28', 'D29'],
    'salary_alternative_months': 6, 'additional_bank_months': 12,
    'documents': {
        'D01': {'requirement_kind': 'mandatory', 'locator': '제402호 제2조 주민등록등본'},
        'D06': {'requirement_kind': 'mandatory', 'locator': '제402호 제2조 제3호 가목 (원칙)',
                'alternatives': ['D07', 'D50', 'D36']},
        'D07': {'requirement_kind': 'alternative', 'locator': '제402호 제2조 제3호 가목 (불가피한 경우 대체)',
                'issuance_options': {'employer_signature': '사용자 서명 또는 기명날인'}, 'alternatives': ['D06', 'D50', 'D36']},
        'D21': {'months': 12, 'requirement_kind': 'mandatory', 'locator': '제402호 제2조 제3호 나목',
                'issuance_options': {'breakdown': '월별 총매출액·필요비·실질소득'}, 'purpose': 'business_income'},
        'D43': {'months': 120, 'requirement_kind': 'conditional', 'locator': '제402호 제2조 (신청일 전 10년 이내 신청이력 있는 경우)',
                'issuance_options': {'content': '사건번호·종국내역·종국일자 및 면책 여부'}},
        'D44': {'period_label': '해당 차입·처분 거래일 및 대금 사용기간', 'requirement_kind': 'preparation',
                'locator': '제402호 제3조 제1호 마목 (부동산매매는 직전 2년, 별도 보정요구 우선)'},
        'D50': {'requirement_kind': 'alternative', 'locator': '제402호 제2조 제3호 가목',
                'issuance_options': {'employer_signature': '사용자 서명 또는 기명날인'}, 'alternatives': ['D06', 'D07', 'D36']}
    },
    'limitations': ['제402호의 추가명령 가능 서류와 법정 필수서류를 구별합니다.',
                   '6개월은 급여증빙 대체 계좌거래의 범위이며 모든 은행계좌에 일괄 적용하지 않습니다.',
                   '지역별 별도 최신 보정명령이 있으면 해당 사건에서 원문에 연결하여 범위를 지정합니다.']
}
courts = {c['id']: {'name': c['name'], 'coverage': 'unverified', 'source_ids': [],
                    'limitations': ['전국 공통 발급기준만 적용. 해당 법원 세부 요청기간·발급옵션은 미검증.']}
          for c in registry['courts']}
for court_id, source in [('CT01', 'SEOUL_402'), ('CT02', 'SUWON_402'), ('CT06', 'GWANGJU_402')]:
    courts[court_id].update(deepcopy(common_court), source_ids=[source])

courts['CT03'].update({
    'coverage': 'verified_dated_checklist', 'source_ids': ['BUSAN_20260701'],
    'effective_from': '2026-07-01', 'all_certificates_freshness_months': 2,
    'mandatory_base': ['D01', 'D02', 'D03', 'D04', 'D09', 'D11', 'D28', 'D29', 'D35', 'D38', 'D48'],
    'salary_additions': ['D06', 'D36'], 'business_additions': ['D05', 'D15', 'D16', 'D18', 'D21'],
    'bank_statement_months': 12, 'bank_statement_requirement': 'alternative',
    'bank_statement_locator': '자료제출목록 1쪽 10번 (계좌통합조회 상세내역으로 대체 가능)',
    'salary_statement_months': 24, 'salary_statement_locator': '자료제출목록 2쪽 16번',
    'documents': {
        'D01': {'requirement_kind': 'mandatory', 'locator': '1쪽 발급 유의사항·3번', 'issuance_options': {'person_number_display': '본인 전체 / 제3자 뒷자리 비공개'}},
        'D02': {'requirement_kind': 'mandatory', 'locator': '1쪽 3번', 'issuance_options': {'address_history': '과거 주소 전체', 'name_changes': '포함', 'resident_number_changes': '포함'}},
        'D03': {'requirement_kind': 'mandatory', 'locator': '1쪽 발급 유의사항·1번', 'issuance_options': {'certificate_type': '상세', 'person_number_display': '본인 전체 / 제3자 뒷자리 비공개'}},
        'D04': {'requirement_kind': 'mandatory', 'locator': '1쪽 발급 유의사항·2번', 'issuance_options': {'certificate_type': '상세', 'person_number_display': '본인 전체 / 제3자 뒷자리 비공개'}},
        'D05': {'months': 36, 'requirement_kind': 'conditional', 'locator': '2쪽 19번 영업소득자', 'purpose': 'business_tax'},
        'D06': {'requirement_kind': 'mandatory', 'locator': '2쪽 16번 원천징수영수증 사본 필수'},
        'D07': {'months': 24, 'requirement_kind': 'preparation', 'locator': '2쪽 16번', 'purpose': 'salary_income'},
        'D15': {'requirement_kind': 'mandatory', 'locator': '2쪽 18번', 'issuance_options': {'content': '사업자등록증명 및 총사업자등록내역 사실증명 (과거·현재 폐업·휴업·계속 전부)'}},
        'D16': {'months': 36, 'requirement_kind': 'conditional', 'locator': '2쪽 19번', 'purpose': 'business_tax', 'alternatives': ['D17']},
        'D17': {'months': 36, 'requirement_kind': 'conditional', 'locator': '2쪽 19번 면세사업자', 'purpose': 'business_tax', 'alternatives': ['D16']},
        'D18': {'months': 36, 'requirement_kind': 'conditional', 'locator': '2쪽 19번', 'purpose': 'business_tax'},
        'D21': {'requirement_kind': 'mandatory', 'locator': '2쪽 22번', 'issuance_options': {'breakdown': '매출액·영업지출 반영 월평균 영업소득 도표'}},
        'D29': {'months': 60, 'requirement_kind': 'mandatory', 'locator': '1쪽 6번', 'issuance_options': {'tax_scope': '모든 세목', 'jurisdiction_scope': '전국'}},
        'D35': {'requirement_kind': 'alternative', 'locator': '1쪽 10번', 'issuance_options': {'content': '금융기관별 계좌내역 및 계좌상세내역서'}, 'alternatives': ['D36']},
        'D36': {'alternatives': ['D35'], 'qualification': '급여 입금 2년과 전체 은행거래 1년은 목적이 다른 범위입니다. 전체 은행거래는 금융기관별 계좌통합 상세조회로 대체 가능합니다.'},
        'D39': {'requirement_kind': 'conditional', 'locator': '1쪽 11번', 'issuance_options': {'content': '보험가입내역 및 예상해약환급금; 환급금이 없으면 보험회사 무환급 증명'}},
        'D43': {'months': 120, 'requirement_kind': 'conditional', 'locator': '실무준칙402호 제2조 제8호 (10년 이력); 자료제출목록4쪽 추가질문9 (2년 이내는 종전 신청서 등 추가)'},
        'D48': {'requirement_kind': 'mandatory', 'locator': '자료제출목록 1~4쪽', 'issuance_options': {'content': '모든 체크·추가질문 답변, 미제출 사유, 편철 순서'}}
    },
    'limitations': ['2026-07-01 시행 공식 목록의 확인된 항목을 적용하며 사건별 추가 보정명령은 별도입니다.',
                   '3년 세무증명은 증명 대상기간이며 발급일 2개월 기준과 별개입니다. 연도별 발급 가능 범위는 실제 과세기간을 대조합니다.']
})

policy = {'version': 'court-document-rules-2026-10-04.2', 'reviewed_at': '2026-10-03',
    'scope': 'personal_rehabilitation', 'sources': sources, 'courts': courts,
    'income_eligibility': {
        'business_only_document_ids': ['D14','D15','D16','D17','D18','D19','D20','D21','D22','D49'],
        'routing_basis': '현재 사업·영업·프리랜서 또는 급여와 사업의 복합소득이 명시된 경우에만 자동 요청',
        'unknown_policy': '세부상담에서 소득유형 확인; 사업자 전용 서류 일괄 요청 금지',
        'manual_and_court_order_requests': '자동 철회 대상에서 제외',
        'classification_note': '소득유형 분기 안전장치이며 새 법률상 제출 의무를 추가하지 않음'},
    'government_document_ids': ['D01','D02','D03','D04','D05','D09','D10','D11','D12','D15','D16','D17','D18','D23','D24','D25','D26','D27','D28','D29','D30','D40','D41'],
    'split': {'D06':'employer','D07':'employer','D08':'employer','D35':'account','D36':'account',
              'D37':'account','D38':'creditor','D39':'insurer'},
    'precedence': ['case_specific_document_scopes', 'dated_court_checklist', 'court_practice_rule', 'national_common'],
    'limitations': ['서류 준비 기준은 인가 가능성을 보장하지 않습니다.', '미검증 법원에 다른 법원의 기간을 전파하지 않습니다.',
                   '현재 법원별 규칙은 확인한 자료의 범위이며 전국 모든 재판부 실무의 전수 검증이 아닙니다.',
                   '개별 사건 보정명령은 document_scopes에 원문 근거와 기간을 지정하여 우선 적용합니다.']}
(ROOT / 'data/court_request_rules.json').write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding='utf-8')
print('Generated', len(courts), 'court profiles; verified local evidence:', 4)
