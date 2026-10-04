"""Local, bounded ZIP intake with durable progress and one atomic case append.

The archive is never extracted into a filesystem tree. Only supported original
documents are read into bounded buffers, locally parsed and content-routed.
Progress files contain labels and IDs, not extracted text or model prompts.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import struct
import threading
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor

from fastapi import Depends, File, Form, HTTPException, UploadFile

from . import domain, store

VERSION = 'local-zip-intake-v1'
SUPPORTED = {'.pdf', '.docx', '.txt', '.csv', '.png', '.jpg', '.jpeg'}
ARCHIVES = {'.zip', '.alz', '.egg', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz'}
MAX_ARCHIVE_BYTES = 50_000_000
MAX_FILE_BYTES = 10_000_000
MAX_TOTAL_BYTES = 100_000_000
MAX_FILES = 100
MAX_RATIO = 200
MAX_PENDING_JOBS = 4
ACTIVE = {'queued', 'reading', 'classifying', 'saving'}
RUNNER_ID = secrets.token_hex(16)
POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='document-import')
LOCK = threading.RLock()
FUTURES = {}


def _member_name(info):
    """Honor verified Unicode path extras, then legacy Korean ZIP names.

    Python uses CP437 when the ZIP UTF-8 bit is absent. Some Korean tools wrote
    CP949 instead. Decoding changes display names only; the archive entry stays
    selected by its original ZipInfo and every resulting path is revalidated.
    """
    name = info.orig_filename
    if info.flag_bits & 0x800:
        return name
    raw = name.encode('cp437')
    extra, offset = info.extra, 0
    while offset + 4 <= len(extra):
        kind, length = struct.unpack_from('<HH', extra, offset)
        data = extra[offset+4:offset+4+length]
        offset += 4 + length
        if kind == 0x7075 and len(data) >= 5 and data[0] == 1 and struct.unpack_from('<I', data, 1)[0] == zlib.crc32(raw):
            try:
                return data[5:].decode('utf-8')
            except UnicodeDecodeError:
                break
    if any(byte >= 128 for byte in raw):
        try:
            return raw.decode('cp949')
        except UnicodeDecodeError:
            pass
    return name


def inspect_archive(content, filename):
    """Validate every member before any decompression or case mutation."""
    extension = Path(filename).suffix.lower()
    if extension in {'.alz', '.egg'}:
        raise domain.DomainError('ARCHIVE_FORMAT_UNSUPPORTED',
            'ALZ·EGG 전용 형식은 아직 지원하지 않습니다. 알집에서 압축 형식을 ZIP으로 선택해 다시 압축해 주세요.')
    domain.require(extension == '.zip', 'ARCHIVE_FORMAT_UNSUPPORTED',
        'ZIP 파일을 선택해 주세요. 알집 프로그램으로 만든 표준 ZIP 파일도 사용할 수 있습니다.')
    domain.require(0 < len(content) <= MAX_ARCHIVE_BYTES, 'ARCHIVE_SIZE', '압축파일은 0바이트 초과 50MB 이하여야 합니다.')
    entries, seen, total = [], set(), 0
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            domain.require(len(infos) <= MAX_FILES * 3, 'ARCHIVE_ENTRY_LIMIT', '압축파일 안의 항목 수가 너무 많습니다. 제출서류만 담아 다시 압축해 주세요.')
            for index, info in enumerate(infos):
                name = _member_name(info)
                normalized = name.replace('\\', '/')
                path = PurePosixPath(normalized)
                domain.require(bool(name) and not any(ord(char) < 32 for char in name)
                    and not normalized.startswith('/') and ':' not in normalized
                    and '..' not in path.parts and len(path.parts) <= 12,
                    'ARCHIVE_UNSAFE_PATH', '압축파일에 안전하지 않은 파일 경로가 있습니다. 서류 파일만 새 ZIP으로 압축해 주세요.')
                domain.require(normalized.casefold() not in seen, 'ARCHIVE_DUPLICATE_PATH',
                    '압축 안에 같은 경로의 항목이 중복되어 있습니다. 중복 항목을 정리해 주세요.')
                seen.add(normalized.casefold())
                kind = stat.S_IFMT(info.external_attr >> 16)
                domain.require(kind in {0, stat.S_IFREG, stat.S_IFDIR}, 'ARCHIVE_SPECIAL_ENTRY',
                    '바로가기·심볼릭 링크 등 일반 서류가 아닌 항목은 압축 제출할 수 없습니다.')
                domain.require(not info.flag_bits & 1, 'ARCHIVE_ENCRYPTED',
                    '암호가 걸린 압축파일은 읽을 수 없습니다. 암호를 해제한 ZIP 파일로 제출해 주세요.')
                if info.is_dir():
                    continue
                domain.require(len(entries) < MAX_FILES, 'ARCHIVE_FILE_LIMIT', '압축파일에는 최대 100개 파일을 담을 수 있습니다.')
                domain.require(info.file_size <= MAX_FILE_BYTES, 'ARCHIVE_MEMBER_SIZE',
                    '압축 안의 각 파일은 해제 후 10MB 이하여야 합니다.')
                total += info.file_size
                domain.require(total <= MAX_TOTAL_BYTES, 'ARCHIVE_EXPANDED_SIZE',
                    '압축 해제 후 전체 파일 합계는 100MB 이하여야 합니다.')
                domain.require(info.file_size / max(1, info.compress_size) <= MAX_RATIO,
                    'ARCHIVE_COMPRESSION_RATIO', '지나치게 높은 압축률의 파일이 있습니다. 서류를 나누거나 압축하지 않은 파일로 제출해 주세요.')
                domain.require(info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED},
                    'ARCHIVE_COMPRESSION_UNSUPPORTED', '표준 ZIP 압축 방식으로 다시 압축해 주세요. 현재 저장·Deflate 방식만 읽을 수 있습니다.')
                suffix = path.suffix.lower()
                domain.require(suffix not in ARCHIVES, 'ARCHIVE_NESTED',
                    '압축 안에 또 다른 압축파일이 있습니다. 실제 서류 파일만 한 ZIP에 담아 주세요.')
                skipped = suffix not in SUPPORTED or path.name in {'.DS_Store', 'Thumbs.db', 'desktop.ini'} or '__MACOSX' in path.parts
                entry = {'entry_id': store.uid('doc'), 'archive_index': index,
                    'filename': path.name[:160], 'extension': suffix, 'declared_size': info.file_size,
                    'status': 'skipped' if skipped else 'pending',
                    'reason': '제출서류 형식이 아닌 부가 파일은 첨부하지 않습니다.' if skipped else '',
                    'request_id': '', 'request_title': '', 'classification_status': None}
                entries.append(entry)
    except domain.DomainError:
        raise
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError) as exc:
        raise domain.DomainError('ARCHIVE_INVALID', '압축파일을 읽지 못했습니다. 정상 ZIP 파일인지 확인해 주세요.') from exc
    domain.require(any(entry['status'] != 'skipped' for entry in entries), 'ARCHIVE_NO_DOCUMENTS',
        '제출할 수 있는 PDF·DOCX·TXT·CSV·PNG·JPG 파일이 없습니다. 설정·JSON 파일은 서류로 첨부하지 않습니다.')
    return entries


def _folder(case_id):
    root = (store.DATA_DIR / 'document_imports').resolve()
    folder = (root / case_id).resolve()
    domain.require(folder.is_relative_to(root) and folder != root, 'IMPORT_PATH', '잘못된 접수 경로입니다.')
    return folder


def _path(case_id, job_id):
    domain.require(bool(re.fullmatch(r'import-[a-f0-9]{12}', job_id)), 'IMPORT_ID', '접수 작업을 찾을 수 없습니다.')
    return _folder(case_id) / (job_id + '.json')


def _save(job, *, create=False):
    """A deleted case's job folder must not be recreated by an old worker."""
    with LOCK:
        folder = _folder(job['case_id'])
        if create:
            folder.mkdir(parents=True, exist_ok=True)
        if not folder.exists():
            return
        path = _path(job['case_id'], job['id'])
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)


def _read(case_id, job_id):
    path = _path(case_id, job_id)
    if not path.is_file():
        raise HTTPException(404, '접수 작업을 찾을 수 없습니다.')
    with LOCK:
        job = json.loads(path.read_text(encoding='utf-8'))
    future = FUTURES.get((str(store.DATA_DIR), job_id))
    interrupted = job.get('runner_id') != RUNNER_ID or (future is not None and future.done())
    if job.get('status') in ACTIVE and interrupted:
        if _recover_committed(job):
            _save(job)
            return job
        # Restart recovery is explicit: a prior process has no in-memory source
        # bytes and may never silently mark its interrupted work successful.
        job.update(status='failed', finished_at=store.now(), error={'code': 'IMPORT_INTERRUPTED',
            'message': '서비스가 다시 시작되어 자료 접수가 중단되었습니다. 사건의 받은 자료를 확인한 뒤 필요한 파일을 다시 제출해 주세요.'})
        _save(job)
    return job


def _recover_committed(job):
    """A crash after SQLite commit but before the final progress save is success."""
    saved = store.get_case(job['case_id'])
    documents = {doc['id']: doc for doc in (saved or {}).get('documents', []) if doc.get('import_job_id') == job['id']}
    expected = [entry for entry in job.get('files', []) if entry['status'] != 'skipped']
    if not expected or any(entry['entry_id'] not in documents for entry in expected):
        return False
    requests = {request['id']: request for request in saved.get('requests', [])}
    for entry in expected:
        document = documents[entry['entry_id']]
        route = document.get('import_routing', {})
        entry.update(status='saved', document_id=document['id'], request_id=document.get('request_id', ''),
            request_title=requests.get(document.get('request_id'), {}).get('title', ''),
            classification_status=route.get('status', 'unmatched'), reason=route.get('reason', ''))
    matched = sum(bool(documents[entry['entry_id']].get('request_id')) for entry in expected)
    job.update(status='completed', case_version=saved['version'], imported_count=len(expected),
        matched_count=matched, review_count=len(expected)-matched,
        skipped_count=sum(entry['status']=='skipped' for entry in job['files']), error=None,
        warning='저장된 원본과 사건 기록을 확인해 접수 결과를 복구했습니다.',
        progress={'completed': len(expected), 'total': len(expected), 'label': '첨부가 끝났습니다.'},
        finished_at=store.now(), updated_at=store.now())
    return True


def _allowed(job, user):
    return user.get('org_id') == job.get('org_id') and (user.get('role') != 'client' or user.get('id') == job.get('actor_id'))


def _public(job):
    keys = ('id', 'status', 'created_at', 'updated_at', 'finished_at', 'archive_name', 'client_token',
            'progress', 'files', 'error', 'case_version', 'imported_count', 'matched_count',
            'review_count', 'skipped_count', 'warning')
    result = {key: copy.deepcopy(job[key]) for key in keys if key in job}
    for entry in result.get('files', []):
        for key in ('archive_index', 'extension', 'declared_size'):
            entry.pop(key, None)
    return result


def _jobs(case_id, user, *, all_actors=False):
    folder = _folder(case_id)
    if not folder.exists():
        return []
    result = []
    for path in folder.glob('import-*.json'):
        try:
            job = _read(case_id, path.stem)
            if all_actors or _allowed(job, user):
                result.append(job)
        except (OSError, ValueError, HTTPException, domain.DomainError):
            continue
    return sorted(result, key=lambda job: (job.get('status') in ACTIVE, job.get('created_at', '')), reverse=True)


def _failed(job, exc):
    if isinstance(exc, domain.DomainError):
        code, message = exc.code, exc.message
    elif isinstance(exc, store.VersionConflict):
        code, message = 'IMPORT_VERSION_CONFLICT', '자료를 읽는 동안 사건 내용이 변경되었습니다. 받은 자료를 확인한 뒤 다시 제출해 주세요. 이번 묶음은 첨부하지 않았습니다.'
    elif isinstance(exc, HTTPException) and exc.status_code in {403, 404}:
        code, message = 'IMPORT_CASE_UNAVAILABLE', '사건이 삭제되었거나 접근 권한이 변경되어 자료 접수를 중단했습니다.'
    else:
        code, message = 'IMPORT_FAILED', '자료를 읽거나 첨부하지 못했습니다. 원본 상태와 받은 자료 목록을 확인해 주세요.'
    job.update(status='failed', error={'code': code, 'message': message}, finished_at=store.now(), updated_at=store.now())
    for entry in job.get('files', []):
        if entry.get('status') not in {'skipped', 'saved'}:
            entry['status'] = 'failed'
    _save(job)


def _worker(job, content, user, authorize, extract_file, commit):
    prepared = []
    try:
        total = len(job['files'])
        job.update(status='reading', progress={'completed': 0, 'total': total, 'label': '압축을 확인하고 서류 내용을 읽고 있습니다.'})
        _save(job)
        actual_total = 0
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            for index, entry in enumerate(job['files']):
                authorize(job['case_id'], user)
                if entry['status'] != 'skipped':
                    entry['status'] = 'reading'
                    job['updated_at'] = store.now()
                    _save(job)
                    try:
                        with archive.open(infos[entry['archive_index']]) as source:
                            raw = source.read(MAX_FILE_BYTES + 1)
                    except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError) as exc:
                        raise domain.DomainError('ARCHIVE_MEMBER_INVALID', '압축 안의 파일이 손상되었거나 읽을 수 없습니다. 원본 파일을 확인해 다시 압축해 주세요.') from exc
                    actual_total += len(raw)
                    domain.require(0 < len(raw) <= MAX_FILE_BYTES and actual_total <= MAX_TOTAL_BYTES,
                        'ARCHIVE_EXPANDED_SIZE', '압축을 해제한 파일 크기가 허용 범위를 벗어납니다.')
                    domain.require(len(raw) == entry['declared_size'], 'ARCHIVE_MEMBER_INVALID', '압축 안의 실제 파일 크기가 기록과 다릅니다.')
                    pages = extract_file(raw, entry['extension'])
                    prepared.append({'id': entry['entry_id'], 'filename': entry['filename'], 'extension': entry['extension'],
                        'content': raw, 'pages': pages, 'sha256': hashlib.sha256(raw).hexdigest(), 'import_job_id': job['id']})
                    entry['status'] = 'read'  # Routing happens after every original is read.
                job.update(progress={'completed': index + 1, 'total': total, 'label': '서류 내용을 읽고 있습니다.'}, updated_at=store.now())
                _save(job)
        current = authorize(job['case_id'], user)
        if current['version'] != job['expected_version']:
            raise store.VersionConflict('Case changed during archive parsing')
        job.update(status='classifying', progress={'completed': 0, 'total': len(prepared), 'label': '파일 내용과 요청서류의 범위를 연결하고 있습니다.'}, updated_at=store.now())
        _save(job)
        from .document_routing import route_documents
        routes = route_documents(current, [{'id': row['id'], 'filename': row['filename'],
            'text': '\n'.join(page.get('text', '') for page in row['pages']), 'page_texts': row['pages']} for row in prepared])
        by_id = {route['entry_id']: route for route in routes}
        request_map = {request['id']: request for request in current.get('requests', [])}
        for index, row in enumerate(prepared):
            route = by_id.get(row['id'], {'entry_id': row['id'], 'request_id': None, 'status': 'unmatched',
                'confidence': 'none', 'reason': '연결할 요청서류를 확인해 주세요.'})
            request_id = route.get('request_id') if route.get('status') == 'matched' and route.get('confidence') == 'high' else ''
            if request_id and request_id not in request_map:
                raise domain.DomainError('IMPORT_SCOPE_CHANGED', '연결할 요청서류가 변경되었습니다. 현재 요청 목록을 확인해 주세요.')
            row.update(request_id=request_id or '', import_routing=copy.deepcopy(route))
            entry = next(item for item in job['files'] if item['entry_id'] == row['id'])
            entry.update(status='classified', request_id=request_id or '',
                request_title=request_map.get(request_id, {}).get('title', ''),
                classification_status=route.get('status', 'unmatched'), reason=route.get('reason', ''))
            job['progress']['completed'] = index + 1
        job.update(status='saving', progress={'completed': 0, 'total': len(prepared), 'label': '읽은 서류를 사건에 한 번에 첨부하고 있습니다.'}, updated_at=store.now())
        _save(job)
        try:
            result = commit(job['case_id'], prepared, job['expected_version'], user)
        except Exception:
            # A post-commit display/scheduling error may return an error even
            # though all originals exist. Report the actual stored result and
            # never invite a blind duplicate upload or remove those originals.
            saved = store.get_case(job['case_id'])
            existing = {doc.get('id') for doc in (saved or {}).get('documents', [])}
            if {row['id'] for row in prepared} <= existing:
                result = {'version': saved['version']}
                job['warning'] = '서류는 모두 저장되었습니다. 후속 진행 상태는 사건 화면에서 다시 확인해 주세요.'
            else:
                raise
        for entry in job['files']:
            if entry['status'] != 'skipped':
                entry.update(status='saved', document_id=entry['entry_id'])
        matched = sum(bool(row.get('request_id')) for row in prepared)
        job.update(status='completed', case_version=result['version'], imported_count=len(prepared),
            matched_count=matched, review_count=len(prepared)-matched,
            skipped_count=sum(entry['status']=='skipped' for entry in job['files']),
            progress={'completed': len(prepared), 'total': len(prepared), 'label': '첨부가 끝났습니다.'},
            finished_at=store.now(), updated_at=store.now())
        _save(job)
    except Exception as exc:
        _failed(job, exc)


def attach(app, current_user, authorize, extract_file, commit):
    @app.post('/api/cases/{case_id}/document-imports', status_code=202)
    async def start_import(case_id: str, archive: UploadFile = File(...), expected_version: int = Form(...),
                           client_token: str = Form(default=''), user=Depends(current_user)):
        case = authorize(case_id, user)
        domain.require(not client_token or bool(re.fullmatch(r'[A-Za-z0-9_-]{8,80}', client_token)),
            'IMPORT_TOKEN', '접수 식별값을 확인해 주세요.')
        content = await archive.read(MAX_ARCHIVE_BYTES + 1)
        filename = Path((archive.filename or 'archive.zip').replace('\\', '/')).name[:160]
        digest = hashlib.sha256(content).hexdigest()
        with LOCK:
            jobs = _jobs(case_id, user, all_actors=True)
            previous = next((job for job in jobs if client_token and job.get('client_token') == client_token
                             and job.get('actor_id') == user['id']), None)
            if previous:
                domain.require(previous.get('archive_sha256') == digest, 'IMPORT_TOKEN_REUSED',
                    '이 접수 식별값은 다른 압축파일에 사용되었습니다. 새 파일로 다시 접수해 주세요.')
                return _public(previous)
            active = next((job for job in jobs if job.get('status') in ACTIVE), None)
            if active:
                raise HTTPException(409, {'code': 'IMPORT_IN_PROGRESS', 'message': '이 사건의 자료를 이미 읽고 있습니다. 진행 중인 접수 결과를 확인해 주세요.',
                    'job_id': active['id'] if _allowed(active, user) else None})
            if case['version'] != expected_version:
                raise store.VersionConflict('사건이 변경되었습니다. 새로고침 후 제출하세요.')
            # Bound retained archive bytes across cases, not only per case.
            # Finished jobs stay on disk for recovery; their futures need not
            # keep result/exception objects alive in this process.
            for key, future in list(FUTURES.items()):
                if future.done():
                    FUTURES.pop(key, None)
            if len(FUTURES) >= MAX_PENDING_JOBS:
                raise HTTPException(429, {'code': 'IMPORT_QUEUE_FULL',
                    'message': '다른 자료 묶음을 처리하고 있습니다. 잠시 후 다시 제출해 주세요.'})
            entries = inspect_archive(content, filename)
            job = {'id': store.uid('import'), 'version': VERSION, 'case_id': case_id, 'org_id': user['org_id'],
                'actor_id': user['id'], 'expected_version': expected_version, 'runner_id': RUNNER_ID,
                'archive_name': filename, 'archive_sha256': digest, 'client_token': client_token,
                'status': 'queued', 'created_at': store.now(), 'updated_at': store.now(),
                'progress': {'completed': 0, 'total': len(entries), 'label': '자료 접수를 준비하고 있습니다.'},
                'files': entries, 'error': None}
            _save(job, create=True)
            response = _public(job)
            try:
                FUTURES[(str(store.DATA_DIR), job['id'])] = POOL.submit(
                    _worker, job, content, copy.deepcopy(user), authorize, extract_file, commit)
            except Exception as exc:
                _failed(job, exc)
                return _public(job)
            return response

    @app.get('/api/cases/{case_id}/document-imports')
    def import_list(case_id: str, user=Depends(current_user)):
        authorize(case_id, user)
        return {'jobs': [_public(job) for job in _jobs(case_id, user)[:10]]}

    @app.get('/api/cases/{case_id}/document-imports/{job_id}')
    def import_status(case_id: str, job_id: str, user=Depends(current_user)):
        authorize(case_id, user)
        job = _read(case_id, job_id)
        if not _allowed(job, user):
            raise HTTPException(404, '접수 작업을 찾을 수 없습니다.')
        return _public(job)
