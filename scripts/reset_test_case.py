"""Remove one explicitly named fictional case, preserving accounts and legal data.

Offline maintenance only. A verified archive precedes any database/file deletion.
The default is a dry run; --apply is deliberately explicit.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.local'
CASE_TABLES = ('history', 'portal_applications', 'runs', 'access_log', 'ax_runs',
               'structured_case_data', 'generated_document_versions', 'court_outcomes', 'portal_reviews')


def checked(path):
    path = Path(path)
    resolved = path.resolve()
    if not resolved.is_relative_to(RUNTIME.resolve()) or resolved == RUNTIME.resolve() or path.is_symlink():
        raise ValueError('Target must remain within this workspace local runtime.')
    for parent in path.parents:
        if parent == RUNTIME:
            break
        if parent.is_symlink():
            raise ValueError('Symlink parent is not an allowed reset target.')
    return path


def reset(case_id, apply=False):
    if not re.fullmatch(r'case-[a-f0-9]{12}', case_id):
        raise ValueError('Use a complete local case ID.')
    database = checked(RUNTIME / 'debtoff.sqlite3')
    with closing(sqlite3.connect(f'file:{database.as_posix()}?mode=ro', uri=True)) as con:
        row = con.execute('SELECT body FROM cases WHERE id=?', (case_id,)).fetchone()
        if not row:
            raise ValueError('Case does not exist.')
        case = json.loads(row[0])
        if not case.get('synthetic'):
            raise ValueError('This utility only removes explicitly fictional cases.')
        account_hash = hashlib.sha256(repr(con.execute('SELECT * FROM accounts ORDER BY id').fetchall()).encode()).hexdigest()
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {table: con.execute(f'SELECT COUNT(*) FROM {table} WHERE case_id=?', (case_id,)).fetchone()[0]
                  for table in CASE_TABLES if table in tables}
    folders = [checked(RUNTIME / kind / case_id) for kind in ('uploads', 'generated')]
    files = []
    for folder in folders:
        if folder.exists():
            for path in folder.rglob('*'):
                checked(path)
                if path.is_file():
                    files.append(path)
    report = {'case_id': case_id, 'applied': False, 'records': counts,
        'files': len(files), 'targets': [str(p.relative_to(ROOT)) for p in folders],
        'preserved': ['accounts', 'sessions', 'other cases', 'matter sequences', 'legal corpus', 'rules', 'OCR runtime', 'configuration']}
    if not apply:
        return report
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(('127.0.0.1', 8000)) == 0:
            raise RuntimeError('Stop this project API before removing the fictional case.')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    backups = checked(RUNTIME / 'backups')
    backups.mkdir(exist_ok=True)
    snapshot = checked(backups / f'case-reset-{stamp}.sqlite3')
    archive_path = checked(backups / f'before-case-reset-{stamp}.zip')
    with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(snapshot)) as dest:
        source.backup(dest)
        if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Backup integrity failed; case retained.')
    manifest = []
    with zipfile.ZipFile(archive_path, 'x', zipfile.ZIP_DEFLATED) as archive:
        for path in [snapshot] + files:
            checked(path)
            payload = path.read_bytes()
            name = 'debtoff.sqlite3' if path == snapshot else path.relative_to(RUNTIME).as_posix()
            archive.writestr(name, payload)
            manifest.append({'path': name, 'sha256': hashlib.sha256(payload).hexdigest()})
        archive.writestr('manifest.json', json.dumps(manifest))
    with zipfile.ZipFile(archive_path) as archive:
        for item in manifest:
            if hashlib.sha256(archive.read(item['path'])).hexdigest() != item['sha256']:
                raise RuntimeError('Archive verification failed; case retained.')
    with closing(sqlite3.connect(database)) as con:
        con.execute('BEGIN IMMEDIATE')
        # Refuse a newly modified case even when the API was shut down meanwhile.
        current = con.execute('SELECT body FROM cases WHERE id=?', (case_id,)).fetchone()
        if not current or json.loads(current[0]) != case:
            raise RuntimeError('Case changed since inspection; nothing removed.')
        for table in counts:
            con.execute(f'DELETE FROM {table} WHERE case_id=?', (case_id,))
        con.execute('DELETE FROM cases WHERE id=?', (case_id,))
        preserved = hashlib.sha256(repr(con.execute('SELECT * FROM accounts ORDER BY id').fetchall()).encode()).hexdigest()
        if preserved != account_hash:
            raise RuntimeError('Account preservation check failed.')
        con.commit()
        report['remaining_cases'] = con.execute('SELECT COUNT(*) FROM cases').fetchone()[0]
    for folder in folders:
        checked(folder)
        if folder.exists():
            shutil.rmtree(folder)
    checked(snapshot).unlink()
    report.update(applied=True, backup=archive_path.relative_to(ROOT).as_posix(),
                  backup_files_verified=len(manifest), accounts_unchanged=True)
    (ROOT / 'reports/test-case-reset.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('case_id')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(reset(args.case_id, args.apply), ensure_ascii=False, indent=2))
