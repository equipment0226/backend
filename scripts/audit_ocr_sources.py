"""Read-only comparison of uploaded synthetic originals and stored extraction.

Does not call the application, models, or write the case database. Expected text
is only an evaluation reference and is never passed into an OCR engine.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pymupdf

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from apps.api.ocr_engine import extract_pages


def audit():
    sources={hashlib.sha256(path.read_bytes()).hexdigest():path for path in ROOT.glob('examples/*/documents/*.pdf')}
    with sqlite3.connect((ROOT/'.local/debtoff.sqlite3').as_uri()+'?mode=ro',uri=True) as con:
        row=con.execute('SELECT body FROM cases WHERE id=?',('case-16ba01fc51d5',)).fetchone()
    if not row:raise ValueError('Known synthetic comparison case is not present.')
    case=json.loads(row[0]);records=[]
    for document in case.get('documents',[]):
        original=sources.get(document.get('sha256'));native=[]
        if original:
            with pymupdf.open(original) as pdf:native=[page.get_text(sort=True).strip() for page in pdf]
        pages=document.get('page_texts',[])
        records.append({'filename':document['filename'],'original':str(original.relative_to(ROOT)) if original else None,
            'sha256':document.get('sha256'),'source_hash_matches':bool(original),'method':document.get('extraction_method'),
            'page_count':len(pages),'native_characters':sum(map(len,native)),
            'stored_characters':sum(len(page.get('text','')) for page in pages),
            'native_equal':bool(native) and all(native) and len(native)==len(pages) and all(a==b.get('text','') for a,b in zip(native,pages)),
            'ocr_line_count':sum(len(page.get('lines',[])) for page in pages),
            'saved_warnings':[warning for page in pages for warning in page.get('warnings',[])]})
    # These are manually checked facts visible on the raster itself. They are
    # tests of existing text, not training data or fallback extraction values.
    payroll=next(d for d in case['documents'] if d.get('extraction_method')=='ocr')
    compact=''.join(''.join(page.get('text','').split()) for page in payroll['page_texts'])
    expected={'person':'김새봄','employer':'새봄테스트회사','gross':'3,100,000','deductions':'300,000','net':'2,800,000',
              'period':'2025-10-04~2026-10-04','issue_date':'2026-10-04','signature_absent':'실제서명아님'}
    checks={key:needle in compact for key,needle in expected.items()}
    report={'synthetic':True,'database_open_mode':'read_only','source_count':len(records),'documents':records,
            'stored_payroll_field_checks':checks,'native_documents_equal':sum(r['native_equal'] for r in records),
            'interpretation':'Native body preserved. Any missing structured field present in these bodies is a semantic extraction gap, not loss in OCR.',
            'case_mutations':0,'model_calls':0}
    out=ROOT/'reports/ocr-audit';out.mkdir(exist_ok=True)
    (out/'stored-original-comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'sources':len(records),'native_exact':report['native_documents_equal'],'stored_payroll_checks':checks,'case_mutations':0,'model_calls':0}))


def audit_original_native():
    """Verify all remaining old PDFs without rerunning the scanned payroll."""
    records=[];extractions=[]
    for path in sorted((ROOT/'examples/synthetic_case/documents').glob('*.pdf')):
        with pymupdf.open(path) as pdf:native=[page.get_text(sort=True).strip() for page in pdf]
        if not all(native):continue  # Payroll has its separate actual OCR benchmark.
        content=path.read_bytes();pages=extract_pages(content,'.pdf')
        extractions.append({'filename':path.name,'sha256':hashlib.sha256(content).hexdigest(),'pages':pages})
        records.append({'filename':path.name,'sha256':hashlib.sha256(content).hexdigest(),'pages':len(pages),
            'text_exact':len(native)==len(pages) and all(text==page['text'] for text,page in zip(native,pages)),
            'characters':sum(map(len,native)),'native_boxes_preserved':all(page.get('lines') for page in pages)})
    out=ROOT/'.work/ocr-audit';out.mkdir(exist_ok=True)
    (out/'original-native-extraction-final.json').write_text(json.dumps({'synthetic':True,'documents':extractions},ensure_ascii=False,indent=2),encoding='utf8')
    report={'synthetic':True,'documents':records,'documents_exact':sum(row['text_exact'] for row in records),
        'scope':'Original synthetic PDFs except scanned payroll, which has a separate OCR field benchmark. CSV is structured input, not image OCR.',
        'case_mutations':0,'model_calls':0}
    (ROOT/'reports/ocr-audit/original-native-comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'original_native_documents':len(records),'documents_exact':report['documents_exact'],'pages':sum(row['pages'] for row in records)}))


if __name__=='__main__':
    audit()
    audit_original_native()
