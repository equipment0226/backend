"""Audit richer fictional originals through the production extraction and PDF path.

Expected values are used only after extraction. No live case, model call or
submission approval is created. Unfilled legal judgments remain visible.
"""
import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def reviewed_layout(case, packet, folder, output):
    """Exercise legal columns separately with explicitly simulated decisions."""
    import pymupdf
    from apps.api import court_forms, legal_calculator
    example = json.loads((folder / 'legal_review_inputs.json').read_text(encoding='utf-8'))
    if example.get('synthetic') is not True or example.get('automatic_application_allowed') is not False:
        raise ValueError('Legal assumptions must be an explicitly nonautomatic fictional example.')
    inputs = copy.deepcopy(packet['inputs'])
    chosen = example['inputs']
    for key in ('recognized_household_size','living_cost_mode','base_living_cost','additional_living_cost',
                'months','prepaid_months','monthly_trustee_fee','preapproval_costs_paid','objection',
                'annual_discount_rate','decisions'):
        inputs[key] = copy.deepcopy(chosen[key])
    if inputs['income']['monthly_amount'] != chosen['income']['monthly_amount']:
        raise ValueError('The actual extracted income does not match the independent example.')
    assumptions = {row['id']:row for row in chosen['assets']}
    for asset in inputs['assets']:
        assumption = assumptions[asset['id']]
        if asset['owned_value'] != assumption['owned_value']:
            raise ValueError('The actual extracted asset value does not match the example.')
        for key in ('secured_deduction','exempt_deduction','disposal_cost'):
            asset[key] = assumption[key]
    expected_creditors = sorted((row['name'],row['principal'],row['interest']) for row in chosen['creditors'])
    if sorted((row['name'],row['principal'],row['interest']) for row in inputs['creditors']) != expected_creditors:
        raise ValueError('The actual extracted creditor balances do not match the example.')
    calculation = legal_calculator.calculate_legal(case, inputs)
    if calculation['status'] != 'ready_for_review':
        return {'passed':False,'calculation_status':calculation['status'],'blockers':calculation.get('blockers',[])}
    # This in-memory test grant only exercises the existing approved-layout
    # renderer. It is never stored as a case approval or an AI decision.
    calculation.update(status='approved', approval={'simulation':True,'scope':'isolated field-placement test'}, stale=False)
    rows, errors = [], []
    for template in court_forms.TEMPLATES.values():
        if not template.get('fields'):
            continue
        template_id = template['id']
        fields = example.get('court_form_fields', {}).get(template_id, {})
        preview = court_forms.preview(case, template_id, fields=fields, calculation=calculation)
        pdf = court_forms.render_pdf(template_id, case, fields=fields, calculation=calculation)
        destination = output / (template_id + '-simulated-reviewed-layout.pdf')
        destination.write_bytes(pdf)
        placement_errors = []
        with pymupdf.open(stream=pdf,filetype='pdf') as document:
            contents = ''.join(page.get_text() for page in document)
            page_map = {source_page:index for index,source_page in enumerate(preview['template']['pages'])}
            appendix = '\n'.join(page.get_text() for page in list(document)[len(page_map):])
            normalized = lambda value: re.sub(r'\s+', '', str(value)).replace(',', '')
            for field in preview['fields']:
                if field['value'] in (None, ''):
                    continue
                expected = normalized(court_forms._display(field['value'], field['key']))
                printed = normalized(document[page_map[field['page']]].get_text(clip=pymupdf.Rect(field['rect'])))
                preserved = expected in printed or (field['key'] in preview['overflow_fields'] and expected in normalized(appendix))
                if not preserved:
                    placement_errors.append(field['key'])
        if preview['missing_fields']:
            errors.append({'template':template_id,'missing_required':preview['missing_fields']})
        if placement_errors:
            errors.append({'template':template_id,'placement_errors':placement_errors})
        rows.append({'template':template_id,'required_missing':preview['missing_fields'],
            'populated_fields':sum(row['value'] is not None for row in preview['fields']),
            'total_fields':len(preview['fields']),'output_has_text':bool(contents.strip()),
            'placement_errors':placement_errors,
            'file':destination.relative_to(ROOT).as_posix()})
    return {'passed':not errors,'failures':errors,'forms':rows,'calculation_summary':calculation['summary'],
        'legal_decisions_simulated':True,'approval_stored':False,'model_verified':False,
        'scope':'가상 판단 입력 후 계산·작성 칸 연결 시험. 실제 승인·AI 검증 결과가 아닙니다.'}


async def audit(render=True):
    from scripts.audit_document_extraction import build_case
    from apps.api import automation, court_forms, evidence_mapping, extraction_readiness, grounded_drafting
    folder = ROOT / 'examples/court_ready_fixture'
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    case = build_case(folder)
    packet = evidence_mapping.build(case)
    failures, documents = [], []
    for entry, doc in zip(manifest['documents'], case['documents']):
        facts = [row for row in packet['facts'] if row['document_id'] == doc['id']]
        tests = []
        for expected in entry.get('expected_facts', []):
            matched = any(row['key'] == expected['key'] and row['value'] == expected['value']
                and all(row.get(key) == expected[key] for key in ('frequency','basis') if key in expected) for row in facts)
            tests.append({**expected, 'passed': matched})
        quotes_ok = all(row['quote'] in next(page['text'] for page in doc['page_texts'] if page['page'] == row['page']) for row in facts)
        extraction_readiness.capture(case, doc)
        state = extraction_readiness.document_state(case, doc)
        if not quotes_ok or not state['can_review'] or any(not row['passed'] for row in tests):
            failures.append(entry['file'])
        documents.append({'file':entry['file'], 'pages':len(doc['page_texts']), 'facts':len(facts),
            'quotes_preserved':quotes_ok, 'extraction_state':state, 'checks':tests})
    expected_values = manifest.get('expected_form_values', manifest.get('expected', {}))
    if not isinstance(expected_values, dict) or not expected_values:
        raise ValueError('An independent nonempty expected form-value map is required.')
    comparisons = [{'key':key, 'expected':value, 'actual':packet['form_values'].get(key),
                    'passed':packet['form_values'].get(key)==value} for key,value in expected_values.items()]
    if not documents or any(not row['checks'] for row in documents):
        failures.append('per_document_expectations_missing')
    if any(not row['passed'] for row in comparisons):
        failures.append('aggregate_mapping')
    forms = []
    for template in court_forms.TEMPLATES.values():
        if not template.get('fields'):
            continue
        preview = court_forms.preview(case, template['id'])
        expected_fields = manifest.get('expected_court_form_values', {}).get(template['id'], {})
        field_checks = []
        for key, expected in expected_fields.items():
            actual = [field['value'] for field in preview['fields'] if field['key'] == key]
            check = {'key':key, 'expected':expected, 'actual':actual,
                     'passed':bool(actual) and all(value == expected for value in actual)}
            field_checks.append(check)
            if not check['passed']:
                failures.append(template['id'] + ':' + key)
        forms.append({'id':template['id'], 'fields_with_values':sum(row['value'] is not None for row in preview['fields']),
                      'total_fields':len(preview['fields']), 'pending_required':preview['missing_fields'],
                      'independent_field_checks':field_checks})
    output = ROOT / 'reports/court-ready-fixture'
    output.mkdir(parents=True, exist_ok=True)
    state_dir = ROOT / '.work/ocr-audit'
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'court_ready_fixture-case.json').write_text(json.dumps(case,ensure_ascii=False,indent=2),encoding='utf-8')
    report = {'synthetic':True, 'passed':not failures, 'failures':failures,
        'document_count':len(documents), 'page_count':sum(row['pages'] for row in documents),
        'fact_count':len(packet['facts']), 'documents':documents, 'aggregate':comparisons,
        'source_only_forms':forms, 'mapping_errors':packet['errors'],
        'live_database_used':False, 'model_called':False, 'expected_answers_given_to_parser':False}
    (output / 'extraction.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if render and not failures:
        case['evidence_mapping'] = packet
        sources = automation._sources(case, verified_only=True, packet=packet)
        items = automation._verification_items(case, sources, packet=packet)
        narrative = grounded_drafting.review_fallback(items, [], [], None, court_id=case['court_id'],
            org_id=case['org_id'], consultation={'text':case['consultation']['notes'],'source_id':'consultation:notes'})
        narrative_path = state_dir / 'court_ready_fixture-narrative.json'
        narrative_path.write_text(json.dumps(narrative,ensure_ascii=False,indent=2),encoding='utf-8')
        from scripts.audit_generated_documents import run
        report['pdf_content_passed'] = await run('court_ready_fixture', narrative_path)
        report['passed'] = report['passed'] and report['pdf_content_passed']
        generated_case = json.loads((state_dir / 'court_ready_fixture-generated-case.json').read_text(encoding='utf-8'))
        layout = reviewed_layout(generated_case, packet, folder, output)
        (output / 'reviewed-layout.json').write_text(json.dumps(layout,ensure_ascii=False,indent=2),encoding='utf-8')
        report['reviewed_layout_passed'] = layout['passed']
        report['passed'] = report['passed'] and layout['passed']
        (output / 'extraction.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return {key: report.get(key) for key in ('passed','failures','document_count','page_count','fact_count','pdf_content_passed','reviewed_layout_passed')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--extraction-only', action='store_true')
    args = parser.parse_args()
    os.environ['DEBTOFF_DATA_DIR'] = str(ROOT / '.work/ocr-audit/court-ready-runtime')
    os.environ['DEBTOFF_AUTO_AX'] = '0'
    os.environ['DEBTOFF_LEGAL_WATCH'] = '0'
    result = asyncio.run(audit(not args.extraction_only))
    print(json.dumps(result,ensure_ascii=False))
    sys.exit(0 if result['passed'] else 1)
