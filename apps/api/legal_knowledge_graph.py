"""Small, source-bound legal graph. This is evidence retrieval, never an approval model.

Only curated public nodes are returned. Client prose is neither stored nor sent to
an external service. Court rules never cross jurisdictions; decisions keep their
actual procedural stage. A changed body invalidates its interpretation until the
curated anchors have been reviewed again by a maintainer.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
GRAPH_PATH = ROOT / 'data/legal_knowledge_graph.json'
VERSION = 'legal-graph-v1'
MAX_REFERENCES = 8
MAX_CONTEXT_CHARS = 10000
STAGES = {'application', 'post_approval', 'discharge'}
ALIASES = {
    'unsecured_limit': 'debt_limit', 'secured_limit': 'debt_limit',
    'UNSECURED_DEBT_LIMIT': 'debt_limit', 'SECURED_DEBT_LIMIT': 'debt_limit',
    'income_positive': 'continuing_income', 'NO_DISPOSABLE_INCOME': 'continuing_income',
    'liquidation': 'liquidation_gap', 'LIQUIDATION_GAP': 'liquidation_gap',
    'LIQUIDATION_VALUE_SHORTFALL': 'liquidation_gap',
    'repayment_period': 'repayment_period', 'PERIOD_EXCEPTION_REQUIRED': 'repayment_period',
    'MISSING_REQUIRED_DOCUMENT': 'document_completeness',
    'MISSING_REQUIRED_FIELDS': 'document_completeness',
    'DOCUMENT_EVIDENCE_MISSING': 'document_completeness',
    'prior_discharge': 'prior_discharge', 'RECENT_DISCHARGE': 'prior_discharge',
    'priority_repayment': 'priority_debt', 'PRIORITY_DEBT_UNPAID': 'priority_debt',
    'plan_feasibility': 'performance_risk', 'PERFORMANCE_RISK': 'performance_risk',
    'additional_living_cost': 'additional_living_cost',
    'investment_loss': 'investment_loss', 'asset_disposal': 'asset_disposal',
    'spouse_property': 'spouse_property', 'fees_paid': 'procedural_cost',
    'INSUFFICIENT_INCOME': 'continuing_income',
    'LIQUIDATION_SHORTFALL': 'liquidation_gap',
    'DEBT_LIMIT_EXCEEDED': 'debt_limit',
    'UNSECURED_LIMIT_EXCEEDED': 'debt_limit', 'SECURED_LIMIT_EXCEEDED': 'debt_limit',
    'MISSING_EVIDENCE': 'document_completeness',
    'PRIOR_DISCHARGE': 'prior_discharge', 'RECENT_BORROWING': 'document_completeness',
    'ASSET_DISPOSAL': 'asset_disposal', 'FAMILY_REPAYMENT': 'preferential_repayment',
    'MINIMUM_REPAYMENT_SHORTFALL': 'objection', 'MINIMUM_REPAYMENT_NOT_MET': 'objection',
    'PREAPPROVAL_COSTS_UNPAID': 'procedural_cost',
    'NO_POSITIVE_REPAYMENT_CAPACITY': 'performance_risk',
    'EXTENDED_PERIOD_REASON_REQUIRED': 'repayment_period', 'SHORT_PERIOD_REASON_REQUIRED': 'repayment_period',
}


def _hash(value):
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def load():
    try:
        value = json.loads(GRAPH_PATH.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _watched_versions(graph):
    from . import corpus
    result = {}
    # Only public source metadata is needed; avoid copying every PDF chunk on
    # every overview refresh.
    catalog = {item['id']: item for item in corpus._snapshot().get('sources', [])}
    for source in graph.get('sources', []):
        if not source.get('watch_semantic_sha256'):
            continue
        current = catalog.get(source['id'])
        if current:
            result[source['id']] = {'status': current.get('status'),
                                    'applicability_status': current.get('applicability_status'),
                                    'semantic_sha256': current.get('watch_semantic_sha256')}
    return result


def signature(policy=None):
    """Fresh content digest: graph edits invalidate existing AI/cache decisions."""
    graph = load()
    content = {key: graph.get(key) for key in ('schema_version', 'version', 'scope', 'nodes', 'edges', 'use_policy')}
    content['sources'] = [{k: v for k, v in s.items() if k not in {'retrieved_at', 'last_error', 'refresh_status', 'source_sha256', 'final_url'}}
                          for s in graph.get('sources', [])]
    return _hash({'version': VERSION, 'graph': content, 'watched_versions': _watched_versions(graph),
                  'policy_signature': (policy or {}).get('signature')})


def _official(url):
    parsed = urlparse(str(url or ''))
    host = (parsed.hostname or '').lower()
    try:
        port = parsed.port
    except ValueError:
        return False
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password
            and port in (None, 443)
            and any(host == suffix or host.endswith('.' + suffix) for suffix in ('law.go.kr', 'scourt.go.kr')))


def _today(policy):
    return str((policy or {}).get('as_of') or datetime.now(timezone(timedelta(hours=9))).date())[:10]


def _pending_ids(policy):
    return {str(item.get('source_id') or item.get('id'))
            for item in (policy or {}).get('pending_changes', []) if isinstance(item, dict)}


def _valid_node(node, sources, court_id, policy, watched=None):
    source = sources.get(node.get('source_id'), {})
    body, quote = source.get('body', ''), node.get('excerpt', '')
    if (not _official(source.get('url')) or source.get('status') != 'verified_snapshot'
            or node.get('status') != 'source_bound' or len(body) < 80
            or _hash(body) != source.get('body_sha256')
            or node.get('bound_body_sha256') != source.get('body_sha256')
            or not quote or quote not in body or len(quote) < 30):
        return False
    if node.get('court_id') and node['court_id'] != court_id:
        return False
    if node.get('effective_from') and node['effective_from'] > _today(policy):
        return False
    if node.get('valid_through') and node['valid_through'] < _today(policy):
        return False
    if set(node.get('watch_source_ids', [source.get('id')])) & _pending_ids(policy):
        return False
    current = (watched or {}).get(source.get('id'))
    if current and (current.get('status') != 'collected'
                    or current.get('applicability_status') not in {'baseline_unchanged', 'watch_scalar_validated'}
                    or current.get('semantic_sha256') != source.get('watch_semantic_sha256')):
        return False
    return True


def _reference(node, source):
    return {'node_id': node['id'], 'id': source['id'], 'url': source['url'],
            'title': node['title'], 'locator': node['locator'], 'excerpt': node['excerpt'],
            'body_sha256': source['body_sha256'], 'source_sha256': source['source_sha256'],
            'court_scope': node.get('court_id') or 'national',
            'decision_stage': node.get('decision_stage'), 'outcome': node.get('outcome')}


def trusted_reference(node_id, court_id=None, policy=None):
    graph = load()
    sources = {s['id']: s for s in graph.get('sources', [])}
    node = next((n for n in graph.get('nodes', []) if n.get('id') == node_id), None)
    if not node or node.get('kind') not in {'statute', 'court_rule', 'case'}:
        return None
    if not _valid_node(node, sources, court_id, policy, _watched_versions(graph)):
        return None
    return {**_reference(node, sources[node['source_id']]), 'graph_signature': signature(policy)}


def _issue_tags(features, graph):
    known = {n['id'].removeprefix('issue:') for n in graph.get('nodes', []) if n.get('kind') == 'issue'}
    result = set()
    for field in ('issue_tags', 'risk_codes', 'rule_codes'):
        rows = features.get(field) or []
        if not isinstance(rows, (list, tuple, set)):
            continue
        for value in rows:
            if isinstance(value, dict):
                value = value.get('code') or value.get('id')
            if isinstance(value, str):
                value = ALIASES.get(value, value)
                if value in known:
                    result.add(value)
    for name in ('spouse_property', 'asset_disposal', 'investment_loss', 'additional_living_cost', 'prior_discharge'):
        if features.get(name) is True:
            result.add(name)
    for feature, tags in {'prior_proceedings': ['prior_proceedings'],
                          'family_repayment': ['preferential_repayment'],
                          'variable_income': ['continuing_income', 'performance_risk'],
                          'objection': ['objection'],
                          'recent_borrowing': ['document_completeness']}.items():
        if features.get(feature) is True:
            result.update(tags)
    if features.get('preapproval_costs_paid') is False:
        result.add('procedural_cost')
    numeric = features.get('numeric_facts') if isinstance(features.get('numeric_facts'), dict) else {}
    calculation = features.get('calculation') if isinstance(features.get('calculation'), dict) else {}
    if isinstance(calculation.get('summary'), dict):
        calculation = calculation['summary']
    def number(values, key):
        value = values.get(key)
        return value if type(value) in (int, float) else None
    for key in ('liquidation_shortfall', 'liquidation_gap'):
        value = number(calculation, key)
        if value is not None and value > 0:
            result.add('liquidation_gap')
    if (number(calculation, 'additional_living_cost') or 0) > 0:
        result.add('additional_living_cost')
    for key in ('monthly_creditor_capacity', 'monthly_disposable_income', 'monthly_available'):
        value = number(calculation, key)
        if value is not None and value <= 0:
            result.add('performance_risk')
    if (number(numeric, 'disposal_proceeds') or 0) > 0:
        result.add('asset_disposal')
    if (number(numeric, 'tax_arrears') or 0) > 0:
        result.add('priority_debt')
    # A specific issue must not displace the current statutory foundation.
    # These are review topics, not findings that a violation or shortfall exists.
    result.update(('document_completeness', 'continuing_income', 'debt_limit', 'liquidation_gap', 'repayment_period'))
    return result


def retrieve(features=None, current_policy=None, *, limit=MAX_REFERENCES, max_chars=MAX_CONTEXT_CHARS):
    features = features if isinstance(features, dict) else {}
    graph = load()
    watched = _watched_versions(graph)
    sources = {s['id']: s for s in graph.get('sources', [])}
    court_id = features.get('court_id') if isinstance(features.get('court_id'), str) else None
    stage = features.get('procedure_stage') or features.get('stage')
    stage = 'application' if stage == 'initial_application' else stage
    stage = stage if stage in STAGES else 'application'
    issues = _issue_tags(features, graph)
    candidates, excluded = [], 0
    for node in graph.get('nodes', []):
        if node.get('kind') not in {'statute', 'court_rule', 'case'}:
            continue
        matched = sorted(set(node.get('issue_tags', [])) & issues)
        if not matched or stage not in node.get('applicable_stages', ['application']):
            continue
        triggers = set(node.get('required_trigger_tags', []))
        if triggers and not triggers.intersection(issues):
            continue
        if not _valid_node(node, sources, court_id, current_policy, watched):
            excluded += 1
            continue
        item = {k: node.get(k) for k in ('id', 'title', 'kind', 'summary', 'required_evidence', 'actions', 'limits', 'decision_stage', 'outcome')}
        item.update(matched_issues=matched, court_scope=node.get('court_id') or 'national',
                    source=_reference(node, sources[node['source_id']]))
        priority = len(matched) * 10 + (3 if node.get('court_id') == court_id and court_id else 0)
        if node['id'] in {'KG579', 'KG611', 'KG614'}:
            priority += 5
        if node['kind'] == 'case':
            priority += 30
        candidates.append((priority, item))
    candidates.sort(key=lambda pair: (-pair[0], pair[1]['id']))
    result = {'version': graph.get('version', VERSION), 'signature': signature(current_policy), 'status': 'unavailable',
              'court_id': court_id, 'issue_tags': sorted(issues), 'matches': [], 'references': [],
              'excluded_count': excluded, 'notes': [
                  '공개 판결의 결론과 사건 단계를 구분한 검토 근거입니다. 인가 확률이나 성공 문서 표본으로 사용하지 않습니다.',
                  '대법원 결정은 전국에 참고되는 법리이며 해당 지역 법원의 유사 인가결정으로 표시하지 않습니다.']}
    budget = min(MAX_CONTEXT_CHARS, max(1000, int(max_chars)))
    for _, item in candidates:
        if len(result['matches']) >= min(MAX_REFERENCES, max(0, int(limit))):
            break
        candidate = {**result, 'matches': result['matches'] + [item], 'references': result['references'] + [item['source']]}
        if len(json.dumps(candidate, ensure_ascii=False, separators=(',', ':'))) <= budget:
            result = candidate
    result['status'] = 'ready' if result['matches'] else 'unavailable'
    return result


def external_context(features=None, current_policy=None):
    """Public, deterministic and bounded. Callers must independently mask facts."""
    selected = retrieve(features, current_policy, max_chars=MAX_CONTEXT_CHARS)
    ids = {item['id'] for item in selected['matches']}
    graph = load()
    edges = [e for e in graph.get('edges', []) if e.get('from') in ids and e.get('relation') in {'addresses', 'requires', 'interprets'}]
    result = {k: selected[k] for k in ('version', 'signature', 'status', 'court_id', 'issue_tags', 'references', 'excluded_count', 'notes')}
    result['nodes'] = [{k: v for k, v in item.items() if k != 'source'} for item in selected['matches']]
    result['edges'] = [{'source': e['from'], 'relation': e['relation'], 'target': e['to']} for e in edges[:16]]
    # Include the issue/evidence endpoints so every returned edge is traversable.
    targets = {e['target'] for e in result['edges']} - ids
    for node in graph.get('nodes', []):
        if node['id'] in targets and node.get('kind') in {'issue', 'evidence'}:
            result['nodes'].append(node)
    valid_ids = {n['id'] for n in result['nodes']}
    result['edges'] = [e for e in result['edges'] if e['target'] in valid_ids]
    while result['edges'] and len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))) > MAX_CONTEXT_CHARS:
        result['edges'].pop()
        endpoints = ids | {e['target'] for e in result['edges']}
        result['nodes'] = [n for n in result['nodes'] if n['id'] in endpoints]
    return result
