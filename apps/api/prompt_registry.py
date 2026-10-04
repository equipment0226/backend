"""Versioned model instructions. Business rules and calculators stay in code/data."""
import hashlib
import json
from pathlib import Path

PROMPT_DIR=Path(__file__).with_name('prompts')


def catalog(include_text=False):
    manifest=json.loads((PROMPT_DIR/'manifest.json').read_text(encoding='utf-8'))
    rows=[]
    for entry in manifest['prompts']:
        text=(PROMPT_DIR/entry['file']).read_text(encoding='utf-8').strip()
        row={**entry,'sha256':hashlib.sha256(text.encode()).hexdigest(),'path':'apps/api/prompts/'+entry['file']}
        if include_text:row['text']=text
        rows.append(row)
    return {'version':manifest['version'],'prompts':rows,'scope':'모델 지시문. 서류 준비 규칙·법률 계산식·출력 검증은 별도 코드에서 실행합니다.'}


def instruction(prompt_id):
    entry=next((p for p in catalog(True)['prompts'] if p['id']==prompt_id),None)
    if not entry:raise ValueError('UNKNOWN_PROMPT_ID')
    return entry['text']


def metadata(prompt_id):
    entry=next(p for p in catalog()['prompts'] if p['id']==prompt_id)
    return {k:entry[k] for k in ('id','version','sha256','path')}


def signature():
    return hashlib.sha256(json.dumps(catalog()['prompts'],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
