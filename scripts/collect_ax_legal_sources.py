"""Download explicitly configured public legal sources, never case data.

Preserves original bytes in .local and checked-in searchable text with hashes and
page locators in data/legal_research. Run from the repository root.
"""
import asyncio
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from apps.api.corpus import _download, _extract


async def main(source_ids=None):
    manifest = json.loads((ROOT / 'data/legal_ax_sources.json').read_text(encoding='utf-8'))
    target = ROOT / 'data/legal_research'
    target.mkdir(parents=True, exist_ok=True)
    raw_target = ROOT / '.local/corpus/raw'
    raw_target.mkdir(parents=True, exist_ok=True)
    previous_path = target / 'manifest.json'
    previous = json.loads(previous_path.read_text(encoding='utf-8')).get('sources', []) if previous_path.exists() else []
    records = {record['id']: record for record in previous}
    async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False) as client:
        for source in manifest['sources']:
            if source_ids and source['id'] not in source_ids:
                continue
            record = {**source, 'retrieved_at': datetime.now(timezone.utc).isoformat()}
            try:
                raw, content_type, final_url, _ = await _download(client, source['url'])
                pages, media = _extract(raw, content_type, source)
                sha = hashlib.sha256(raw).hexdigest()
                ext = 'pdf' if raw.startswith(b'%PDF') else 'html'
                raw_path = raw_target / (source['id'] + '-' + sha[:16] + '.' + ext)
                raw_path.write_bytes(raw)
                text_path = target / (source['id'] + '.txt')
                text = '\n\n'.join(('PDF PAGE ' + str(page) + '\n' if page else '') + body for page, body in pages)
                text_path.write_bytes(text.encode('utf-8'))
                record.update(status='downloaded', sha256=sha, text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                              raw_path=str(raw_path.relative_to(ROOT)).replace('\\','/'),
                              text_path=str(text_path.relative_to(ROOT)).replace('\\','/'),
                              page_count=len(pages), bytes=len(raw), final_url=final_url,
                              applicability_status='source_snapshot_not_case_outcome_prediction')
            except Exception as exc:
                record.update(status='failed', error=type(exc).__name__ + ': ' + str(exc))
            records[source['id']] = record
            print(source['id'], record['status'], record.get('bytes', 0))
    (target / 'manifest.json').write_text(json.dumps({'sources': list(records.values())}, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', action='append', help='Download only this configured source ID; preserve other snapshots.')
    asyncio.run(main(parser.parse_args().source))
