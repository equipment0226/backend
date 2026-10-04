import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from apps.api import corpus


def fixture_seed(source_id="TEST", court_id=None):
    return {"id": source_id, "title": "수집 테스트 문서", "url": "https://slb.scourt.go.kr/test.html", "source_type": "court_guide", "court_id": court_id, "required_text": ["개인회생 테스트 본문"]}


def fixture_html():
    return ("<html><head><title>제목</title></head><body><form><h1>개인회생 테스트 본문</h1><p>" + "이 문장은 네트워크 수집 검증을 위한 가상의 서류 안내 테스트입니다. " * 12 + "</p></form><script>숨길스크립트</script><div style='display: none'>숨길내용</div></body></html>").encode()


class CorpusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="debtoff-corpus-test-")
        self.path_patch = patch.object(corpus, "CORPUS_DIR", Path(self.temp.name))
        self.path_patch.start()
        self.client_type = httpx.AsyncClient

    def tearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()

    def client_factory(self, handler):
        return lambda **kwargs: self.client_type(**{**kwargs, "transport": httpx.MockTransport(handler)})

    async def test_downloaded_bytes_hash_and_real_text_are_retained(self):
        raw = fixture_html()
        async with self.client_type(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw, headers={"content-type": "text/html; charset=utf-8"}))) as client:
            source, chunks = await corpus._ingest_source(client, fixture_seed(), asyncio.Semaphore(1))
        self.assertEqual(source["status"], "collected")
        self.assertEqual(source["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual((Path(self.temp.name) / source["raw_file"]).read_bytes(), raw)
        self.assertIn("개인회생 테스트 본문", chunks[0]["text"])
        self.assertNotIn("숨길스크립트", chunks[0]["text"])
        self.assertNotIn("숨길내용", chunks[0]["text"])
        self.assertIsNone(source["effective_date"])
        self.assertEqual(source["applicability_status"], "not_verified")

    async def test_http_200_menu_shell_is_failed_not_searchable(self):
        raw = ("<html><body>저장 닫기 인쇄 파일형식" + " 메뉴 " * 100 + "</body></html>").encode()
        async with self.client_type(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw))) as client:
            source, chunks = await corpus._ingest_source(client, fixture_seed(), asyncio.Semaphore(1))
        self.assertEqual(source["status"], "failed")
        self.assertEqual(chunks, [])
        self.assertIn("본문 검증 실패", source["error"])

    async def test_redirect_to_private_or_unapproved_host_is_never_fetched(self):
        called = []
        def handler(request):
            called.append(str(request.url))
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8000/private"})
        async with self.client_type(transport=httpx.MockTransport(handler)) as client:
            source, chunks = await corpus._ingest_source(client, fixture_seed(), asyncio.Semaphore(1))
        self.assertEqual(len(called), 1)
        self.assertEqual(source["status"], "failed")
        self.assertEqual(chunks, [])

    async def test_failed_refresh_keeps_prior_snapshot_and_marks_stale(self):
        with patch.object(corpus, "seeds", return_value=[fixture_seed()]):
            with patch.object(corpus.httpx, "AsyncClient", self.client_factory(lambda request: httpx.Response(200, content=fixture_html()))):
                first = await corpus.ingest_all()
            signature = corpus.corpus_signature()
            self.assertEqual(first["stats"]["collected_count"], 1)
            with patch.object(corpus.httpx, "AsyncClient", self.client_factory(lambda request: httpx.Response(503))):
                second = await corpus.ingest_all()
            self.assertEqual(second["stats"]["stale_count"], 1)
            self.assertGreater(second["stats"]["chunk_count"], 0)
            self.assertNotEqual(signature, corpus.corpus_signature())
            self.assertEqual(second["sources"][0]["sha256"], first["sources"][0]["sha256"])
            self.assertIn("last_attempt_at", second["sources"][0])

    def test_search_respects_court_and_personal_procedure(self):
        chunks = []
        for source_id, court, scope, text in [
            ("SEOUL", "CT01", "personal_rehabilitation", "개인회생 급여소득자의 통장거래내역 서류"),
            ("BUSAN", "CT03", "personal_rehabilitation", "개인회생 급여소득자의 통장거래내역 서류"),
            ("NATIONAL", None, "personal_rehabilitation", "개인회생 급여소득자에 필요한 서류"),
            ("GENERAL", "CT01", "general_rehabilitation", "개인회생 검색어가 있더라도 일반회생은 별개 서류"),
        ]:
            chunks.append({"id": source_id + ":1", "source_id": source_id, "court_id": court, "document_scope": scope, "text": text})
        (Path(self.temp.name) / "snapshot.json").write_text(json.dumps({"sources": [], "chunks": chunks}), encoding="utf-8")
        results = corpus.search("개인회생 급여소득자 통장", court_id="CT01")
        self.assertEqual({r["source_id"] for r in results}, {"SEOUL", "NATIONAL"})
        self.assertEqual(corpus.search("zxqv9876"), [])

    def test_heading_date_is_not_legal_verification(self):
        value, status = corpus._effective_date([(None, "법률 [시행 2026. 10. 2.] 제589조 본문")])
        self.assertEqual(value, "2026-10-02")
        self.assertEqual(status, "heading_detected_not_legally_verified")

    def test_future_or_unassessed_rules_are_readable_but_not_current_strategy_context(self):
        chunks = [{'id': sid + ':1', 'source_id': sid, 'court_id': None, 'text': '개인회생 급여소득 기준',
                   'effective_date': effective, 'applicability_status': state} for sid, effective, state in
                  [('current', '2020-01-01', 'baseline_unchanged'), ('future', '2099-01-01', 'staged'),
                   ('unassessed', None, 'awaiting_rule_assessment')]]
        (Path(self.temp.name) / 'snapshot.json').write_text(json.dumps({'sources': [], 'chunks': chunks}), encoding='utf-8')
        self.assertEqual([s['source_id'] for s in corpus.search('개인회생 급여')], ['current'])

    def test_disallow_external_userinfo_and_non_https_sources(self):
        for value in ("http://www.law.go.kr/page", "https://evil-law.go.kr/", "https://www.law.go.kr.evil.test/", "https://secret@www.law.go.kr/", "https://localhost/", "https://www.gov.kr:8443/"):
            self.assertFalse(corpus._allowed(value), value)
        self.assertTrue(corpus._allowed("https://www.gov.kr/"))
        self.assertTrue(corpus._allowed("https://slb.scourt.go.kr/"))

    def test_current_verified_article_supersedes_old_alias_and_full_law_only_for_same_statute(self):
        configured = [
            {'id': 'FULL', 'source_type': 'statute'},
            {'id': 'ALIAS', 'source_type': 'statute', 'derive_from': 'FULL', 'article': 579},
            {'id': 'WATCH', 'source_type': 'statute', 'baseline_source_id': 'FULL', 'article': 579},
            {'id': 'OTHER_LAW', 'source_type': 'statute'},
            {'id': 'PRECEDENT', 'source_type': 'precedent'},
        ]
        sources = [{**s, 'title': s['id'], 'status': 'collected', 'effective_date': '2020-01-01',
                    'applicability_status': 'not_verified'} for s in configured]
        sources[2].update(effective_date='2026-01-01', applicability_status='watch_scalar_validated')
        chunks = []
        for sid, suffix, locator, text in [
            ('FULL', 'old', '제579조(정의) · 단락 10', '개인회생 무담보 채무한도 예전 10억원'),
            ('ALIAS', 'old', '제579조(정의) · 단락 2', '개인회생 무담보 채무한도 예전 10억원'),
            ('WATCH', 'new', '제579조(정의) · 단락 2', '개인회생 무담보 채무한도 개정 12억원'),
            ('FULL', 'other', '제614조(인가요건) · 단락 15', '개인회생 인가요건에서 제579조 채무한도와 구별할 요건'),
            ('FULL', 'subarticle', '제579조의2(별도조문) · 단락 11', '개인회생 무담보 채무한도 별도 조문'),
            ('OTHER_LAW', 'unrelated', '제579조(다른법률) · 단락 1', '개인회생 무담보 채무한도와 관련한 다른 법률'),
            ('PRECEDENT', 'past', '제579조 참조', '개인회생 무담보 채무한도에 관한 과거 판례'),
        ]:
            source = next(s for s in sources if s['id'] == sid)
            chunks.append({'id': sid + ':' + suffix, 'source_id': sid, 'court_id': None,
                           'source_type': source['source_type'], 'effective_date': source['effective_date'],
                           'locator': locator, 'text': text})
        (Path(self.temp.name) / 'snapshot.json').write_text(json.dumps({'sources': sources, 'chunks': chunks}), encoding='utf-8')
        with patch.object(corpus, 'seeds', return_value=configured):
            results = {r['id'] for r in corpus.search('개인회생 무담보 채무한도', limit=20)}
            self.assertNotIn('FULL:old', results)
            self.assertNotIn('ALIAS:old', results)
            self.assertTrue({'WATCH:new', 'FULL:other', 'FULL:subarticle', 'OTHER_LAW:unrelated', 'PRECEDENT:past'} <= results)
            # Supersession is a retrieval rule, never deletion of the old source.
            self.assertEqual(len(corpus.source_detail('ALIAS')['chunks']), 1)

    def test_future_or_unreviewed_watched_edition_does_not_suppress_last_applicable_article(self):
        configured = [{'id': 'FULL', 'source_type': 'statute'},
                      {'id': 'WATCH', 'source_type': 'statute', 'article': 579, 'baseline_source_id': 'FULL'}]
        for effective, status in [('2099-01-01', 'staged'), ('2026-01-01', 'review_required')]:
            with self.subTest(status=status):
                sources = [{**configured[0], 'status': 'collected', 'effective_date': '2020-01-01'},
                           {**configured[1], 'status': 'collected', 'effective_date': effective, 'applicability_status': status}]
                chunks = [{'id': source['id'] + ':1', 'source_id': source['id'], 'court_id': None,
                           'source_type': 'statute', 'locator': '제579조(정의)',
                           'effective_date': source['effective_date'], 'text': '개인회생 채무한도'} for source in sources]
                (Path(self.temp.name) / 'snapshot.json').write_text(json.dumps({'sources': sources, 'chunks': chunks}), encoding='utf-8')
                with patch.object(corpus, 'seeds', return_value=configured):
                    self.assertEqual([r['id'] for r in corpus.search('개인회생 채무한도')], ['FULL:1'])

    def test_same_effective_date_verified_substantive_correction_overrides_pinned_baseline(self):
        configured = [{'id': 'FULL', 'source_type': 'statute'},
                      {'id': 'WATCH', 'source_type': 'statute', 'article': 579, 'baseline_source_id': 'FULL',
                       'baseline_semantic_sha256': 'old-reviewed-body'}]
        sources = [{**s, 'status': 'collected', 'effective_date': '2026-01-01'} for s in configured]
        sources[1].update(applicability_status='watch_scalar_validated', watch_semantic_sha256='verified-corrected-body')
        chunks = [{'id': s['id'] + ':1', 'source_id': s['id'], 'court_id': None, 'source_type': 'statute',
                   'locator': '제579조(정의)', 'effective_date': s['effective_date'], 'text': '개인회생 채무한도'} for s in sources]
        (Path(self.temp.name) / 'snapshot.json').write_text(json.dumps({'sources': sources, 'chunks': chunks}), encoding='utf-8')
        with patch.object(corpus, 'seeds', return_value=configured):
            self.assertEqual([r['id'] for r in corpus.search('개인회생 채무한도')], ['WATCH:1'])

    async def test_article_aliases_share_real_full_law_fetch_and_reader_has_complete_body(self):
        header = '채무자 회생 및 파산에 관한 법률 (약칭: 채무자회생법)\n[시행 2026. 10. 2.]'
        first = '제579조(정의)\n' + '개인회생채무자의 소득과 채무한도를 증빙으로 확인한다. ' * 16
        second = '제611조(변제계획의 내용)\n' + '변제기간과 계획의 수행가능성을 확인한다. ' * 16
        raw = ('<html><body><div>' + (header + '\n' + first + '\n' + second).replace('\n', '</div><div>') + '</div></body></html>').encode()
        base = {**fixture_seed('FULL'), 'source_type': 'statute', 'required_text': ['제579조(']}
        aliases = [{**base, 'id': 'ARTICLE579', 'derive_from': 'FULL', 'article': 579},
                   {**base, 'id': 'ARTICLE611', 'derive_from': 'FULL', 'article': 611, 'required_text': ['제611조(']}]
        called = []
        def response(request):
            called.append(str(request.url))
            return httpx.Response(200, content=raw)
        with patch.object(corpus, 'seeds', return_value=[base, *aliases]):
            with patch.object(corpus.httpx, 'AsyncClient', self.client_factory(response)):
                await corpus.ingest_all()
            self.assertEqual(len(called), 1)
            article = corpus.source_detail('ARTICLE579')
            self.assertEqual(article['body_status'], 'available')
            self.assertIn(first.strip(), article['text'])
            self.assertNotIn('제611조', article['text'])
            self.assertEqual(article['effective_date'], '2026-10-02')
            self.assertEqual(article['pages'][0]['text'], article['text'])
            self.assertEqual(article['parent_source_id'], 'FULL')
            # Reindex preserves the actual retrieval time and does no HTTP request.
            await corpus.ingest_all(['ARTICLE579'], saved_only=True)
            self.assertEqual(corpus.source_detail('ARTICLE579')['fetched_at'], article['fetched_at'])

    def test_short_official_jurisdiction_guide_requires_all_registered_body_markers(self):
        seed = {**fixture_seed('J02'), 'required_text': ['관할구역', '수원시', '오산시', '군포시'],
                'require_all_text': True, 'min_body_chars': 80}
        guide = '관할구역\n수원시 오산시 군포시\n' + '추가 관할 안내 내용입니다. ' * 6
        pages, _ = corpus._extract(guide.encode(), 'text/html; charset=utf-8', seed)
        self.assertIn('수원시', pages[0][1])
        with self.assertRaises(ValueError):
            corpus._extract(guide.replace('오산시', '').encode(), 'text/html; charset=utf-8', seed)

    async def test_targeted_refresh_preserves_other_sources_and_rejects_unknown_id(self):
        seeds = [fixture_seed('A'), fixture_seed('B')]
        with patch.object(corpus, 'seeds', return_value=seeds):
            with patch.object(corpus.httpx, 'AsyncClient', self.client_factory(lambda request: httpx.Response(200, content=fixture_html()))):
                await corpus.ingest_all()
                before = corpus.source_detail('B')['fetched_at']
                await corpus.ingest_all(['A'])
                self.assertEqual(corpus.source_detail('B')['fetched_at'], before)
                with self.assertRaises(ValueError):
                    await corpus.ingest_all(['unregistered'])


if __name__ == "__main__":
    unittest.main()
