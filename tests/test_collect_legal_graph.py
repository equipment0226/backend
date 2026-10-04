"""Official law shells are resolved without executing or trusting arbitrary JS."""
import copy
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from scripts import collect_legal_graph as collector
from apps.api import legal_knowledge_graph as graph


SHELL_URL = 'https://www.law.go.kr/LSW/LsiJoLinkP.do?joNo=058900000'
EDITION_URL = 'https://www.law.go.kr/LSW/lsInfoR.do?lsiSeq=290631&efYd=20261002&chrClsCd=010202'
SHELL = ('<html>채무자 회생 및 파산에 관한 법률'
         '<input id="lsiSeq" value="290631">'
         '<script>var efYd = "20261002";</script></html>').encode()


class CollectLegalGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_dynamic_shell_resolves_official_edition_and_extracts_requested_article(self):
        download = AsyncMock(side_effect=[(SHELL, 'text/html', SHELL_URL, 200),
                                         (b'official-law-body', 'text/html', EDITION_URL, 200)])
        with patch.object(collector.corpus, '_download', download), patch.object(collector.corpus, '_extract', return_value=([(None, 'article body')], 'text/html')) as extract:
            result = await collector._download_public_body(None, {'url': SHELL_URL}, {'article': 589}, {})
        self.assertEqual([call.args[1] for call in download.await_args_list], [SHELL_URL, EDITION_URL])
        extract.assert_called_once_with(b'official-law-body', 'text/html', {'article': 589})
        self.assertEqual(result[2], EDITION_URL)

    async def test_multiple_articles_share_downloaded_edition_bytes(self):
        second_url = SHELL_URL.replace('058900000', '059500000')
        async def response(client, url):
            return (b'body' if url == EDITION_URL else SHELL, 'text/html', url, 200)
        download = AsyncMock(side_effect=response)
        cache = {}
        with patch.object(collector.corpus, '_download', download), patch.object(collector.corpus, '_extract', return_value=([(None, 'article')], 'text/html')):
            await collector._download_public_body(None, {'url': SHELL_URL}, {'article': 589}, cache)
            await collector._download_public_body(None, {'url': second_url}, {'article': 595}, cache)
        self.assertEqual([call.args[1] for call in download.await_args_list].count(EDITION_URL), 1)
        self.assertEqual(download.await_count, 3)

    async def test_arbitrary_script_destination_is_not_followed(self):
        raw = b'<script>window.location="https://example.invalid/private"</script>'
        download = AsyncMock(return_value=(raw, 'text/html', SHELL_URL, 200))
        with patch.object(collector.corpus, '_download', download), patch.object(collector.corpus, '_extract', side_effect=ValueError('missing legal body')):
            with self.assertRaises(ValueError):
                await collector._download_public_body(None, {'url': SHELL_URL}, {'article': 589}, {})
        self.assertEqual(download.await_count, 1)

    async def test_changed_article_is_saved_but_interpretation_stays_on_hold(self):
        original = graph.load()
        source = copy.deepcopy(next(s for s in original['sources'] if s['id'] == 'LW589'))
        node = copy.deepcopy(next(n for n in original['nodes'] if n['id'] == 'KG589'))
        changed_body = source['body'] + '\n추가되는 실질 요건에 대한 시험 문장.'
        fixture = {'sources': [source], 'nodes': [node]}
        download = AsyncMock(return_value=(b'changed', 'text/html', EDITION_URL, [(None, changed_body)]))
        with patch.object(collector, '_download_public_body', download):
            changed = await collector.refresh(fixture, set())
        self.assertEqual(changed, ['LW589'])
        self.assertEqual(node['status'], 'source_changed_requires_review')
        self.assertNotEqual(source['body_sha256'], node['bound_body_sha256'])
        self.assertEqual(source['previous_verified_snapshot']['body_sha256'], node['bound_body_sha256'])
        self.assertNotIn('추가되는 실질 요건', source['previous_verified_snapshot']['body'])
        self.assertEqual(source['refresh_status'], 'downloaded')

    async def test_failed_refresh_preserves_verified_body_and_records_failure(self):
        source = copy.deepcopy(next(s for s in graph.load()['sources'] if s['id'] == 'LW589'))
        before = source['body']
        fixture = {'sources': [source], 'nodes': []}
        with patch.object(collector, '_download_public_body', AsyncMock(side_effect=httpx.ConnectError('connection unavailable'))):
            changed = await collector.refresh(fixture, set())
        self.assertEqual(changed, [])
        self.assertEqual(source['body'], before)
        self.assertEqual(source['body_sha256'], graph._hash(before))
        self.assertEqual(source['refresh_status'], 'failed_previous_snapshot_preserved')


if __name__ == '__main__':
    unittest.main()
