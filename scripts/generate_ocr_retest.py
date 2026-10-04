"""A second, explicitly fictional case. Generates evidence, never database facts.

The expected values in the manifest are an independent test oracle. Production
extraction never reads them. Keep fresh_start as the original regression fixture.
"""
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.generate_synthetic_case import pdf_document


def generate(as_of='2026-10-04'):
    today = date.fromisoformat(as_of)
    out = ROOT / 'examples/ocr_retest'
    docs = out / 'documents'
    errors = out / 'optional_error_checks'
    docs.mkdir(parents=True, exist_ok=True)
    errors.mkdir(exist_ok=True)
    period = f'{today.replace(year=today.year - 1).isoformat()} ~ {as_of}'
    name, company, bank = '이하늘', '하늘테스트물류', '하늘테스트은행'
    account = 'TEST-ACCOUNT-SKY-002'
    address = '서울특별시 가상구 다시봄길 200 테스트동 202호'
    header = [f'성명: {name} / 식별자: TEST-PERSON-SKY', f'발급일: {as_of}']
    materials = [
        ('D01', '주민등록표등본', [f'주소: {address}', f'세대주: {name} / 세대원: {name} 1명',
            '주소변동: 없음 / 상세주소 포함', '실제 주민등록번호는 기재하지 않은 가상자료']),
        ('D03', '가족관계증명서', [f'본인: {name} / 미혼 / 자녀 없음 / 부양가족 없음',
            '증명서 종류: 상세 / 주민등록번호 뒷자리 표시 없음']),
        ('D35', '계좌통합조회결과', ['조회범위: 전체 금융기관 / 전체 계좌', '보유 계좌 수: 1개',
            f'금융기관: {bank} / 계좌: {account}', '예금잔액: 2,400,000원 / 다른 보유 계좌 없음']),
        ('D38', '부채증명서', [f'채무자: {name} / 채권자: {bank}', f'채무잔액 기준일: {as_of}',
            '원금: 69,000,000원 / 이자: 3,000,000원', '채무 합계: 72,000,000원 / 담보 없음 / 보증인 없음',
            '금융채무 외 가족 차용금: 0원 / 조세채무: 0원', '차용용도: 생활비 / 최초 차용: 2022년']),
        ('D06', '근로소득원천징수영수증', [f'근무처: {company}', f'소득 확인기간: {period}',
            '직전 12개월 총급여: 42,600,000원', '직전 12개월 세금 및 사회보험료 공제 합계: 4,200,000원',
            '직전 12개월 실수령 합계: 38,400,000원 / 월평균 3,200,000원',
            '월 총급여: 3,550,000원 / 월 공제액: 350,000원']),
        ('D08', '재직증명서', [f'근무처: {company} / 입사일: 2024-03-01',
            '근로형태: 정규직 / 현재 재직 중 / 월 실수령액: 3,200,000원',
            '담당업무: 물류 관리 / 급여 지급일: 매월 25일', '사용자 기명: 가상 테스트 담당자 / 실제 서명 없음']),
        ('D09', '건강보험자격득실확인서', [f'직장가입자: {name} / 사업장: {company}',
            '자격취득일: 2024-03-01 / 현재 가입 유지', '자격확인 범위: 전체 가입 이력']),
        ('D11', '연금산정용가입내역확인서', [f'가입자: {name} / 사업장: {company}',
            '가입일: 2024-03-01 / 현재 가입 유지', '월 기준소득: 3,550,000원 / 전체 가입 이력 표시']),
        ('D28', '지적전산자료조회결과서', [f'조회대상: {name} / 조회범위: 전국',
            '소유 부동산 검색 결과: 없음 / 부동산 소유지분: 0원']),
        ('D29', '지방세세목별과세증명서', [f'조회기간: {today.year - 5}-01-01 ~ {as_of}',
            '조회지역: 전국 / 조회세목: 모든 세목', '부동산·차량 관련 과세내역: 없음 / 체납액: 0원',
            '다른 지역·세목 제외 없음']),
        ('D39', '보험가입및해약환급금확인', ['조회범위: 전체 보험회사 계약조회',
            '보유 보험계약: 없음 / 보험계약대출: 0원', '해약환급금: 0원']),
        ('SUPPORT-HOUSING', '무상거주확인', [f'주소: {address}', f'가족 소유의 별도 주택에 {name} 단독 거주',
            '소유자 본인 여부: 아니오 / 본인 소유지분: 0원', '임차보증금: 0원 / 월 차임: 0원',
            '서명·날인 없는 사실관계 판독용 가상자료']),
        ('SUPPORT-HISTORY', '채무발생및생활비진술', ['2022~2023년 불규칙한 소득으로 부족한 생활비를 차용함',
            '2024년 정규직 취업 후에도 기존 원리금 상환 부담이 누적됨',
            '월 실수령소득: 3,200,000원 / 실제 월생활지출: 1,600,000원',
            '식비550000 + 교통180000 + 통신70000 + 공과금150000 + 기타650000',
            '현재 금융채무: 72,000,000원 / 예금: 2,400,000원',
            '최근 1년 신규 차용·재산 처분·세금 체납 없음', '사업소득·가족 차용금·과거 회생·파산 이력 없음',
            '매월 정기 급여를 유지하며 확인된 소득으로 성실히 변제할 계획']),
    ]
    manifest = []
    for number, (catalog_id, title, lines) in enumerate(materials, 1):
        filename = f'{number:02d}_{title}_가상자료.pdf'
        pdf_document(docs / filename, title + ' · 가상자료', [header + lines])
        manifest.append({'catalog_id': catalog_id, 'title': title, 'file': 'documents/' + filename})
    bank_pages = [header + [f'금융기관: {bank} / 예금주: {name}', f'계좌번호: {account}',
        f'전체 조회기간: {period}', '조회 시작 잔액: 2,400,000원 / 조회 종료 잔액: 2,400,000원',
        ['월', '급여 입금', '생활비 출금', '상환 출금']]]
    for offset in range(12):
        year, month = divmod(today.year * 12 + today.month - 2 - offset, 12)
        bank_pages[0].append([f'{year}-{month+1:02d}', '3,200,000', '1,600,000', '1,600,000'])
    bank_pages.append(header + [f'금융기관: {bank} / 계좌: {account}', f'전체 조회기간: {period}',
        '각 급여일 25일에 3,200,000원 입금', '26일 생활비 1,600,000원 출금 / 28일 상환 1,600,000원 출금',
        '그 외 거래 없음 / 급여외 소득 없음 / 다른 계좌로 이체 없음', '예금잔액: 2,400,000원 / 계좌 제한·압류 없음',
        '가상 월별 거래 합계이며 실제 은행 발급자료가 아닙니다.'])
    filename = '14_예금계좌거래내역_가상자료.pdf'
    pdf_document(docs / filename, '예금계좌 거래내역 · 가상자료', bank_pages)
    manifest.append({'catalog_id': 'D36', 'title': '예금계좌 거래내역', 'file': 'documents/' + filename, 'period': period})
    filename = '15_급여명세서_스캔가상자료.pdf'
    pdf_document(docs / filename, '급여명세서 · 이미지 판독 테스트', [header + [f'근무처: {company}',
        f'확인기간: {period}', '매월 총급여: 3,550,000원 / 공제액: 350,000원',
        '매월 실수령액: 3,200,000원', '사용자 기명: 가상 테스트 담당자 / 실제 서명 아님']], scanned=True)
    manifest.append({'catalog_id': 'D07', 'title': '급여명세서', 'file': 'documents/' + filename, 'period': period})
    application = f'''가상 테스트 자료 · 실제 법원 제출용이 아닙니다.
성명: {name}. 상담 기준일: {as_of}. 신청 관할: 서울회생법원.
주소: {address}(존재하지 않는 주소). 미혼 1인 가구, 부양가족 없음.
2024년 3월부터 {company}에서 물류 관리 정규직으로 재직 중입니다.
월 총급여 3,550,000원, 세금·사회보험 공제 350,000원, 실수령액 3,200,000원입니다.
소득 확인기간: {period}.
2022~2023년 불규칙한 소득으로 생활비가 부족해 차용했습니다.
2024년 정규직 취업 후에도 기존 원리금 상환 부담이 누적됐습니다.
채권자 {bank}의 원금 69,000,000원과 이자 3,000,000원, 총 72,000,000원이 있습니다.
모두 무담보이며 보증인·가족 차용금·제3자 대납·조세채무는 없습니다.
보유 계좌는 {bank} {account} 하나, 예금잔액 2,400,000원입니다.
자산 합계 2,400,000원이며 부동산·자동차·보험·임차보증금은 없습니다.
가족 소유의 별도 주택에 혼자 무상거주하여 월 차임은 0원입니다.
월생활지출은 식비550000, 교통180000, 통신70000, 공과금150000, 기타650000으로 총1,600,000원입니다.
사업소득, 최근 1년 신규 차용·재산 처분·세금 체납, 과거 회생·파산 이력은 없습니다.
급여외 소득은 없습니다. 정기 급여를 유지하며 소득 범위에서 성실히 변제하고 싶습니다.
실제 주민등록번호·전화번호·서명은 제공하지 않는 가상자료입니다.
생활비 인정액·청산가치 공제·변제계획은 자동 초안의 검토 제안이며 최종 법률 판단은 아닙니다.
'''
    (out / 'application.txt').write_text(application, encoding='utf-8')
    pdf_document(errors / '다른사람_급여명세서.pdf', '명의 불일치 테스트', [
        ['성명: 이다른', f'발급일: {as_of}', f'근무처: {company}', '월 실수령액: 9,900,000원']])
    pdf_document(errors / '기간부족_거래내역.pdf', '기간 부족 테스트', [header + [
        f'금융기관: {bank} / 계좌번호: {account}', f'조회기간: {as_of} ~ {as_of}', '예금잔액: 2,400,000원']])
    (out / 'README.txt').write_text('''이하늘 · 새 가상사례 · 실제 제출 불가
1. 고객 http://localhost:5173 에서 customer / customer 로그인
2. 내 사건 → 상담 신청 → 가상 상담 예시 불러오기 → 직접 신청
3. 직원 http://localhost:5174 에서 staff / staff 로그인
4. 세부 상담 요청 후 application.txt의 사실관계를 확인하여 상담 완료
5. 고객 화면에서 요청별 기관·계좌·기간을 확인하여 documents 자료 제출
6. 무상거주확인·채무발생및생활비진술도 함께 제출
7. 요청을 모두 충족하고 검토가 끝나면 OCR → 항목 대조 → 계산·전략 → 1차 문서 자동 작성
8. 직원/변호사는 원문 근거와 AI 검토, 부족한 정보·법률 가정을 확인하고 보완
9. lawyer / lawyer 로 최종 검토. 실제 접수·서명·개인정보는 이 자료로 대체하지 않음

검산: 월 실수령 3,200,000원 / 월 총급여 3,550,000원 / 월 공제350,000원
연 실수령38,400,000원 / 채무72,000,000원(원금69,000,000+이자3,000,000)
예금2,400,000원 / 실제 월생활지출1,600,000원 / 세대원1명
연금 기준소득3,550,000원은 실수령액과 다른 값입니다.
거래내역의 ‘급여외 소득 없음’은 무직을 뜻하지 않습니다.
optional_error_checks는 명의·기간 불일치 테스트용이며 정상 자료와 혼합하지 마세요.
manifest.json은 독립적인 검산표이며 업로드할 증빙이 아닙니다.
주민등록번호·전화번호·서명은 의도적으로 제공하지 않았습니다. 해당 최종 제출 항목은 보완 대상입니다.
''', encoding='utf-8')
    for record in manifest:
        record['sha256'] = hashlib.sha256((out / record['file']).read_bytes()).hexdigest()
    metadata = {'synthetic': True, 'name': name, 'as_of': as_of, 'court_id': 'CT01',
        'automatic_case_creation': False, 'expected': {'monthly_net_income': 3200000,
        'monthly_gross_income': 3550000, 'monthly_deductions': 350000, 'annual_net_income': 38400000,
        'annual_gross_income': 42600000, 'annual_deductions': 4200000, 'debt_principal': 69000000,
        'debt_interest': 3000000, 'debt_total': 72000000, 'assets_total': 2400000,
        'bank_balance': 2400000, 'living_expenses': 1600000, 'household_size': 1,
        'housing_deposit': 0, 'housing_cost': 0, 'insurance_surrender': 0, 'employment_type': '급여소득자'},
        'intentionally_missing': ['실제 주민등록번호', '실제 연락처', '서명·날인', '최종 법률 판단'], 'documents': manifest}
    (out / 'manifest.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    bundle = ROOT / 'examples/ocr_retest_bundle.zip'
    with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(out.rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(out))
    return {'name': name, 'documents': len(manifest), 'bundle': str(bundle.relative_to(ROOT)), 'case_created': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--as-of', default='2026-10-04')
    print(json.dumps(generate(parser.parse_args().as_of), ensure_ascii=False))
