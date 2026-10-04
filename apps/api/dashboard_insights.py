"""Connect dashboard facts, public legal knowledge and current reviewed strategy.

This projection is read-only. It never calls a model or makes a document or
legal decision on page load. Public-case similarity is an issue match, not a
successful-outcome probability or an instruction to dispose of property.
"""
from __future__ import annotations

import copy

from . import case_assessment, legal_calculator, legal_watch, store


def knowledge_policy(case):
    return {**legal_calculator.policy(), **legal_watch.case_policy_status(case)}


def build(case):
    from . import legal_knowledge_graph
    result = case_assessment.build(case)
    features = result.pop('reasoning_features', {})
    knowledge = legal_knowledge_graph.retrieve(features, knowledge_policy(case))
    result['knowledge'] = knowledge
    actions = result['actions']
    has_evidence = bool(result['metrics']['required_fields']['source'].get('verified'))
    for match in knowledge.get('matches', []) if has_evidence else []:
        if match.get('kind') not in {'case', 'precedent', 'practice', 'court_rule'}:
            continue
        if not match.get('matched_issues'):
            continue
        source = match.get('source', {})
        for index, text in enumerate(match.get('actions', [])[:2]):
            actions.append({'id': 'knowledge:' + match['id'] + ':' + str(index),
                'category': 'supporting', 'priority': 'low', 'title': match['title'],
                'summary': text, 'reason': match.get('summary'),
                'required_evidence': match.get('required_evidence', []),
                'limits': match.get('limits', []), 'source_refs': [source],
                'target': {'tab': 'issues', 'anchor': None},
                'origin': 'public_issue_match', 'court_scope': match.get('court_scope'),
                'decision_stage': match.get('decision_stage')})
    candidates = list(reversed(case.get('strategy_analyses', [])))
    latest = next((row for row in candidates if not row.get('stale')
                   and row.get('input_revision') == case.get('input_revision')
                   and row.get('knowledge_signature') == knowledge.get('signature')
                   and result['freshness']['calculation'] == 'current'
                   and row.get('calculation_id') == result['freshness'].get('calculation_id')), None)
    review = (latest or {}).get('verification') or {}
    result['strategy_review'] = {'status': review.get('status', 'stale' if candidates else 'not_attempted'),
        'updated_at': (latest or {}).get('created_at'),
        'summary': '현재 자료와 법률 근거의 추가 검토 결과입니다.' if latest else
                   '자료 확인과 계산이 진행되면 현재 근거를 바탕으로 전략을 다시 검토합니다.',
        'model_called_on_page_load': False}
    # AI proposals stay proposals. A suggestion does not increase the number
    # of confirmed unmet quantitative rules or create an approval percentage.
    if latest:
        for index, row in enumerate(latest.get('strategies', [])):
            if row.get('origin') != 'ai_strategy_review' or not row.get('source_refs'):
                continue
            actions.append({'id': 'strategy:' + str(index), 'category': row.get('category', 'legal'),
                'priority': 'medium', 'title': row.get('title', '추가 전략 검토'),
                'summary': row.get('description', ''), 'reason': row.get('reason', ''),
                'source_refs': copy.deepcopy(row['source_refs']), 'origin': 'ai_strategy_review',
                'target': {'tab': 'issues', 'anchor': None}})
    seen, unique = set(), []
    for action in actions:
        identity = (action.get('category'), action.get('title'), action.get('summary'))
        if identity not in seen:
            seen.add(identity)
            unique.append(action)
    result['actions'] = sorted(unique, key=lambda row:
        {'high': 1, 'medium': 2, 'low': 3}.get(row.get('priority'), 3))
    result['input_signature'] = store.digest([result['freshness'], knowledge.get('signature'),
        result['input_revision'], (latest or {}).get('id')])
    return result
