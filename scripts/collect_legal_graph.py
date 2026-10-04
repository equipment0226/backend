"""Compile a small public-law graph; optionally refresh its official snapshots.

Default compiles verified local corpus/research sources, with no HTTP or client DB
access. --refresh fetches the same allowlisted URLs. Changed bodies invalidate
their curated nodes, preserving the previous interpretation for review. Rebinding
requires --accept-reviewed SOURCE_ID after a maintainer has compared the change.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.api import corpus
from apps.api.legal_watch import _law_edition_url
from apps.api.legal_knowledge_graph import GRAPH_PATH, _hash, _official


# Source-linked interpretation templates; numbers are quoted from the law, not
# learned from a favourable judgment or a customer's submitted document.
RULES = [
    ('KG579', 'LW579', 579, '개인회생 대상과 계속적인 소득',
     ['debt_limit', 'continuing_income'],
     '담보채무와 일반채무를 나누어 한도를 확인하고 계속 얻을 수 있는 소득을 입증합니다.',
     ['채무종류별 잔액증명', '급여명세·입금내역·재직증명'],
     ['담보와 무담보 채무를 구분하여 신청 당시 잔액을 대조합니다.', '일시적인 입금과 정기 소득을 구분해 변제재원을 설명합니다.'],
     ['한도 충족만으로 인가되는 것은 아닙니다.']),
    ('KG589', 'LW589', 589, '필수 기재사항과 첨부서류',
     ['document_completeness', 'prior_proceedings', 'correction_insufficient'],
     '신청원인·재산·채무를 기재하고 채권자·재산·수입지출 목록과 소득 증명·진술서를 연결합니다.',
     ['필수목록별 원본 및 추출내용', '최근 10년 절차가 있으면 관련서류'],
     ['빠진 기재항목과 그 항목을 증명할 서류를 한 묶음으로 보완합니다.'],
     ['원문 항목을 읽은 비율과 법률요건 충족 여부는 서로 다른 지표입니다.']),
    ('KG595', 'LW595', 595, '개시신청 기각사유 확인',
     ['document_completeness', 'prior_discharge', 'procedural_cost', 'correction_insufficient', 'prior_proceedings'],
     '미제출·허위 기재·기한·절차비용과 최근 면책 여부를 구분해 확인합니다.',
     ['보정명령과 송달일', '비용납부 영수증', '종전 면책결정 및 확정 관련 자료'],
     ['법원 제출기한을 확인하고 부족한 항목의 제출 또는 소명 계획을 정합니다.', '이전 사건 신청 사실과 5년 이내 면책 사실을 구분합니다.'],
     ['과거 신청이나 폐지 사실만으로 최근 면책에 해당한다고 판단하지 않습니다.']),
    ('KG611', 'LW611', 611, '변제기간과 우선권 채권',
     ['repayment_period', 'priority_debt', 'liquidation_gap'],
     '변제기간은 원칙상 3년 이내이며 특별한 사정이 있으면 5년 이내 범위를 검토합니다. 우선권 채권의 전액변제도 확인합니다.',
     ['우선권 채권 증빙', '변제기간·금액표', '기간 예외가 필요한 사유'],
     ['청산가치나 우선권 채권 부족액을 계산하고 법이 정한 기간 예외 사유를 검토합니다.'],
     ['5년 연장은 자동 해결책이 아니며 특별한 사정을 확인해야 합니다.']),
    ('KG614', 'LW614', 614, '수행가능성·청산가치·인가요건',
     ['liquidation_gap', 'performance_risk', 'procedural_cost', 'objection'],
     '변제계획의 적법성·형평·수행가능성, 비용 납부 및 청산가치 보장을 함께 확인합니다.',
     ['자산가액·공제 근거', '월 소득 및 필요한 지출', '이의 진술 여부와 총변제 현재가치'],
     ['자산 평가와 정당한 공제를 다시 확인하고 실제 투입할 수 있는 소득·재산으로 부족액을 줄일 방안을 비교합니다.', '매각하면 잔여대금의 소재와 사용처를 증빙하여 재산목록을 다시 계산합니다.'],
     ['재산 매각 자체로 청산가치가 없어지지 않습니다.', '이의가 있으면 제2항 추가요건도 적용합니다.']),
    ('KGSEOUL405', 'C03', 405, '서울회생법원 추가 생계비 검토',
     ['additional_living_cost', 'performance_risk'],
     '지속적으로 필요한 주거비·의료비·미성년 자녀 교육비는 합리적인 범위에서 추가 생계비로 검토할 수 있습니다.',
     ['임대차계약과 실제 지급내역', '진단·치료계획·의료비 영수증', '교육비 및 부양 필요 자료'],
     ['정기적으로 지출할 사유와 금액을 입증하고 해당 연도 서울 생계비 기준에 맞는 추가 인정 가능성을 검토합니다.'],
     ['지출 전액이나 추가 생계비 자동 인정이 아닙니다. 연도별 의결 기준을 별도로 확인합니다.']),
    ('KGSEOUL406', 'C03', 406, '서울회생법원 배우자 명의 재산',
     ['spouse_property', 'asset_disposal', 'liquidation_gap'],
     '배우자 명의 재산의 일률적인 합산을 피하고 명의신탁 또는 부인권 예외 여부를 확인합니다.',
     ['취득자금과 소유 경위', '배우자에게 이전한 재산의 계약·대금 자료'],
     ['실질 소유와 자금흐름을 확인해 배우자 고유재산과 예외 대상 재산을 구분합니다.'],
     ['서울 실무준칙입니다. 다른 법원에 자동 적용하지 않습니다.', '배우자 명의라는 이유만으로 모든 재산을 제외하지 않습니다.']),
    ('KGSEOUL408', 'C03', 408, '서울회생법원 투자 손실금 처리',
     ['investment_loss', 'liquidation_gap'],
     '주식·가상화폐 투자 손실금의 청산가치 산입과 은닉 등 예외를 구분해 확인합니다.',
     ['투자거래 전체 내역', '현재 보유액과 출금대금 사용처'],
     ['실제 손실과 남아 있는 자산을 구분하고 은닉이나 회복할 재산이 없는지 거래 흐름을 확인합니다.'],
     ['손실 처리에 관한 서울 기준이며 남은 투자자산이나 허위 소명을 허용하지 않습니다.']),
]


def _canonical_body(text):
    # PDF extraction whitespace is not a change to the words of the rule. Keep
    # exact normalized lines in both saved bodies and the quotations they bind.
    return '\n'.join(line for line in (re.sub(r'[\t ]+', ' ', x).strip() for x in text.splitlines()) if line)


def _section(body, number, court=False):
    pattern = (rf'(?m)^제\s*{number}\s*호\s*$' if court else rf'(?m)^제{number}조\(')
    match = re.search(pattern, body)
    if not match:
        raise ValueError(f'operative section {number} missing')
    rest = body[match.start():]
    end = re.search(r'(?m)^제\s*\d+\s*호\s*$' if court else r'(?m)^제\d+조\(', rest[1:])
    return rest[:end.start() + 1].strip() if end else rest.strip()


def _source_from_corpus(source_id):
    detail = corpus.source_detail(source_id)
    if not detail or detail.get('status') != 'collected':
        raise ValueError(f'{source_id}: collected official body unavailable')
    if detail.get('applicability_status') not in {'baseline_unchanged', 'watch_scalar_validated'}:
        raise ValueError(f'{source_id}: source assessment pending')
    chunks = detail.get('chunks', [])
    body = _canonical_body('\n\n'.join(c['text'] for c in chunks))
    return {'id': source_id, 'title': detail['title'], 'url': detail['url'],
            'source_type': detail['source_type'], 'status': 'verified_snapshot',
            'retrieved_at': detail.get('last_success_at') or detail.get('fetched_at'),
            'source_sha256': detail['sha256'], 'body_sha256': _hash(body), 'body': body,
            'effective_date': detail.get('effective_date'),
            'watch_semantic_sha256': detail.get('watch_semantic_sha256'),
            'court_id': detail.get('court_id')}


def _case_sources():
    records = json.loads((ROOT / 'data/legal_research/manifest.json').read_text(encoding='utf-8'))['sources']
    result = []
    for record in records:
        if record['id'] not in {'AXP01', 'AXP02', 'AXP03', 'AXP04'}:
            continue
        body = (ROOT / record['text_path']).read_text(encoding='utf-8')
        if record.get('status') != 'downloaded' or _hash(body) != record['text_sha256']:
            raise ValueError('public decision snapshot hash mismatch')
        body = _canonical_body(body)
        result.append({'id': record['id'], 'title': record['title'], 'url': record['url'],
                       'source_type': 'precedent', 'status': 'verified_snapshot',
                       'retrieved_at': record['retrieved_at'], 'source_sha256': record['sha256'],
                       'body_sha256': _hash(body), 'body': body, 'court_id': None})
    return result


def _quote(body, number, court=False):
    section = _section(body, number, court)
    # Preserve exact source bytes, including PDF wrapping. A quotation can be
    # shorter than a section; the full section remains available in the source.
    if number == 405 and court:
        start = section.index('제3조')
        end = section.find('②', start)
        return section[start:end].strip()
    if number == 611:
        start = section.index('⑤')
        return section[start:section.find('⑥', start)].strip()
    if number == 614:
        return section[:section.index('②')].strip()
    if number == 579:
        end = section.find('3. “영업소득자”')
        return section[:end].strip()
    return section[:950].strip()


def compile_graph():
    sources = [_source_from_corpus(source_id) for source_id in dict.fromkeys(row[1] for row in RULES)] + _case_sources()
    by_id = {s['id']: s for s in sources}
    nodes, edges = [], []
    for node_id, source_id, number, title, issues, summary, evidence, actions, limits in RULES:
        source = by_id[source_id]
        court = source_id == 'C03'
        quote = _quote(source['body'], number, court)
        # Store only the selected public sections, not an entire 373-page book.
        nodes.append({'id': node_id, 'kind': 'court_rule' if court else 'statute',
                      'source_id': source_id, 'title': title, 'status': 'source_bound',
                      'bound_body_sha256': source['body_sha256'], 'excerpt': quote,
                      'locator': f'서울회생법원 실무준칙 제{number}호' if court else f'채무자회생법 제{number}조',
                      'effective_from': source.get('effective_date'), 'court_id': 'CT01' if court else None,
                      'watch_source_ids': [source_id], 'issue_tags': issues, 'summary': summary,
                      'required_evidence': evidence, 'actions': actions, 'limits': limits,
                      'applicable_stages': ['application', 'post_approval', 'discharge']})
    precedents = json.loads((ROOT / 'data/ax_legal_precedents.json').read_text(encoding='utf-8'))['precedents']
    for precedent in precedents:
        source = by_id[precedent['id']]
        body = source['body']
        # Exact operative judgment paragraph, never the case listing alone.
        operative = body.split('【이 유】', 1)[-1].strip()
        start = operative.find('\n1.')
        quote = operative[start + 1:] if start >= 0 else operative
        quote = quote[:750].strip()
        stage = {'AXP01': 'application', 'AXP02': 'application', 'AXP03': 'post_approval', 'AXP04': 'discharge'}[precedent['id']]
        nodes.append({'id': 'KG' + precedent['id'], 'kind': 'case', 'source_id': source['id'],
                      'title': f"{precedent['court']} {precedent['decided_at']} {precedent['case_number']}",
                      'status': 'source_bound', 'bound_body_sha256': source['body_sha256'],
                      'excerpt': quote, 'locator': precedent['case_number'] + ' 결정 이유',
                      'case_number': precedent['case_number'], 'decided_at': precedent['decided_at'],
                      'court_id': None, 'deciding_court': precedent['court'], 'decision_stage': stage,
                      'applicable_stages': [stage], 'outcome': precedent['outcome'],
                      'not_an_approval_sample': True, 'issue_tags': precedent['issue_tags'],
                      'summary': precedent['holding_summary'], 'required_evidence': precedent['required_evidence'],
                      'actions': precedent['strategy_candidates'], 'limits': precedent['excluded_inferences']})
    # Prune large source bodies to exactly those operative sections used in the
    # graph. A transport hash still identifies the complete official download.
    for source in sources:
        if source['id'] == 'C03':
            source['body'] = _canonical_body('\n\n'.join(_section(source['body'], n, True) for n in (405, 406, 408)))
            source['body_sha256'] = _hash(source['body'])
            for node in nodes:
                if node['source_id'] == 'C03':
                    node['bound_body_sha256'] = source['body_sha256']
    issue_ids, evidence_nodes = set(), {}
    for node in list(nodes):
        for issue in node['issue_tags']:
            issue_ids.add(issue)
            edges.append({'from': node['id'], 'relation': 'addresses', 'to': 'issue:' + issue})
        for evidence in node['required_evidence']:
            evidence_id = 'evidence:' + _hash(evidence)[:12]
            evidence_nodes[evidence_id] = {'id': evidence_id, 'kind': 'evidence', 'title': evidence}
            edges.append({'from': node['id'], 'relation': 'requires', 'to': evidence_id})
    for source, target in [('KGAXP01', 'KG595'), ('KGAXP01', 'KG614'), ('KGAXP02', 'KG595'), ('KGAXP02', 'KG589')]:
        edges.append({'from': source, 'relation': 'interprets', 'to': target})
    triggers = {'KGSEOUL406': ['spouse_property'],
                'KGSEOUL408': ['investment_loss'],
                'KGAXP01': ['preferential_repayment', 'asset_disposal'],
                'KGAXP02': ['correction_insufficient', 'spouse_property', 'procedural_dismissal']}
    for node in nodes:
        if node['id'] in triggers:
            node['required_trigger_tags'] = triggers[node['id']]
    nodes += [{'id': 'issue:' + issue, 'kind': 'issue', 'title': issue} for issue in sorted(issue_ids)] + list(evidence_nodes.values())
    return {'schema_version': 1, 'version': '2026-10-04.1', 'compiled_at': datetime.now(timezone.utc).isoformat(),
            'scope': 'personal_rehabilitation', 'sources': sources, 'nodes': nodes, 'edges': edges,
            'use_policy': '공개 법령·실무준칙·판례의 쟁점과 증빙 연결. 인가 확률 학습자료가 아님. 판례 당시 구법 수치를 현재 규칙에 이식하지 않음.'}


async def refresh(graph, accept_reviewed):
    changed = []
    async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False) as client:
        seeds = {s['id']: s for s in corpus.seeds()}
        downloads = {}
        for source in graph['sources']:
            if not _official(source['url']):
                raise ValueError('non-official source rejected')
            try:
                spec = seeds.get(source['id'], source)
                raw, content_type, final_url, pages = await _download_public_body(client, source, spec, downloads)
                body = _canonical_body('\n\n'.join(text for _, text in pages))
                if source['id'] == 'C03':
                    body = _canonical_body('\n\n'.join(_section(body, n, True) for n in (405, 406, 408)))
                digest = _hash(body)
                if digest != source['body_sha256']:
                    changed.append(source['id'])
                    bound_current = any(node.get('source_id') == source['id']
                                        and node.get('status') == 'source_bound'
                                        and node.get('bound_body_sha256') == source['body_sha256']
                                        for node in graph['nodes'])
                    if bound_current:
                        source['previous_verified_snapshot'] = {
                            key: source.get(key) for key in ('body', 'body_sha256', 'source_sha256',
                                                            'retrieved_at', 'effective_date', 'url', 'final_url')}
                    for node in graph['nodes']:
                        if node.get('source_id') != source['id']:
                            continue
                        node['status'] = 'source_changed_requires_review'
                # Approval may follow a previous refresh; rebinding must also
                # work when the changed body is now the saved snapshot.
                if source['id'] in accept_reviewed:
                    for node in graph['nodes']:
                        if node.get('source_id') == source['id'] and node['excerpt'] in body:
                            node.update(status='source_bound', bound_body_sha256=digest)
                source.update(body=body, body_sha256=digest, source_sha256=hashlib.sha256(raw).hexdigest(),
                              retrieved_at=datetime.now(timezone.utc).isoformat(), status='verified_snapshot', final_url=final_url,
                              refresh_status='downloaded')
                source.pop('last_error', None)
            except (httpx.HTTPError, OSError, ValueError) as exc:
                source['last_error'] = type(exc).__name__
                # Preserve the last good snapshot, but mark freshness separately.
                source['refresh_status'] = 'failed_previous_snapshot_preserved'
    graph['last_refresh_at'] = datetime.now(timezone.utc).isoformat()
    return changed


async def _download_public_body(client, source, spec, downloads):
    """Resolve only the official numeric law-edition link, never arbitrary JS.

    Three article links return an HTML frame with an edition ID and effectivity
    date. Share the existing watcher resolver, then extract the requested article
    from that official edition. Shared edition bytes are fetched once per run.
    """
    async def download(url):
        if not _official(url):
            raise ValueError('non-official edition URL rejected')
        if url not in downloads:
            downloads[url] = await corpus._download(client, url)
        return downloads[url]

    raw, content_type, final_url, _ = await download(source['url'])
    edition_url = _law_edition_url(raw, final_url)
    if edition_url:
        raw, content_type, final_url, _ = await download(edition_url)
    pages, _ = corpus._extract(raw, content_type, spec)
    return raw, content_type, final_url, pages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--accept-reviewed', action='append', default=[])
    parser.add_argument('--rebuild', action='store_true', help='Recompile from locally assessed official sources.')
    args = parser.parse_args()
    graph = compile_graph() if args.rebuild or not GRAPH_PATH.exists() else json.loads(GRAPH_PATH.read_text(encoding='utf-8'))
    changed = asyncio.run(refresh(graph, set(args.accept_reviewed))) if args.refresh else []
    temporary = GRAPH_PATH.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(GRAPH_PATH)
    print(json.dumps({'sources': len(graph['sources']), 'nodes': len(graph['nodes']), 'edges': len(graph['edges']),
                      'changed_sources': changed,
                      'refresh_downloaded': sum(s.get('refresh_status') == 'downloaded' for s in graph['sources']),
                      'refresh_failed': [s['id'] for s in graph['sources'] if s.get('last_error')]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
