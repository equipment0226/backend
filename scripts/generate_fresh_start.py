"""Prepare uploadable fictional evidence; never creates or approves a case."""
import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.generate_synthetic_case import pdf_document


def generate(as_of=None):
    today = date.fromisoformat(as_of) if as_of else datetime.now(timezone(timedelta(hours=9))).date()
    start = today.replace(year=today.year - 1)
    out = ROOT / 'examples/fresh_start'
    docs = out / 'documents'
    edges = out / 'optional_error_checks'
    docs.mkdir(parents=True, exist_ok=True)
    edges.mkdir(exist_ok=True)
    issue = today.isoformat()
    period = f'{start.isoformat()} ~ {issue}'
    notes = f'''가상 테스트 자료 · 실제 신청 및 법원 제출용이 아닙니다.
성명: 김새봄. 상담 기준일: {issue}. 신청 관할: 서울회생법원.
주소: 서울특별시 가상구 새출발로 100 테스트동 101호(존재하지 않는 주소).
2024년 1월부터 새봄테스트회사에서 정규직으로 근무하고 있습니다.
월 급여는 세전 3,100,000원, 세금 및 사회보험료 공제 합계 300,000원, 세후 실수령액 2,800,000원입니다.
소득 확인기간: {period}. 미혼이고 1인 가구이며 부양가족은 없습니다.
2021년부터 2023년까지 소득이 일정하지 않아 부족한 생활비를 차용하였고, 이후 원리금 상환 부담이 누적되었습니다.
현재 금융채무는 새봄테스트은행 한 곳에 원금 58,000,000원, 이자 2,000,000원, 합계 60,000,000원입니다.
가족 차용금이나 제3자 대납은 없으며 채무 전부가 무담보입니다. 최근 1년 신규 차입은 없습니다.
재산은 새봄테스트은행 예금 1,200,000원입니다. 부동산, 자동차, 임차보증금과 보험계약은 없습니다.
가족 소유의 별도 주택에서 혼자 무상거주하고 있으며 월 주거비는 0원입니다.
식비 500,000원, 교통비 150,000원, 통신비 80,000원, 공과금 120,000원, 그 밖의 생활비 650,000원으로 실제 월지출은 1,500,000원입니다.
은행 계좌는 새봄테스트은행 TEST-ACCOUNT-SPRING-001 하나이며 급여 수령용으로 사용합니다.
사업소득, 세금 체납, 과거 회생·파산 이력은 없습니다. 최근 재산 처분은 없습니다.
정기 급여를 유지하면서 확인된 생활비를 제외한 범위에서 채무를 분할 변제하는 방안을 상담하고 싶습니다.
실제 주민등록번호·전화번호·서명은 사용하지 않았습니다. 인물과 기관은 모두 가상입니다.
'''
    (out / 'application.txt').write_text(notes, encoding='utf-8')
    address = '주소: 서울특별시 가상구 새출발로 100 테스트동 101호'
    header = ['성명: 김새봄 / 식별자: TEST-PERSON-SPRING', f'발급일: {issue}']
    material = [
        ('D01', '주민등록표등본', [address, '세대주: 김새봄 / 세대원: 김새봄 1명', '주소변동: 없음 / 상세주소 포함 / 본인 주민번호는 TEST 식별자로 대체']),
        ('D03', '가족관계증명서', ['본인: 김새봄 / 미혼 / 자녀 없음 / 부양가족 없음', '증명서 종류: 상세 / 주민등록번호 뒷자리 표시 없음', '가족 이름과 실제 개인정보는 기재하지 않은 기능 시험 자료']),
        ('D35', '계좌통합조회결과', ['조회범위: 전체 금융기관 / 전체 계좌', '보유 계좌 수: 1개', '금융기관: 새봄테스트은행 / 계좌: TEST-ACCOUNT-SPRING-001', '예금잔액: 1,200,000원 / 다른 보유 계좌 없음']),
        ('D38', '부채증명서', ['채무자: 김새봄 / 채권자: 새봄테스트은행', f'채무잔액 기준일: {issue}', '원금: 58,000,000원 / 이자: 2,000,000원', '채무 합계: 60,000,000원 / 담보 없음 / 보증인 없음', '금융채무 외 가족 차용금: 0원 / 조세채무: 0원']),
        ('D06', '근로소득원천징수영수증', ['근무처: 새봄테스트회사', f'소득 확인기간: {period}', '직전 12개월 총급여: 37,200,000원', '직전 12개월 세금 및 사회보험료 공제 합계: 3,600,000원', '직전 12개월 실수령 합계: 33,600,000원 / 월평균 2,800,000원', '항목별 세액 산식을 검증하기 위한 공식 세무 양식은 아님']),
        ('D08', '재직증명서', ['근무처: 새봄테스트회사 / 입사일: 2024-01-01', '근로형태: 정규직 / 현재 재직 중 / 월 실수령액: 2,800,000원', '사용자 기명: 가상 테스트 담당자 / 실제 서명·인감 없음']),
        ('D09', '건강보험자격득실확인서', ['직장가입자: 김새봄 / 사업장: 새봄테스트회사', '자격취득일: 2024-01-01 / 현재 가입 유지', '자격확인 범위: 전체 가입 이력']),
        ('D11', '연금산정용가입내역확인서', ['가입자: 김새봄 / 사업장: 새봄테스트회사', '가입일: 2024-01-01 / 현재 가입 유지', '월 기준소득: 3,100,000원 / 전체 가입 이력 표시']),
        ('D28', '지적전산자료조회결과서', ['조회대상: 김새봄 / 조회범위: 전국', '소유 부동산 검색 결과: 없음 / 부동산 소유지분: 0원']),
        ('D29', '지방세세목별과세증명서', [f'조회기간: {today.year - 5}-01-01 ~ {issue}', '조회지역: 전국 / 조회세목: 모든 세목', '부동산·차량 관련 과세내역: 없음 / 체납액: 0원', '다른 지역·세목 제외 없음']),
        ('D39', '보험가입및해약환급금확인', ['조회범위: 전체 보험회사 계약조회', '보유 보험계약: 없음 / 보험계약대출: 0원', '해약환급금: 0원']),
        ('SUPPORT-HOUSING', '무상거주확인', [address, '가족 소유의 별도 주택에 김새봄 단독 거주', '소유자 본인 여부: 아니오 / 본인 소유지분: 0원', '임차보증금: 0원 / 월 차임: 0원', '서명·날인 없는 사실관계 판독용 가상자료']),
        ('SUPPORT-HISTORY', '채무발생및생활비진술', ['2021~2023년 불규칙한 소득으로 부족한 생활비를 차용함', '2024년 정규직 취업 후에도 기존 원리금 상환 부담이 누적됨', '월 실수령소득: 2,800,000원 / 실제 월생활지출: 1,500,000원', '식비500000 + 교통150000 + 통신80000 + 공과120000 + 기타650000', '현재 금융채무: 60,000,000원 / 예금: 1,200,000원', '법률상 인정생계비와 변제금은 별도 계산·검토 대상']),
    ]
    manifest = []
    for index, (catalog_id, title, lines) in enumerate(material, 1):
        filename = f'{index:02d}_{title}_가상자료.pdf'
        pdf_document(docs / filename, title + ' · 가상자료', [header + lines])
        manifest.append({'catalog_id': catalog_id, 'title': title, 'file': 'documents/' + filename})
    bank_pages = [header + ['금융기관: 새봄테스트은행 / 예금주: 김새봄', '계좌번호: TEST-ACCOUNT-SPRING-001', f'전체 조회기간: {period}', '아래 월별 요약은 해당 전체 기간에 속하는 모든 거래의 합계입니다.', '조회 시작 잔액: 1,200,000원 / 조회 종료 잔액: 1,200,000원', ['월', '급여 입금', '생활비 출금', '상환 출금']]]
    for offset in range(12):
        month_index = today.year * 12 + today.month - 2 - offset
        year, month = divmod(month_index, 12)
        bank_pages[0].append([f'{year}-{month+1:02d}', '2,800,000', '1,500,000', '1,300,000'])
    bank_pages.append(header + ['금융기관: 새봄테스트은행 / 계좌: TEST-ACCOUNT-SPRING-001', f'전체 조회기간: {period}', '각 급여일 25일에 2,800,000원 입금, 26일 생활비 1,500,000원 출금, 28일 상환 1,300,000원 출금', '그 외 거래 없음 / 급여외 소득 없음 / 다른 계좌로 이체 없음', '예금잔액: 1,200,000원 / 계좌 제한·압류 없음', '거래 상세를 실제 은행에 제출하는 양식이 아닌 판독 테스트 요약입니다.'])
    pdf_document(docs / '14_예금계좌거래내역_가상자료.pdf', '예금계좌 거래내역 · 가상자료', bank_pages)
    manifest.append({'catalog_id': 'D36', 'title': '예금계좌 거래내역', 'file': 'documents/14_예금계좌거래내역_가상자료.pdf', 'period': period})
    payroll = header + ['근무처: 새봄테스트회사', f'확인기간: {period}', '매월 총급여: 3,100,000원 / 공제액: 300,000원', '매월 실수령액: 2,800,000원', '사용자 기명: 가상 테스트 담당자 / 실제 서명 아님']
    pdf_document(docs / '15_급여명세서_스캔가상자료.pdf', '급여명세서 · 이미지 판독 테스트', [payroll], scanned=True)
    manifest.append({'catalog_id': 'D07', 'title': '급여명세서', 'file': 'documents/15_급여명세서_스캔가상자료.pdf', 'period': period})
    pdf_document(edges / '다른사람_급여명세서.pdf', '명의 불일치 테스트', [['성명: 이다른', f'발급일: {issue}', '근무처: 새봄테스트회사', '실수령액: 9,900,000원']])
    pdf_document(edges / '기간부족_거래내역.pdf', '기간 부족 테스트', [header + ['금융기관: 새봄테스트은행 / 계좌번호: TEST-ACCOUNT-SPRING-001', f'조회기간: {issue} ~ {issue}', '예금잔액: 1,200,000원']])
    readme = '''새 출발 테스트 · 김새봄 · 모두 가상자료

1. 고객 화면에서 customer / customer로 로그인합니다.
2. 내 사건 → 상담 신청에서 성명 김새봄, 서울회생법원을 선택합니다.
3. application.txt의 상담 내용을 붙여넣고 동의한 뒤 신청합니다. 신청 전 사건은 0건입니다.
4. 직원 staff / staff, 변호사 lawyer / lawyer 계정에서 같은 새 수임번호를 확인합니다.
5. 고객의 요청 카드에 맞는 documents 파일을 제출합니다. 한 종류에 여러 범위가 있으면 카드의 기관·계좌·기간을 확인하세요.
6. 건강보험·연금·지적·지방세 자료도 포함되어 있습니다. 원천징수영수증이 확인되면 대체 급여자료 요청이 철회될 수 있습니다.
7. 기관/계좌가 미확정이면 직원의 자료 검증에서 새봄테스트은행 / TEST-ACCOUNT-SPRING-001, 새봄테스트회사를 확인합니다.
8. Case Automation에서 원문 검증, 법률 계산, 전략, 작성 및 재검증을 확인합니다. 검토 사유를 확인하고 필요한 자료/판단을 보완하세요.
9. optional_error_checks는 별도의 오류 확인용입니다. 다른사람/기간부족 자료를 정상 자료와 함께 일괄 제출하지 마세요.

이 파일은 관공서 발급문서, 실거래내역, 서명·날인된 문서가 아닙니다. 실제 신청에는 사용할 수 없습니다.
검증을 강제로 통과시키는 테스트 우회 설정은 없습니다. 모든 요청이 충족되지 않거나 판독/근거가 부족하면 보완 상태가 정상입니다.
자료와 manifest.json은 사건 DB에 자동 등록되지 않습니다. 고객이 신청·제출해야 진행됩니다.
manifest.json과 이 안내문은 증빙으로 업로드하지 마세요.
'''
    (out / 'README.txt').write_text(readme, encoding='utf-8')
    for item in manifest:
        item['sha256'] = hashlib.sha256((out / item['file']).read_bytes()).hexdigest()
    (out / 'manifest.json').write_text(json.dumps({'synthetic': True, 'name': '김새봄', 'as_of': issue,
        'court_id': 'CT01', 'automatic_case_creation': False,
        'expected': {'monthly_net_income': 2800000, 'debt_total': 60000000, 'assets_total': 1200000},
        'documents': manifest}, ensure_ascii=False, indent=2), encoding='utf-8')
    archive_path = ROOT / 'examples/fresh_start_bundle.zip'
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(out.rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(out))
    return {'name': '김새봄', 'as_of': issue, 'documents': len(manifest), 'bundle': str(archive_path.relative_to(ROOT)), 'case_created': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--as-of')
    print(json.dumps(generate(parser.parse_args().as_of), ensure_ascii=False))
