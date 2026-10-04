"""Read actual fictional PDFs through OCR and audit each document's typed facts.

No case is inserted. Expected values live only in this test harness and are never
provided to OCR, the extractor, the mapping code or a model.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def build_case(folder):
    from apps.api import ax_engine, domain, ocr_engine
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    notes = (folder / 'application.txt').read_text(encoding='utf-8')
    case = domain.new_case(manifest['name'], 'CT01', '서울회생법원', notes, True)
    case['case_type'] = 'personal_rehabilitation'
    case['consultation'] = {'notes': notes, 'status': 'completed', 'version': 1}
    case['intake'] = {'status': 'completed'}
    for number, entry in enumerate(manifest['documents'], 1):
        raw = (folder / entry['file']).read_bytes()
        pages = ocr_engine.extract_pages(raw, '.pdf')
        case['documents'].append({'id': f'doc-audit-{number:02d}', 'filename': Path(entry['file']).name,
            'status': 'verified', 'version': 1, 'text': '\n'.join(page['text'] for page in pages),
            'page_texts': pages, 'sha256': hashlib.sha256(raw).hexdigest(),
            'scope_confirmed': True, 'content_confirmed': True, 'person_confirmed': True,
            'audit_catalog_id': entry['catalog_id']})
    case['extraction_candidates'] = ax_engine.extract_factor_candidates(ax_engine.case_sources(case))
    return case


def audit(folder, case):
    from apps.api import document_facts, evidence_mapping
    old = folder.name == 'fresh_start'
    name, company, bank = ('김새봄', '새봄테스트회사', '새봄테스트은행') if old else ('이하늘', '하늘테스트물류', '하늘테스트은행')
    net, gross, deductions, principal, interest, cash, living = (2800000,3100000,300000,58000000,2000000,1200000,1500000) if old else (3200000,3550000,350000,69000000,3000000,2400000,1600000)
    checks = {
        'D01': [('client_name', name), ('household_size', 1)],
        'D03': [('dependent_count', 0), ('children_count', 0), ('marital_status', '미혼')],
        'D35': [('cash_balance', cash), ('institution', bank)],
        'D38': [('creditor_name', bank), ('creditor_principal', principal), ('creditor_interest', interest), ('creditor_total', principal + interest), ('creditor_kind', '무담보')],
        'D06': [('income_gross', gross*12, 'annual'), ('income_net', net*12, 'annual'), ('income_deductions', deductions*12, 'annual'), ('income_net', net, 'monthly')],
        'D08': [('employer', company), ('employment_type', '급여소득자'), ('income_net', net, 'monthly')],
        'D09': [('health_qualification', '직장가입자'), ('employer', company)],
        'D11': [('pension_assessed_income', gross), ('pension_membership', '현재 가입 유지')],
        'D28': [('real_estate_ownership', False)],
        'D29': [('tax_arrears', 0)],
        'D39': [('insurance_contracts', False), ('insurance_surrender', 0)],
        'SUPPORT-HOUSING': [('housing_type', '무상거주'), ('housing_deposit', 0), ('housing_cost', 0)],
        'SUPPORT-HISTORY': [('living_expenses', living)],
        'D36': [('cash_balance', cash), ('bank_payroll_deposit', net), ('bank_living_expense', living)],
        'D07': [('income_net', net, 'monthly'), ('income_gross', gross, 'monthly'), ('income_deductions', deductions, 'monthly'), ('employment_type', '급여소득자')],
    }
    if not old:
        checks['D08'].append(('job_title', '물류 관리'))
    packet = evidence_mapping.build(case)
    observations = packet['facts']
    documents, failures = [], []
    for doc in case['documents']:
        facts = [row for row in observations if row['document_id'] == doc['id']]
        findings = []
        for expected in checks[doc['audit_catalog_id']]:
            key, value, *frequency = expected
            passed = any(row['key'] == key and row['value'] == value and (not frequency or row.get('frequency') == frequency[0]) for row in facts)
            findings.append({'key': key, 'expected': value, 'frequency': frequency[0] if frequency else None, 'passed': passed})
        provenance = all(row['quote'] and row['quote'] in next(page['text'] for page in doc['page_texts'] if page['page'] == row['page']) for row in facts)
        if not provenance or not all(row['passed'] for row in findings):
            failures.append(doc['filename'])
        documents.append({'title': doc['filename'], 'sha256': doc['sha256'], 'pages': len(doc['page_texts']),
            'typed_observations': len(facts), 'exact_page_quotes': provenance, 'checks': findings})
    expected_aggregate = {'monthly_income': net, 'bank_balance': cash, 'principal_total': principal,
        'interest_total': interest, 'total_debt': principal + interest, 'assets_total': cash,
        'living_expenses': living, 'household_size': 1, 'employment_type': '급여소득자', 'employer': company}
    aggregate = [{'key': key, 'expected': value, 'actual': packet['form_values'].get(key),
                  'passed': packet['form_values'].get(key) == value} for key, value in expected_aggregate.items()]
    if not all(row['passed'] for row in aggregate):
        failures.append('aggregate_mapping')
    unemployment = [row for row in case['extraction_candidates'] if row['key'] == 'employment_type' and row['value'] == '무직 진술']
    bank_facts = [row for row in observations if row['key'] == 'bank_payroll_deposit']
    months = {row.get('period_start') for row in bank_facts}
    if unemployment:
        failures.append('false_unemployment')
    if len(months) != 12:
        failures.append('bank_month_rows')
    report = {'fixture': folder.name, 'synthetic': True, 'actual_ocr': True, 'fixture_manifest_used_by_extractor': False,
        'document_count': len(documents), 'observations': len(observations), 'documents': documents,
        'aggregate': aggregate, 'bank_months': len(months), 'false_unemployment': len(unemployment),
        'failures': failures, 'passed': not failures,
        'limitations': ['Synthetic native/scanned PDFs, not a benchmark of every real issuing institution.',
            'Document review simulated in isolated memory after comparing known fictional sources; no case inserted.',
            'No AI or legal-approval result is inferred from deterministic extraction checks.']}
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', choices=['fresh_start', 'ocr_retest', 'all'], default='all')
    args = parser.parse_args()
    report_dir = ROOT / 'reports/ocr-audit'
    report_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for name in (['fresh_start', 'ocr_retest'] if args.fixture == 'all' else [args.fixture]):
        folder = ROOT / 'examples' / name
        case = build_case(folder)
        record = audit(folder, case)
        records.append(record)
        (ROOT / '.work/ocr-audit').mkdir(parents=True, exist_ok=True)
        (ROOT / '.work/ocr-audit' / (name + '-case.json')).write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding='utf-8')
        (report_dir / (name + '-extraction.json')).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({key: record[key] for key in ('fixture','passed','document_count','observations','bank_months','failures')}, ensure_ascii=False))
    sys.exit(0 if all(row['passed'] for row in records) else 1)
