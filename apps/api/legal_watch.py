"""Bounded, conditional public-law polling and versioned policy overlays.

No client facts or model calls are used. A changed court rule is a review hold,
never an invented JSON rule. Only four statutory scalars have an automatic
adapter, guarded by an approved fingerprint of every surrounding legal word.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import threading
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from . import corpus, store

POLICY_PATH = Path(__file__).resolve().parents[2] / 'data/legal_watch_policy.json'
_run_lock = threading.Lock()
_file_lock = threading.RLock()
SEOUL = timezone(timedelta(hours=9))
TASKS = ['document_selection', 'legal_calculation', 'legal_strategy', 'document_drafting']
SCALARS = {'unsecured_debt_limit', 'secured_debt_limit', 'normal_max_months', 'exception_max_months'}


def _now(as_of=None):
    if as_of is None:
        return datetime.now(timezone.utc)
    if isinstance(as_of, str):
        as_of = datetime.fromisoformat(as_of.replace('Z', '+00:00'))
    if isinstance(as_of, date) and not isinstance(as_of, datetime):
        as_of = datetime.combine(as_of, datetime.min.time(), SEOUL)
    return as_of.replace(tzinfo=timezone.utc) if as_of.tzinfo is None else as_of.astimezone(timezone.utc)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _directory():
    return store.DATA_DIR / 'legal_watch'


def _read(path, default):
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding='utf-8'))


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def _state():
    return _read(_directory() / 'state.json', {'schema_version': 1, 'sources': {}, 'runs': [], 'budget': {}})


def policy():
    value = _read(POLICY_PATH, {})
    overrides = _read(_directory() / 'policy.json', {})
    value.update({k: v for k, v in overrides.items()
                  if k in {'enabled', 'interval_hours', 'max_sources', 'source_ids'}})
    return value


def configure(changes):
    """Persist only a small settings whitelist; URLs/adapters are code reviewed."""
    allowed = {'enabled', 'interval_hours', 'max_sources', 'source_ids'}
    if not isinstance(changes, dict) or set(changes) - allowed:
        raise ValueError('변경할 수 없는 수집 설정입니다.')
    base = policy()
    candidate = {**base, **changes}
    if type(candidate.get('enabled')) is not bool:
        raise ValueError('enabled는 참 또는 거짓이어야 합니다.')
    for key, low, high in [('interval_hours', 24, 720), ('max_sources', 1, 20)]:
        if type(candidate.get(key)) is not int or not low <= candidate[key] <= high:
            raise ValueError(f'{key}는 {low}~{high} 범위의 정수여야 합니다.')
    ids = candidate.get('source_ids')
    catalog = {s['id'] for s in base['sources']}
    if not isinstance(ids, list) or not ids or any(not isinstance(s, str) for s in ids) or set(ids) - catalog:
        raise ValueError('등록된 공식 출처를 한 개 이상 선택해야 합니다.')
    candidate['source_ids'] = list(dict.fromkeys(ids))
    with _file_lock:
        _write(_directory() / 'policy.json', {k: candidate[k] for k in allowed})
    return status()


def _normalized(text):
    # Editorial amendment dates are not substantive changes. Actual operative
    # dates remain in version metadata and are not lost by this normalization.
    text = re.sub(r'<(?:개정|신설|전문개정)[^>]*>', '', text)
    return re.sub(r'\s+', '', unicodedata.normalize('NFC', text))


def _legal_body(pages, seed):
    text = '\n'.join(t for _, t in pages)
    article = seed.get('article')
    if article:
        found = re.search(rf'제\s*{article}\s*조\s*\(', text)
        if not found:
            raise ValueError('지정 조문을 원문에서 확인하지 못했습니다.')
        text = text[found.start():]
        text = re.split(r'\n제\s*\d+조(?:의\d+)?\s*\(|파일형식\s*선택|파일형식|조문체계도', text, maxsplit=1)[0]
    elif seed.get('source_type') == 'court_rule':
        # Ignore table of contents and web banners before the actual rules.
        found = re.search(r'(?m)^\s*제\s*\d{3}\s*호\s*$', text)
        if found:
            text = text[found.start():]
        text = re.sub(r'(?m)^\s*[-–]?\s*\d+\s*[-–]?\s*$', '', text)
    if len(_normalized(text)) < 100:
        raise ValueError('검증할 법률 본문이 부족합니다.')
    return text.strip()


def _controlled_values(body, adapter):
    """Return parsed scalars AND the full body with only those tokens replaced."""
    text = _normalized(body)
    if adapter == 'statute_579':
        secured = re.search(r'담보된개인회생채권은(\d+)억원', text)
        unsecured = re.search(r'나\.가목외의개인회생채권은(\d+)억원', text)
        if not secured or not unsecured:
            raise ValueError('제579조 채무한도 문언이 기존 검증 형식과 다릅니다.')
        values = {'secured_debt_limit': int(secured[1]) * 100_000_000,
                  'unsecured_debt_limit': int(unsecured[1]) * 100_000_000}
        if not all(0 < n <= 100_000_000_000 for n in values.values()):
            raise ValueError('채무한도 검증 범위를 벗어났습니다.')
        spans = [(secured.start(1), secured.end(1), '{secured}'),
                 (unsecured.start(1), unsecured.end(1), '{unsecured}')]
    elif adapter == 'statute_611':
        normal = re.search(r'⑤변제계획에서정하는변제기간은변제개시일부터(\d+)년을초과하여서는아니된다\.', text)
        special = re.search(r'특별한사정이있는때에는변제개시일부터(\d+)년을초과하지아니하는범위', text)
        if not normal or not special:
            raise ValueError('제611조 변제기간 문언이 기존 검증 형식과 다릅니다.')
        values = {'normal_max_months': int(normal[1]) * 12, 'exception_max_months': int(special[1]) * 12}
        if not 0 < values['normal_max_months'] <= values['exception_max_months'] <= 120:
            raise ValueError('변제기간 검증 범위를 벗어났습니다.')
        spans = [(normal.start(1), normal.end(1), '{normal}'),
                 (special.start(1), special.end(1), '{exception}')]
    else:
        return {}, None
    for start, end, token in sorted(spans, reverse=True):
        text = text[:start] + token + text[end:]
    return values, hashlib.sha256(text.encode()).hexdigest()


def _edition_date(pages):
    text = '\n'.join(t for _, t in pages[:2])
    match = re.search(r'\[\s*시행\s*(\d{4})\s*[.년-]\s*(\d{1,2})\s*[.월-]\s*(\d{1,2})', text)
    if not match:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3])).isoformat()
    except ValueError:
        return None


def _seed(item):
    base = next((s for s in corpus.seeds() if s['id'] == item['id']), {})
    result = {**base, **item}
    if not corpus._allowed(result.get('url', '')):
        raise ValueError('공식 HTTPS 출처만 수집할 수 있습니다.')
    return result


def _budget(state, config, now):
    day = now.astimezone(SEOUL).date().isoformat()
    budget = state.setdefault('budget', {})
    if budget.get('date') != day:
        budget.clear()
        budget.update(date=day, checked_ids=[], http_requests=0)
    budget['sources_checked'] = len(budget['checked_ids'])
    budget['max_sources_per_day'] = config['max_sources']
    budget['max_http_requests_per_day'] = min(40, int(config.get('max_http_requests_per_day', 40)))
    return budget


def _law_edition_url(raw, url):
    """Resolve ONLY the official law link shell's numeric edition, not script."""
    parsed = urlparse(url)
    if parsed.hostname not in {'law.go.kr', 'www.law.go.kr'} or not parsed.path.endswith('/LsiJoLinkP.do'):
        return None
    text = raw.decode('utf-8', errors='replace')
    if '채무자 회생 및 파산에 관한 법률' not in text:
        return None
    # This attribute combination was checked against the official October2026
    # response. Never execute JavaScript or follow arbitrary embedded URLs.
    tag = re.search(r'<input\b[^>]*\bid=[\"\']lsiSeq[\"\'][^>]*>', text, re.I)
    value = re.search(r'\bvalue=[\"\'](\d{1,12})[\"\']', tag[0]) if tag else None
    effective = re.search(r'\bvar\s+efYd\s*=\s*[\"\'](\d{8})[\"\']', text)
    if value and effective:
        # The official body endpoint needs both edition AND effectivity; missing
        # efYd returns a menu shell despite HTTP200.
        date.fromisoformat(effective[1])
        return f'https://www.law.go.kr/LSW/lsInfoR.do?lsiSeq={value[1]}&efYd={effective[1]}&chrClsCd=010202'
    return None


async def _fetch(client, seed, previous, state, config, now, fetched=None):
    """Count every attempt/redirect before sending it, including across restarts."""
    budget = _budget(state, config, now)
    fetched = fetched if fetched is not None else {}
    last_error = None
    for attempt in range(min(2, int(config.get('max_attempts_per_source', 2)))):
        url = seed['url']
        for redirect in range(5):
            if not corpus._allowed(url):
                raise ValueError('공식 출처 외부로 이동하는 요청을 차단했습니다.')
            if url in fetched:
                return copy.deepcopy(fetched[url])
            if budget['http_requests'] >= budget['max_http_requests_per_day']:
                raise ValueError('하루 HTTP 요청 한도에 도달했습니다.')
            budget['http_requests'] += 1
            headers = {}
            if previous.get('final_url', seed['url']) == url:
                if previous.get('etag'):
                    headers['If-None-Match'] = previous['etag']
                if previous.get('last_modified'):
                    headers['If-Modified-Since'] = previous['last_modified']
            with _file_lock:
                _write(_directory() / 'state.json', state)
            try:
                async with client.stream('GET', url, headers=headers) as response:
                    if response.status_code == 304:
                        if not previous.get('semantic_sha256'):
                            raise ValueError('비교할 저장 원문 없이 304 응답을 받았습니다.')
                        return {'unchanged': True}
                    if response.is_redirect:
                        url = urljoin(str(response.url), response.headers['location'])
                        continue
                    response.raise_for_status()
                    if int(response.headers.get('content-length', '0')) > corpus.MAX_SOURCE_BYTES:
                        raise ValueError('원문 크기 제한을 초과했습니다.')
                    raw = bytearray()
                    async for block in response.aiter_bytes():
                        raw.extend(block)
                        if len(raw) > corpus.MAX_SOURCE_BYTES:
                            raise ValueError('원문 크기 제한을 초과했습니다.')
                    edition_url = _law_edition_url(bytes(raw), str(response.url))
                    if edition_url:
                        url = edition_url
                        continue
                    result = {'raw': bytes(raw), 'content_type': response.headers.get('content-type', ''),
                            'url': str(response.url), 'etag': response.headers.get('etag'),
                            'last_modified': response.headers.get('last-modified')}
                    fetched[url] = result
                    return result
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500 and exc.response.status_code != 429:
                    raise
                last_error = exc
                break
            except httpx.TransportError as exc:
                last_error = exc
                break
        else:
            raise ValueError('허용된 리다이렉트 횟수를 초과했습니다.')
    raise last_error or ValueError('공식 원문을 수집하지 못했습니다.')


def _applicable(source, case):
    return source.get('court_id') in (None, '', case.get('court_id'))


def _overlay(state, now):
    calculator, versions, pending, staged, refs = {}, {}, [], [], []
    for source_id, record in sorted(state.get('sources', {}).items()):
        active = record.get('active')
        cutoff = now.astimezone(SEOUL).date().isoformat()
        if active and active.get('effective_date') and active['effective_date'] > cutoff:
            candidates = [v for v in record.get('active_history', [])
                          if not v.get('effective_date') or v['effective_date'] <= cutoff]
            active = max(candidates, key=lambda v: v.get('effective_date') or '', default=None)
        if active:
            versions[source_id] = {'semantic_sha256': active['semantic_sha256'],
                                   'court_id': record.get('court_id'), 'effective_date': active.get('effective_date')}
            calculator.update({k: v for k, v in active.get('calculator', {}).items() if k in SCALARS})
            if active.get('calculator'):
                refs.append({'id': source_id, 'title': record['title'], 'url': record['url'],
                             'effective_date': active.get('effective_date'), 'sha256': active['semantic_sha256']})
        if record.get('pending_change') and (not record['pending_change'].get('effective_date') or record['pending_change']['effective_date'] <= cutoff):
            pending.append(record['pending_change'])
        if record.get('staged_change'):
            staged.append({k: v for k, v in record['staged_change'].items() if k not in {'calculator'}})
    value = {'calculator': calculator, 'source_versions': versions, 'review_required': pending,
             'staged': staged, 'sources': refs, 'court_rules': {}}
    value['effective_date'] = max((s['effective_date'] for s in refs if s.get('effective_date')), default=None)
    value['version'] = _hash(value)
    value['updated_at'] = now.isoformat()
    return value


def active_overlays(as_of=None):
    # Build from the single atomic state file so the companion export can never
    # produce a mixed old/new policy after a process crash.
    return _overlay(_state(), _now(as_of))


def _activate(record, active):
    old = record.get('active')
    if old and old['semantic_sha256'] != active['semantic_sha256']:
        history = record.setdefault('active_history', [])
        if not any(v['semantic_sha256'] == old['semantic_sha256'] for v in history):
            history.append(old)
        record['active_history'] = history[-100:]
    record['active'] = active


def _change(seed, state, effective_date, reason, semantic_hash):
    return {'source_id': seed['id'], 'title': seed['title'], 'court_id': seed.get('court_id'),
            'url': seed['url'], 'state': state, 'effective_date': effective_date,
            'reason': reason, 'message': reason, 'semantic_sha256': semantic_hash,
            'affected_source_ids': [seed['id']], 'affected_tasks': seed.get('affected_tasks', TASKS),
            'applicability_status': state}


def _assess(seed, record, body, pages, now):
    semantic = hashlib.sha256(_normalized(body).encode()).hexdigest()
    effective = _edition_date(pages)
    baseline = seed.get('baseline_semantic_sha256')
    old = record.get('semantic_sha256', baseline)
    values, shape, parse_error = {}, None, None
    try:
        values, shape = _controlled_values(body, seed.get('adapter'))
    except ValueError as exc:
        parse_error = str(exc)
    known_shape = shape and shape == seed.get('approved_shape_sha256')
    unchanged = semantic == old
    baseline_match = semantic == baseline
    # A header date is adequate only for the reviewed exact scalar adapter;
    # arbitrary court transitional clauses never become automatic effective dates.
    future = effective and effective > now.astimezone(SEOUL).date().isoformat()
    supported = bool(known_shape and effective and not parse_error)
    if baseline_match:
        supported = True
        effective = effective or seed.get('baseline_effective_date')
    previous_effective = (record.get('active') or {}).get('effective_date')
    if previous_effective and effective and effective < previous_effective and not unchanged:
        supported = False
        parse_error = '수집한 시행본이 적용 중인 버전보다 과거입니다. 버전 회귀 여부 확인이 필요합니다.'
    state = ('unchanged' if unchanged and record.get('active') else 'baseline' if baseline_match else 'active') if supported else 'review_required'
    reason = ((parse_error or '법률 본문이 변경되어 적용 규칙 검토가 필요합니다.') if not supported else
              '확인한 공식 본문과 동일합니다.' if unchanged or baseline_match else
              '공식 조문에서 검증 가능한 수치 변경을 적용했습니다.')
    change = _change(seed, state, effective, reason, semantic)
    change['calculator'] = values if supported else {}
    if future:
        change.update(state='staged', applicability_status='staged', message='장래 시행본을 저장했습니다. 시행일까지 기존 규칙을 유지합니다.', reason='시행일 도래 전 대기', supported=supported)
        record['staged_change'] = change
        # A future edition must not clear an unresolved CURRENT edition.
    elif supported:
        _activate(record, {'semantic_sha256': semantic, 'effective_date': effective,
                            'calculator': values, 'raw_file': record['raw_file'],
                            'content_type': record['content_type'], 'final_url': record['final_url']})
        record.pop('pending_change', None)
        record.pop('staged_change', None)
    elif not unchanged or not record.get('pending_change'):
        record['pending_change'] = {k: v for k, v in change.items() if k != 'calculator'}
        record.pop('staged_change', None)
    elif record.get('pending_change'):
        change = {**record['pending_change'], 'calculator': {}}
    record.update(semantic_sha256=semantic, effective_date=effective,
                  state=change['state'], message=change['message'], shape_sha256=shape)
    return change


async def _publish(seed, record, active):
    """Reuse the existing downloader-independent ingester, with atomic merge."""
    existing = next((s for s in corpus._snapshot()['sources'] if s['id'] == seed['id']), None)
    if (existing and existing.get('status') == 'collected'
            and existing.get('sha256') == active.get('raw_sha256', record.get('raw_sha256'))
            and existing.get('watch_semantic_sha256') == active['semantic_sha256']
            and existing.get('text_file')
            and (corpus.CORPUS_DIR / existing['text_file']).is_file()):
        return True
    if not corpus._ingest_lock.acquire(blocking=False):
        return False
    try:
        path = (_directory() / active['raw_file']).resolve()
        if not path.is_relative_to(_directory().resolve()) or not path.is_file():
            raise ValueError('저장 원문 경로가 유효하지 않습니다.')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != active.get('raw_sha256', record.get('raw_sha256')):
            raise ValueError('저장 원문 해시가 일치하지 않습니다.')
        source, chunks = await corpus._ingest_source(None, seed, asyncio.Semaphore(1), saved={
            'raw': raw, 'content_type': active['content_type'], 'url': active['final_url'],
            'fetched_at': record.get('checked_at', _now().isoformat())})
        if source['status'] != 'collected':
            raise ValueError(source.get('error') or '검색 원문 갱신 실패')
        source.update(watch_semantic_sha256=active['semantic_sha256'],
                      applicability_status='watch_scalar_validated' if active.get('calculator') else 'baseline_unchanged',
                      effective_date=active.get('effective_date'))
        before = corpus._snapshot()
        snapshot = {**before, 'schema_version': 1, 'ingested_at': _now().isoformat(),
                    'sources': [s for s in before['sources'] if s['id'] != seed['id']] + [source],
                    'chunks': [c for c in before['chunks'] if c['source_id'] != seed['id']] + chunks}
        _write(corpus.CORPUS_DIR / 'snapshot.json', snapshot)
        return True
    finally:
        corpus._ingest_lock.release()


async def run_once(force=False, as_of=None):
    """Run at most one bounded batch. Force bypasses interval, NEVER daily caps."""
    if not _run_lock.acquire(blocking=False):
        return {**status(as_of), 'status': 'already_running'}
    try:
        config, state, now = policy(), _state(), _now(as_of)
        if not config['enabled']:
            return {**status(as_of), 'status': 'disabled'}
        last = state.get('last_checked_at')
        if last and not force and now < _now(last) + timedelta(hours=config['interval_hours']):
            return {**status(as_of), 'status': 'not_due'}
        budget = _budget(state, config, now)
        changes, checked = [], []
        today = now.astimezone(SEOUL).date().isoformat()
        catalog = {item['id']: _seed(item) for item in config['sources']}
        # Staged versions become active on the first scheduled tick after their
        # date, independently of a temporary HTTP failure or disabled source.
        for source_id, record in state['sources'].items():
            staged = record.get('staged_change')
            if staged and staged.get('effective_date') and staged['effective_date'] <= today:
                change = {**staged, 'state': 'active' if staged.get('supported') else 'review_required'}
                change['applicability_status'] = change['state']
                if staged.get('supported'):
                    _activate(record, {k: staged[k] for k in ('semantic_sha256', 'effective_date', 'calculator', 'raw_file', 'raw_sha256', 'content_type', 'final_url')})
                    record.pop('pending_change', None)
                else:
                    record['pending_change'] = {k: v for k, v in change.items() if k not in {'calculator', 'raw_file', 'raw_sha256', 'content_type', 'final_url', 'supported'}}
                record.pop('staged_change', None)
                record['state'] = change['state']
                changes.append(change)
        selected = sorted((catalog[s] for s in config['source_ids']),
                          key=lambda s: state['sources'].get(s['id'], {}).get('checked_at', ''))
        timeout = min(30, int(config.get('timeout_seconds', 20)))
        fetched = {}
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False,
                                     headers={'User-Agent': 'Debtoff-Public-Law-Watch/1.0'}) as client:
            for seed in selected:
                if seed['id'] in budget['checked_ids']:
                    continue
                if len(budget['checked_ids']) >= config['max_sources'] or budget['http_requests'] >= budget['max_http_requests_per_day']:
                    break
                record = state['sources'].setdefault(seed['id'], {k: seed.get(k) for k in ('title', 'court_id', 'url')})
                budget['checked_ids'].append(seed['id'])
                budget['sources_checked'] = len(budget['checked_ids'])
                checked.append(seed['id'])
                previous = copy.deepcopy(record)
                record['checked_at'] = now.isoformat()
                try:
                    result = await _fetch(client, seed, previous, state, config, now, fetched)
                    if result.get('unchanged'):
                        record['state'] = 'review_required' if record.get('pending_change') else 'staged' if record.get('staged_change') else 'unchanged'
                        record['message'] = '공식 서버에서 원문 변경 없음(304)을 확인했습니다.'
                        record.pop('error', None)
                        continue
                    pages, _ = await asyncio.to_thread(corpus._extract, result['raw'], result['content_type'], seed)
                    body = _legal_body(pages, seed)
                    raw_hash = hashlib.sha256(result['raw']).hexdigest()
                    relative = f'raw/{seed["id"]}-{raw_hash}.bin'
                    raw_path = _directory() / relative
                    raw_path.parent.mkdir(parents=True, exist_ok=True)
                    if not raw_path.exists():
                        raw_path.write_bytes(result['raw'])
                    record.update(raw_file=relative, raw_sha256=raw_hash, content_type=result['content_type'],
                                  final_url=result['url'], etag=result.get('etag'), last_modified=result.get('last_modified'))
                    change = _assess(seed, record, body, pages, now)
                    if record.get('active') and record['active']['semantic_sha256'] == record['semantic_sha256']:
                        record['active']['raw_sha256'] = raw_hash
                    if record.get('staged_change'):
                        record['staged_change'].update(raw_file=relative, raw_sha256=raw_hash,
                                                       content_type=result['content_type'], final_url=result['url'])
                    record.pop('error', None)
                    if change['state'] not in ('unchanged', 'baseline'):
                        changes.append({k: v for k, v in change.items() if k != 'calculator'})
                    snapshot = {'source_id': seed['id'], 'url': seed['url'], 'final_url': result['url'],
                                'checked_at': now.isoformat(), 'raw_sha256': raw_hash,
                                'semantic_sha256': record['semantic_sha256'], 'effective_date': record.get('effective_date'),
                                'state': record['state'], 'reason': record['message']}
                    _write(raw_path.with_suffix('.json'), snapshot)
                except (ValueError, OSError, httpx.HTTPError) as exc:
                    # The old active version is intentionally preserved.
                    record.update(state='failed', error=str(exc)[:300], message='수집 실패: 마지막 정상 원문을 유지합니다.')
        # Retry corpus publication even if a previous index job owned the lock.
        for source_id, record in state['sources'].items():
            active = record.get('active')
            if active and record.get('indexed_semantic_sha256') != active['semantic_sha256']:
                try:
                    if await _publish(catalog[source_id], record, active):
                        record['indexed_semantic_sha256'] = active['semantic_sha256']
                        record['indexed_raw_sha256'] = active['raw_sha256']
                        record.pop('index_error', None)
                except (ValueError, OSError) as exc:
                    record['index_error'] = str(exc)[:300]
        run = {'checked_at': now.isoformat(), 'checked_source_ids': checked, 'changes': changes,
               'failed_source_ids': [s for s in checked if state['sources'][s].get('error')]}
        state.update(last_checked_at=now.isoformat(), changes=changes, runs=(state['runs'] + [run])[-100:])
        with _file_lock:
            _write(_directory() / 'state.json', state)
            _write(_directory() / 'active_overlays.json', _overlay(state, now))
        return status(as_of)
    finally:
        _run_lock.release()


async def _assess_saved(seed, candidate, base_dir, state, now):
    """Recheck original bytes; imported state flags never authorize a rule."""
    if candidate.get('url') != seed['url'] or not corpus._allowed(candidate.get('final_url', seed['url'])):
        raise ValueError('저장 출처 URL이 등록된 공식 출처와 일치하지 않습니다.')
    raw_path = (base_dir / candidate['raw_file']).resolve()
    if not raw_path.is_relative_to(base_dir.resolve()) or not raw_path.is_file():
        raise ValueError('저장 출처 파일이 허용 디렉터리 밖에 있습니다.')
    if raw_path.stat().st_size > corpus.MAX_SOURCE_BYTES:
        raise ValueError('저장 원문 크기 제한을 초과했습니다.')
    raw = raw_path.read_bytes()
    raw_hash = hashlib.sha256(raw).hexdigest()
    if raw_hash != candidate.get('raw_sha256', candidate.get('sha256')):
        raise ValueError('저장 원문 해시가 일치하지 않습니다.')
    content_type = candidate.get('content_type') or candidate.get('media_type') or 'text/html'
    pages, _ = await asyncio.to_thread(corpus._extract, raw, content_type, seed)
    body = _legal_body(pages, seed)
    relative = f'raw/{seed["id"]}-{raw_hash}.bin'
    destination = _directory() / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        destination.write_bytes(raw)
    record = state['sources'].setdefault(seed['id'], {k: seed.get(k) for k in ('title', 'court_id', 'url')})
    record.update(raw_file=relative, raw_sha256=raw_hash, content_type=content_type,
                  final_url=candidate.get('final_url', seed['url']),
                  checked_at=candidate.get('checked_at') or candidate.get('fetched_at'),
                  assessed_at=now.isoformat(), etag=candidate.get('etag'),
                  last_modified=candidate.get('last_modified'))
    change = _assess(seed, record, body, pages, now)
    record.pop('error', None)
    if record.get('staged_change'):
        record['staged_change'].update(raw_file=relative, raw_sha256=raw_hash,
                                       content_type=content_type, final_url=record['final_url'])
    active = record.get('active')
    if active and active['semantic_sha256'] == record['semantic_sha256']:
        active['raw_sha256'] = raw_hash
    if active:
        # A manual ingester may already have published a future/unknown edition.
        # Restore the last applicable version while retaining new raw evidence.
        if await _publish(seed, record, active):
            record['indexed_semantic_sha256'] = active['semantic_sha256']
            record['indexed_raw_sha256'] = active['raw_sha256']
            record.pop('index_error', None)
    elif record.get('staged_change') or record.get('pending_change'):
        if corpus._ingest_lock.acquire(blocking=False):
            try:
                snapshot = copy.deepcopy(corpus._snapshot())
                for item in snapshot['sources']:
                    if item['id'] == seed['id']:
                        item['applicability_status'] = 'staged' if record.get('staged_change') else 'review_required'
                for item in snapshot['chunks']:
                    if item['source_id'] == seed['id']:
                        item['applicability_status'] = 'staged' if record.get('staged_change') else 'review_required'
                _write(corpus.CORPUS_DIR / 'snapshot.json', snapshot)
            finally:
                corpus._ingest_lock.release()
    return {k: v for k, v in change.items() if k != 'calculator'}


def _commit_assessment(state, changes, now, run):
    state['changes'] = changes
    state['runs'] = (state.get('runs', []) + [run])[-100:]
    with _file_lock:
        _write(_directory() / 'state.json', state)
        _write(_directory() / 'active_overlays.json', _overlay(state, now))


async def assess_collected_sources(source_ids=None, as_of=None):
    """Assess manual corpus ingestion too, so it cannot bypass change holds.

This is offline; it consumes no HTTP allowance and keeps acquisition timestamps.
Only registry URLs and hash-checked corpus raw files are accepted.
"""
    acquired = False
    for attempt in range(121):
        if _run_lock.acquire(blocking=False):
            acquired = True
            break
        if attempt < 120:
            await asyncio.sleep(0.25)
    if not acquired:
        return {**status(as_of), 'status': 'already_running'}
    try:
        config, state, now = policy(), _state(), _now(as_of)
        catalog = {item['id']: _seed(item) for item in config['sources']}
        actual = {s['id']: s for s in corpus._snapshot()['sources']}
        changes, assessed, errors = [], [], []
        for source_id in source_ids or list(catalog):
            if source_id not in catalog:
                continue
            candidate = actual.get(source_id)
            if not candidate or candidate.get('status') != 'collected' or not candidate.get('raw_file'):
                continue
            try:
                change = await _assess_saved(catalog[source_id], candidate, corpus.CORPUS_DIR, state, now)
                assessed.append(source_id)
                if change['state'] not in ('baseline', 'unchanged'):
                    changes.append(change)
            except (OSError, ValueError, KeyError) as exc:
                errors.append({'source_id': source_id, 'error': str(exc)[:300]})
                # A registered source failed integrity/extraction checks after a
                # manual update; the ordinary generation gate must see it.
                record = state['sources'].setdefault(source_id, {k: catalog[source_id].get(k) for k in ('title', 'court_id', 'url')})
                record['pending_change'] = _change(catalog[source_id], 'review_required', None,
                                                   '수집 원문 무결성 또는 본문 검증 실패: ' + str(exc)[:200], candidate.get('sha256'))
        _commit_assessment(state, changes, now, {'checked_at': now.isoformat(), 'kind': 'corpus_assessment',
                                                'assessed_source_ids': assessed, 'errors': errors,
                                                'http_requests': 0, 'changes': changes})
        return {**status(as_of), 'assessed_source_ids': assessed, 'assessment_errors': errors}
    finally:
        _run_lock.release()


async def import_verified_checkpoint(checkpoint_dir, as_of=None):
    """Import a local diagnostic acquisition without repeating HTTP or erasing audit.

Internal maintenance function, not a caller-upload endpoint. Every byte is
revalidated against the current allowlist and legal adapter before activation.
Imported today's HTTP attempts count toward the SAME production daily ceiling.
"""
    if not _run_lock.acquire(blocking=False):
        return {**status(as_of), 'status': 'already_running'}
    try:
        origin = Path(checkpoint_dir).resolve()
        if not (origin / 'state.json').exists():
            origin = origin / 'legal_watch'
        if origin == _directory().resolve():
            raise ValueError('현재 수집 상태를 다시 가져올 수 없습니다.')
        checkpoint = _read(origin / 'state.json', None)
        if not checkpoint or not isinstance(checkpoint.get('sources'), dict):
            raise ValueError('검증할 수집 체크포인트가 없습니다.')
        config, state, now = policy(), _state(), _now(as_of)
        import_id = _hash({'directory': str(origin), 'checkpoint': checkpoint})
        if import_id in state.get('imported_checkpoints', []):
            return {**status(as_of), 'status': 'already_imported'}
        budget = _budget(state, config, now)
        incoming = checkpoint.get('budget', {})
        added_http = int(incoming.get('http_requests', 0)) if incoming.get('date') == budget['date'] else 0
        if added_http < 0 or budget['http_requests'] + added_http > budget['max_http_requests_per_day']:
            raise ValueError('체크포인트의 요청을 합산하면 오늘 수집 한도를 초과합니다.')
        incoming_ids = incoming.get('checked_ids', []) if incoming.get('date') == budget['date'] else []
        ids = list(dict.fromkeys(budget['checked_ids'] + incoming_ids))
        if len(ids) > config['max_sources']:
            raise ValueError('체크포인트의 출처를 합산하면 오늘 출처 한도를 초과합니다.')
        catalog = {item['id']: _seed(item) for item in config['sources']}
        changes, imported, errors = [], [], []
        for source_id, candidate in checkpoint['sources'].items():
            if source_id not in catalog or candidate.get('state') == 'failed' or not candidate.get('raw_file'):
                continue
            try:
                change = await _assess_saved(catalog[source_id], candidate, origin, state, now)
                imported.append(source_id)
                if change['state'] not in ('baseline', 'unchanged'):
                    changes.append(change)
            except (OSError, ValueError, KeyError) as exc:
                errors.append({'source_id': source_id, 'error': str(exc)[:300]})
        budget.update(http_requests=budget['http_requests'] + added_http,
                      checked_ids=ids, sources_checked=len(ids))
        state['imported_checkpoints'] = (state.get('imported_checkpoints', []) + [import_id])[-100:]
        _commit_assessment(state, changes, now, {'checked_at': now.isoformat(), 'kind': 'verified_checkpoint_import',
                                                'checkpoint_id': import_id, 'imported_source_ids': imported,
                                                'imported_http_requests': added_http, 'errors': errors, 'changes': changes})
        return {**status(as_of), 'imported_source_ids': imported, 'import_errors': errors}
    finally:
        _run_lock.release()


def case_policy_status(case, as_of=None):
    config, state, now = policy(), _state(), _now(as_of)
    overlay = _overlay(state, now)
    pending = [v for v in overlay['review_required'] if _applicable(v, case)]
    actual = {s['id']: s for s in corpus._snapshot()['sources']}
    for item in config['sources']:
        collected = actual.get(item['id'])
        if not collected or collected.get('status') != 'collected' or not _applicable(item, case):
            continue
        recorded = state['sources'].get(item['id'], {})
        active = recorded.get('active') or {}
        expected_raw = {active.get('raw_sha256') or item.get('baseline_raw_sha256')}
        if recorded.get('indexed_semantic_sha256') == active.get('semantic_sha256'):
            expected_raw.add(recorded.get('indexed_raw_sha256'))
        if collected.get('sha256') not in expected_raw:
            hold = _change(item, 'review_required', None,
                           '새로 수집한 원문의 적용 규칙 검증이 완료되지 않았습니다.', collected.get('sha256'))
            hold['code'] = 'SOURCE_ASSESSMENT_REQUIRED'
            pending.append(hold)
    filing_date = case.get('application_date') or case.get('filing_date')
    for item in config['sources']:
        active = state['sources'].get(item['id'], {}).get('active', {})
        values = active.get('calculator')
        if (filing_date and active.get('effective_date') and str(filing_date)[:10] < active['effective_date']
                and values and values != item.get('baseline_values') and _applicable(item, case)):
            pending.append(_change(item, 'review_required', active['effective_date'],
                                   '접수일 이후 개정된 수치입니다. 부칙·경과규정에 따른 사건 적용 여부 확인이 필요합니다.', active['semantic_sha256']))
    applicable_versions = {s: v for s, v in overlay['source_versions'].items() if _applicable(v, case)}
    # Changes to applicable collected legal content also invalidate downstream
    # work. Timestamp/error changes do not pretend the legal text changed.
    relevant_ids = {s['id'] for s in config['sources'] if _applicable(s, case)}
    cached = [(s['id'], s.get('watch_semantic_sha256') or s.get('extraction_sha256') or s.get('sha256'))
              for s in corpus._snapshot()['sources'] if s['id'] in relevant_ids and s.get('status') in ('collected', 'stale')]
    signature = _hash({'versions': applicable_versions, 'pending': pending, 'corpus': sorted(cached)})
    return {'signature': signature, 'active_version': _hash(applicable_versions),
            'pending_changes': pending, 'as_of': now.astimezone(SEOUL).date().isoformat()}


def dependencies_signature(case):
    return case_policy_status(case)['signature']


def status(as_of=None):
    config, state, now = policy(), _state(), _now(as_of)
    overlay = _overlay(state, now)
    sources = []
    for item in config['sources']:
        seed = _seed(item)
        record = state['sources'].get(seed['id'], {})
        sources.append({'id': seed['id'], 'source_id': seed['id'], 'title': seed['title'],
                        'url': seed['url'], 'court_id': seed.get('court_id'),
                        'enabled': seed['id'] in config['source_ids'], 'state': record.get('state', 'pending'),
                        'checked_at': record.get('checked_at'), 'effective_date': record.get('effective_date'),
                        'message': record.get('message', '첫 정기 확인 대기'), 'error': record.get('error'),
                        'index_error': record.get('index_error'), 'affected_tasks': seed.get('affected_tasks', TASKS)})
    last = state.get('last_checked_at')
    next_check = (_now(last) + timedelta(hours=config['interval_hours'])).isoformat() if last else now.isoformat()
    return {'enabled': config['enabled'], 'interval_hours': config['interval_hours'],
            'max_sources': config['max_sources'], 'source_ids': config['source_ids'],
            'last_checked_at': last, 'next_check_at': next_check, 'next_due': next_check,
            'status': 'disabled' if not config['enabled'] else 'review_required' if overlay['review_required'] else
                      'warning' if any(s['error'] or s['index_error'] for s in sources) else 'ready',
            'changes': state.get('changes', []), 'sources': sources, 'budget': _budget(state, config, now),
            'active_overlay_version': overlay['version'], 'review_required': overlay['review_required'],
            'staged': overlay['staged'], 'runs': state['runs'][-20:]}
