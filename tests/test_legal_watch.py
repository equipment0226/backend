"""Offline HTTP/clock tests for legal-change activation and hard daily bounds."""
import copy
import hashlib
import json
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from apps.api import corpus, legal_watch, store

ARTICLE_579 = '''제579조(용어의 정의) 이 절차에서 사용하는 용어의 정의는 다음과 같다.
1. “개인채무자”라 함은 파산의 원인인 사실이 있거나 그러한 사실이 생길 염려가 있는 자로서 개인회생절차개시의 신청 당시 다음 각목의 금액 이하의 채무를 부담하는 급여소득자 또는 영업소득자를 말한다.
가. 유치권ㆍ질권ㆍ저당권ㆍ양도담보권ㆍ가등기담보권ㆍ전세권 또는 우선특권으로 담보된 개인회생채권은 15억원
나. 가목 외의 개인회생채권은 10억원
2. “급여소득자”라 함은 급여ㆍ연금 그 밖에 이와 유사한 정기적이고 확실한 수입을 얻을 가능성이 있는 개인을 말한다.
3. “영업소득자”라 함은 부동산임대소득ㆍ사업소득ㆍ농업소득ㆍ임업소득 그 밖에 이와 유사한 수입을 장래에 계속적으로 또는 반복하여 얻을 가능성이 있는 개인을 말한다.'''
ARTICLE_611 = '''제611조(변제계획의 내용) ①변제계획에는 다음 각호의 사항을 정하여야 한다.
1. 채무변제에 제공되는 재산 및 소득에 관한 사항
2. 개인회생재단채권 및 일반의 우선권 있는 개인회생채권의 전액의 변제에 관한 사항
⑤변제계획에서 정하는 변제기간은 변제개시일부터 3년을 초과하여서는 아니된다. 다만, 제614조제1항제4호의 요건을 충족하기 위하여 필요한 경우 등 특별한 사정이 있는 때에는 변제개시일부터 5년을 초과하지 아니하는 범위에서 변제기간을 정할 수 있다.
⑥법원은 필요한 경우 변제계획의 이행을 위하여 인적ㆍ물적 담보를 제공하게 할 수 있다.'''


def html(body=ARTICLE_579, effective='2026. 10. 2.', banner=''):
    return f'<html><head><meta charset="utf-8"></head><body><nav>{banner}</nav><h1>채무자 회생 및 파산에 관한 법률</h1><p>[시행 {effective}]</p><div>{body}</div><p>파일형식 선택</p><footer>{banner}</footer></body></html>'.encode()


def source(source_id, article, body, court_id=None):
    values, shape = legal_watch._controlled_values(body, f'statute_{article}')
    return {'id': source_id, 'title': f'테스트 제{article}조', 'url': f'https://www.law.go.kr/{source_id}',
            'court_id': court_id, 'source_type': 'statute', 'article': article,
            'adapter': f'statute_{article}', 'required_text': [f'제{article}조('],
            'approved_shape_sha256': shape, 'baseline_values': values, 'baseline_effective_date': '2026-10-02',
            'baseline_semantic_sha256': hashlib.sha256(legal_watch._normalized(body).encode()).hexdigest()}


class LegalWatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / 'policy.json'
        self.seeds = [source('LW579', 579, ARTICLE_579), source('LW611', 611, ARTICLE_611)]
        self.config = {'enabled': True, 'interval_hours': 24, 'max_sources': 20,
                       'max_http_requests_per_day': 40, 'max_attempts_per_source': 2,
                       'source_ids': ['LW579'], 'sources': self.seeds}
        self.save_config()
        self.responses, self.calls = [], []
        self.client_type = httpx.AsyncClient
        self.patches = [patch.object(store, 'DATA_DIR', self.root / 'local'),
                        patch.object(corpus, 'CORPUS_DIR', self.root / 'corpus'),
                        patch.object(corpus, 'seeds', return_value=self.seeds),
                        patch.object(legal_watch, 'POLICY_PATH', self.config_path),
                        patch.object(legal_watch.httpx, 'AsyncClient', side_effect=self.client)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False), encoding='utf-8')

    def client(self, **kwargs):
        return self.client_type(**kwargs, transport=httpx.MockTransport(self.respond))

    def respond(self, request):
        self.calls.append(request)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def response(self, body=ARTICLE_579, effective='2026. 10. 2.', **headers):
        return httpx.Response(200, content=html(body, effective), headers={'content-type': 'text/html; charset=utf-8', **headers})

    async def run_day(self, day=3, force=False):
        return await legal_watch.run_once(force=force, as_of=f'2026-10-{day:02d}T09:00:00+09:00')

    async def baseline(self):
        self.responses.append(self.response(etag='"v1"', **{'last-modified': 'Sat, 03 Oct 2026 00:00:00 GMT'}))
        return await self.run_day()

    async def test_baseline_download_indexes_and_persists_official_source(self):
        result = await self.baseline()
        self.assertEqual(result['sources'][0]['state'], 'baseline')
        self.assertEqual(result['budget']['http_requests'], 1)
        self.assertEqual(legal_watch.active_overlays()['calculator']['unsecured_debt_limit'], 1_000_000_000)
        self.assertEqual(corpus.list_sources()['sources'][0]['status'], 'collected')
        self.assertTrue((store.DATA_DIR / 'legal_watch' / 'active_overlays.json').is_file())
        self.assertEqual(result['sources'][1]['enabled'], False)

    async def test_conditional_get_304_does_not_change_dependency_signature(self):
        await self.baseline()
        signature = legal_watch.dependencies_signature({'court_id': 'CT01'})
        self.responses.append(httpx.Response(304))
        result = await self.run_day(4)
        self.assertEqual(self.calls[-1].headers['if-none-match'], '"v1"')
        self.assertIn('if-modified-since', self.calls[-1].headers)
        self.assertEqual(result['sources'][0]['state'], 'unchanged')
        self.assertEqual(legal_watch.dependencies_signature({'court_id': 'CT01'}), signature)

    async def test_whitespace_and_web_banner_do_not_change_legal_hash(self):
        await self.baseline()
        old = legal_watch.dependencies_signature({'court_id': 'CT01'})
        self.responses.append(httpx.Response(200, content=html(ARTICLE_579.replace(' ', '  '), banner='오늘 방문자999명')))
        result = await self.run_day(4)
        self.assertEqual(result['changes'], [])
        self.assertEqual(legal_watch.dependencies_signature({'court_id': 'CT01'}), old)

    async def test_known_numeric_change_updates_only_whitelisted_policy_values(self):
        await self.baseline()
        self.responses.append(self.response(ARTICLE_579.replace('10억원', '12억원'), '2026. 10. 4.'))
        result = await self.run_day(4)
        self.assertEqual(result['changes'][0]['state'], 'active')
        overlay = legal_watch.active_overlays(as_of='2026-10-04')
        self.assertEqual(overlay['calculator']['unsecured_debt_limit'], 1_200_000_000)
        self.assertLessEqual(set(overlay['calculator']), legal_watch.SCALARS)
        self.assertEqual(overlay['effective_date'], '2026-10-04')
        self.assertEqual(legal_watch.active_overlays(as_of='2026-10-03')['calculator']['unsecured_debt_limit'], 1_000_000_000)

    async def test_unknown_nonnumeric_legal_change_holds_existing_good_policy(self):
        await self.baseline()
        self.responses.append(self.response(ARTICLE_579.replace('급여소득자 또는 영업소득자', '급여소득자'), '2026. 10. 4.'))
        result = await self.run_day(4)
        self.assertEqual(result['status'], 'review_required')
        self.assertEqual(legal_watch.active_overlays()['calculator']['unsecured_debt_limit'], 1_000_000_000)
        self.assertTrue(legal_watch.case_policy_status({'court_id': 'CT01'}, as_of='2026-10-04')['pending_changes'])

    async def test_changed_numeric_context_is_not_promoted_even_with_valid_numbers(self):
        await self.baseline()
        self.responses.append(self.response(ARTICLE_579.replace('10억원', '20억원').replace('이하의 채무', '미만의 채무')))
        await self.run_day(4)
        self.assertEqual(legal_watch.status()['status'], 'review_required')
        self.assertEqual(legal_watch.active_overlays()['calculator']['unsecured_debt_limit'], 1_000_000_000)

    async def test_future_numeric_update_stages_then_activates_even_when_fetch_fails(self):
        await self.baseline()
        self.responses.append(self.response(ARTICLE_579.replace('10억원', '12억원'), '2026. 10. 6.'))
        result = await self.run_day(4)
        self.assertEqual(result['sources'][0]['state'], 'staged')
        self.assertFalse(legal_watch.case_policy_status({'court_id': 'CT01'}, as_of='2026-10-04')['pending_changes'])
        self.assertEqual(legal_watch.active_overlays(as_of='2026-10-04')['calculator']['unsecured_debt_limit'], 1_000_000_000)
        self.responses.extend([httpx.Response(503), httpx.Response(503)])
        await self.run_day(6)
        self.assertEqual(legal_watch.active_overlays(as_of='2026-10-06')['calculator']['unsecured_debt_limit'], 1_200_000_000)
        self.assertEqual(legal_watch.status(as_of='2026-10-06')['status'], 'warning')

    async def test_future_unknown_update_holds_only_after_effective_day(self):
        await self.baseline()
        self.responses.append(self.response(ARTICLE_579.replace('이하의 채무', '미만의 채무'), '2026. 10. 6.'))
        await self.run_day(4)
        self.assertFalse(legal_watch.case_policy_status({'court_id': 'CT01'}, as_of='2026-10-04')['pending_changes'])
        self.responses.append(httpx.Response(304))
        await self.run_day(6)
        self.assertTrue(legal_watch.case_policy_status({'court_id': 'CT01'}, as_of='2026-10-06')['pending_changes'])

    async def test_older_filed_case_requires_transition_review_new_cases_use_change(self):
        await self.baseline()
        self.responses.append(self.response(ARTICLE_579.replace('10억원', '12억원'), '2026. 10. 4.'))
        await self.run_day(4)
        self.assertTrue(legal_watch.case_policy_status({'court_id': 'CT01', 'application_date': '2026-10-03'}, as_of='2026-10-05')['pending_changes'])
        self.assertFalse(legal_watch.case_policy_status({'court_id': 'CT01', 'application_date': '2026-10-05'}, as_of='2026-10-05')['pending_changes'])

    async def test_network_failures_are_bounded_and_retain_last_good(self):
        await self.baseline()
        before = legal_watch.dependencies_signature({'court_id': 'CT01'})
        self.responses.extend([httpx.Response(503), httpx.Response(503)])
        result = await self.run_day(4)
        self.assertEqual(result['budget']['http_requests'], 2)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(legal_watch.dependencies_signature({'court_id': 'CT01'}), before)
        self.assertEqual(result['sources'][0]['state'], 'failed')

    async def test_daily_source_cap_persists_and_force_never_bypasses_it(self):
        self.config.update(max_sources=1, source_ids=['LW579', 'LW611'])
        self.save_config()
        await self.baseline()
        await self.run_day(force=True)
        self.assertEqual(len(self.calls), 1)
        self.responses.append(self.response(ARTICLE_611))
        result = await self.run_day(4)
        self.assertEqual(result['budget']['checked_ids'], ['LW611'])

    async def test_daily_http_cap_counts_retries(self):
        self.config.update(max_http_requests_per_day=1)
        self.save_config()
        self.responses.append(httpx.Response(503))
        result = await self.run_day()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['budget']['http_requests'], 1)
        self.assertEqual(result['sources'][0]['state'], 'failed')

    async def test_redirect_cannot_fetch_untrusted_or_local_host(self):
        self.responses.append(httpx.Response(302, headers={'location': 'http://127.0.0.1/admin'}))
        result = await self.run_day()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['sources'][0]['state'], 'failed')
        self.assertIn('차단', result['sources'][0]['error'])

    async def test_legal_api_shape_error_does_not_replace_index_or_active_values(self):
        await self.baseline()
        old = copy.deepcopy(corpus._snapshot())
        self.responses.append(httpx.Response(200, content=b'<html><p>login required</p></html>'))
        result = await self.run_day(4)
        self.assertEqual(result['sources'][0]['state'], 'failed')
        self.assertEqual(corpus._snapshot(), old)

    async def test_court_change_does_not_invalidate_unrelated_court_case(self):
        self.seeds[0]['court_id'] = 'CT01'
        self.save_config()
        await self.baseline()
        seoul = legal_watch.dependencies_signature({'court_id': 'CT01'})
        busan = legal_watch.dependencies_signature({'court_id': 'CT03'})
        self.responses.append(self.response(ARTICLE_579.replace('이하의 채무', '미만의 채무')))
        await self.run_day(4)
        self.assertNotEqual(legal_watch.dependencies_signature({'court_id': 'CT01'}), seoul)
        self.assertEqual(legal_watch.dependencies_signature({'court_id': 'CT03'}), busan)
        self.assertFalse(legal_watch.case_policy_status({'court_id': 'CT03'})['pending_changes'])

    async def test_disabled_setting_prevents_network_and_manual_force(self):
        legal_watch.configure({'enabled': False})
        self.assertEqual((await self.run_day(force=True))['status'], 'disabled')
        self.assertFalse(self.calls)

    async def test_not_due_skips_network(self):
        await self.baseline()
        self.assertEqual((await self.run_day())['status'], 'not_due')
        self.assertEqual(len(self.calls), 1)

    async def test_configuration_validation_rejects_unregistered_sources_and_unbounded_rates(self):
        for update in ({'interval_hours': 23}, {'interval_hours': 721}, {'max_sources': 21},
                       {'max_sources': True}, {'source_ids': ['private-api']}, {'source_ids': []},
                       {'url': 'https://attacker.test'}, {'enabled': 'true'}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                legal_watch.configure(update)
        value = legal_watch.configure({'source_ids': ['LW611'], 'interval_hours': 48, 'max_sources': 2})
        self.assertEqual(value['source_ids'], ['LW611'])
        self.assertEqual(value['interval_hours'], 48)

    async def test_611_exact_adapter_updates_months_from_years(self):
        self.config['source_ids'] = ['LW611']
        self.save_config()
        self.responses.append(self.response(ARTICLE_611))
        await self.run_day()
        self.responses.append(self.response(ARTICLE_611.replace('3년', '4년')))
        await self.run_day(4)
        self.assertEqual(legal_watch.active_overlays()['calculator']['normal_max_months'], 48)
        self.assertEqual(legal_watch.active_overlays()['calculator']['exception_max_months'], 60)

    async def test_official_link_shell_resolves_edition_and_effective_date_under_same_budget(self):
        self.seeds[0]['url'] = 'https://www.law.go.kr/LSW/LsiJoLinkP.do?joNo=057900000'
        self.save_config()
        shell = '<title>채무자 회생 및 파산에 관한 법률</title><script>var efYd = "20261002";</script><input type="hidden" id="lsiSeq" value="290631" />'
        self.responses.extend([httpx.Response(200, content=shell.encode()), self.response()])
        result = await self.run_day()
        self.assertEqual(result['sources'][0]['state'], 'baseline')
        self.assertEqual(result['budget']['http_requests'], 2)
        self.assertEqual(self.calls[1].url.params['efYd'], '20261002')
        self.assertEqual(self.calls[1].url.params['lsiSeq'], '290631')

    async def test_historical_edition_regression_cannot_downgrade_active_limits(self):
        await self.baseline()
        self.responses.append(self.response(ARTICLE_579.replace('10억원', '12억원'), '2026. 10. 4.'))
        await self.run_day(4)
        self.responses.append(self.response())
        result = await self.run_day(5)
        self.assertEqual(result['status'], 'review_required')
        self.assertEqual(legal_watch.active_overlays(as_of='2026-10-05')['calculator']['unsecured_debt_limit'], 1_200_000_000)
        self.assertIn('과거', result['review_required'][0]['reason'])

    async def test_manual_corpus_collection_cannot_bypass_changed_rule_gate(self):
        await self.baseline()
        snapshot = copy.deepcopy(corpus._snapshot())
        record = snapshot['sources'][0]
        raw = html(ARTICLE_579.replace('이하의 채무', '미만의 채무'))
        (corpus.CORPUS_DIR / record['raw_file']).write_bytes(raw)
        record['sha256'] = hashlib.sha256(raw).hexdigest()
        legal_watch._write(corpus.CORPUS_DIR / 'snapshot.json', snapshot)
        pending = legal_watch.case_policy_status({'court_id': 'CT01'})['pending_changes']
        self.assertEqual(pending[0]['code'], 'SOURCE_ASSESSMENT_REQUIRED')
        count = len(self.calls)
        result = await legal_watch.assess_collected_sources(as_of='2026-10-04')
        self.assertEqual(result['assessed_source_ids'], ['LW579'])
        self.assertEqual(result['status'], 'review_required')
        self.assertEqual(len(self.calls), count)
        restored = corpus._snapshot()['sources'][0]
        self.assertNotEqual(restored['sha256'], record['sha256'])

    async def test_manual_future_ingestion_restores_active_search_body(self):
        await self.baseline()
        snapshot = copy.deepcopy(corpus._snapshot())
        record = snapshot['sources'][0]
        original_sha = record['sha256']
        raw = html(ARTICLE_579.replace('10억원', '12억원'), '2027. 1. 1.')
        record['raw_file'] = 'raw/future.html'
        (corpus.CORPUS_DIR / record['raw_file']).write_bytes(raw)
        record['sha256'] = hashlib.sha256(raw).hexdigest()
        legal_watch._write(corpus.CORPUS_DIR / 'snapshot.json', snapshot)
        result = await legal_watch.assess_collected_sources(as_of='2026-10-03')
        self.assertTrue(result['staged'])
        self.assertEqual(corpus._snapshot()['sources'][0]['sha256'], original_sha)
        self.assertFalse(legal_watch.case_policy_status({'court_id': 'CT01'})['pending_changes'])

    async def test_checkpoint_import_revalidates_bytes_ignores_untrusted_rule_fields_and_keeps_audit(self):
        await self.baseline()
        origin = store.DATA_DIR / 'legal_watch'
        diagnostic = self.root / 'diagnostic'
        shutil.copytree(origin, diagnostic)
        state = json.loads((diagnostic / 'state.json').read_text(encoding='utf-8'))
        state['sources']['LW579']['active']['calculator']['unsecured_debt_limit'] = 99
        legal_watch._write(diagnostic / 'state.json', state)
        with patch.object(store, 'DATA_DIR', self.root / 'production'):
            prior = legal_watch._state()
            prior['budget'] = {'date': '2026-10-03', 'checked_ids': ['LW579'], 'http_requests': 2}
            prior['runs'] = [{'kind': 'earlier_failure', 'http_requests': 2}]
            legal_watch._write(legal_watch._directory() / 'state.json', prior)
            result = await legal_watch.import_verified_checkpoint(diagnostic, as_of='2026-10-03')
            self.assertEqual(result['imported_source_ids'], ['LW579'])
            self.assertEqual(result['budget']['http_requests'], 3)
            self.assertEqual(result['runs'][0]['kind'], 'earlier_failure')
            self.assertEqual(legal_watch.active_overlays()['calculator']['unsecured_debt_limit'], 1_000_000_000)
            repeat = await legal_watch.import_verified_checkpoint(diagnostic, as_of='2026-10-03')
            self.assertEqual(repeat['status'], 'already_imported')
            self.assertEqual(repeat['budget']['http_requests'], 3)

    async def test_checkpoint_import_refuses_to_erase_daily_limit(self):
        await self.baseline()
        diagnostic = self.root / 'diagnostic'
        shutil.copytree(store.DATA_DIR / 'legal_watch', diagnostic)
        state = legal_watch._state()
        state['budget']['http_requests'] = 40
        legal_watch._write(legal_watch._directory() / 'state.json', state)
        with self.assertRaises(ValueError):
            await legal_watch.import_verified_checkpoint(diagnostic, as_of='2026-10-03')

    async def test_tampered_corpus_snapshot_adds_integrity_review_hold(self):
        await self.baseline()
        record = corpus._snapshot()['sources'][0]
        (corpus.CORPUS_DIR / record['raw_file']).write_bytes(b'changed after download')
        result = await legal_watch.assess_collected_sources(as_of='2026-10-03')
        self.assertEqual(len(result['assessment_errors']), 1)
        self.assertTrue(result['review_required'])


if __name__ == '__main__':
    unittest.main()
