"""Actual bounded offline OCR, with field/line-level fixture evaluation.

References below are evaluation annotations only. Production extraction receives
only original PDF/image bytes; no expected name, amount or wording is supplied.
Native documents are read as native text and are never counted as OCR successes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from apps.api.ocr_engine import extract_pages

# Manually checked reference facts on the fictional originals. Patterns retain
# row breaks, so a label cannot borrow a matching amount from a different row.
FRESH={
    '01':{'person':r'성명:김새봄','household':r'세대원:김새봄1명','address':r'새출발로100테스트동101호'},
    '02':{'person':r'본인:김새봄','unmarried':r'미혼','dependents_absent':r'부양가족없음'},
    '03':{'institution':r'금융기관:새봄테스트은행','account':r'계좌:TEST-ACCOUNT-SPRING-001','balance':r'예금잔액:1,200,000원'},
    '04':{'creditor':r'채권자:새봄테스트은행','principal':r'원금:58,000,000원','interest':r'이자:2,000,000원','total':r'채무합계:60,000,000원','unsecured':r'담보없음'},
    '05':{'employer':r'근무처:새봄테스트회사','gross_annual':r'총급여:37,200,000원','deductions_annual':r'공제합계:3,600,000원','net_annual':r'실수령합계:33,600,000원','net_monthly':r'월평균2,800,000원'},
    '06':{'employer':r'근무처:새봄테스트회사','joined':r'입사일:2024-01-01','net_monthly':r'월실수령액:2,800,000원'},
    '07':{'person':r'가입자:김새봄','employer':r'사업장:새봄테스트회사','joined':r'2024-01-01'},
    '08':{'person':r'가입자:김새봄','gross_monthly':r'월기준소득:3,100,000원'},
    '09':{'nationwide':r'조회범위:전국','no_estate':r'소유부동산검색결과:없음','ownership_zero':r'부동산소유지분:0원'},
    '10':{'tax_scope':r'조회세목:모든세목','no_arrears':r'체납액:0원','no_asset_tax':r'부동산·차량관련과세내역:없음'},
    '11':{'contracts_absent':r'보유보험계약:없음','insurance_loan_zero':r'보험계약대출:0원','surrender_zero':r'해약환급금:0원'},
    '12':{'sole_resident':r'김새봄단독거주','not_owner':r'소유자본인여부:아니오','lease_zero':r'임차보증금:0원','rent_zero':r'월차임:0원'},
    '13':{'net_monthly':r'월실수령소득:2,800,000원','actual_living_cost':r'실제월생활지출:1,500,000원','food':r'식비500000','debt':r'현재금융채무:60,000,000원'},
    '14':{'institution':r'금융기관:새봄테스트은행','account':r'계좌번호:TEST-ACCOUNT-SPRING-001','opening_balance':r'조회시작잔액:1,200,000원','closing_balance':r'조회종료잔액:1,200,000원','monthly_rows':r'2026-09\|?2,800,000\|?1,500,000\|?1,300,000'},
    '15':{'person':r'성명:김새봄','employer':r'근무처:새봄테스트회사','period':r'확인기간:2025-10-04[~～]2026-10-04','gross':r'총급여:3,100,000원','deductions':r'공제액:300,000원','net':r'실수령액:2,800,000원','unsigned':r'실제서명아님'},
}
ORIGINAL={'person':r'성명:김가온','gross':r'총지급액\|?3,100,000','deductions':r'공제합계\|?300,000',
    'net_table':r'차인지급액\(실수령액\)\|?2,800,000','base_pay':r'기본급\|?2,900,000','meal':r'식대\|?200,000',
    'account':r'TEST-ACCOUNT-001','disclaimer_line':r'공제내역은테스트합계이며실제보험료·세액산식이아닙니다\.',
    'issuer_line':r'발급:가상회사테스트담당/날인없음'}

# Independent new fixture annotations, checked against its visible originals.
# These substitutions affect evaluation patterns only, never document extraction.
RETEST_REPLACEMENTS={
    '김새봄':'이하늘','새봄테스트은행':'하늘테스트은행','새봄테스트회사':'하늘테스트물류',
    'TEST-ACCOUNT-SPRING-001':'TEST-ACCOUNT-SKY-002','새출발로100테스트동101호':'다시봄길200테스트동202호',
    '2024-01-01':'2024-03-01','58,000,000':'69,000,000','2,000,000':'3,000,000',
    '60,000,000':'72,000,000','37,200,000':'42,600,000','3,600,000':'4,200,000',
    '33,600,000':'38,400,000','3,100,000':'3,550,000','300,000':'350,000',
    '2,800,000':'3,200,000','1,200,000':'2,400,000','1,500,000':'1,600,000',
    '1,300,000':'1,600,000','식비500000':'식비550000',
}
def retest_references():
    references={}
    for prefix,fields in FRESH.items():
        references[prefix]={}
        for field,pattern in fields.items():
            # Single replacement pass avoids altering numbers inserted earlier.
            references[prefix][field]=re.sub('|'.join(re.escape(v) for v in RETEST_REPLACEMENTS),
                lambda match:RETEST_REPLACEMENTS[match.group()],pattern)
    for year,month in [(2025,10),(2025,11),(2025,12)]+[(2026,m) for m in range(1,10)]:
        references['14'][f'transaction_row_{year}_{month:02}']=fr'{year}-{month:02}\|?3,200,000\|?1,600,000\|?1,600,000'
    references['14']['second_page_salary']=r'각급여일25일에3,200,000원입금'
    return references


def evaluate(document, reference, every_page=False):
    pages=document['pages'];checks=[]
    scope=[[page] for page in pages] if every_page else [pages]
    for subset in scope:
        text='\n'.join(re.sub(r'[^\S\n]+','',page.get('text','')) for page in subset)
        raw='\n'.join(re.sub(r'[^\S\n]+','',page.get('raw_text',page.get('text',''))) for page in subset)
        alternatives='\n'.join(re.sub(r'[^\S\n]+','',candidate.get('text','')) for page in subset for line in page.get('lines',[]) for candidate in line.get('recognition_candidates',[]))
        for field,pattern in reference.items():
            passed=re.search(pattern,text) is not None
            raw_match=re.search(pattern,raw) is not None
            alternative_match=re.search(pattern,alternatives) is not None
            checks.append({'field':field,'pages':[page['page'] for page in subset],
                'passed':passed,'present_in_raw':raw_match,
                'present_in_review_alternative':alternative_match,
                'classification':'matched' if passed else 'review_alternative' if alternative_match else 'confidence_filtered' if raw_match else 'missing_or_misread',
                'reference_pattern':pattern})
    return checks


def run_retest(phase):
    import pymupdf
    out=ROOT/'.work/ocr-audit';out.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((ROOT/'examples/ocr_retest/manifest.json').read_text(encoding='utf8'))
    references=retest_references();documents=[];raw_documents=[];started=time.perf_counter()
    for source in manifest['documents']:
        path=ROOT/'examples/ocr_retest'/source['file'];content=path.read_bytes()
        item={'filename':path.name,'sha256':hashlib.sha256(content).hexdigest(),'pages':extract_pages(content,'.pdf')}
        raw_documents.append(item)
        checks=evaluate(item,references[path.name[:2]])
        with pymupdf.open(stream=content,filetype='pdf') as pdf:
            native_matches=[page['text']==pdf[page['page']-1].get_text(sort=True).strip() for page in item['pages'] if page['extraction_method']=='native_text']
        documents.append({'fixture':'ocr_retest/'+path.name,'sha256':item['sha256'],'manifest_sha_matches':item['sha256']==source['sha256'],
            'page_count':len(item['pages']),'native_pages_exact':native_matches,
            'methods':[p['extraction_method'] for p in item['pages']],'checks':checks,'passed_fields':sum(c['passed'] for c in checks),
            'total_fields':len(checks),'warnings':[w for p in item['pages'] for w in p.get('warnings',[])]})
    seconds=round(time.perf_counter()-started,3)
    (out/('retest-extraction-'+phase+'.json')).write_text(json.dumps({'synthetic':True,'documents':raw_documents,'seconds':seconds},ensure_ascii=False,indent=2),encoding='utf8')
    report={'synthetic':True,'phase':phase,'actual_ocr_performed':True,'expected_answers_passed_to_engine':False,'seconds':seconds,
        'documents':documents,'passed_fields':sum(d['passed_fields'] for d in documents),'total_fields':sum(d['total_fields'] for d in documents),
        'case_mutations':0,'external_calls':0,'limitations':['고정 합성 자료의 항목 검증이며 일반 OCR 정확도가 아닙니다.','native PDF는 이미지 OCR 통계에서 구분합니다.']}
    report['all_fields_passed']=report['passed_fields']==report['total_fields']
    path=ROOT/'reports/ocr-audit'/('retest-benchmark-'+phase+'.json');path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:report[k] for k in ('phase','passed_fields','total_fields','all_fields_passed','seconds')}))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--reuse-baseline',action='store_true')
    parser.add_argument('--phase',default='current');parser.add_argument('--retest-only',action='store_true');args=parser.parse_args()
    if args.retest_only:return run_retest(args.phase)
    out=ROOT/'.work/ocr-audit';out.mkdir(parents=True,exist_ok=True)
    started=time.perf_counter()
    if args.reuse_baseline:
        fresh=json.loads((out/'fresh-extraction-baseline.json').read_text(encoding='utf-8'))['documents']
        original=json.loads((out/'original-extraction-baseline.json').read_text(encoding='utf-8'))
        original['filename']=Path(original['fixture']).name
        original['sha256']=hashlib.sha256((ROOT/original['fixture']).read_bytes()).hexdigest()
    else:
        fresh=[]
        for path in sorted((ROOT/'examples/fresh_start/documents').glob('*.pdf')):
            content=path.read_bytes();fresh.append({'filename':path.name,'sha256':hashlib.sha256(content).hexdigest(),'pages':extract_pages(content,'.pdf')})
        path=next((ROOT/'examples/synthetic_case/documents').glob('01_*.pdf'));content=path.read_bytes()
        original={'filename':path.name,'sha256':hashlib.sha256(content).hexdigest(),'pages':extract_pages(content,'.pdf')}
        (out/('fresh-extraction-'+args.phase+'.json')).write_text(json.dumps({'synthetic':True,'documents':fresh},ensure_ascii=False,indent=2),encoding='utf-8')
        (out/('original-extraction-'+args.phase+'.json')).write_text(json.dumps(original,ensure_ascii=False,indent=2),encoding='utf-8')
    documents=[]
    for item in fresh:
        checks=evaluate(item,FRESH[item['filename'][:2]])
        documents.append({'fixture':'fresh_start/'+item['filename'],'sha256':item['sha256'],'page_count':len(item['pages']),
            'methods':[p['extraction_method'] for p in item['pages']],'checks':checks,'passed_fields':sum(c['passed'] for c in checks),
            'total_fields':len(checks),'warnings':[w for p in item['pages'] for w in p.get('warnings',[])]})
    checks=evaluate(original,ORIGINAL,every_page=True)
    documents.append({'fixture':'synthetic_case/'+original['filename'],'sha256':original['sha256'],'page_count':len(original['pages']),
        'methods':[p['extraction_method'] for p in original['pages']],'checks':checks,'passed_fields':sum(c['passed'] for c in checks),
        'total_fields':len(checks),'warnings':[w for p in original['pages'] for w in p.get('warnings',[])]})
    report={'synthetic':True,'phase':args.phase,'actual_ocr_performed':not args.reuse_baseline,'expected_answers_passed_to_engine':False,
            'seconds':round(time.perf_counter()-started,3),'documents':documents,
            'passed_fields':sum(d['passed_fields'] for d in documents),'total_fields':sum(d['total_fields'] for d in documents),
            'limitations':['고정 합성 원본의 항목·줄 검증이며 일반 OCR 정확도 지표가 아닙니다.','미인식한 식별자·문장은 원문 확인 대상으로 남깁니다.','native PDF는 이미지 OCR 통계로 계산하지 않습니다.'],
            'case_mutations':0,'external_calls':0}
    report['all_fields_passed']=report['passed_fields']==report['total_fields']
    path=ROOT/'reports/ocr-audit'/('field-benchmark-'+args.phase+'.json');path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'phase':args.phase,'passed_fields':report['passed_fields'],'total_fields':report['total_fields'],'all_fields_passed':report['all_fields_passed'],'seconds':report['seconds']}))


if __name__=='__main__':main()
