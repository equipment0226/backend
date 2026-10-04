"""Re-extract saved official bytes without network; preserve original fetch dates."""
import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from apps.api import corpus


async def rebuild():
    path=corpus.CORPUS_DIR/'snapshot.json'
    previous=json.loads(path.read_text(encoding='utf-8'))
    sources=[];chunks=[]
    definitions={s['id']:s for s in corpus.seeds()}
    for old in previous['sources']:
        raw_path=corpus.CORPUS_DIR/old.get('raw_file','missing')
        if old['status'] not in ('collected','stale') or not raw_path.is_file():
            sources.append(old);chunks.extend(c for c in previous['chunks'] if c['source_id']==old['id']);continue
        raw=raw_path.read_bytes()
        async def saved_download(client,url):
            return raw,old.get('media_type','text/html'),old.get('final_url') or url,200
        with patch.object(corpus,'_download',saved_download):
            source,records=await corpus._ingest_source(None,definitions[old['id']],asyncio.Semaphore(1))
        source['reextracted_at']=corpus._now()
        source['fetched_at']=old['fetched_at']
        source['last_success_at']=old.get('last_success_at',old['fetched_at'])
        for record in records:
            record.update(fetched_at=old['fetched_at'],retrieved_at=old['fetched_at'])
        sources.append(source);chunks.extend(records)
    result={**previous,'reextracted_at':corpus._now(),'sources':sources,'chunks':chunks}
    temporary=path.with_suffix('.rebuild.tmp')
    temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(temporary,path)
    report={'method':'saved bytes re-extraction; no network','reextracted_at':result['reextracted_at'],**corpus.list_sources()['stats']}
    (ROOT/'reports/corpus-reextraction.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))


if __name__=='__main__':asyncio.run(rebuild())
