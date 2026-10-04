"""Downloaded official Korean legal sources; no case facts leave this module.

Raw bytes and a replaceable search snapshot live outside version control. An HTTP
200 menu, script shell or error page never counts as a collected legal source.
Retrieval is deterministic BM25 with Korean bigrams, not a model or legal ruling.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pymupdf

ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = Path(os.getenv("DEBTOFF_CORPUS_DIR", str(ROOT / ".local/corpus")))
MANIFEST_PATH = ROOT / "data/legal_sources_seed.json"
AX_MANIFEST_PATH = ROOT / "data/legal_ax_sources.json"
RESEARCH_MANIFEST_PATH = ROOT / "data/legal_research/manifest.json"
WATCH_MANIFEST_PATH = ROOT / "data/legal_watch_policy.json"
_cache = {}
_cache_lock = threading.RLock()
_ingest_lock = threading.Lock()
MAX_SOURCE_BYTES = 30 * 1024 * 1024
EXTRACTOR_VERSION = 'official-text-v3'
ALLOWED_SUFFIXES = (".scourt.go.kr", ".law.go.kr", ".easylaw.go.kr", ".gov.kr")


def _now():
    return datetime.now(timezone.utc).isoformat()


def seeds():
    registry = json.loads((ROOT / "data/registry.json").read_text(encoding="utf-8"))
    known = {s["id"]: s for s in registry["sources"]}
    configured = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["sources"]
    if AX_MANIFEST_PATH.exists():
        configured += json.loads(AX_MANIFEST_PATH.read_text(encoding="utf-8"))["sources"]
    if WATCH_MANIFEST_PATH.exists():
        configured += json.loads(WATCH_MANIFEST_PATH.read_text(encoding="utf-8"))["sources"]
    return list({s["id"]: {**known.get(s["id"], {}), **s} for s in configured}.values())


def _allowed(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (parsed.scheme == "https" and parsed.port in (None, 443)
            and not parsed.username and not parsed.password
            and any(host == suffix[1:] or host.endswith(suffix) for suffix in ALLOWED_SUFFIXES))


class _VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        style = re.sub(r"\s", "", attrs.get("style", "")).lower()
        skip = tag in {"script", "style", "noscript", "nav", "header", "footer", "head", "button", "select"} or "display:none" in style or "hidden" in attrs
        if tag not in {"br", "hr", "img", "input", "meta", "link", "wbr", "source", "area", "embed", "param"}:
            self.stack.append((tag, skip))
            self.ignored += int(skip)
        if not self.ignored and tag in {"br", "p", "div", "li", "tr", "td", "h1", "h2", "h3", "h4", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        # HTML in legacy court pages can be unbalanced; recover to the matching tag.
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                self.ignored -= sum(int(skip) for _, skip in self.stack[i:])
                del self.stack[i:]
                break
        if not self.ignored and tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def _html_text(raw, content_type=""):
    declared = re.search(rb'charset\s*=\s*["\']?([a-zA-Z0-9_-]+)', raw[:8192])
    header_charset = re.search(r"charset=([^;\s]+)", content_type)
    charset = declared.group(1).decode("ascii") if declared else (header_charset.group(1) if header_charset else "utf-8")
    try:
        text = raw.decode(charset)
    except (LookupError, UnicodeDecodeError):
        text = raw.decode("cp949", errors="replace")
    parser = _VisibleHTML()
    parser.feed(text)
    return "\n".join(line for line in (re.sub(r"[\t\r \u00a0]+", " ", s).strip() for s in "".join(parser.parts).splitlines()) if line)


def _segments(text, max_chars=1100):
    # Preserve paragraphs and bounded context; split long paragraphs with overlap.
    bucket = []
    size = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if size + len(line) + 1 > max_chars and bucket:
            yield "\n".join(bucket)
            bucket, size = [], 0
        while len(line) > max_chars:
            yield line[:max_chars]
            line = line[max_chars - 140:]
        bucket.append(line)
        size += len(line) + 1
    if bucket:
        yield "\n".join(bucket)


def _extract(raw, content_type, seed):
    pages = []
    if raw.startswith(b"%PDF"):
        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            for index, page in enumerate(doc):
                if page.rect.width > page.rect.height:
                    # Suwon publishes two facing printed pages on one PDF page.
                    w, h = page.rect.width, page.rect.height
                    text = "\n".join(page.get_text("text", clip=clip, sort=True) for clip in [pymupdf.Rect(0, 0, w / 2, h), pymupdf.Rect(w / 2, 0, w, h)]).strip()
                else:
                    text = page.get_text("text", sort=True).strip()
                if text:
                    pages.append((index + 1, text))
        media_type = "application/pdf"
    else:
        text = _html_text(raw, content_type)
        start = seed.get("start_after")
        if start and start in text:
            text = text[text.index(start):]
        pages.append((None, text))
        media_type = "text/html"
    if type(seed.get('article')) is int:
        full = '\n'.join(text for _, text in pages)
        matches = list(re.finditer(rf'(?m)^\s*제\s*{seed["article"]}\s*조\s*\(', full))
        if not matches:
            raise ValueError('공식 원문에서 요청한 조문을 찾지 못했습니다.')
        body = full[matches[0].start():]
        end = re.search(r'(?m)^\s*(?:제\s*\d+\s*조(?:의\s*\d+)?\s*\(|부칙)', body[1:])
        if end:
            body = body[:end.start() + 1]
        heading = '\n'.join(line for line in full[:matches[0].start()].splitlines()
                            if '[시행' in line or '법률 (' in line)
        pages = [(None, (heading + '\n' + body.strip()).strip())]
    joined = "\n".join(text for _, text in pages)
    normalized = re.sub(r"\s+", "", joined)
    if joined.count('\ufffd')>max(5,len(joined)//1000):
        raise ValueError('문자 인코딩 손상이 있어 법률 검색 자료로 등록하지 않습니다.')
    required = [re.sub(r'\s+', '', phrase) in normalized for phrase in seed.get('required_text', ['개인회생'])]
    minimum = max(80, min(250, seed.get('min_body_chars', 250)))
    if len(joined) < minimum or not (all(required) if seed.get('require_all_text') else any(required)):
        raise ValueError("본문 검증 실패: 필요한 법률 본문이 없거나 짧습니다. 메뉴·스크립트 화면은 검색자료로 등록하지 않습니다.")
    return pages, media_type


async def _download(client, url):
    # Validate every redirect, so even a compromised manifest cannot fetch local URLs.
    for _ in range(6):
        if not _allowed(url):
            raise ValueError("수집 대상은 허용된 공식 HTTPS 주소만 가능합니다.")
        async with client.stream("GET", url) as response:
            if response.is_redirect:
                url = str(response.url.join(response.headers["location"]))
                continue
            response.raise_for_status()
            if int(response.headers.get("content-length", "0")) > MAX_SOURCE_BYTES:
                raise ValueError("원문 크기 제한(30MB)을 초과합니다.")
            data = bytearray()
            async for block in response.aiter_bytes():
                data.extend(block)
                if len(data) > MAX_SOURCE_BYTES:
                    raise ValueError("원문 크기 제한(30MB)을 초과합니다.")
            return bytes(data), response.headers.get("content-type", ""), str(response.url), response.status_code
    raise ValueError("리다이렉트 횟수가 너무 많습니다.")


def _effective_date(pages):
    # A heading's date is evidence about that edition, never a claim of currency.
    first = "\n".join(text for _, text in pages[:2])
    match = re.search(r"\[\s*시행\s*(\d{4})[.년\s-]+(\d{1,2})[.월\s-]+(\d{1,2})", first)
    if match:
        return "-".join([match[1], match[2].zfill(2), match[3].zfill(2)]), "heading_detected_not_legally_verified"
    return None, "unverified"


def _saved_research_source(seed):
    """Reuse only matching, hash-pinned public bytes; never a caller file path."""
    if not RESEARCH_MANIFEST_PATH.exists():
        return None
    manifest = json.loads(RESEARCH_MANIFEST_PATH.read_text(encoding='utf-8'))
    record = next((r for r in manifest['sources'] if r['id'] == seed['id']), None)
    if not record or record.get('status') != 'downloaded' or record.get('url') != seed['url']:
        return None
    if not _allowed(seed['url']) or not _allowed(record.get('final_url', seed['url'])):
        raise ValueError('저장 원문의 공식 URL 검증에 실패했습니다.')
    path = (ROOT / record['raw_path']).resolve()
    allowed = (ROOT / '.local/corpus/raw').resolve()
    if not path.is_relative_to(allowed) or not path.is_file():
        return None
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError('저장 원문 크기 제한을 초과했습니다.')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != record.get('sha256'):
        raise ValueError('저장 원문 SHA256 불일치: 재수집이 필요합니다.')
    return {'raw': raw, 'content_type': 'application/pdf' if raw.startswith(b'%PDF') else 'text/html',
            'url': record.get('final_url', seed['url']), 'fetched_at': record['retrieved_at']}


async def _ingest_source(client, seed, semaphore, saved=None):
    source = {k: seed.get(k) for k in ("id", "title", "url", "court_id", "source_type")}
    source.update(status="failed", fetched_at=_now(), effective_date=None, effective_date_status="unverified", applicability_status="not_verified", chunk_count=0, char_count=0, bytes=0, error=None, sha256=None)
    source["limitation"] = "수집한 원문 스냅샷입니다. 현행성·개정 반영·개별 사건 적용 여부는 별도 검토해야 합니다."
    async with semaphore:
        try:
            if saved is None:
                raw, content_type, final_url, http_status = await _download(client, seed["url"])
            else:
                raw, content_type, final_url, http_status = saved['raw'], saved['content_type'], saved['url'], 200
                source.update(fetched_at=saved['fetched_at'], reindexed_at=_now(), from_saved_snapshot=True)
            sha = hashlib.sha256(raw).hexdigest()
            extension = "pdf" if raw.startswith(b"%PDF") else "html"
            raw_path = CORPUS_DIR / "raw" / f"{seed['id']}-{sha[:16]}.{extension}"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(raw)
            source.update(sha256=sha, bytes=len(raw), final_url=final_url, http_status=http_status, raw_file=str(raw_path.relative_to(CORPUS_DIR)))
            pages, media_type = await asyncio.to_thread(_extract, raw, content_type, seed)
            effective_date, effective_date_status = _effective_date(pages)
            source.update(effective_date=effective_date, effective_date_status=effective_date_status, media_type=media_type, page_count=len(pages), char_count=sum(len(text) for _, text in pages),extractor_version=EXTRACTOR_VERSION,extraction_sha256=hashlib.sha256('\n'.join(text for _,text in pages).encode()).hexdigest())
            text_path = CORPUS_DIR / 'text' / f'{seed["id"]}-{source["extraction_sha256"][:16]}.json'
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(json.dumps([{'page': page, 'text': text} for page, text in pages], ensure_ascii=False), encoding='utf-8')
            source['text_file'] = str(text_path.relative_to(CORPUS_DIR))
            if seed.get('derive_from'):
                source.update(parent_source_id=seed['derive_from'], article=seed['article'],
                              collection_method='article_from_official_full_law')
            chunks = []
            scope = seed.get('document_scope', 'personal_rehabilitation' if seed['source_type'] == 'precedent' else 'unspecified')
            for page, text in pages:
                if seed["source_type"] == "court_rule":
                    part = re.search(r"(?m)^제\s*([1-6])\d\d\s*호", text[:200])
                    if part:
                        scope = {"1":"common", "2":"general_rehabilitation", "3":"bankruptcy", "4":"personal_rehabilitation", "5":"international_insolvency", "6":"common"}[part[1]]
                # Keep statutory articles apart so a citation has a real article locator.
                sections = re.split(r"(?=^제\d+조(?:의\d+)?\s*\()", text, flags=re.M) if seed["source_type"] == "statute" else [text]
                paragraphs = [part for section_text in sections for part in _segments(section_text)]
                article_locator = None
                for section, paragraph in enumerate(paragraphs, 1):
                    locator = f"PDF {page}쪽 · 단락 {section}" if page else f"본문 단락 {section}"
                    if seed["source_type"] == "statute":
                        article = re.match(r"제(\d+)조(?:의\d+)?\s*\([^)]+\)", paragraph)
                        if article:
                            article_locator = article[0]
                            no = int(article[1])
                            scope = "personal_rehabilitation" if 579 <= no <= 627 else "bankruptcy" if 294 <= no <= 578 else "general_rehabilitation" if 34 <= no <= 293 else "common"
                        if article_locator:
                            locator = article_locator + f" · 단락 {section}"
                    chunks.append({"id": f"{seed['id']}:{sha[:12]}:{page or 0}:{section}", "source_id": seed["id"], "title": seed["title"], "text": paragraph, "url": final_url + (f"#page={page}" if page else ""), "page": page, "locator": locator, "court_id": seed.get("court_id"), "source_type": seed["source_type"], "document_scope": scope, "sha256": sha, "content_hash": sha, "fetched_at": source["fetched_at"], "retrieved_at": source["fetched_at"], "effective_date": effective_date, "effective_date_status": effective_date_status, "applicability_status": "not_verified"})
            source.update(status="collected", chunk_count=len(chunks), last_success_at=source["fetched_at"])
            return source, chunks
        except Exception as exc:
            # No credentials or case details are included in error metadata.
            source["error"] = str(exc)[:450]
            if isinstance(exc, httpx.HTTPStatusError):
                source["http_status"] = exc.response.status_code
            return source, []


def _saved_snapshot_source(source):
    path = (CORPUS_DIR / source.get('raw_file', '')).resolve()
    if not path.is_relative_to((CORPUS_DIR / 'raw').resolve()) or not path.is_file():
        raise ValueError('저장 원문 경로가 유효하지 않습니다.')
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES or hashlib.sha256(raw).hexdigest() != source.get('sha256'):
        raise ValueError('저장 원문 해시 검증에 실패했습니다.')
    return {'raw': raw, 'content_type': source.get('media_type', ''),
            'url': source.get('final_url') or source['url'], 'fetched_at': source['fetched_at']}


async def ingest_all(source_ids=None, saved_only=False):
    """Fetch only manifest URLs; atomic snapshot commit; retain prior good snapshots."""
    if not _ingest_lock.acquire(blocking=False):
        return {"status": "already_running", **list_sources()}
    try:
        before = _snapshot()
        prior_sources = {s["id"]: s for s in before["sources"]}
        semaphore = asyncio.Semaphore(4)
        catalog = seeds()
        selected = [seed for seed in catalog if source_ids is None or seed['id'] in source_ids]
        if source_ids is not None and set(source_ids) - {s['id'] for s in catalog}:
            raise ValueError('등록된 공식 출처만 수집할 수 있습니다.')
        # Article aliases share one full-law fetch, instead of repeatedly downloading
        # menu-only popup URLs or fabricating article text from their descriptions.
        parents = {seed['derive_from'] for seed in selected if seed.get('derive_from')}
        primary = [seed for seed in catalog if not seed.get('derive_from') and
                   (seed['id'] in {s['id'] for s in selected} or seed['id'] in parents)]
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15), follow_redirects=False, trust_env=False,
                headers={"User-Agent": "Debtoff-Research/0.3 (public legal document retrieval)", "Accept": "text/html,application/pdf"}) as client:
            async def collect(seed):
                if saved_only:
                    old = prior_sources.get(seed['id'])
                    if not old or not old.get('raw_file'):
                        return None
                    try:
                        saved = _saved_snapshot_source(old)
                    except ValueError:
                        return None
                    return await _ingest_source(None, seed, semaphore, saved=saved)
                return await _ingest_source(client, seed, semaphore)
            results = [result for result in await asyncio.gather(*(collect(seed) for seed in primary)) if result]
            current = dict(prior_sources)
            for source, _ in results:
                if source['status'] == 'collected':
                    current[source['id']] = source
                elif source['id'] in prior_sources:
                    current[source['id']] = {**prior_sources[source['id']], 'status': 'stale', 'error': source.get('error')}
            for seed in selected:
                if not seed.get('derive_from'):
                    continue
                parent = current.get(seed['derive_from'])
                if not parent or parent.get('status') not in ('collected', 'stale'):
                    continue
                source, parts = await _ingest_source(None, seed, semaphore, saved=_saved_snapshot_source(parent))
                if parent.get('status') == 'stale' and source['status'] == 'collected':
                    source.update(status='stale', error=parent.get('error'))
                results.append((source, parts))
        changed_ids = {source['id'] for source, _ in results}
        sources = [s for s in before['sources'] if s['id'] not in changed_ids]
        chunks = [c for c in before['chunks'] if c['source_id'] not in changed_ids]
        for source, source_chunks in results:
            previous = prior_sources.get(source["id"])
            if source["status"] == "failed" and previous and previous.get("status") in ("collected", "stale"):
                error, attempted_at = source["error"], source["fetched_at"]
                source = {**previous, "status": "stale", "error": error, "last_attempt_at": attempted_at}
                source_chunks = [c for c in before["chunks"] if c["source_id"] == source["id"]]
            sources.append(source)
            chunks.extend(source_chunks)
        snapshot = {"schema_version": 1, "ingested_at": _now(), "sources": sources, "chunks": chunks}
        CORPUS_DIR.mkdir(parents=True, exist_ok=True)
        temporary = CORPUS_DIR / "snapshot.tmp"
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, CORPUS_DIR / "snapshot.json")
        return {"status": "completed", **list_sources()}
    finally:
        _ingest_lock.release()


async def ingest_local_sources():
    """Index newly collected public artifacts without fetching existing sources.

    Original retrieval timestamps are retained. A failed or missing artifact does
    not erase an existing good snapshot. This is not a network freshness check.
    """
    if not _ingest_lock.acquire(blocking=False):
        return {'status': 'already_running', **list_sources()}
    try:
        before = _snapshot()
        sources = {s['id']: s for s in before['sources']}
        chunks = list(before['chunks'])
        imported, failures = [], []
        for seed in seeds():
            try:
                saved = _saved_research_source(seed)
                if saved is None:
                    continue
                source, records = await _ingest_source(None, seed, asyncio.Semaphore(1), saved=saved)
                if source['status'] != 'collected':
                    failures.append({'id': seed['id'], 'error': source['error']})
                    continue
                sources[seed['id']] = source
                chunks = [c for c in chunks if c['source_id'] != seed['id']] + records
                imported.append({'id': seed['id'], 'chunks': len(records)})
            except (OSError, ValueError, KeyError) as exc:
                failures.append({'id': seed['id'], 'error': str(exc)[:250]})
        snapshot = {**before, 'schema_version': 1, 'ingested_at': _now(),
                    'sources': list(sources.values()), 'chunks': chunks}
        CORPUS_DIR.mkdir(parents=True, exist_ok=True)
        temporary = CORPUS_DIR / 'snapshot.local.tmp'
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, CORPUS_DIR / 'snapshot.json')
        return {'status': 'completed', 'imported': imported, 'failures': failures, **list_sources()}
    finally:
        _ingest_lock.release()


def _snapshot():
    path = CORPUS_DIR / "snapshot.json"
    if not path.exists():
        return {"sources": [], "chunks": [], "ingested_at": None}
    key = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
    with _cache_lock:
        if _cache.get("key") != key:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            frequencies = [Counter(_tokens(c["text"])) for c in snapshot["chunks"]]
            posting = defaultdict(list)
            for index, frequency in enumerate(frequencies):
                for term in frequency:
                    posting[term].append(index)
            _cache.clear()
            _cache.update(key=key, snapshot=snapshot, frequencies=frequencies, posting=posting, lengths=[sum(f.values()) for f in frequencies])
        return _cache["snapshot"]


def list_sources():
    snapshot = _snapshot()
    actual = {s["id"]: s for s in snapshot["sources"]}
    sources = [actual.get(s["id"], {**{k: s.get(k) for k in ("id", "title", "url", "court_id", "source_type")}, "status": "pending", "chunk_count": 0, "fetched_at": None, "effective_date": None, "effective_date_status": "unverified"}) for s in seeds()]
    chunks = snapshot["chunks"]
    stats = {"total_count": len(sources), "collected_count": sum(s["status"] == "collected" for s in sources), "failed_count": sum(s["status"] == "failed" for s in sources), "stale_count": sum(s["status"] == "stale" for s in sources), "pending_count": sum(s["status"] == "pending" for s in sources), "chunk_count": len(chunks), "char_count": sum(s.get("char_count", 0) for s in sources), "bytes": sum(s.get("bytes", 0) for s in sources), "last_fetched_at": snapshot.get("ingested_at"), "source_types": dict(Counter(s["source_type"] for s in sources if s["status"] in ("collected", "stale"))), "retrieval": "BM25 + Korean character bigrams", "legal_currency_verified": False}
    return {"sources": sources, "stats": stats}


def source_detail(source_id):
    source = next((s for s in list_sources()["sources"] if s["id"] == source_id), None)
    if source is None:
        return None
    chunks = [c for c in _snapshot()["chunks"] if c['source_id'] == source_id]
    pages = []
    if source.get('text_file'):
        path = (CORPUS_DIR / source['text_file']).resolve()
        if path.is_relative_to((CORPUS_DIR / 'text').resolve()) and path.is_file():
            candidate = json.loads(path.read_text(encoding='utf-8'))
            digest = hashlib.sha256('\n'.join(page['text'] for page in candidate).encode()).hexdigest()
            if digest == source.get('extraction_sha256'):
                pages = candidate
    text = '\n\n'.join(page['text'] for page in pages) if pages else '\n\n'.join(c['text'] for c in chunks)
    return {**source, 'chunks': chunks, 'pages': pages, 'text': text,
            'body_status': 'available' if text else 'unavailable'}


def corpus_signature(court_id=None):
    snapshot = _snapshot()
    # Same bytes on a refresh preserve previous agent evidence; changed/failed state invalidates it.
    payload = [(s["id"], s.get("sha256"), s.get('extraction_sha256'), s["status"]) for s in snapshot["sources"]
               if not court_id or not s.get('court_id') or s.get('court_id') == court_id]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _tokens(text):
    for token in re.findall(r"[가-힣]+|[a-z0-9]+", text.lower()):
        yield token
        if re.fullmatch(r"[가-힣]+", token) and len(token) > 2:
            for i in range(len(token) - 1):
                yield token[i:i+2]


def search(query, court_id=None, limit=6):
    """Court-specific material never crosses courts; national sources remain eligible."""
    query = (query or "").strip()[:1200]
    if not query or limit < 1:
        return []
    with _cache_lock:
        snapshot = _snapshot()
        if not snapshot["chunks"]:
            return []
        frequencies, posting, lengths = _cache["frequencies"], _cache["posting"], _cache["lengths"]
        chunks = snapshot["chunks"]
        today = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
        excluded = {'staged', 'review_required', 'awaiting_rule_assessment'}
        withheld = {source['id'] for source in snapshot['sources']
            if source.get('applicability_status') in excluded or (source.get('effective_date') or '') > today}
        source_map = {source['id']: source for source in snapshot['sources']}
        configured = {source['id']: source for source in seeds()}

        def statute_family(source_id):
            # Article aliases and watched current links share a registered full
            # statute. Article numbers alone must never conflate different laws.
            visited = set()
            while source_id not in visited:
                visited.add(source_id)
                seed = configured.get(source_id, {})
                if seed.get('statute_id'):
                    return seed['statute_id']
                parent = seed.get('derive_from') or seed.get('baseline_source_id')
                if not parent:
                    return source_id
                source_id = parent
            return source_id

        editions = {}
        for source in snapshot['sources']:
            seed = configured.get(source['id'], {})
            effective = source.get('effective_date')
            if (source['id'] in withheld or source.get('source_type') != 'statute'
                    or source.get('status') not in ('collected', 'stale')
                    or source.get('applicability_status') not in ('baseline_unchanged', 'watch_scalar_validated')
                    or type(seed.get('article')) is not int or not effective):
                continue
            key = (statute_family(source['id']), seed['article'])
            changed = bool(source.get('watch_semantic_sha256') and seed.get('baseline_semantic_sha256')
                           and source['watch_semantic_sha256'] != seed['baseline_semantic_sha256'])
            candidate = {'date': effective, 'id': source['id'], 'changed': changed}
            if key not in editions or (effective, changed) > (editions[key]['date'], editions[key]['changed']):
                editions[key] = candidate

        superseded = set()
        for index, chunk in enumerate(chunks):
            source = source_map.get(chunk['source_id'], {})
            if chunk.get('source_type', source.get('source_type')) != 'statute':
                continue
            seed = configured.get(chunk['source_id'], {})
            article = seed.get('article')
            if type(article) is not int:
                locator = re.match(r'제\s*(\d+)\s*조(?!\s*의)', chunk.get('locator', ''))
                if not locator:
                    continue
                article = int(locator[1])
            latest = editions.get((statute_family(chunk['source_id']), article))
            effective = chunk.get('effective_date') or source.get('effective_date')
            if (latest and chunk['source_id'] != latest['id']
                    and (not effective or effective < latest['date']
                         or (effective == latest['date'] and latest['changed']))):
                # Same-date substantive corrections also supersede the pinned
                # baseline; legacy source detail stays readable for its history.
                superseded.add(index)
        count = len(chunks)
        average = sum(lengths) / max(count, 1)
        scores = defaultdict(float)
        terms = set(_tokens(query))
        personal_only = "개인회생" in re.sub(r"\s+", "", query) and "파산" not in query
        for term in terms:
            indices = posting.get(term, [])
            weight = math.log(1 + (count - len(indices) + 0.5) / (len(indices) + 0.5))
            for index in indices:
                chunk = chunks[index]
                if index in superseded:
                    continue
                if chunk['source_id'] in withheld or chunk.get('applicability_status') in excluded or (chunk.get('effective_date') or '') > today:
                    continue
                if court_id and chunk["court_id"] not in (None, court_id):
                    continue
                if personal_only and chunk.get("document_scope") in ("general_rehabilitation", "international_insolvency", "bankruptcy"):
                    continue
                tf = frequencies[index][term]
                scores[index] += weight * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * lengths[index] / max(average, 1)))
        for index in scores:
            if court_id and chunks[index]["court_id"] == court_id:
                scores[index] *= 1.15
            if query in chunks[index]["text"]:
                scores[index] *= 1.25
        ranked = sorted(scores, key=lambda index: (-scores[index], chunks[index]["id"]))
        # Prevent a long rule book from monopolising the context window.
        result, per_source = [], Counter()
        for index in ranked:
            chunk = chunks[index]
            if per_source[chunk["source_id"]] >= max(2, math.ceil(limit / 2)):
                continue
            result.append({**chunk, "score": round(scores[index], 4)})
            per_source[chunk["source_id"]] += 1
            if len(result) >= min(limit, 30):
                break
        return result
