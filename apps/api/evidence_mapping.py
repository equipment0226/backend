"""Deterministic document observations, arithmetic and explicitly proposed law inputs.

Re-extract from verified original pages on every build. Stored candidates, model
answers and generated prose are never proof. A code-checked observation does not
mean that a semantic AI review or a legal approval has passed.
"""
from __future__ import annotations

import copy
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from . import document_facts, legal_calculator, store

VERSION = 'typed-evidence-mapping-v1'
DIRECT = {'client_name', 'resident_id', 'address', 'phone', 'employer', 'employer_address',
          'employer_phone', 'employment_period', 'employment_start', 'job_title', 'housing_type',
          'housing_deposit', 'housing_cost', 'household_size', 'dependent_count', 'employment_type',
          'living_expenses', 'insurance_surrender', 'tax_arrears', 'prior_proceedings', 'income_seizure'}


def sources(case):
    result = []
    for doc in case.get('documents', []):
        if doc.get('status') != 'verified' or doc.get('automated_check', {}).get('coverage_status') == 'identity_conflict':
            continue
        pages = doc.get('page_texts') or [{'page': 1, 'text': doc.get('text', '')}]
        for index, page in enumerate(pages):
            text = page.get('text', '') if isinstance(page, dict) else str(page)
            number = page.get('page', index + 1) if isinstance(page, dict) else index + 1
            if text.strip():
                result.append({'id': f"doc:{doc['id']}:p{number}:0", 'document_id': doc['id'],
                    'kind': 'case_document', 'text': text, 'page': number, 'version': doc.get('version', 1)})
    return result


def build(case):
    original_sources = sources(case)
    facts = document_facts.extract(original_sources)
    source_map = {source['id']: source for source in original_sources}
    # The extractor supplies typed context and exact source lines. Re-running it
    # prevents a forged typed_fact_id/value on a stored candidate from authorizing a field.
    facts = [row for row in facts if row.get('quote') and row['quote'] in source_map[row['source_id']]['text']]
    by_key = defaultdict(list)
    for row in facts:
        by_key[row['key']].append(row)
    values, origins, errors, derivations = {}, {}, [], []

    def put(key, value, rows, operation='source_observation'):
        if value is None:
            return
        values[key] = value
        origins[key] = {'type': 'deterministic_evidence', 'status': 'source_checked',
            'source_ids': sorted({row['document_id'] for row in rows}),
            'fact_ids': [row['id'] for row in rows], 'operation': operation,
            'semantic_review_required': True}
        if operation != 'source_observation':
            derivations.append({'key': key, 'value': value, 'operation': operation,
                                'operand_fact_ids': [row['id'] for row in rows]})

    def unique(key, rows=None):
        rows = by_key[key] if rows is None else rows
        choices = {store.dumps(row['value']) for row in rows}
        if len(choices) == 1:
            return rows[0]['value'], rows
        if len(choices) > 1:
            errors.append({'code': 'CONFLICTING_TYPED_VALUES', 'key': key, 'fact_ids': [row['id'] for row in rows]})
        return None, rows

    for key in DIRECT:
        value, rows = unique(key)
        put(key, value, rows)
    registered=[row for row in by_key['address'] if re.search(r'주민\s*등록.*등본',source_map[row['source_id']]['text'][:300])]
    value,rows=unique('registered_address',registered)
    put('registered_address',value,rows)
    if 'employment_period' not in values:
        active_starts=[row for row in by_key['employment_start'] if any(
            job['source_id']==row['source_id'] and job['value']=='급여소득자' and re.search(
                r'현재\s*(?:재직|근무)|재직\s*중|재직(?:하고)?\s*있',job['quote'])
            for job in by_key['employment_type'])]
        start,rows=unique('employment_start',active_starts)
        if start:
            put('employment_period',str(start)+' ~ 현재',rows,'start_date_plus_explicit_current_employment')
    tenant, rows = unique('housing_tenant')
    put('tenant_name', tenant, rows)

    # Prefer explicitly monthly net earnings. Gross, annual taxable income,
    # pension assessment and bank transfers are never silently monthly net pay.
    monthly = [row for row in by_key['income_net'] if row.get('frequency') == 'monthly']
    if not monthly:
        monthly = [row for row in by_key['income_gross'] if row.get('frequency') == 'monthly']
    income_value, income_rows = unique('monthly_income', monthly)
    basis = income_rows[0].get('basis') if income_rows and income_value is not None else None
    if income_value is not None:
        put('monthly_income', income_value, income_rows)
        put('income_manwon', income_value / 10000, income_rows, 'KRW_divide_10000')
        put('annual_income', income_value * 12, income_rows, 'monthly_times_12_projection')
        put('income_label', '급여', income_rows, 'typed_payroll_label')
        put('income_period_label', '월평균', income_rows, 'explicit_monthly_period')

    # An account/loan identity is never its row index or just a similar amount.
    # Duplicated documents for the same dated balance do not double count money.
    balances = defaultdict(list)
    unscoped_balances = []
    for row in by_key['cash_balance']:
        if row.get('balance_kind') == 'opening':
            continue
        identity = (row.get('institution'), row.get('account_key'))
        if not all(identity):
            unscoped_balances.append(row)
            continue
        balances[identity].append(row)
    balance_rows = []
    for identity, rows in balances.items():
        dated = [row for row in rows if row.get('as_of')]
        if dated:
            latest = max(row['as_of'] for row in dated)
            rows = [row for row in dated if row['as_of'] == latest]
        value, rows = unique('cash_balance', rows)
        if value is not None:
            balance_rows.append(rows[0])
    if balance_rows and len(balance_rows) == len(balances):
        put('bank_balance', sum(row['value'] for row in balance_rows), balance_rows, 'distinct_latest_accounts_sum')
        if len(balance_rows) == 1:
            row = balance_rows[0]
            put('bank_name', row.get('institution'), [row], 'source_account_metadata')
            put('bank_account', row.get('account_key'), [row], 'source_account_metadata')
        if any(row['value'] != values['bank_balance'] for row in unscoped_balances):
            errors.append({'code': 'UNSCOPED_BALANCE_RECONCILIATION', 'key': 'bank_balance'})
    elif not balances and unscoped_balances:
        # A narrative total is a single total, never an additional account. It
        # can populate the observed-total field, but cannot identify each account.
        value, rows = unique('cash_balance', unscoped_balances)
        put('bank_balance', value, rows, 'unscoped_total_requires_account_reconciliation')

    debt_docs = defaultdict(list)
    for row in facts:
        if row['key'].startswith('creditor_'):
            # A PDF can contain several creditors or several loans at one bank.
            # Page-local issuer/loan metadata identifies the entity, never names[0].
            debt_docs[(row['document_id'], row.get('institution'), row.get('loan_key'), row.get('page'))].append(row)
    entities = defaultdict(list)
    for (doc_id, institution, loan, page), rows in debt_docs.items():
        names = [row for row in rows if row['key'] == 'creditor_name']
        name = names[0]['value'] if names else institution
        if not name:
            continue
        entities[(name, loan)].append((doc_id + ':page:' + str(page), rows))
    creditors, creditor_fact_rows = [], []
    for (name, loan), documents in entities.items():
        if len(documents) > 1 and not loan:
            errors.append({'code': 'LOAN_IDENTITY_REQUIRED', 'key': 'creditors'})
            continue
        rows = [row for _, grouped in documents for row in grouped]
        row = {'id': 'creditor-' + store.digest([name, loan, documents[0][0]])[:12],
               'name': name, 'kind': None, 'principal': None, 'interest': None,
               'evidence_ids': sorted({r['document_id'] for r in rows})}
        for suffix, target in [('principal', 'principal'), ('interest', 'interest'), ('cause', 'cause'), ('address', 'address')]:
            row[target], _ = unique('creditor_' + suffix, [r for r in rows if r['key'] == 'creditor_' + suffix])
        kind, _ = unique('creditor_kind', [r for r in rows if r['key'] == 'creditor_kind'])
        row['kind'] = {'무담보': 'unsecured', '담보': 'secured', '우선권': 'priority'}.get(kind)
        index = len(creditors)
        creditors.append(row)
        creditor_fact_rows.extend(rows)
        for key, value in row.items():
            if key not in {'id', 'evidence_ids'}:
                put(f'creditors.{index}.{key}', value, rows, 'document_creditor_binding')
        put(f'creditors.{index}.number', index + 1, rows, 'list_order')
        put(f'creditors.{index}.basis', '원문에 기재된 원금·이자 및 기준일 대조', rows, 'evidence_description')
    unresolved_creditors=any(error['code'] in {'LOAN_IDENTITY_REQUIRED','CONFLICTING_TYPED_VALUES'} and
                            error.get('key','').startswith('creditor') for error in errors)
    if creditors and not unresolved_creditors:
        for field, target in [('principal', 'principal_total'), ('interest', 'interest_total')]:
            if all(type(row[field]) is int for row in creditors):
                put(target, sum(row[field] for row in creditors), creditor_fact_rows, 'distinct_creditors_sum')
        if all(type(row[field]) is int for row in creditors for field in ('principal', 'interest')):
            put('total_debt', sum(row['principal'] + row['interest'] for row in creditors), creditor_fact_rows, 'principal_plus_interest')
            if all(row['kind'] for row in creditors):
                for secured, key in [(True, 'secured_debt'), (False, 'unsecured_debt')]:
                    put(key, sum(row['principal'] + row['interest'] for row in creditors if (row['kind'] == 'secured') == secured),
                        creditor_fact_rows, 'classified_creditors_sum')
    if creditors:
        def amount(value):
            return f'{value:,}원' if type(value) is int else '미확인'
        readable=[]
        for row in creditors:
            category={'unsecured':'무담보','secured':'담보부','priority':'우선권'}.get(row['kind'],'분류 미확인')
            readable.append(f"{row['name']} · 원금 {amount(row['principal'])} · 이자 {amount(row['interest'])} · {category}")
        put('creditors','\n'.join(readable),creditor_fact_rows,'readable_creditor_summary')

    assets = []
    for key, label in [('bank_balance', '예금'), ('housing_deposit', '임차보증금'), ('insurance_surrender', '보험해약환급금')]:
        if type(values.get(key)) is int:
            assets.append({'id': key, 'label': label, 'owned_value': values[key], 'secured_deduction': None,
                'exempt_deduction': None, 'disposal_cost': None, 'evidence_ids': origins[key]['source_ids']})
    for key, target in [('vehicle_ownership', 'vehicle_value'), ('real_estate_ownership', 'real_estate_value')]:
        owned, rows = unique(key)
        if owned is False:
            put(target, 0, rows, 'explicit_no_ownership')
    contracts, rows = unique('insurance_contracts')
    if contracts is False and 'insurance_surrender' not in values:
        put('insurance_surrender', 0, rows, 'explicit_no_contract')
    if assets:
        # This is the sum of observed assets, not a declaration of exhaustive ownership.
        asset_rows = [row for row in facts if row['id'] in {fid for asset in assets for fid in origins[asset['id']]['fact_ids']}]
        put('assets_total', sum(row['owned_value'] for row in assets), asset_rows, 'observed_assets_sum')

    rules = legal_calculator.policy()
    inputs = {key: None for key in ('recognized_household_size', 'additional_living_cost', 'base_living_cost',
        'months', 'prepaid_months', 'monthly_trustee_fee', 'preapproval_costs_paid', 'objection', 'annual_discount_rate', 'living_cost_mode')}
    period_rows = by_key['income_period']
    period = ' / '.join(dict.fromkeys(row['value'] for row in period_rows)) or next((
        f"{row['period_start']}~{row['period_end']}" for row in income_rows if row.get('period_start') and row.get('period_end')), None)
    put('income_period',period,period_rows or income_rows,'explicit_source_periods')
    inputs.update(policy_id=rules['id'], as_of=date.today().isoformat(), assets=assets, creditors=creditors, decisions={},
        income={'kind': 'wage' if monthly and (values.get('employment_type') == '급여소득자' or values.get('employer')) else None,
            'basis': basis, 'monthly_amount': income_value, 'period': period,
            # Net inputs must not deduct tax twice. This zero is algebra, not absence of tax.
            'taxes_and_social_insurance': 0 if basis == 'net' else unique('income_deductions', [r for r in by_key['income_deductions'] if r.get('frequency') == 'monthly'])[0],
            'business_expenses': 0 if monthly and values.get('employment_type') == '급여소득자' else None,
            'evidence_ids': sorted({row['document_id'] for row in income_rows})})
    proposed = [{'key': 'months', 'value': rules['normal_max_months'], 'status': 'proposed',
        'source_ids': ['statute-611'], 'reason': '일반 변제기간 상한을 적용한 검토안이며 기간 특례·수행 가능성 검토가 필요합니다.'}]
    if case.get('court_id') == 'CT01' and values.get('household_size') in range(1, 8):
        people = values['household_size']
        median = rules['median_income_2026'][str(people)]
        base = int((Decimal(median) * Decimal(rules['seoul_base_living_ratio'])).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        proposed.extend([
            {'key': 'recognized_household_size', 'value': people, 'status': 'proposed',
             'source_ids': origins['household_size']['source_ids'], 'reason': '서류상 가구원 수를 출발점으로 한 안이며 법률상 부양인원 인정은 미확정입니다.'},
            {'key': 'base_living_cost', 'value': base, 'status': 'proposed', 'source_ids': ['median-2026', 'seoul-living-2026'],
             'reason': '위 인정인원 제안에 따른 서울 기준 생계비. 실제 지출·추가생계비 판단과 구별합니다.'}])
    return {'version': VERSION, 'sources': original_sources, 'facts': facts, 'form_values': values, 'origins': origins,
        'inputs': inputs, 'derivations': derivations, 'errors': errors, 'proposed_decisions': proposed,
        'source_signature': store.digest(original_sources), 'input_revision': case.get('input_revision'),
        'semantic_verification_required': True, 'legal_approval': False}


def candidate_rows(packet):
    """Compatibility projections are code-derived, not new source observations."""
    rows = []
    by_id = {row['id']: row for row in packet['facts']}
    for key, value in packet['form_values'].items():
        if '.' in key:
            continue
        origin = packet['origins'][key]
        evidence = [by_id[fid] for fid in origin['fact_ids'] if fid in by_id]
        if not evidence:
            continue
        rows.append({'id': 'mapped-' + store.digest([key, value, origin])[:16], 'key': key,
            'label': document_facts.LABELS.get(key, key), 'value': value, 'status': 'source_checked',
            'document_id': evidence[0]['document_id'], 'source_ids': origin['source_ids'],
            'quote': evidence[0]['quote'], 'origin': VERSION, 'derivation': origin,
            'typed_fact_ids': origin['fact_ids'], 'source_type': 'case_document'})
    return rows


def form_facts(packet, template_id):
    """Preserve every supported observation in the relevant form's review annex."""
    def target(row):
        key = row['key']
        if key.startswith('creditor_') or key in {'tax_arrears', 'loan_key'}:
            return 'D5106'
        if key.startswith('bank_') or key in {'cash_balance', 'account_key', 'account_scope', 'institution',
                'insurance_contracts', 'insurance_surrender', 'real_estate_ownership', 'vehicle_ownership', 'housing_ownership'}:
            return 'D5101'
        if key.startswith('income_') or key in {'living_expenses', 'pension_assessed_income'}:
            return 'D5103'
        if key.startswith(('employment_', 'employer')) or key in {'health_qualification', 'pension_membership'}:
            return 'D5115'
        if key in {'client_name', 'resident_id', 'address', 'phone'}:
            return 'D5100'
        return 'D5105'
    return [copy.deepcopy(row) for row in packet['facts'] if target(row) == template_id]


def mapping(sources_input, context=None):
    """Calculator interface using the same parser; no model call or legal defaults."""
    case = {'documents': [{'id': row.get('document_id') or row['id'], 'status': 'verified', 'text': row['text'],
        'version': row.get('version', 1)} for row in sources_input], **(context or {})}
    packet = build(case)
    if not packet['facts']:
        return None
    inputs = packet['inputs']
    inputs.update({key: context[key] for key in ('as_of', 'policy_id') if (context or {}).get(key)})
    return {'status': 'extracted' if not packet['errors'] else 'needs_review', 'inputs': inputs,
        'evidence': [], 'errors': packet['errors'], 'typed_facts': packet['facts'],
        'deterministic_verification': {'passed': True, 'scope': 'typed_original_pages_units_and_arithmetic'},
        'semantic_verification_required': True, 'semantic_review_nonblocking_for_first_draft': True,
        'proposed_decisions': packet['proposed_decisions'], 'input_sha256': store.digest(sources_input),
        'external_processing': False, 'version': VERSION}
