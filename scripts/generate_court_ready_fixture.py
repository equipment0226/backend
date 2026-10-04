"""Generate a detailed fictional evidence bundle; never seed a database or parser.

The PDFs are deliberately marked practice documents, not official replicas.
The independent manifest and legal-decision examples are not upload evidence.
"""
from __future__ import annotations

import argparse
import calendar
import csv
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import zipfile

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'examples/court_ready_fixture'
AS_OF = '2026-10-04'
BALANCE_DATE = '2026-09-30'
PERIOD = '2025-10-01 ~ 2026-09-30'
BANK_PERIOD = '2025-10-04 ~ 2026-10-04'
NAME = '박지우'
PHONE = '010-0000-0000'
ADDRESS = '서울특별시 가상구 새출발로 120 가상빌라 301호'
EMPLOYER = '가상다온물류 주식회사'
EMPLOYER_ADDRESS = '서울특별시 가상구 연습산업로 45 가상물류동 2층'
WARNING = '가상 연습자료 · 실제 제출 불가'
FONT_PATHS = ['C:/Windows/Fonts/malgun.ttf', '/usr/share/fonts/truetype/nanum/NanumGothic.ttf',
              '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc']
MONTHS = [f'{2025 + (9 + i) // 12}-{(9 + i) % 12 + 1:02d}' for i in range(12)]
PAY = {'gross': 3900000, 'deductions': 450000, 'net': 3450000}
EXPENSES = [('월세', 650000), ('식비', 450000), ('교통비', 100000), ('통신비', 60000),
            ('공과금', 110000), ('의료비', 50000), ('보험료', 80000), ('기타 생활비', 150000)]
BANKS = [{'name': '가상새봄은행', 'account': 'TEST-JIWOO-A-001', 'closing': 1800000},
         {'name': '가상온유은행', 'account': 'TEST-JIWOO-B-002', 'closing': 700000}]
LOANS = [
    {'name': '가상새봄은행', 'loan': 'TEST-LOAN-JW-A', 'principal': 31500000, 'interest': 700000,
     'first_amount': 45000000, 'started': '2021-03-15', 'maturity': '2026-12-15',
     'monthly_principal': 600000, 'annual_rate': '0.072', 'address': '서울특별시 가상구 금융연습로 11 3층',
     'phone': '02-0000-0001', 'cause': '신용대출 · 치료비 및 생활비, 기존 고금리 채무 대환', 'account': 0},
    {'name': '가상한결저축은행', 'loan': 'TEST-LOAN-JW-B', 'principal': 23000000, 'interest': 500000,
     'first_amount': 30000000, 'started': '2022-09-20', 'maturity': '2027-03-20',
     'monthly_principal': 300000, 'annual_rate': '0.09', 'address': '서울특별시 가상구 금융연습로 22 5층',
     'phone': '02-0000-0002', 'cause': '신용대출 · 임차보증금과 이사비, 기존 채무 상환', 'account': 0},
    {'name': '가상디딤캐피탈', 'loan': 'TEST-LOAN-JW-C', 'principal': 13000000, 'interest': 300000,
     'first_amount': 18000000, 'started': '2023-08-10', 'maturity': '2027-08-10',
     'monthly_principal': 200000, 'annual_rate': '0.12', 'address': '서울특별시 가상구 금융연습로 33 4층',
     'phone': '02-0000-0003', 'cause': '신용대출 · 누적 이자 상환과 긴급 생활자금', 'account': 1},
]


def won(value):
    return f'{value:,}원'


def checksum_digit(first12):
    return (11 - sum(int(n) * weight for n, weight in zip(first12, [2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5])) % 11) % 10


def invalid_resident_id():
    prefix = '920415100000'
    return prefix[:6] + '-' + prefix[6:] + str((checksum_digit(prefix) + 1) % 10)


RESIDENT_ID = invalid_resident_id()


class EvidencePDF:
    """A4 evidence-style tables, measured wrapping and repeated page safeguards."""
    def __init__(self, path, title, issuer, code):
        self.path, self.title, self.issuer, self.code = path, title, issuer, code
        font_path = next((Path(p) for p in FONT_PATHS if Path(p).is_file()), None)
        if font_path is None:
            raise RuntimeError('A Korean font is required to generate the practice documents.')
        self.font_path = font_path
        self.font = pymupdf.Font(fontfile=str(font_path))
        self.pdf = pymupdf.open()
        self.y = 0
        self.page()

    def lines(self, text, width, size):
        result = []
        for paragraph in str(text).split('\n'):
            current = ''
            for char in paragraph:
                if current and self.font.text_length(current + char, fontsize=size) > width:
                    result.append(current)
                    current = ''
                current += char
            result.append(current)
        return result

    def page(self, subtitle=None):
        self.p = self.pdf.new_page(width=595, height=842)
        self.p.insert_font(fontname='KR', fontfile=str(self.font_path))
        self.p.draw_rect((35, 23, 560, 47), fill=(1, .94, .90), color=(.75, .28, .18), width=.5)
        self.p.insert_text((44, 40), WARNING + ' / 관인·직인·실제 서명 없음', fontname='KR', fontsize=10,
                           color=(.6, .15, .08))
        self.p.insert_text((38, 77), self.title, fontname='KR', fontsize=18, color=(.08, .2, .27))
        self.p.insert_text((39, 97), f'{self.issuer} | 문서번호 {self.code} | 작성일 {AS_OF}',
                           fontname='KR', fontsize=8, color=(.3, .4, .45))
        self.p.draw_line((38, 105), (557, 105), color=(.2, .4, .42), width=1)
        self.y = 124
        if subtitle:
            self.text(subtitle, size=10)

    def reserve(self, height):
        if self.y + height > 773:
            self.page('계속 · 동일 문서의 이어지는 내용')

    def text(self, value, size=9, color=(.12, .16, .19), space=5):
        lines = self.lines(value, 510, size)
        self.reserve(len(lines) * (size + 5) + space)
        for line in lines:
            self.p.insert_text((42, self.y), line, fontname='KR', fontsize=size, color=color)
            self.y += size + 5
        self.y += space

    def section(self, title):
        self.reserve(31)
        self.p.draw_rect((38, self.y - 13, 557, self.y + 10), fill=(.91, .95, .95), color=None)
        self.p.insert_text((45, self.y + 2), title, fontname='KR', fontsize=10, color=(.12, .3, .32))
        self.y += 29

    def table(self, headers, rows, widths=None, size=8.2):
        widths = widths or [519 / len(headers)] * len(headers)
        assert abs(sum(widths) - 519) < .1

        def row(cells, heading=False):
            wrapped = [self.lines(cell, width - 12, size) for cell, width in zip(cells, widths)]
            height = max(22, max(map(len, wrapped)) * (size + 4) + 8)
            if self.y + height > 773:
                self.page('계속 · 표의 항목과 단위는 앞 페이지와 같습니다.')
                if not heading:
                    row(headers, True)
            x = 38
            for col, width in enumerate(widths):
                self.p.draw_rect((x, self.y - 12, x + width, self.y + height - 12),
                                 color=(.72, .78, .8), fill=(.93, .96, .97) if heading else None, width=.4)
                for index, line in enumerate(wrapped[col]):
                    self.p.insert_text((x + 6, self.y + index * (size + 4)), line, fontname='KR', fontsize=size)
                x += width
            self.y += height
        row(headers, True)
        for cells in rows:
            row([str(cell) for cell in cells])
        self.y += 11

    def identity(self, extra=()):
        self.text(f'성명: {NAME} / 주민등록번호: {RESIDENT_ID} (검사숫자 무효인 연습용 번호)')
        for line in extra:
            self.text(line)

    def save(self):
        count = len(self.pdf)
        for number, page in enumerate(self.pdf, 1):
            page.draw_line((38, 790), (557, 790), color=(.65, .7, .72), width=.5)
            page.insert_text((39, 806), f'{WARNING} | 존재하지 않는 기관·계좌·주소 | {number}/{count}쪽',
                             fontname='KR', fontsize=8, color=(.65, .2, .12))
            page.insert_text((39, 820), '공식 발급문서가 아닙니다. 서명란은 의도적으로 비워 두며 제출·신원확인에 사용할 수 없습니다.',
                             fontname='KR', fontsize=7, color=(.45, .45, .45))
        self.pdf.subset_fonts()
        self.pdf.save(self.path, deflate=True, garbage=4)
        self.pdf.close()
        return count


def financial_schedule():
    loans = []
    for item in LOANS:
        loan = dict(item)
        opening = loan['principal'] + 12 * loan['monthly_principal']
        loan['opening_principal'] = opening
        loan['schedule'] = []
        for month in MONTHS:
            interest = int((Decimal(opening) * Decimal(loan['annual_rate']) / 12).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            principal = loan['monthly_principal']
            loan['schedule'].append({'month': month, 'opening_principal': opening, 'principal_paid': principal,
                                     'interest_paid': interest, 'payment': principal + interest,
                                     'closing_principal': opening - principal})
            opening -= principal
        assert opening == loan['principal']
        loans.append(loan)
    banks = []
    for index, item in enumerate(BANKS):
        monthly = []
        raw = []
        for offset, month in enumerate(MONTHS):
            transactions = []
            def tx(day, description, party, incoming=0, outgoing=0):
                actual_day = min(day, calendar.monthrange(int(month[:4]), int(month[5:]))[1])
                transactions.append({'date': f'{month}-{actual_day:02d}', 'description': description, 'counterparty': party,
                                     'deposit': incoming, 'withdrawal': outgoing})
            if index == 0:
                tx(25, '급여', EMPLOYER, incoming=PAY['net'])
                tx(26, '본인 계좌이체', BANKS[1]['account'], outgoing=1110000)
                for day, label, party, amount in [(27, '주거비', '가상임대인 이새움', 650000),
                    (27, '공과금', '가상공공요금센터', 110000), (27, '통신비', '가상통신', 60000),
                    (28, '보험료', '가상온기생명', 80000)]:
                    tx(day, label, party, outgoing=amount)
            else:
                tx(26, '본인 계좌이체', BANKS[0]['account'], incoming=1110000)
                for day, label, amount in [(27, '식비', 450000), (27, '교통비', 100000),
                                            (28, '의료비', 50000), (28, '기타 생활비', 150000)]:
                    tx(day, label, '가상생활결제센터', outgoing=amount)
            repayment = 0
            for loan in loans:
                if loan['account'] == index:
                    payment = loan['schedule'][offset]['payment']
                    tx(29, '대출 원리금', loan['name'], outgoing=payment)
                    repayment += payment
            raw.extend(transactions)
            monthly.append({'month': month, 'salary': PAY['net'] if index == 0 else 0,
                            'living': 900000 if index == 0 else 750000, 'repayment': repayment,
                            'deposits': sum(t['deposit'] for t in transactions),
                            'withdrawals': sum(t['withdrawal'] for t in transactions)})
        balance = item['closing'] - sum(t['deposit'] - t['withdrawal'] for t in raw)
        opening = balance
        assert opening >= 0
        for transaction in raw:
            transaction_date = date.fromisoformat(transaction['date'])
            assert date(2025, 10, 4) <= transaction_date <= date.fromisoformat(AS_OF)
            balance += transaction['deposit'] - transaction['withdrawal']
            transaction['balance'] = balance
            assert balance >= 0
        assert balance == item['closing']
        for summary in monthly:
            summary['closing'] = [t['balance'] for t in raw if t['date'].startswith(summary['month'])][-1]
        banks.append({**item, 'opening': opening, 'transactions': raw, 'monthly': monthly})
    assert sum(b['closing'] for b in banks) == 2500000
    assert sum(b['monthly'][i]['living'] for b in banks for i in range(12)) == sum(v for _, v in EXPENSES) * 12
    return loans, banks


def generate():
    docs = OUT / 'documents'
    docs.mkdir(parents=True, exist_ok=True)
    manifest = []
    loans, banks = financial_schedule()
    service_days = (date.fromisoformat(BALANCE_DATE) - date(2022, 3, 1)).days + 1
    retirement = int((Decimal(11700000) / 92 * 30 * service_days / 365).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    def new(number, title, catalog, issuer, facts=()):
        filename = f'{number:02d}_{title}_연습용.pdf'
        expected_facts = list(facts)
        if not any(row['key'] == 'client_name' for row in expected_facts):
            expected_facts.insert(0, {'key': 'client_name', 'value': NAME})
        record = {'id': f'FIX{number:02d}', 'catalog_id': catalog, 'title': title,
                  'file': 'documents/' + filename, 'expected_facts': expected_facts, 'synthetic': True}
        manifest.append(record)
        return EvidencePDF(docs / filename, title, issuer, f'PRACTICE-JW-{number:02d}')

    def finish(pdf):
        record = manifest[-1]
        record['pages'] = pdf.save()
        record['sha256'] = hashlib.sha256((OUT / record['file']).read_bytes()).hexdigest()

    def fact(key, value, **extra):
        return {'key': key, 'value': value, **extra}

    p = new(1, '주민등록표 등본', 'D01', '가상동 행정연습센터', [fact('client_name', NAME), fact('resident_id', RESIDENT_ID), fact('address', ADDRESS), fact('household_size', 1)])
    p.identity([f'주소: {ADDRESS}', f'본인 연락처: {PHONE}', '세대주: 박지우 / 세대원: 박지우 1명', '전입일: 2022-09-20 / 변동 사유: 전입'])
    p.section('세대 구성 및 등록사항')
    p.table(['관계', '성명', '생년월일', '전입일', '동거 여부'], [['본인/세대주', NAME, '1992-04-15', '2022-09-20', '1인 단독 거주']])
    p.table(['발급 항목', '선택 내용'], [['세대 구성 사유/일자', '전입으로 세대 구성 / 2022-09-20'], ['주소 표시', '도로명 및 상세주소 전체'], ['주민번호 표시', '전체 자리의 무효 예시번호'], ['다른 세대원', '없음']])
    p.text('주소와 행정구역은 존재하지 않습니다. 과거 주민등록번호 검사식의 검증 숫자를 의도적으로 틀리게 만든 자료입니다.')
    finish(p)

    p = new(2, '주민등록표 초본', 'D02', '가상동 행정연습센터')
    p.identity(['발급범위: 최근 10년 주소변동 전체 / 성명 변경 없음 / 군복무 해당사항 없음'])
    p.section('주소 변동 이력')
    p.table(['변동일', '이전 주소 또는 현주소', '사유'], [['2016-03-01', '서울특별시 가상구 첫걸음로 20 101호', '전입'], ['2020-01-15', '서울특별시 가상구 회복길 9 201호', '전입'], ['2022-09-20', ADDRESS, '현재 주소 전입']],[82,330,107])
    p.text('최근 10년 전출입 이력은 위 3건입니다. 최근 1년 주소 변동은 없습니다. 상세 주소 생략 없이 작성한 연습자료입니다.')
    finish(p)

    p = new(3, '가족관계증명서 상세', 'D03', '가상 가족등록 연습센터', [fact('children_count', 0), fact('dependent_count', 0)])
    p.identity(['등록기준지: 서울특별시 가상구 가족연습로 3', '본인 혼인상태: 미혼 / 자녀 없음 / 실제 부양가족 없음'])
    p.table(['구분', '성명', '출생', '현재 생계 관계'], [['본인', NAME, '1992-04-15', '급여소득으로 단독 생계'], ['부', '박가온(가상)', '1964-01-10', '별거·독립 생계 / 정기 부양 송금 없음'], ['모', '윤다솜(가상)', '1966-05-12', '별거·독립 생계 / 정기 부양 송금 없음']],[55,125,90,249])
    p.text('가족관계와 생계 관계는 별개 항목입니다. 서류상 가족 3명이라는 이유로 생계비 인정인원을 3명으로 확정할 수 없습니다.')
    finish(p)
    p = new(4, '혼인관계증명서 상세', 'D04', '가상 가족등록 연습센터', [fact('children_count', 0)])
    p.identity(['증명 범위: 상세 / 현재 및 과거 혼인·이혼 전체 이력'])
    p.table(['사항', '기재 결과'], [['현재 혼인', '없음'], ['과거 혼인', '없음'], ['이혼·배우자', '해당사항 없음'], ['자녀', '없음']])
    p.text('해당사항 없음은 이 가상 인물의 설정이며 실제 가족등록자료 조회 결과가 아닙니다.')
    finish(p)

    p = new(5, '계좌통합조회 및 잔액확인', 'D35', '가상 금융조회 연습센터')
    p.identity([f'조회범위: 전체 금융기관 / 조회 기준일: {AS_OF}', '활동 계좌 2개 / 휴면·해지·증권 계좌 없음'])
    p.table(['금융기관', '계좌 식별', '종류', '확인 잔액'], [[b['name'], b['account'], '입출금', won(b['closing'])] for b in banks],[100,185,65,169])
    p.text('두 계좌 사이의 본인 이체는 소득이나 지출로 중복 계산하지 않습니다. 계좌별 상세 내역과 잔액확인은 별도 서류에 있습니다.')
    p.text('잔액 합계: 2,500,000원 / 수중 현금: 0원 / 주식·펀드·가상자산·외화 예금: 없음')
    finish(p)

    for number, loan in enumerate(loans, 6):
        p = new(number, '부채증명서_' + loan['name'], 'D38', loan['name'] + ' 여신관리 연습부',
                [fact('creditor_name', loan['name']), fact('creditor_principal', loan['principal']), fact('creditor_interest', loan['interest']), fact('creditor_total', loan['principal'] + loan['interest'])])
        p.identity([f'채권자: {loan["name"]}', f'채권자 주소: {loan["address"]}', f'채권자 전화: {loan["phone"]} / 팩스: 02-0000-0099',
                    f'대출번호: {loan["loan"]} / 채무잔액 기준일: {BALANCE_DATE}'])
        p.section('채권의 발생과 현재액')
        p.text(f'채권 발생일: {loan["started"]} / 최초 대출액: {won(loan["first_amount"])}')
        p.text(f'차용용도: {loan["cause"]} / 만기: {loan["maturity"]}')
        p.text(f'원금: {won(loan["principal"])} / 이자: {won(loan["interest"])}')
        p.text(f'채무 합계: {won(loan["principal"] + loan["interest"])} / 담보 없음 / 보증인 없음')
        p.table(['현재액 산정 항목', '내역'], [['원금', '최초 대출원금에서 기준일까지 납입한 원금을 차감한 장부잔액'], ['미지급 이자', '2025-09-30 이전 발생한 미납이자의 유예잔액. 아래 12개월 지급이자와 별개'], ['현재 상환방식', f'월 원금 {won(loan["monthly_principal"])} 및 잔액별 약정이자. 나머지 원금은 만기에 상환'], ['약정 연이율', f'{Decimal(loan["annual_rate"])*100}% / 검산용 월이자는 기초원금 × 연이율 ÷ 12, 원 단위 반올림'], ['양도·분쟁·담보', '채권양도 없음 / 원금 다툼 없음 / 담보 및 보증채무 없음']],[115,404])
        p.text('발급 담당자: 가상 여신담당자 / 기명 확인만 표시 / 실제 날인·서명 없음')
        p.page('최근 12개월 원리금 납입 내역 · 계좌 거래내역과 대조')
        p.text(f'채권자: {loan["name"]} / 대출번호: {loan["loan"]} / 기간: {PERIOD}')
        p.table(['월', '기초 원금', '원금 납입', '이자 납입', '납입 합계', '기말 원금'], [[r['month'], *[f'{r[k]:,}' for k in ('opening_principal','principal_paid','interest_paid','payment','closing_principal')]] for r in loan['schedule']], [65,94,85,85,95,95])
        p.text(f'12개월 원금 납입합계: {won(sum(r["principal_paid"] for r in loan["schedule"]))} / 이자 납입합계: {won(sum(r["interest_paid"] for r in loan["schedule"]))}')
        p.text('현재 미지급 이자 유예잔액은 이 표의 당기 이자 납부 합계와 상계하지 않았습니다. 기준일 이후 이자는 별도이며 장래 이자를 현재 원금에 더하지 않습니다.')
        finish(p)

    p = new(9, '근로소득원천징수영수증', 'D06', EMPLOYER + ' 급여연습팀', [fact('income_gross', 39900000, frequency='annual')])
    p.identity([f'근무처: {EMPLOYER}', '귀속연도: 2025년 / 소득 확인기간: 2025-01-01 ~ 2025-12-31'])
    p.section('근무처별 소득 명세 및 비과세 소득')
    p.table(['구분', '2025년 1~9월', '2025년 10~12월', '연간 합계'], [['지급액 합계', '30,600,000', '11,700,000', '42,300,000'], ['비과세 식대', '1,800,000', '600,000', '2,400,000'], ['과세 총급여', '28,800,000', '11,100,000', '39,900,000'], ['세금·사회보험 공제', '3,600,000', '1,350,000', '4,950,000'], ['실수령 지급액', '27,000,000', '10,350,000', '37,350,000']],[155,120,120,124])
    p.text('2025년 과세 총급여: 39,900,000원 / 지급액 총합계: 42,300,000원 / 비과세 소득: 2,400,000원')
    p.text('현재 12개월 급여 자료는 2025-10~2026-09이며 이 영수증의 귀속연도와 다릅니다. 2025-10-01 임금 인상 전후 지급액을 구분했습니다.')
    p.section('세액 정산 내역')
    p.table(['구분', '소득세', '지방소득세'], [['결정세액', '830,000', '83,000'], ['기납부세액', '830,000', '83,000'], ['차감징수세액', '0', '0']])
    p.text('표시된 세액·공제는 합성 급여장부의 입력값입니다. 세법상 공제한도·연도별 요율을 검증하는 예시가 아니며 실제 신고에 사용할 수 없습니다.')
    finish(p)
    p = new(10, '소득금액증명', 'D05', '가상 세무연습센터')
    p.identity(['증명 구분: 근로소득자 / 귀속연도: 2025년 / 과세기간: 2025-01-01 ~ 2025-12-31'])
    p.table(['소득 구분', '지급자', '과세 대상 총급여', '근로소득금액'], [['근로소득', EMPLOYER, '39,900,000원', '28,665,000원']],[75,160,135,149])
    p.text('근로소득공제 차감 후 소득금액: 28,665,000원 / 근로소득공제 표시액: 11,235,000원')
    p.text('사업소득 없음 / 연금·이자·배당 등 별도 신고소득 없음. 소득금액은 급여통장 실수령액이 아닙니다.')
    p.text('사용 목적: 개인회생 상담 연습 / 수량: 1부 / 발급 담당자: 가상 세무담당자')
    finish(p)

    p = new(11, '급여명세서 12개월', 'D07', EMPLOYER + ' 급여연습팀', [fact('income_gross', PAY['gross'], frequency='monthly'), fact('income_deductions', PAY['deductions'], frequency='monthly'), fact('income_net', PAY['net'], frequency='monthly')])
    deductions = [('국민연금', 170000), ('건강보험료', 140000), ('장기요양보험료', 18000), ('고용보험료', 33000), ('소득세', 80000), ('지방소득세', 9000)]
    for index, month in enumerate(MONTHS):
        if index:
            p.page()
        p.identity([f'근무처: {EMPLOYER} / 사원번호: TEST-EMP-JW-001', f'급여월: {month} / 지급일: {month}-25', '소속: 운영관리팀 / 직위: 물류관리 주임 / 근로형태: 정규직'])
        p.section('지급 항목')
        p.table(['항목', '계산 근거', '지급액'], [['기본급', '근로계약 고정월급', '3,200,000원'], ['고정 연장근로수당', '근로조건 확인서상 고정수당', '500,000원'], ['비과세 식대', '합성 급여장부상 식대', '200,000원']],[135,230,154])
        p.text('지급총액: 3,900,000원')
        p.section('공제 항목')
        p.table(['공제 항목', '공제액', '비고'], [[label, won(value), '연습용 고정 장부값'] for label, value in deductions],[160,135,224])
        p.text('공제합계: 450,000원 / 실지급액: 3,450,000원', size=11)
        p.text(f'수령계좌: {BANKS[0]["name"]} {BANKS[0]["account"]} / 예금주: {NAME}')
        p.text('공제는 실제 요율 산정 예시가 아닌 가상 장부값입니다. 무급휴직·상여·별도 부업 지급은 없습니다.', size=8)
    finish(p)

    p = new(12, '재직증명서 및 근로조건확인서', 'D08', EMPLOYER + ' 인사연습팀', [fact('employer', EMPLOYER), fact('employer_address', EMPLOYER_ADDRESS), fact('employer_phone', '02-0000-0010'), fact('employment_type', '급여소득자')])
    p.identity([f'근무처: {EMPLOYER}', f'직장 주소: {EMPLOYER_ADDRESS}', '직장 전화: 02-0000-0010', '입사일: 2022-03-01 / 재직기간: 2022-03-01 ~ 현재', '담당업무: 물류 출고 일정·재고 관리 / 직위: 주임 / 현재 재직 중'])
    p.table(['근로조건', '기재 내용'], [['고용 형태', '기간의 정함이 없는 정규직'], ['근무 시간', '평일 09:00~18:00 / 주 40시간 / 휴게 1시간'], ['현재 급여 적용일', '2025-10-01부터'], ['월 지급 구성', '기본급 3,200,000원 + 고정수당 500,000원 + 식대 200,000원'], ['지급일 및 지급방식', '매월 25일 본인 새봄은행 계좌 이체'], ['급여 압류·가압류', '현재 없음'], ['퇴직급여', '일반 퇴직금 제도 / 퇴직연금 미가입']],[145,374])
    p.text('대표자명: 최가상 / 인사담당: 홍연습 / 대표·근로자 서명란: 공란(연습자료)')
    p.page('근로조건 변경 이력 및 계속 근무 확인')
    p.table(['기간', '월 지급총액', '월 공제', '월 실수령'], [['2022-03~2024-08', '2,900,000', '300,000', '2,600,000'], ['2024-09~2025-09', '3,400,000', '400,000', '3,000,000'], ['2025-10~현재', '3,900,000', '450,000', '3,450,000']])
    p.text('2025년 10월 인사발령에 따른 임금 인상 이후 12개월 급여는 동일합니다. 현재 휴직·퇴직 예정 통보는 없으나 장래 계속 고용을 보장하는 문서는 아닙니다.')
    p.text('직업은 급여소득자이며 사업자등록 또는 사업매출은 없습니다. 원천징수 2025년 금액과 최근 12개월 금액의 차이는 임금 인상일과 귀속기간 차이입니다.')
    finish(p)

    p = new(13, '건강보험 자격득실 및 납부확인', 'D09', '가상 건강보험 연습센터', [fact('health_qualification', '직장가입자'), fact('employer', EMPLOYER)])
    p.identity([f'사업장: {EMPLOYER}', '가입자 구분: 직장가입자 / 자격취득일: 2022-03-01 / 현재 자격 유지', '조회 범위: 전체 자격 이력 / 현재 체납 없음'])
    p.table(['기간', '가입 구분', '사업장 또는 사유'], [['2020-01~2022-02', '지역가입', '이전 일용 근로·회복기간'], ['2022-03~현재', '직장가입', EMPLOYER]])
    p.table(['납부기간', '건강보험료', '장기요양보험료', '납부 여부'], [[m,'140,000','18,000','완납(연습값)'] for m in MONTHS])
    finish(p)
    p = new(14, '연금산정용 가입내역확인서', 'D11', '가상 연금 연습센터', [fact('pension_assessed_income', 3700000), fact('employer', EMPLOYER)])
    p.identity([f'사업장: {EMPLOYER}', '국민연금 가입일: 2022-03-01 / 사업장가입자 / 현재 가입 유지', '월 기준소득: 3,700,000원 / 실수령 소득과 구분'])
    p.table(['조회월', '가입 상태', '본인 납부액', '체납'], [[m,'사업장가입','170,000원','없음'] for m in MONTHS])
    p.text('장래 노령연금 수급액은 이 자료로 확정하지 않습니다. 납부액은 합성 급여 장부와 일치시키기 위한 연습값입니다.')
    finish(p)

    for number, bank in enumerate(banks, 15):
        p = new(number, '입출금거래내역_' + bank['name'], 'D36', bank['name'] + ' 거래연습센터', [fact('cash_balance', bank['closing'])])
        for quarter in range(4):
            if quarter:
                p.page()
            selected_months = MONTHS[quarter*3:quarter*3+3]
            rows = [t for t in bank['transactions'] if t['date'][:7] in selected_months]
            endmonth = selected_months[-1]
            enddate = f'{endmonth}-{calendar.monthrange(int(endmonth[:4]),int(endmonth[5:]))[1]:02d}'
            segment_start = '2025-10-04' if quarter == 0 else selected_months[0] + '-01'
            if quarter == 3:
                enddate = AS_OF
            p.identity([f'금융기관: {bank["name"]} / 계좌번호: {bank["account"]}',
                        f'전체 조회기간: {BANK_PERIOD}', f'이 페이지 표시구간: {segment_start} ~ {enddate}',
                        f'잔액 기준일: {enddate} / 예금잔액: {won(rows[-1]["balance"])}'])
            p.table(['거래일', '내용·상대방', '입금', '출금', '거래 후 잔액'],
                    [[t['date'], t['description']+' / '+t['counterparty'], f'{t["deposit"]:,}', f'{t["withdrawal"]:,}', f'{t["balance"]:,}'] for t in rows],
                    [69,198,80,80,92], size=7.3)
        p.page('전체 조회기간 합계 및 월별 거래 분류')
        p.identity([f'금융기관: {bank["name"]} / 계좌번호: {bank["account"]}', f'전체 조회기간: {BANK_PERIOD}',
                    f'조회 시작 잔액: {won(bank["opening"])} / 잔액 기준일: {AS_OF}', f'조회 종료 잔액: {won(bank["closing"])}'])
        p.table(['월', '급여 입금', '생활비 출금', '상환 출금'], [[m['month'], f'{m["salary"]:,}', f'{m["living"]:,}', f'{m["repayment"]:,}'] for m in bank['monthly']], [99,140,140,140])
        p.text(f'총 입금: {won(sum(t["deposit"] for t in bank["transactions"]))} / 총 출금: {won(sum(t["withdrawal"] for t in bank["transactions"]))}')
        p.text('본인계좌 이체는 생활비·급여 분류에서 제외했습니다. 전표별 출금은 위 상세 거래표에 모두 기재되어 있으며 누락·상계 거래는 없습니다.')
        p.text('급여외 소득 없음 / 계좌 압류·가압류 없음 / 은행 이자·수수료는 이 합성기간에 0원으로 설정했습니다.')
        p.text('2026-10-01~2026-10-04 거래 없음. 2026년 10월 급여 지급일은 25일로 아직 지급 전이며 급여명세서는 완료된 2025년 10월~2026년 9월 12개월분입니다.')
        finish(p)

    p = new(17, '주거용 임대차계약서', 'D33', '가상임대인·임차인 계약 연습', [fact('housing_deposit', 12000000), fact('housing_cost', 650000), fact('housing_type', '월세')])
    p.identity([f'소재지: {ADDRESS}', '건물 형태: 다세대주택 / 전용면적: 28.5㎡ / 용도: 주거', '임차인: 박지우 / 임대인: 이새움(가상)', '임대인 연락처: 010-0000-0001 / 임대인 주소: 서울특별시 가상구 임대연습길 7'])
    p.table(['계약 항목', '내용'], [['임대차 종류', '보증금 있는 월세'], ['임대차 기간', '2026-09-01 ~ 2028-08-31 (갱신)'], ['처음 거주한 날', '2022-09-20'], ['임차보증금', '12,000,000원'], ['월 차임', '650,000원 / 매월 27일 지급'], ['관리비', '별도 고정 관리비 없음 / 실제 공공요금 본인 부담'], ['보증금 반환채권', '양도·질권 설정 없음 / 현재 차임 연체 없음'], ['주택 소유자 본인 여부', '아니오 / 임대인 소유']],[155,364])
    p.text('보증금은 최초 계약 시 납입한 12,000,000원을 그대로 승계합니다. 갱신계약의 추가 보증금 납입은 0원입니다.')
    p.text('전입신고일: 2022-09-20 / 확정일자 기재: 2022-09-20(연습 설정). 대항력·우선변제권·면제범위는 별도 법률 검토 사항입니다.')
    p.text('임대인 서명: __________________ / 임차인 서명: __________________ (실제 서명 없음)')
    finish(p)
    p = new(18, '보증금 및 차임 납부확인', 'SUPPORT-HOUSING', '가상 임대차 납부 연습')
    p.identity([f'임차물건: {ADDRESS}', '보증금 지급일: 2022-09-20 / 지급액: 12,000,000원 / 반환받은 금액: 0원', '잔여 보증금: 12,000,000원 / 차임 미납 없음'])
    p.table(['납부월', '납부일', '월 차임', '지급 계좌'], [[m,m+'-27','650,000원',BANKS[0]['account']] for m in MONTHS],[78,100,95,246])
    p.text('임대인 확인란: 이새움(가상 인물) / 서명 공란. 거래 확인은 해당 은행 원문 내역과 함께 대조합니다.')
    finish(p)
    p = new(19, '보험가입 조회결과', 'D39', '가상 보험조회 연습센터', [fact('insurance_contracts', True)])
    p.identity([f'조회 기준일: {BALANCE_DATE} / 조회범위: 전체 보험회사', '보유 보험계약: 1건 / 다른 보장·저축·연금보험 없음'])
    p.table(['보험회사', '상품', '증권번호', '계약상태'], [['가상온기생명','새출발보장 연습보험','TEST-POLICY-JW-001','유지']],[110,160,165,84])
    p.text('계약자·피보험자·수익자: 모두 박지우 / 계약일: 2018-04-15 / 월 보험료: 80,000원')
    p.text('환급 예상액과 보험계약대출은 보험회사 별도 확인서에서 확인합니다.')
    finish(p)
    p = new(20, '보험 해약환급금 확인서', 'D39', '가상온기생명 계약연습부', [fact('insurance_surrender', 2400000)])
    p.identity(['보험회사: 가상온기생명 / 상품명: 새출발보장 연습보험', '증권번호: TEST-POLICY-JW-001 / 계약일: 2018-04-15', f'환급금 기준일: {BALANCE_DATE}'])
    p.table(['환급 산정 항목', '금액'], [['해약환급금', '2,400,000원'], ['보험계약대출 원금', '0원'], ['보험계약대출 이자', '0원'], ['미납 보험료', '0원'], ['실제 해약 시 예상 순환급액', '2,400,000원']])
    p.text('해약환급금: 2,400,000원 / 계약 해지는 실행하지 않은 상태입니다. 향후 해약 여부와 보호 필요성은 검토 사항입니다.')
    finish(p)
    p = new(21, '예상퇴직금 확인서', 'SUPPORT-RETIREMENT', EMPLOYER + ' 인사연습팀', [fact('retirement_expected', retirement)])
    p.identity([f'근무처: {EMPLOYER}', '입사일: 2022-03-01 / 계속 근무 중', f'산정 기준일: {BALANCE_DATE}', '퇴직급여 제도: 일반 퇴직금(퇴직연금 미가입)'])
    p.table(['산정 요소', '확인 값'], [['최근 3개월 지급총액', '2026-07~09 / 11,700,000원'], ['평균임금 산정 일수', '92일'], ['계속근로 일수', f'{service_days}일'], ['추정 산식', '11,700,000 ÷ 92 × 30 × 계속근로일수 ÷ 365'], ['예상 퇴직금 총액', won(retirement)], ['중간 정산·담보 제공', '없음'], ['퇴직연금(DB/DC/IRP)', '미가입']],[185,334])
    p.text(f'예상 퇴직금 총액: {won(retirement)}')
    p.text('이 금액은 사용자가 제시한 현재 기준 예상 총액입니다. 압류금지액·면제재산·청산가치 공제 후 가액이 아니며 법률 판단을 자동 확정하지 않습니다.')
    p.text('대표자명: 최가상 / 서명·날인 공란 / 퇴직 또는 지급을 확약하는 문서가 아닙니다.')
    finish(p)

    p = new(22, '지적전산자료 조회결과', 'D28', '가상 부동산조회 연습센터', [fact('real_estate_ownership', False)])
    p.identity([f'조회범위: 전국 / 조회 기준일: {BALANCE_DATE}', '소유 부동산 검색 결과: 없음'])
    p.table(['조회 항목', '결과'], [['토지·건물·집합건물', '본인 명의 없음'], ['공유지분·분양권', '본인 진술 및 합성 조회에 없음'], ['최근 5년 소유변동', '없음'], ['현재 주거', '임차주택이며 본인 소유 아님']])
    finish(p)
    p = new(23, '자동차 소유 조회확인', 'SUPPORT-VEHICLE', '가상 차량등록 연습센터', [fact('vehicle_ownership', False)])
    p.identity([f'조회 기준일: {BALANCE_DATE}', '소유 자동차: 없음 / 오토바이·건설기계 없음'])
    p.table(['조회 범위', '결과'], [['전국 본인 명의 현재등록', '0건'], ['최근 5년 취득·처분', '없음'], ['이전 처분 참고', '2020-02 경차 매각은 5년 밖의 과거 이력. 당시 대금은 생활비·기존 채무 상환에 사용했다는 별도 진술']])
    finish(p)
    p = new(24, '지방세 세목별 과세증명', 'D29', '가상 지방세 연습센터', [fact('tax_arrears', 0)])
    p.identity(['조회기간: 2021-01-01 ~ 2026-09-30 / 조회지역: 전국 / 조회세목: 모든 세목'])
    p.table(['연도', '부동산 재산세', '자동차세', '체납'], [[str(year),'과세 없음','과세 없음','0원'] for year in range(2021,2027)])
    p.text('지방세 체납액: 0원 / 누락 지역·세목 없음 / 조회기간 이전 과세는 이 증명의 대상이 아닙니다.')
    finish(p)
    p = new(25, '국세 납세 및 사업등록 사실확인', 'SUPPORT-TAX', '가상 세무연습센터', [fact('tax_arrears', 0)])
    p.identity([f'증명 기준일: {BALANCE_DATE}', '국세 체납액: 0원 / 징수유예·체납처분 유예 없음', '사업자등록 사실: 없음 / 현재 사업소득 없음'])
    p.table(['확인 항목', '결과'], [['근로소득 신고', '2025년 귀속 소득금액증명 별첨'], ['종합소득 사업신고', '없음'], ['국세 체납', '없음'], ['조세 관련 소송', '없음']])
    p.text('급여소득자에게 사업자등록증 제출을 요구하는 서류가 아닙니다. 사업영위 여부에 관한 본인 설정을 확인하기 위한 보조자료입니다.')
    finish(p)

    p = new(26, '채무발생 경위 및 생활현황 진술', 'SUPPORT-HISTORY', '박지우 본인 진술 연습', [fact('phone', PHONE), fact('living_expenses', 1650000, frequency='monthly')])
    p.identity([f'현주소: {ADDRESS}', f'휴대전화: {PHONE}', '최종 학력: 2011-02-10 가상새길고등학교 졸업', '혼인·이혼 이력: 없음', '거주 시작일: 2022-09-20'])
    p.section('학력·직업·주거 경력')
    p.table(['기간', '기재 내용'], [['2016-03~2020-01', '가상한빛배송 근무 / 배송보조 / 월 수입 변동'], ['2020-02~2021-02', '단기 물류 근무와 구직 / 생활비 부족'], ['2021-03~2021-08', '본인 부상 치료·회복으로 근무 중단 / 진료비 연습자료 별첨'], ['2021-09~2022-02', '단기 근로 재개'], ['2022-03~현재', EMPLOYER+' 정규직 / 물류관리'], ['2022-09~현재', '현재 월세주택 단독 거주 / 보증금 12,000,000원']],[125,394])
    p.section('채무가 늘어난 과정')
    p.text('2020년까지 소득이 일정하지 않아 생활비와 기존 대출 상환을 위해 차용했습니다. 2021년 3월 부상 치료와 회복기간에는 근로소득이 줄었고, 치료비 및 필수생활비를 충당하면서 기존 고금리 채무를 대환했습니다. 치료·대출·이체 자료에 기재된 지출과 진술 범위는 별도로 표시했습니다.')
    p.text('2022년 3월 정규직으로 취업했지만 과거 원리금 부담을 함께 감당해야 했습니다. 같은 해 거처를 옮기며 보증금과 이사 비용이 들었고, 2023년에는 누적 이자와 긴급 생활비를 다시 빌렸습니다. 이후 새 대출로 해결하려는 방식이 채무를 줄이지 못했습니다.')
    p.page('현재 상환 곤란의 원인과 확인해야 할 사정')
    p.table(['발생일', '계약', '최초 금액', '주요 사용처'], [[loan['started'],loan['loan'],won(loan['first_amount']),loan['cause']] for loan in loans],[85,130,100,204])
    p.text('최근 12개월은 약정된 원금 일부와 당기 이자를 납입했습니다. 다만 과거에 발생하여 유예된 미지급 이자 1,500,000원이 남아 있고, 2026년 12월부터 만기에 남은 원금을 한꺼번에 갚아야 합니다. 현재 소득과 보유 재산만으로 이 만기 채무를 일시에 갚기 어렵기 때문에 상담을 신청합니다.')
    p.text('현재 월 실수령소득: 3,450,000원 / 실제 월생활지출: 1,650,000원. 최근 급여와 은행 내역을 기준으로 정기 소득과 필수지출을 구분했습니다. 법원이 인정할 생계비·변제기간·면제재산은 아직 결정되지 않았습니다.')
    p.text('최근 1년 신규 차용·재산 처분·가족 대납 없음. 도박·주식·가상자산 투자에 사용한 채무 없음. 사업소득·가족 차용금·보증채무·급여 압류 없음. 과거 개인회생·파산·면책 이력 없음. 현재 진행 중인 채권소송·지급명령·강제집행은 없다는 본인 진술입니다.')
    p.text('과거 절차 이용 이력: 개인회생·파산·면책·워크아웃 이용 사실 없음(본인 진술).')
    p.text('소득을 유지하면서 감당할 수 있는 계획을 세우고 싶습니다. 장래 직장 유지나 인가를 보장하는 진술은 하지 않습니다. 모든 채무와 자산을 자료에 따라 확인받고 누락된 내용이 있으면 보완하겠습니다.')
    p.text('본인 기명: 박지우 / 작성일: 2026-10-04 / 자필 서명: 공란. 이 문서는 당사자 진술이며 기관 발급 증빙과 구별합니다.')
    finish(p)

    p = new(27, '월 생활지출 명세', 'SUPPORT-EXPENSE', '박지우 지출 정리 연습', [fact('living_expenses', 1650000)])
    p.identity([f'확인기간: {PERIOD}', '실제 월생활지출: 1,650,000원 / 변제금·본인계좌 이체 제외'])
    p.table(['항목', '월평균', '결제 근거', '비고'], [[label,won(value), '임대차·은행 거래·납부내역 대조', '1인 부담'] for label,value in EXPENSES],[100,105,225,89])
    p.text('합계: 1,650,000원 / 연간 합계: 19,800,000원')
    p.text('저축·채무 원리금 상환과 본인계좌 간 이체는 생활지출 합계에 넣지 않았습니다. 실제 지출을 법률상 인정 생계비로 자동 확정할 수 없습니다. 일회성 치료비는 과거 채무 경위 자료에만 표시했습니다.')
    finish(p)
    p = new(28, '생활지출 납부 및 결제내역', 'SUPPORT-EXPENSE-RECEIPTS', '가상 생활결제 연습센터', [fact('housing_cost', 650000, frequency='monthly')])
    p.identity([f'확인기간: {PERIOD} / 결제 대상자: {NAME}', '은행 거래의 식비·교통·의료·기타 지출은 아래 가상 전표를 월 합산한 금액입니다.'])
    p.table(['전표월', '식비', '교통비', '의료비', '기타', '소계'], [[m,'450,000','100,000','50,000','150,000','750,000'] for m in MONTHS],[89,86,86,86,86,86])
    p.section('고정비 납부내역')
    p.table(['항목', '월 납부액', '전표 식별', '결제일'], [['통신비','60,000원','TEST-PHONE-JW-월별','매월 27일'], ['전기·수도·가스','110,000원','TEST-UTIL-JW-월별','매월 27일'], ['보장보험료','80,000원','TEST-POLICY-JW-001','매월 28일'], ['월 차임','650,000원','임대인 납부확인 별첨','매월 27일']],[90,100,215,114])
    p.text('개별 상점·의료기관은 존재하지 않는 연습용 결제처입니다. 이 내역과 은행 출금 합계는 일치하지만 실제 영수증의 효력은 없습니다.')
    finish(p)
    p = new(29, '과거 치료비 납입 및 차입금 사용내역', 'SUPPORT-HISTORY-EVIDENCE', '가상 회복의원·본인 기록 연습')
    p.identity(['진료기간: 2021-03-02 ~ 2021-08-31 / 진료 대상자: 박지우', '진료 내역: 부상 치료 및 회복 프로그램(가상 설정) / 진료비 본인부담 납입 합계: 12,000,000원'])
    p.table(['납입일', '비용 구분', '납입액', '전표'], [['2021-03-15','입원·수술 본인부담','8,000,000원','TEST-MED-JW-001'], ['2021-04-15','회복치료','2,000,000원','TEST-MED-JW-002'], ['2021-06-15','후속 회복치료','2,000,000원','TEST-MED-JW-003']],[95,160,115,149])
    p.section('최초 차입금 사용처 정리 · 본인 진술과 합성 거래장부 대조')
    p.table(['계약', '사용처 구분', '금액'], [['A 45,000,000원','치료비 12,000,000 + 생활비 9,000,000 + 종전 채무 대환 24,000,000','45,000,000원'], ['B 30,000,000원','임차보증금 12,000,000 + 이사·가재도구 3,000,000 + 종전 채무 상환 15,000,000','30,000,000원'], ['C 18,000,000원','이자·기존 채무 납부 9,000,000 + 긴급지출 5,000,000 + 직업교육 1,000,000 + 생활비 3,000,000','18,000,000원']],[120,280,119])
    p.text('이 사용처 표의 과거 생활비·긴급지출은 본인 기록에 따른 진술입니다. 치료비·보증금처럼 별도 자료가 있는 항목과 동일한 증명력으로 취급하지 않습니다. 현재 채무 69,000,000원에 과거 대환금액을 다시 더하지 않습니다.')
    finish(p)

    expected = {'client_name': NAME, 'resident_id': RESIDENT_ID, 'address': ADDRESS, 'phone': PHONE,
        'employer': EMPLOYER, 'employer_address': EMPLOYER_ADDRESS, 'employer_phone': '02-0000-0010',
        'monthly_income': PAY['net'], 'monthly_net_income': PAY['net'], 'annual_income': PAY['net']*12,
        'monthly_gross_income': PAY['gross'], 'monthly_deductions': PAY['deductions'],
        'bank_balance': 2500000, 'principal_total': 67500000, 'interest_total': 1500000, 'total_debt': 69000000,
        'insurance_surrender': 2400000, 'housing_deposit': 12000000, 'housing_cost': 650000,
        'living_expenses': sum(value for _,value in EXPENSES), 'household_size': 1,
        'retirement_expected': retirement, 'assets_total': 16900000 + retirement,
        'employment_type': '급여소득자', 'vehicle_value': 0, 'real_estate_value': 0, 'tax_arrears': 0}
    for bank in banks:
        with (OUT / ('transactions_' + bank['account'] + '.csv')).open('w', encoding='utf-8-sig', newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=['date','description','counterparty','deposit','withdrawal','balance'])
            writer.writeheader();writer.writerows(bank['transactions'])
    (OUT/'financial_reconciliation.json').write_text(json.dumps({'synthetic':True,'loans':loans,'banks':banks,
        'expense_items':dict(EXPENSES),'retirement':{'gross':retirement,'service_days':service_days,'last_three_month_wages':11700000,'average_wage_days':92}},ensure_ascii=False,indent=2),encoding='utf-8')

    application = f'''{WARNING}
성명: {NAME}
휴대전화: {PHONE}
주소: {ADDRESS}
희망 상담: 서울회생법원 관할 개인회생 상담
정규직 급여소득자로 혼자 월세집에 거주합니다. 현재 월 실수령액은 3,450,000원입니다.
과거 치료기간의 소득 감소와 생활비 부족으로 차입했고, 취업 후에도 기존 채무와 보증금 마련 부담이 이어졌습니다.
은행·저축은행·캐피탈 3곳의 채무가 합계 69,000,000원입니다. 원금 일부와 당기 이자는 납입했지만 유예된 이자와 다가오는 만기 원금을 한꺼번에 갚기 어렵습니다.
급여·계좌거래·부채·임대차·보험·퇴직금 자료를 준비했습니다. 필요한 절차와 자료를 상담하고 싶습니다.
이 글은 상담 신청용 가상 문구이며 상세 상담 완료·법률 판단·서류 제출을 대신하지 않습니다.
'''
    consultation = f'''{WARNING}
상담일: {AS_OF} / 내담자: {NAME} / 관할 후보: 서울회생법원
주민등록번호: {RESIDENT_ID} (검사숫자 무효) / 휴대전화: {PHONE}
주민등록상 주소 및 현주소: {ADDRESS} / 송달장소 희망: 현주소
1. 1992-04-15 출생, 미혼, 자녀 및 실제 부양가족 없음, 1인 단독 세대. 부모는 별도 거주·독립 생계.
2. 2011-02-10 가상새길고등학교 졸업. 2016~2020 배송 보조, 2020~2022 단기 근로와 치료기간을 거쳐 2022-03-01부터 {EMPLOYER} 정규직 재직. 직무는 물류 출고 일정·재고 관리, 현재 주임.
3. 2025-10-01부터 월 지급총액3,900,000원, 공제450,000원, 실수령3,450,000원. 2025-10~2026-09 12개월 확인자료. 2025귀속 지급총액42,300,000원·과세급여39,900,000원은 다른 귀속기간이므로 월급 환산에 혼용하지 않음.
4. 차입 경위는 26번 진술과 29번 치료비/사용처 자료 참조. 치료비는 별도 납입 내역이 있고, 과거 생활비 등은 본인 진술 부분으로 구분.
5. 채무 원금67,500,000원 + 과거 발생 후 유예된 이자1,500,000원 =69,000,000원. 3개 무담보 대출. 가족 차용·보증·조세채무 없음. 2026-12부터 만기 잔금 상환이 도래함.
6. 현재 예금은 새봄1,800,000원+온유700,000원=2,500,000원. 매월 두 계좌 사이1,110,000원 이체는 본인 거래이며 소득·지출에 중복 반영하지 않음.
7. 2022-09-20부터 현재 주소에 월세 거주. 보증금12,000,000원, 월 차임650,000원, 차임 연체·보증금 담보제공 없음. 2026-09-01~2028-08-31 갱신 계약.
8. 보험1계약, 해약환급금2,400,000원, 보험대출0원. 일반퇴직금 제도이며 예상 총액{retirement:,}원. 퇴직연금 미가입. 공제 전 금액이므로 압류금지·면제와 청산가치는 변호사 확인 필요.
9. 부동산·차량·주식·가상자산·수중현금 없음. 과거 회생·파산·면책 및 현재 소송·강제집행 없음이라는 본인 진술. 최근1년 신규 차용·자산 처분·가족 대납 없음.
10. 월생활지출1,650,000원=월세650,000+식비450,000+교통100,000+통신60,000+공과금110,000+의료50,000+보험80,000+기타150,000. 원리금 상환은 별도. 실제지출과 인정생계비를 동일시하지 않음.
11. 대리인 선임·변제 시작일·기간·회생위원 보수·추가생계비·면제재산·기납입 비용·이의 여부는 미확정. legal_review_inputs.json은 별도 검토용 가정이며 고객 진술이나 증빙이 아님.
'''
    (OUT/'application.txt').write_text(application,encoding='utf-8')
    (OUT/'staff_consultation.txt').write_text(consultation,encoding='utf-8')
    write_legal_example(retirement)
    write_coverage(manifest)
    metadata={'synthetic':True,'name':NAME,'as_of':AS_OF,'court_id':'CT01','court_name':'서울회생법원',
        'automatic_case_creation':False,'period':PERIOD,'bank_period':BANK_PERIOD,
        'balance_date':BALANCE_DATE,'bank_balance_date':AS_OF,'expected':expected,
        'expected_form_values':{key:value for key,value in expected.items() if key not in {'monthly_net_income','monthly_gross_income','monthly_deductions'}},
        'identity_safety':{'resident_id':RESIDENT_ID,'legacy_checksum_valid':False,'phone':PHONE,'all_addresses_fictional':True},
        'documents':manifest,'limitations':['서명·날인·관인 없음','가상 주소·기관·식별자','법률판단 예시 자동 입력 금지','파서 지원 여부와 자료 존재 여부는 별도'],
        'oracle_is_not_evidence':True}
    extend_expectations(metadata)
    digits=RESIDENT_ID.replace('-','')
    assert checksum_digit(digits[:12]) != int(digits[-1])
    assert sum(l['principal']+l['interest'] for l in loans)==expected['total_debt']
    assert PAY['gross']-PAY['deductions']==PAY['net']
    (OUT/'manifest.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/'README.txt').write_text(f'''박지우 · 공식 서식 항목을 준비하는 가상 증빙 묶음
{WARNING}

application.txt는 고객의 상담 신청 문구입니다. staff_consultation.txt는 상세 상담 연습용입니다.
고객이 직접 제출할 자료는 documents/의 PDF {len(manifest)}개입니다. 모든 페이지에 가상 표시가 있습니다.
인적사항, 급여12개월, 계좌2개, 대출3건, 월세·보험·퇴직금, 학력/경력/채무경위와 생활지출을 포함합니다.
원천징수 귀속연도2025와 최근 급여기간2025-10~2026-09는 다릅니다.
기관·주민번호·주소·계좌·전화는 모두 무효 연습값이며 실제 서명·관인·직인을 넣지 않았습니다.

manifest.json, financial_reconciliation.json, CSV는 검산용 정답·거래표이며 앱의 파서가 읽는 답안이 아닙니다.
court_form_coverage.json은 원본 서식 항목별 증빙과 현재 자동 입력의 지원 범위를 구분합니다.
legal_review_inputs.json은 담당자가 판단한 것으로 가정하는 연습 예시입니다. 증빙으로 올리거나 자동 확정하지 마세요.
전체 원문 추출과 AI 대조 범위가 완료돼야 서류별 원문+값 검토 및 후속 문서작성으로 진행합니다.
법률 판단이 남으면 그 이유를 확인하고 필요한 판단만 직접 검토합니다. 실제 인가·제출 성공을 보장하는 자료가 아닙니다.

검산: 월 실수령3,450,000원 / 채무69,000,000원 / 예금2,500,000원
보증금12,000,000원 / 보험환급2,400,000원 / 예상퇴직금 총액{retirement:,}원
실제 월생활지출1,650,000원 / 공제 전 관측재산 합계{expected['assets_total']:,}원
''',encoding='utf-8')
    bundle=write_bundle()
    return {'name':NAME,'documents':len(manifest),'pages':sum(d['pages'] for d in manifest),
            'expected':expected,'bundle':str(bundle.relative_to(ROOT)),'case_created':False}


def write_legal_example(retirement):
    policy=json.loads((ROOT/'data/legal_calculation_rules.json').read_text(encoding='utf-8'))
    base=int((Decimal(policy['median_income_2026']['1'])*Decimal(policy['seoul_base_living_ratio'])).quantize(Decimal(1),rounding=ROUND_HALF_UP))
    assets=[{'id':key,'label':label,'owned_value':value,'secured_deduction':0,'exempt_deduction':0,'disposal_cost':0,
             'evidence_document_ids':ids} for key,label,value,ids in [('bank_balance','예금2계좌',2500000,['FIX15','FIX16']),
                ('housing_deposit','임차보증금',12000000,['FIX17','FIX18']),('insurance_surrender','보험해약환급금',2400000,['FIX20']),
                ('retirement_expected','일반퇴직금 예상총액',retirement,['FIX21'])]]
    example={'synthetic':True,'evidence':False,'automatic_application_allowed':False,'requires_explicit_lawyer_review':True,
        'title':'가상의 법률 판단을 직접 입력해 보는 연습안 · 법원 결정 또는 현재 승인값 아님',
        'policy_id':policy['id'],'as_of':AS_OF,'inputs':{'recognized_household_size':1,'living_cost_mode':'seoul_median_60',
            'base_living_cost':base,'additional_living_cost':1650000-base,'months':36,'prepaid_months':0,
            'monthly_trustee_fee':0,'preapproval_costs_paid':True,'objection':False,'annual_discount_rate':0.05,
            'income':{'kind':'wage','basis':'net','monthly_amount':3450000,'taxes_and_social_insurance':0,'business_expenses':0,'period':PERIOD,
                      'evidence_document_ids':['FIX11','FIX12','FIX15']},'assets':assets,
            'creditors':[{'id':f'loan-{i+1}','name':loan['name'],'kind':'unsecured','principal':loan['principal'],'interest':loan['interest'],
                          'evidence_document_ids':[f'FIX{i+6:02d}']} for i,loan in enumerate(LOANS)],
            'decisions':{'household_reason':'본인 단독 생계·부양관계를 확인했다고 가정한 1인 검토안.',
                'living_cost_reason':'기본60%와 실제지출 차액을 추가생계비로 인정한다고 가정한 비교안. 법원 인정·허용항목 검토 필요.',
                'asset_reason':'보호범위를 확정하기 전 공제0으로 보수적으로 비교하는 연습안. 특히 퇴직금 압류금지범위와 주거보증금 면제는 별도 검토해야 하며 실제 제출값으로 사용할 수 없음.',
                'debt_reason':'3개 대출의 원금·유예된 미지급이자와 최근납입 내역을 대조했다고 가정함.',
                'fee_reason':'가상 연습에서 회생위원 보수0, 사전 비용납부 완료를 선택한 조건. 실제 납부증빙이나 법원 보수 결정이 존재한다는 뜻이 아님.',
                'objection_reason':'계산 비교를 위한 이의없음 가정. 실제 송달·채권자 이의 여부는 미확정.',
                'period_reason':'일반36개월을 선택한 가상 계획. 수행가능성 및 기간예외 검토 후 결정.',
                'discount_reason':'등록된 서울 계산정책의 연5% 비교안을 선택한 예시이며 적용 기준 확인 필요.'}},
        'court_form_fields':{'D5110':{'start_year':2026,'start_month':11,'start_day':25,
                                    'end_year':2029,'end_month':10,'end_day':25}},
        'court_form_field_reason':'가상 변제계획 작성일정 선택(법원결정 아님). 신청일에서 자동 추정하지 않은 담당자 입력 예시.',
        'not_proven_by_documents':['recognized_household_size','living_cost_mode','base_living_cost','additional_living_cost','months','prepaid_months',
            'monthly_trustee_fee','preapproval_costs_paid','objection','annual_discount_rate','asset 공제','변제 시작·종료일'],
        'form_variant':'D5110 가용소득만으로 변제. 자산 매각대금 투입 계획 없음.',
        'reference_policy_sha256':hashlib.sha256((ROOT/'data/legal_calculation_rules.json').read_bytes()).hexdigest()}
    (OUT/'legal_review_inputs.json').write_text(json.dumps(example,ensure_ascii=False,indent=2),encoding='utf-8')


def write_coverage(manifest):
    catalog=json.loads((ROOT/'data/court_forms/catalog.json').read_text(encoding='utf-8'))
    document_map={row['id']:row['file'] for row in manifest}
    evidence={
        'client_name':(['FIX01'],'성명'), 'resident_id':(['FIX01'],'무효 예시 주민등록번호'),
        'registered_address':(['FIX01'],'주민등록상 주소'), 'address':(['FIX01','FIX17'],'주소·임차물건'),
        'phone':(['FIX01'],'본인 연락처'), 'service_address':(['FIX26'],'송달장소 희망은 상세상담에서 확인'),
        'employer':(['FIX12'],'근무처'), 'employer_address':(['FIX12'],'직장 주소'), 'employer_phone':(['FIX12'],'직장 전화'),
        'employment_period':(['FIX12'],'입사일~현재'), 'job_title':(['FIX12'],'직위·업무'),
        'income_start_year':(['FIX11'],'현재 월소득을 확인한 급여기간 시작 연도(2025년). 입사연도가 아님'),
        'income_start_month':(['FIX11'],'현재 월소득을 확인한 급여기간 시작 월(10월)'),
        'income_start_day':(['FIX11'],'현재 월소득을 확인한 급여기간 시작 일(1일)'),
        'monthly_income':(['FIX11','FIX15'],'각 월 실지급액 및 급여입금'), 'annual_income':(['FIX11'],'월급×12 환산'),
        'income_manwon':(['FIX11'],'월 실수령액의 만원 환산'), 'income_label':(['FIX11'],'급여'), 'income_period_label':(['FIX11'],'월평균'),
        'income_seizure':(['FIX12'],'급여 압류·가압류 없음'), 'household_size':(['FIX01','FIX03'],'세대수 사실과 법률상 부양인원은 별개'),
        'bank_balance':(['FIX15','FIX16'],'기준일별 계좌잔액의 합계'), 'bank_name':(['FIX15'],'첫 번째 예금 금융기관명'),
        'bank_account':(['FIX15'],'첫 번째 예금 계좌 식별'), 'bank_balance_1':(['FIX15'],'첫 번째 계좌 기준일 잔액'),
        'bank_name_2':(['FIX16'],'두 번째 예금 금융기관명'), 'bank_account_2':(['FIX16'],'두 번째 예금 계좌 식별'),
        'bank_balance_2':(['FIX16'],'두 번째 계좌 기준일 잔액'), 'insurance_surrender':(['FIX20'],'해약환급금'),
        'insurance_name':(['FIX20'],'보험회사'), 'insurance_policy':(['FIX20'],'보험 증권번호'),
        'housing_deposit':(['FIX17','FIX18'],'미반환 임차보증금'),
        'housing_cost':(['FIX17','FIX18','FIX27'],'월 차임·납부내역'), 'landlord':(['FIX17'],'가상 임대인 이새움'),
        'lease_terms':(['FIX17'],'보증금·월차임·계약기간'), 'vehicle_value':(['FIX23'],'자동차 없음'),
        'real_estate_value':(['FIX22'],'소유 부동산 없음'), 'retirement_value':(['FIX21'],'예상 총액에서 압류금지범위 등을 판단한 뒤 가액 결정'),
        'assets_total':(['FIX15','FIX16','FIX17','FIX20','FIX21'],'관측 재산 합계와 청산가치는 별개'),
        'cash':(['FIX05'],'수중현금 없음'), 'prior_proceedings':(['FIX26'],'과거 회생·파산·면책 이력 없음 진술'),
        'statement':(['FIX26','FIX29','FIX11','FIX06','FIX07','FIX08'],'구체 경위·지속소득·채무구성·상환곤란 원문'),
        'principal_total':(['FIX06','FIX07','FIX08'],'채권자별 원금 합계'), 'interest_total':(['FIX06','FIX07','FIX08'],'미지급이자 합계'),
        'total_debt':(['FIX06','FIX07','FIX08'],'원금+이자 합계'), 'unsecured_debt':(['FIX06','FIX07','FIX08'],'무담보 채권 합계'),
        'secured_debt':(['FIX06','FIX07','FIX08'],'각 부채증명서의 담보없음'),
        'refund_bank':(['FIX15'],'환급계좌로 사용할지 담당자 선택 필요'), 'refund_account':(['FIX15'],'환급계좌로 사용할지 담당자 선택 필요')}
    legal={'months','monthly_deposit','recognized_living_cost','median_income','median_percent','liquidation_value','exempt_value',
           'base_living_cost','monthly_disposable_income','monthly_trustee_fee','monthly_creditor_capacity','total_creditor_payment',
           'present_value','allocation_principal_total','allocation_monthly_total','principal_repayment_percent','additional_cost_reason'}
    manual={'service_address','refund_bank','refund_account','cash','landlord','retirement_value'}
    result=[]
    for template in catalog['templates']:
        if template['id'] not in {'D5100','D5101','D5103','D5105','D5106','D5110','D5115'}:continue
        original=ROOT/'data/court_forms/originals'/(template['id']+'.pdf')
        with pymupdf.open(original) as pdf:
            pages=[page.get_text(sort=True) for page in pdf]
        fields=[]
        for field in template['fields']:
            key=field['key']
            ids,label=evidence.get(key,([],field['label']))
            category='documentary_fact'
            if key.startswith('creditors.'):
                index=int(key.split('.')[1]);ids=[f'FIX{6+index:02d}'] if index<3 else []
                category='creditor_detail' if index<3 else 'not_applicable_empty_row'
                label='채권자명·발생원인·주소·연락처·원금·이자·산정근거'
            elif key in legal or key.startswith(('allocations.','start_','end_')):
                category='legal_judgment_or_calculation';ids=ids or ['FIX11','FIX15','FIX16','FIX06','FIX07','FIX08']
            elif key.startswith('lawyer_'):
                category='appointment_and_manual_entry';ids=[]
            elif key=='court_prefix':category='confirmed_jurisdiction'
            elif not ids:category='manual_or_unsupported'
            fields.append({'key':key,'label':field['label'],'page':field['page']+1,'required':field.get('required',False),
                'source_document_ids':ids,'source_files':[document_map[i] for i in ids if i in document_map],
                'source_item':label,'category':category,
                'automatic_mapping_status':'review_or_manual_entry_required' if key in manual or category in {'legal_judgment_or_calculation','appointment_and_manual_entry','manual_or_unsupported'} else 'verify_parser_output_against_original',
                'not_automatically_confirmed':category in {'legal_judgment_or_calculation','appointment_and_manual_entry','manual_or_unsupported'} or key in manual})
        result.append({'template_id':template['id'],'title':template['title'],'official_pdf':str(original.relative_to(ROOT)),
            'official_pdf_sha256':hashlib.sha256(original.read_bytes()).hexdigest(),'official_pages_read':len(pages),
            'official_text_characters':sum(map(len,pages)),'fields':fields})
    coverage={'synthetic':True,'purpose':'공식 원본 입력 항목과 가상 증빙의 대응. 지원 여부는 실제 추출·작성 결과로 검증하며 정답을 앱에 주입하지 않음.',
        'templates':result,'unmapped_original_items':[
            {'template':'D5105','items':['최종 학력','이전 직업 경력','과거 혼인·이혼','거주 시작일','과거 소송·집행'], 'source_document_ids':['FIX04','FIX12','FIX26'],'status':'자료 존재·현재 원본 좌표 자동 기재 미지원'},
            {'template':'D5103','items':['가족관계 전체 표'], 'source_document_ids':['FIX03'],'status':'자료 존재·현재 원본 좌표 자동 기재 미지원'},
            {'template':'D5115','items':['대표자명','증명서 작성일','대표 서명'], 'source_document_ids':['FIX12'],'status':'기명·날짜 자료 존재 / 서명 공란·자동 서명 금지'},
            {'template':'D5106','items':['발생일','만기','채권자 전화·팩스','자세한 산정근거'], 'source_document_ids':['FIX06','FIX07','FIX08'],'status':'자료 존재 / 현재 매핑 밖 항목은 별지·수기 확인'},
            {'template':'D5100','items':['대리인 선임','송달장소 결정','환급계좌 선택'], 'source_document_ids':['FIX01','FIX15','FIX26'],'status':'선임·선택 필요 / 자동확정 금지'}],
        'legal_judgment_example':'legal_review_inputs.json','expected_is_not_source':'manifest.json은 테스트 비교용이며 증빙 또는 추출 입력으로 사용하지 않습니다.'}
    (OUT/'court_form_coverage.json').write_text(json.dumps(coverage,ensure_ascii=False,indent=2),encoding='utf-8')


def extend_expectations(metadata):
    """Independent fixture values; never infer answers from application output."""
    known={'bank_name':BANKS[0]['name'],'bank_account':BANKS[0]['account'],'bank_balance_1':BANKS[0]['closing'],
           'bank_name_2':BANKS[1]['name'],'bank_account_2':BANKS[1]['account'],'bank_balance_2':BANKS[1]['closing'],
           'insurance_name':'가상온기생명','insurance_policy':'TEST-POLICY-JW-001',
           'income_start_year':2025,'income_start_month':10,'income_start_day':1,'employment_start':'2022-03-01'}
    metadata['expected'].update(known)
    metadata['expected_form_values'].update(known)
    metadata['expected_court_form_values']={
        'D5101':{key:value for key,value in known.items() if key.startswith(('bank_', 'insurance_'))},
        'D5115':{key:value for key,value in known.items() if key.startswith('income_start_')}}
    metadata['expected_court_form_values']['D5101']['insurance_surrender']=2400000
    per_document={'FIX12':[{'key':'employment_start','value':'2022-03-01'}],
                  'FIX20':[{'key':'insurance_name','value':'가상온기생명'},
                           {'key':'insurance_policy','value':'TEST-POLICY-JW-001'}]}
    for document in metadata['documents']:
        for expected in per_document.get(document['id'], []):
            existing=[row for row in document['expected_facts'] if row['key']==expected['key']]
            if existing and existing != [expected]:
                raise ValueError('Conflicting independent fixture expectations.')
            if not existing:
                document['expected_facts'].append(expected)
    metadata['income_certificate_period_note']='D5115의 소득 시작일은 확인된 급여기간2025-10-01입니다. 재직 입사일2022-03-01과 구분합니다.'


def write_bundle():
    bundle=ROOT/'examples/court_ready_fixture_bundle.zip'
    temporary=bundle.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(OUT.rglob('*')):
            if path.is_file():archive.write(path,path.relative_to(OUT))
    temporary.replace(bundle)
    return bundle


def refresh_metadata():
    """Refresh test metadata without regenerating or changing evidence PDFs."""
    path=OUT/'manifest.json'
    metadata=json.loads(path.read_text(encoding='utf-8'))
    before={row['file']:hashlib.sha256((OUT/row['file']).read_bytes()).hexdigest()
            for row in metadata['documents']}
    if any(before[row['file']] != row['sha256'] for row in metadata['documents']):
        raise ValueError('An original PDF differs from its manifest; metadata-only refresh refused.')
    extend_expectations(metadata)
    write_coverage(metadata['documents'])
    path.write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    bundle=write_bundle()
    assert all(hashlib.sha256((OUT/name).read_bytes()).hexdigest()==digest for name,digest in before.items())
    return {'metadata_only':True,'documents':len(before),'original_pdf_bytes_unchanged':True,
            'bundle':str(bundle.relative_to(ROOT)),'case_created':False}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata-only',action='store_true',help='Keep PDF originals and refresh metadata/bundle only.')
    args=parser.parse_args()
    print(json.dumps(refresh_metadata() if args.metadata_only else generate(),ensure_ascii=False,indent=2))
