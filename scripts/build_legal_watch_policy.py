"""Build reviewed baseline fingerprints from already collected official bytes.

This is a developer maintenance command, never a scheduled self-approval path.
Changing the baseline requires checking the diff and the identified raw source.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.api import corpus, legal_watch


def main():
    actual = {s['id']: s for s in corpus._snapshot()['sources']}
    seeds = {s['id']: s for s in corpus.seeds()}
    sources = [
        {'id': 'LW579', 'title': '현행 채무자회생법 제579조 채무한도·소득',
         'url': 'https://www.law.go.kr/LSW/lsLinkCommonInfo.do?chrClsCd=010202&lsJoLnkSeq=1023847839',
         'source_type': 'statute', 'article': 579, 'adapter': 'statute_579', 'baseline_source_id': 'L13'},
        {'id': 'LW611', 'title': '현행 채무자회생법 제611조 변제계획 기간',
         'url': 'https://law.go.kr/LSW/lsLinkCommonInfo.do?chrClsCd=010202&lsJoLnkSeq=1024710777',
         'source_type': 'statute', 'article': 611, 'adapter': 'statute_611', 'baseline_source_id': 'L13'},
    ]
    for article in (589, 595, 614):
        sources.append({'id': f'LW{article}', 'title': f'현행 채무자회생법 제{article}조',
                        'url': 'https://www.law.go.kr/LSW/LsiJoLinkP.do?docType=JO&joNo=' + f'{article:04d}00000' + '&languageType=KO&lsNm=%EC%B1%84%EB%AC%B4%EC%9E%90+%ED%9A%8C%EC%83%9D+%EB%B0%8F+%ED%8C%8C%EC%82%B0%EC%97%90+%EA%B4%80%ED%95%9C+%EB%B2%95%EB%A5%A0&paras=1',
                        'source_type': 'statute', 'article': article, 'baseline_source_id': 'L13'})
    for source_id in ['C01', 'C02', 'C03', 'AXC06', 'AXB03', 'L08']:
        sources.append({**seeds[source_id], 'baseline_source_id': source_id})
    for seed in sources:
        seed.setdefault('court_id', None)
        seed.setdefault('required_text', [f'제{seed["article"]}조('] if seed.get('article') else ['개인회생'])
        seed['affected_tasks'] = legal_watch.TASKS
        reference = actual[seed['baseline_source_id']]
        raw = (corpus.CORPUS_DIR / reference['raw_file']).read_bytes()
        pages, _ = corpus._extract(raw, reference.get('media_type', ''), seeds[seed['baseline_source_id']])
        body = legal_watch._legal_body(pages, seed)
        import hashlib
        seed['baseline_semantic_sha256'] = hashlib.sha256(legal_watch._normalized(body).encode()).hexdigest()
        seed['baseline_raw_sha256'] = hashlib.sha256(raw).hexdigest()
        seed['baseline_url'] = seeds[seed['baseline_source_id']]['url']
        seed['baseline_checked_at'] = '2026-10-03'
        seed['baseline_effective_date'] = legal_watch._edition_date(pages)
        if seed.get('adapter'):
            values, shape = legal_watch._controlled_values(body, seed['adapter'])
            seed['approved_shape_sha256'] = shape
            seed['baseline_values'] = values
    value = {'schema_version': 1, 'enabled': True, 'interval_hours': 24, 'max_sources': 20,
             'max_http_requests_per_day': 40, 'max_attempts_per_source': 2, 'timeout_seconds': 20,
             'note': '공식 공개 원문만 조건부 수집. 동적 현행 조문 주소 사용; 고정 법령판 URL은 현행성 감시로 사용하지 않음. 의미가 바뀐 법원 규칙·미지원 조문은 검토 보류. 검증된 수치 외에는 스크랩 결과를 실행 규칙으로 승격하지 않음.',
             'source_ids': [s['id'] for s in sources], 'sources': sources}
    legal_watch.POLICY_PATH.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
