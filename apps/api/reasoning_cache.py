"""Bound expensive anonymous reasoning calls; never persist customer prompts.

SQLite coordinates worker threads/processes. Keys are hashes of the validated
anonymous input, prompt/schema, model and active legal versions. Only complete,
validated anonymous answers are cached. Failed calls still consume a reservation
so an outage or invalid answer cannot start an unbounded retry loop.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone

from . import store

VERSION = 'anonymous-reasoning-cache-v1'


class ReasoningLimitError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(code)


def _integer(name, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(os.getenv(name, default))))
    except (ValueError, TypeError):
        return default


def settings():
    saved = {}
    if cache_path().exists():
        try:
            with closing(sqlite3.connect(cache_path(), timeout=5)) as con:
                row = con.execute("SELECT value FROM preferences WHERE name='daily_call_limit'").fetchone()
                if row:
                    saved['daily_call_limit'] = int(row[0])
        except (sqlite3.Error, ValueError, TypeError):
            pass
    return {'ttl_seconds': _integer('DEBTOFF_REASONING_CACHE_TTL_SECONDS', 7 * 86400, 60, 30 * 86400),
            'daily_call_limit': saved.get('daily_call_limit', _integer('DEBTOFF_REASONING_DAILY_CALL_LIMIT', 20, 0, 1000)),
            'max_calls_per_stage': 2}


def cache_path():
    return store.DATA_DIR / 'reasoning-cache.sqlite3'


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _day(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now, timezone(timedelta(hours=9))).date().isoformat()


def identity(*, safe_payload, messages, schema, model, provider, stage, legal_signature):
    if stage not in {'strategy', 'document_macro'}:
        raise ReasoningLimitError('INVALID_REASONING_STAGE', '등록된 고급 검증 단계가 아닙니다.')
    value = {'version': VERSION, 'facts_and_public_law': safe_payload,
             'prompt': messages, 'schema': schema, 'model': model, 'provider': provider,
             'stage': stage, 'legal_versions': legal_signature, 'reasoning_effort': 'high'}
    key = _digest(value)
    # Output limits and timeout are intentionally absent: a complete valid
    # answer remains reusable after a response-budget adjustment.
    return key, _digest({'key': key, 'stage': stage})


@contextmanager
def _db():
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript('''
            CREATE TABLE IF NOT EXISTS answers (
                cache_key TEXT PRIMARY KEY, created_at REAL NOT NULL,
                expires_at REAL NOT NULL, response TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cache_key TEXT NOT NULL,
                stage_key TEXT NOT NULL, stage TEXT NOT NULL, ordinal INTEGER NOT NULL,
                day TEXT NOT NULL, started_at REAL NOT NULL, lease_until REAL NOT NULL,
                status TEXT NOT NULL, prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS attempts_stage ON attempts(stage_key, started_at);
            CREATE INDEX IF NOT EXISTS attempts_cache ON attempts(cache_key, status);
            CREATE TABLE IF NOT EXISTS counters (day TEXT PRIMARY KEY, cache_hits INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS preferences (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _cached(con, key, now):
    row = con.execute('SELECT response FROM answers WHERE cache_key=? AND expires_at>?', (key, now)).fetchone()
    return json.loads(row['response']) if row else None


def _hit(con, now):
    con.execute('INSERT INTO counters(day,cache_hits) VALUES (?,1) ON CONFLICT(day) DO UPDATE SET cache_hits=cache_hits+1', (_day(now),))


def reserve(key, stage_key, stage, timeout, attempt='auto'):
    if attempt not in {'auto', 'primary', 'repair'}:
        raise ReasoningLimitError('INVALID_REASONING_ATTEMPT', '검증 실행 종류가 올바르지 않습니다.')
    now, config = time.time(), settings()
    with _db() as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute('DELETE FROM answers WHERE expires_at<=?', (now,))
        con.execute('DELETE FROM attempts WHERE started_at<?', (now - 31 * 86400,))
        con.execute('DELETE FROM counters WHERE day<?', (_day(now - 31 * 86400),))
        cached = _cached(con, key, now)
        if cached is not None:
            _hit(con, now)
            return {'state': 'cached', 'response': cached}
        active = con.execute("SELECT id FROM attempts WHERE cache_key=? AND status='running' AND lease_until>? ORDER BY id DESC LIMIT 1", (key, now)).fetchone()
        if active:
            return {'state': 'wait', 'attempt_id': active['id']}
        con.execute("UPDATE attempts SET status='lease_expired' WHERE cache_key=? AND status='running' AND lease_until<=?", (key, now))
        count = con.execute('SELECT count(*) FROM attempts WHERE stage_key=? AND started_at>?', (stage_key, now - config['ttl_seconds'])).fetchone()[0]
        # A successful answer may naturally expire after seven days; that starts
        # a new bounded window. Within it: one primary and at most one repair.
        if count >= config['max_calls_per_stage'] or (attempt == 'primary' and count) or (attempt == 'repair' and count != 1):
            raise ReasoningLimitError('REASONING_STAGE_BUDGET_EXHAUSTED', '동일 자료의 검증과 보완 검증 한도에 도달했습니다. 변경된 자료를 확인하세요.')
        daily = con.execute('SELECT count(*) FROM attempts WHERE day=?', (_day(now),)).fetchone()[0]
        if daily >= config['daily_call_limit']:
            raise ReasoningLimitError('REASONING_DAILY_BUDGET_EXHAUSTED', '오늘의 고급 검증 실행 한도에 도달했습니다. 저장된 자료와 검토 이력은 유지됩니다.')
        cursor = con.execute('INSERT INTO attempts(cache_key,stage_key,stage,ordinal,day,started_at,lease_until,status) VALUES (?,?,?,?,?,?,?,?)',
                             (key, stage_key, stage, count + 1, _day(now), now, now + timeout + 10, 'running'))
        return {'state': 'reserved', 'attempt_id': cursor.lastrowid, 'ordinal': count + 1}


def _token(value):
    return value if type(value) is int and value >= 0 else 0


def complete(attempt_id, key, response, cacheable):
    now = time.time()
    usage = response.get('usage') or {}
    reasoning = (usage.get('completion_tokens_details') or {}).get('reasoning_tokens', 0)
    with _db() as con:
        con.execute('BEGIN IMMEDIATE')
        current = con.execute('SELECT status FROM attempts WHERE id=?', (attempt_id,)).fetchone()
        if not current or current['status'] != 'running':
            return
        con.execute('UPDATE attempts SET status=?,prompt_tokens=?,completion_tokens=?,reasoning_tokens=? WHERE id=?',
                    ('completed' if cacheable else 'not_cacheable',
                     _token(response.get('prompt_eval_count', usage.get('prompt_tokens'))),
                     _token(response.get('eval_count', usage.get('completion_tokens'))), _token(reasoning), attempt_id))
        if cacheable:
            con.execute('INSERT OR REPLACE INTO answers VALUES (?,?,?,?)',
                        (key, now, now + settings()['ttl_seconds'], json.dumps(response, ensure_ascii=False, allow_nan=False)))


def fail(attempt_id):
    with _db() as con:
        con.execute("UPDATE attempts SET status='failed' WHERE id=? AND status='running'", (attempt_id,))


async def _wait_for_result(key, attempt_id, timeout):
    deadline = time.monotonic() + timeout + 12
    while time.monotonic() < deadline:
        with _db() as con:
            now = time.time()
            cached = _cached(con, key, now)
            if cached is not None:
                _hit(con, now)
                return cached
            row = con.execute('SELECT status,lease_until FROM attempts WHERE id=?', (attempt_id,)).fetchone()
            if not row or row['status'] != 'running' or row['lease_until'] <= now:
                raise ReasoningLimitError('REASONING_SHARED_CALL_INCOMPLETE', '같은 자료의 선행 검증이 완료되지 않았습니다. 결과를 확인한 뒤 보완 검증할 수 있습니다.')
        await asyncio.sleep(0.15)
    raise ReasoningLimitError('REASONING_WAIT_TIMEOUT', '진행 중인 검증의 응답을 기다리는 시간이 초과되었습니다.')


async def run(*, key, stage_key, stage, operation, validate, timeout, attempt='auto'):
    claim = reserve(key, stage_key, stage, timeout, attempt)
    if claim['state'] in {'cached', 'wait'}:
        result = claim.get('response')
        if result is None:
            result = await _wait_for_result(key, claim['attempt_id'], timeout)
        result = copy.deepcopy(result)
        result['cost_control'] = {'cache_hit': True, 'shared_inflight': claim['state'] == 'wait', 'cache_key': key,
                                  'stage': stage, 'new_expensive_calls': 0}
        return result
    try:
        raw = await operation()
        validated = validate(raw)
        complete(claim['attempt_id'], key, validated if validated is not None else raw, validated is not None)
        raw = copy.deepcopy(validated if validated is not None else raw)
        raw['cost_control'] = {'cache_hit': False, 'shared_inflight': False, 'cache_key': key, 'stage': stage,
                              'attempt': 'primary' if claim['ordinal'] == 1 else 'repair', 'new_expensive_calls': 1}
        return raw
    except BaseException:
        fail(claim['attempt_id'])
        raise


def operational_metrics():
    """Server/operator metrics only, never product-model labels or client data."""
    with _db() as con:
        row = con.execute('SELECT count(*) calls,coalesce(sum(prompt_tokens),0) prompt_tokens,coalesce(sum(completion_tokens),0) completion_tokens,coalesce(sum(reasoning_tokens),0) reasoning_tokens FROM attempts WHERE day=?', (_day(),)).fetchone()
        hits = con.execute('SELECT cache_hits FROM counters WHERE day=?', (_day(),)).fetchone()
        return {'day': _day(), **dict(row), 'cache_hits': hits['cache_hits'] if hits else 0,
                'used': row['calls'], 'cached': hits['cache_hits'] if hits else 0, 'daily_limit': settings()['daily_call_limit'],
                'daily_call_limit': settings()['daily_call_limit'], 'ttl_seconds': settings()['ttl_seconds']}


def configure(changes):
    """Persist lawyer-authorized settings; caller enforces the API role check."""
    if not isinstance(changes, dict) or set(changes) != {'daily_call_limit'} or type(changes['daily_call_limit']) is not int or not 1 <= changes['daily_call_limit'] <= 100:
        raise ValueError('INVALID_REASONING_BUDGET')
    with _db() as con:
        con.execute('INSERT INTO preferences(name,value) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                    ('daily_call_limit', str(changes['daily_call_limit'])))
    return settings()
