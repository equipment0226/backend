"""Consistent SQLite snapshot and referenced files, restored only into a new directory."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import zipfile
from contextlib import closing

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from apps.api import store


def create_backup(destination):
    destination=Path(destination).resolve()
    if destination.exists():
        raise ValueError('백업 파일이 이미 존재합니다. 새 경로를 사용하세요.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    snapshot=destination.with_suffix('.snapshot.sqlite3')
    if snapshot.exists():
        raise ValueError('임시 스냅샷 경로가 이미 존재합니다.')
    with store.db() as source, closing(sqlite3.connect(snapshot)) as target:
        source.backup(target)
    with closing(sqlite3.connect(snapshot)) as con:
        cases=[json.loads(r[0]) for r in con.execute('SELECT body FROM cases')]
    entries=[(snapshot,'debtoff.sqlite3')]
    for case in cases:
        references=[('uploads',doc['storage_name'],doc['sha256']) for doc in case['documents']]
        references.extend(('uploads',receipt['storage_name'],receipt['sha256']) for receipt in case.get('submission_receipts',[]))
        references.extend(('generated',doc['id']+'.pdf',doc['sha256']) for doc in case.get('court_documents',[]))
        for folder,filename,expected_hash in references:
            path=store.DATA_DIR/folder/case['id']/filename
            if not path.resolve().is_relative_to(store.DATA_DIR.resolve()) or not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected_hash:
                snapshot.unlink(missing_ok=True)
                raise ValueError('참조 원문이 없거나 해시가 다릅니다. 백업을 중단합니다.')
            entries.append((path,str(path.relative_to(store.DATA_DIR)).replace('\\','/')))
    manifest={'format':'debtoff-backup-v1','created_at':store.now(),'files':[]}
    try:
        with zipfile.ZipFile(destination,'x',zipfile.ZIP_DEFLATED) as z:
            for path,name in entries:
                content=path.read_bytes()
                z.writestr(name,content)
                manifest['files'].append({'name':name,'sha256':hashlib.sha256(content).hexdigest()})
            z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False))
    finally:
        snapshot.unlink(missing_ok=True)
    return manifest


def restore_backup(source,destination):
    destination=Path(destination).resolve()
    if destination.exists():
        raise ValueError('기존 데이터를 덮어쓰지 않습니다. 새 복원 폴더를 지정하세요.')
    with zipfile.ZipFile(source) as z:
        manifest=json.loads(z.read('manifest.json'))
        if manifest.get('format')!='debtoff-backup-v1':
            raise ValueError('지원하지 않는 백업 형식입니다.')
        verified=[]
        for row in manifest['files']:
            path=(destination/row['name']).resolve()
            if not path.is_relative_to(destination):
                raise ValueError('잘못된 백업 경로입니다.')
            content=z.read(row['name'])
            if hashlib.sha256(content).hexdigest()!=row['sha256']:
                raise ValueError('백업 해시 검증에 실패했습니다.')
            verified.append((path,content))
        for path,content in verified:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(content)
    with closing(sqlite3.connect(destination/'debtoff.sqlite3')) as con:
        if con.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise ValueError('복원 DB 무결성 검사가 실패했습니다.')
        for row in con.execute('SELECT body FROM cases'):
            case=json.loads(row[0])
            references=[('uploads',doc['storage_name'],doc['sha256']) for doc in case['documents']]
            references.extend(('uploads',receipt['storage_name'],receipt['sha256']) for receipt in case.get('submission_receipts',[]))
            references.extend(('generated',doc['id']+'.pdf',doc['sha256']) for doc in case.get('court_documents',[]))
            for folder,filename,expected in references:
                path=(destination/folder/case['id']/filename).resolve()
                if not path.is_relative_to(destination) or not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
                    raise ValueError('복원한 사건의 원문 또는 생성 PDF가 누락되었거나 변경되었습니다.')
        # A restored environment never resurrects old login sessions.
        con.execute('DELETE FROM sessions')
        con.commit()
    return {'files_verified':len(verified),'integrity':'ok','sessions_revoked':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['create','restore'])
    parser.add_argument('path')
    parser.add_argument('--destination')
    args=parser.parse_args()
    if args.action=='restore' and not args.destination:
        parser.error('restore에는 --destination 새폴더가 필요합니다.')
    result=create_backup(args.path) if args.action=='create' else restore_backup(args.path,args.destination)
    print(json.dumps(result,ensure_ascii=False,indent=2))
