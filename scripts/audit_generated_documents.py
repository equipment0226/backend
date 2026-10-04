"""Audit original-field placement and source-fact coverage in seven real PDFs.

Run extraction audit first. No live case or approval is created. Rendering is
tested with semantic review unavailable to ensure confirmed source facts survive.
An optional actual local-model narrative can be included, preserving its verdict.
"""
import argparse
import asyncio
import copy
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def run(fixture, narrative_path=None):
    import pymupdf
    from apps.api import court_forms, evidence_mapping, preliminary_drafting, store
    case = json.loads((ROOT / '.work/ocr-audit' / (fixture + '-case.json')).read_text(encoding='utf-8'))
    original_document_hashes = [row['sha256'] for row in case['documents']]
    packet = evidence_mapping.build(case)
    narrative = None
    if narrative_path:
        narrative = json.loads(Path(narrative_path).read_text(encoding='utf-8'))
        narrative.update(id=store.uid('narrative'), created_at=store.now())
        case['narrative_runs'] = [copy.deepcopy(narrative)]
    # This audit intentionally does not claim that model/filing checks passed.
    skipped = {'status': 'unavailable', 'passed': False, 'error': {'code': 'AUDIT_RENDER_ONLY',
               'message': 'Isolated content audit; semantic-review outage path exercised.'}}
    draft = await preliminary_drafting.create(case, [], {'review'}, ocr_check=skipped,
        local_failure=skipped, narrative=narrative)
    records = case.get('court_documents', [])
    forms, failures, covered = [], [], set()
    norm = lambda value: re.sub(r'\s+', '', str(value)).replace(',', '')
    for record in records:
        preview = record['preview']
        path = store.DATA_DIR / 'generated' / case['id'] / (record['id'] + '.pdf')
        raw = path.read_bytes()
        assertions = []
        with pymupdf.open(stream=raw, filetype='pdf') as pdf:
            page_map = {source_page: index for index, source_page in enumerate(preview['template']['pages'])}
            appendix = '\n'.join(page.get_text() for page in list(pdf)[len(page_map):])
            for field in preview['fields']:
                if field.get('value') in (None, ''):
                    continue
                expected = court_forms._display(field['value'], field['key'])
                printed = pdf[page_map[field['page']]].get_text(clip=pymupdf.Rect(field['rect']))
                in_field = norm(expected) in norm(printed)
                in_appendix = norm(expected) in norm(appendix)
                # Overflow is explicitly flagged and preserved in the appendix.
                preserved = in_field or (field['key'] in preview['overflow_fields'] and in_appendix)
                assertions.append({'key': field['key'], 'value': field['value'],
                    'original_field_matches': in_field, 'preserved': preserved,
                    'source_type': field.get('source', {}).get('type')})
                if not preserved:
                    failures.append(record['template_id'] + ':' + field['key'])
            for fact in preview.get('source_facts', []):
                if norm(fact['quote']) in norm(appendix):
                    covered.add(fact['id'])
                else:
                    failures.append(record['template_id'] + ':source_quote:' + fact['key'])
            forms.append({'template_id': record['template_id'], 'title': preview['template']['title'],
                'file': path.relative_to(ROOT).as_posix(), 'sha256': hashlib.sha256(raw).hexdigest(),
                'hash_matches': hashlib.sha256(raw).hexdigest() == record['sha256'], 'pages': len(pdf),
                'fields': assertions, 'source_facts': len(preview.get('source_facts', [])),
                'missing_fields': preview['missing_fields'], 'overflow_fields': preview['overflow_fields'],
                'submission_ready': record.get('submission_ready'), 'ai_review': record['ai_review']['status']})
    missing_facts = [{'key': row['key'], 'document_id': row['document_id'], 'page': row['page']}
                     for row in packet['facts'] if row['id'] not in covered]
    if missing_facts:
        failures.append('missing_source_fact_coverage')
    if len(records) != 7:
        failures.append('seven_forms_required')
    statement = next((field for record in records if record['template_id'] == 'D5105'
                      for field in record['preview']['fields'] if field['key'] == 'statement'), {})
    narrative_present = bool(statement.get('value'))
    if narrative_path and not narrative_present:
        failures.append('statement_body_missing')
    # The editable package must preserve the same substantive content too.
    generated_folder = store.DATA_DIR / 'generated' / case['id']
    docx_path = generated_folder / (draft['id'] + '.docx')
    with zipfile.ZipFile(docx_path) as archive:
        document = ET.fromstring(archive.read('word/document.xml'))
        docx_text = '\n'.join(node.text or '' for node in document.iter()
                              if node.tag.endswith('}t'))
    html_path = generated_folder / (draft['id'] + '.html')
    class BodyText(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts, self.skip = [], 0
        def handle_starttag(self, tag, attrs):
            if tag in {'script', 'style'}:
                self.skip += 1
        def handle_endtag(self, tag):
            if tag in {'script', 'style'}:
                self.skip = max(0, self.skip - 1)
        def handle_data(self, text):
            if not self.skip:
                self.parts.append(text)
    parser = BodyText()
    parser.feed(html_path.read_text(encoding='utf-8'))
    html_text = ' '.join(parser.parts)
    editable = []
    for name, content in [('DOCX', docx_text), ('HTML', html_text)]:
        checks = {key: norm(value) in norm(content) for key, value in packet['form_values'].items()
                  if key in {'monthly_income', 'total_debt', 'assets_total', 'living_expenses'}}
        checks['statement'] = bool(statement.get('value')) and norm(statement['value']) in norm(content)
        if not all(checks.values()):
            failures.append(name + ':missing_core_content')
        editable.append({'format': name, 'checks': checks, 'characters': len(content)})
    report = {'fixture': fixture, 'synthetic': True, 'passed': not failures, 'failures': failures,
        'forms': forms, 'editable_packages': editable,
        'source_facts': len(packet['facts']), 'preserved_facts': len(covered),
        'missing_facts': missing_facts, 'statement_present': narrative_present,
        'actual_local_narrative': bool(narrative and narrative.get('audit', {}).get('actual_local_model')),
        'narrative_status': (narrative or {}).get('status'),
        'source_documents_unchanged': original_document_hashes == [row['sha256'] for row in case['documents']],
        'live_database_used': False, 'filing_approved': False,
        'limitations': ['Rendering and exact-source coverage audit; semantic document review intentionally unavailable.',
            'Unknown identity/signature and legal decisions remain pending, not filled with invented facts.']}
    destination = ROOT / 'reports/ocr-audit' / (fixture + '-generated-documents.json')
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (ROOT / '.work/ocr-audit' / (fixture + '-generated-case.json')).write_text(json.dumps(case,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('fixture','passed','failures','source_facts','preserved_facts','statement_present')},ensure_ascii=False))
    return report['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', choices=['fresh_start', 'ocr_retest'], default='ocr_retest')
    parser.add_argument('--narrative')
    args = parser.parse_args()
    os.environ['DEBTOFF_DATA_DIR'] = str(ROOT / '.work/ocr-audit/render-runtime')
    os.environ['DEBTOFF_AUTO_AX'] = '0'
    os.environ['DEBTOFF_LEGAL_WATCH'] = '0'
    sys.exit(0 if asyncio.run(run(args.fixture, args.narrative)) else 1)
