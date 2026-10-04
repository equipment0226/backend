"""Explicit local reset, with a verified offline archive before any deletion.

Official legal sources, rules, OCR software/models and existing backups remain.
This is a command-line maintenance operation, never a remotely callable API.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.local'
TARGETS = (
    'debtoff.sqlite3', 'debtoff.sqlite3-wal', 'debtoff.sqlite3-shm',
    'uploads', 'generated', 'ocr/jobs', 'ocr/test-salary.png',
    'reasoning-cache.sqlite3', 'reasoning-cache.sqlite3-wal', 'reasoning-cache.sqlite3-shm',
    'ax-browser-case-v3.json', 'ax-deepseek-benchmark.json', 'ax-llama-benchmark.json',
    'ax-llama-intake-spans.json', 'ax-synthetic-model-debug.json',
    'add_hwp_button.py', 'cleanup_test_cases.py', 'probe_legal_ui.py',
    'recheck_document_ax.py', 'wire_document_ui.py', 'final-tests.log', 'final-tests-2.log',
)


def checked_path(relative):
    path = RUNTIME / relative
    resolved = path.resolve()
    if not resolved.is_relative_to(RUNTIME.resolve()) or resolved == RUNTIME.resolve() or path.is_symlink():
        raise ValueError('Reset target must stay inside the local runtime directory.')
    return path


def reset(apply=False):
    targets = [checked_path(name) for name in TARGETS if checked_path(name).exists()]
    files = []
    for target in targets:
        candidates = list(target.rglob('*')) if target.is_dir() else [target]
        for path in candidates:
            checked_path(path.relative_to(RUNTIME))
            if path.is_file():
                files.append(path)
    report = {'operation': 'fresh_start', 'applied': False,
              'targets': [str(p.relative_to(ROOT)) for p in targets],
              'file_count': len(files),
              'preserved': ['official legal corpus', 'legal watch and policies',
                            'OCR models and runtime', 'API configuration', 'backups', 'source and test reports']}
    if not apply:
        return report
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(('127.0.0.1', 8000)) == 0:
            raise RuntimeError('Stop this project API before resetting its local data.')
    stamp = datetime.now(timezone(timedelta(hours=9))).strftime('%Y%m%d-%H%M%S')
    backup = checked_path('backups') / f'before-fresh-start-{stamp}.zip'
    backup.parent.mkdir(parents=True, exist_ok=True)
    manifest = []
    database = checked_path('debtoff.sqlite3')
    if database.exists():
        with closing(sqlite3.connect(database)) as con:
            con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Original database integrity check failed; nothing was removed.')
            names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            report['previous_record_counts'] = {name: con.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in names}
    with zipfile.ZipFile(backup, 'x', zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            if not path.exists():  # SQLite may remove an empty WAL after checkpoint.
                continue
            name = str(path.relative_to(RUNTIME)).replace('\\', '/')
            payload = path.read_bytes()
            archive.writestr(name, payload)
            manifest.append({'path': name, 'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)})
        archive.writestr('reset-manifest.json', json.dumps({'created_at': stamp, 'files': manifest}, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(backup) as archive:
        for item in manifest:
            if hashlib.sha256(archive.read(item['path'])).hexdigest() != item['sha256']:
                raise RuntimeError('Backup verification failed; nothing was removed.')
    # Every final absolute path was validated above; validate again before removal.
    for path in targets:
        checked_path(path.relative_to(RUNTIME))
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    checked_path('start-profile.json').write_text(json.dumps({'profile': 'fresh', 'created_at': stamp}, indent=2), encoding='utf-8')
    report.update(applied=True, backup=str(backup.relative_to(ROOT)), backup_files_verified=len(manifest),
                  profile='fresh', initial_case_count=0)
    (ROOT / 'reports/fresh-start-reset.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Archive, verify, then clear the listed local runtime data.')
    print(json.dumps(reset(parser.parse_args().apply), ensure_ascii=False, indent=2))
