"""SQLite transaction boundary. Case snapshots are immutable in the history table."""
import copy
import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv('DEBTOFF_DATA_DIR', str(ROOT / '.local'))).resolve()


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix):
    return f'{prefix}-{secrets.token_hex(6)}'


def digest(obj):
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@contextmanager
def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA_DIR / 'debtoff.sqlite3', timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA foreign_keys=ON')
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def initialize():
    with db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY, org_id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS history(case_id TEXT, version INTEGER, body TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(case_id,version));
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            role TEXT NOT NULL, org_id TEXT NOT NULL, password_hash TEXT NOT NULL, active INTEGER NOT NULL,
            auth_profile TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS portal_applications(org_id TEXT NOT NULL, user_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL, case_id TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(org_id,user_id,idempotency_key));
        CREATE TABLE IF NOT EXISTS matter_sequences(org_id TEXT NOT NULL, year TEXT NOT NULL, value INTEGER NOT NULL,
            PRIMARY KEY(org_id,year));
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, case_id TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS access_log(id INTEGER PRIMARY KEY, user_id TEXT, case_id TEXT, action TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS ax_runs(id TEXT PRIMARY KEY, case_id TEXT NOT NULL, signature TEXT NOT NULL, knowledge_signature TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS intake_runs(id TEXT PRIMARY KEY, org_id TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS collection_runs(id TEXT PRIMARY KEY, org_id TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS structured_case_data(id TEXT PRIMARY KEY, case_id TEXT NOT NULL, input_revision INTEGER NOT NULL, source_hash TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS generated_document_versions(id TEXT PRIMARY KEY, case_id TEXT NOT NULL, kind TEXT NOT NULL, content_hash TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS court_outcomes(id TEXT PRIMARY KEY, case_id TEXT NOT NULL, org_id TEXT NOT NULL, court_id TEXT NOT NULL, outcome TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_outcome_scope ON court_outcomes(org_id,court_id,outcome);
        ''')
        rows = con.execute("SELECT * FROM runs WHERE status IN ('queued','running')").fetchall()
        for row in rows:
            body = json.loads(row['body'])
            body.update(status='failed', error='서버 재시작으로 중단됨. 현재 사건을 확인한 후 다시 실행하세요.')
            con.execute('UPDATE runs SET status=?,body=? WHERE id=?', ('failed', dumps(body), row['id']))
        for table in ('ax_runs','intake_runs','collection_runs'):
            for row in con.execute(f"SELECT * FROM {table} WHERE status IN ('queued','running')").fetchall():
                body=json.loads(row['body'])
                body.update(status='failed',error='서버 재시작으로 작업이 중단됐습니다. 저장된 자료를 확인하고 다시 실행하세요.')
                con.execute(f'UPDATE {table} SET status=?,body=? WHERE id=?',('failed',dumps(body),row['id']))


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


def insert_case(case):
    with db() as con:
        body = dumps(case)
        con.execute('INSERT INTO cases VALUES (?,?,?,?)', (case['id'], case['org_id'], case['version'], body))
        con.execute('INSERT INTO history VALUES (?,?,?,?)', (case['id'], case['version'], body, now()))
        _persist_ax_records(con, case)


def insert_application(case, user, key, payload_hash):
    """Allocate a readable matter number and deduplicate retries in the case transaction."""
    from datetime import timedelta
    year = datetime.now(timezone(timedelta(hours=9))).strftime('%Y')
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        existing = con.execute('SELECT * FROM portal_applications WHERE org_id=? AND user_id=? AND idempotency_key=?',
                               (user['org_id'], user['id'], key)).fetchone()
        if existing:
            if existing['payload_hash'] != payload_hash:
                raise VersionConflict('같은 신청 키의 내용이 변경되었습니다. 기존 신청을 확인한 뒤 새 신청을 시작해주세요.')
            row = con.execute('SELECT body FROM cases WHERE id=?', (existing['case_id'],)).fetchone()
            if not row:
                raise VersionConflict('기존 신청을 찾을 수 없습니다. 담당자에게 문의해주세요.')
            return json.loads(row['body']), False
        con.execute('INSERT OR IGNORE INTO matter_sequences VALUES (?,?,0)', (user['org_id'], year))
        con.execute('UPDATE matter_sequences SET value=value+1 WHERE org_id=? AND year=?', (user['org_id'], year))
        number = con.execute('SELECT value FROM matter_sequences WHERE org_id=? AND year=?', (user['org_id'], year)).fetchone()[0]
        case['matter_number'] = f'{year}-{number:06d}'
        body = dumps(case)
        con.execute('INSERT INTO cases VALUES (?,?,?,?)', (case['id'], case['org_id'], case['version'], body))
        con.execute('INSERT INTO history VALUES (?,?,?,?)', (case['id'], case['version'], body, now()))
        con.execute('INSERT INTO portal_applications VALUES (?,?,?,?,?,?)',
                    (user['org_id'], user['id'], key, payload_hash, case['id'], now()))
        _persist_ax_records(con, case)
    return case, True


def _persist_ax_records(con, case):
    """Append-only evidence/output ledger, atomic with the corresponding case revision."""
    for row in case.get('structured_data', []):
        con.execute('INSERT OR IGNORE INTO structured_case_data VALUES (?,?,?,?,?,?)',
                    (row['id'], case['id'], row['input_revision'], row['source_hash'], dumps(row), row['created_at']))
    for kind in ('drafts', 'court_documents', 'bundles', 'filing_packages'):
        for row in case.get(kind, []):
            content_hash = row.get('content_hash') or row.get('snapshot_hash') or digest(row)
            # Include content hash so later metadata changes never overwrite the generated version.
            record_id = row['id'] + ':' + content_hash
            con.execute('INSERT OR IGNORE INTO generated_document_versions VALUES (?,?,?,?,?,?)',
                        (record_id, case['id'], kind, content_hash, dumps(row), row.get('created_at', now())))
    for row in case.get('court_outcomes', []):
        con.execute('INSERT OR IGNORE INTO court_outcomes VALUES (?,?,?,?,?,?,?)',
                    (row['id'], case['id'], case['org_id'], case.get('court_id', 'unknown'), row['outcome'], dumps(row), row['created_at']))


def get_case(case_id):
    with db() as con:
        row = con.execute('SELECT body FROM cases WHERE id=?', (case_id,)).fetchone()
    return json.loads(row['body']) if row else None


class VersionConflict(Exception):
    pass


def mutate(case_id, expected_version, user, action, fn):
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT body FROM cases WHERE id=?', (case_id,)).fetchone()
        if row is None:
            raise KeyError(case_id)
        case = json.loads(row['body'])
        if case['version'] != expected_version:
            raise VersionConflict('다른 작업으로 사건이 변경되었습니다. 새로고침 후 비교하여 저장하세요.')
        old = copy.deepcopy(case)
        fn(case)
        case['version'] += 1
        case['updated_at'] = now()
        case['audit'].append({'id': uid('ev'), 'action': action, 'actor': user['name'], 'role': user['role'], 'at': now(), 'from_version': old['version'], 'to_version': case['version'], 'previous_hash': digest(old)})
        body = dumps(case)
        con.execute('UPDATE cases SET version=?,body=? WHERE id=?', (case['version'], body, case_id))
        con.execute('INSERT INTO history VALUES (?,?,?,?)', (case_id, case['version'], body, now()))
        _persist_ax_records(con, case)
    return case


def access(user, case_id, action):
    with db() as con:
        con.execute('INSERT INTO access_log(user_id,case_id,action,created_at) VALUES (?,?,?,?)', (user['id'], case_id, action, now()))
