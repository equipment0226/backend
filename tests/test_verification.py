import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from apps.api import agent_engine, model_client, verification as verify
from apps.api import reasoning_cache
from apps.api import corpus


def reply(value, **extra):
    return {"done": True, "done_reason": "stop", "message": {"content": json.dumps(value, ensure_ascii=False)}, **extra}


def local_payload():
    return {"sources": [{"id": "doc:private-person", "version": 2, "text": "홍길동의 월 소득 280만원. 연락처 010-1234-5678."}],
            "items": [{"id": "income", "key": "monthly_income", "value": 2800000, "source_ids": ["doc:private-person"]}]}


def local_output(**changes):
    check = {"item_id": "income", "status": "supported", "source_id": "doc:private-person", "quote": "월 소득 280만원", "reason": "급여 월 소득의 기재와 값이 일치합니다."}
    check.update(changes)
    return {"checks": [check]}


class PrivacyBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = httpx.AsyncClient
        settings = tempfile.TemporaryDirectory()
        self.addCleanup(settings.cleanup)
        file_root = patch.object(model_client, "ROOT", Path(settings.name))
        file_root.start()
        self.addCleanup(file_root.stop)
        cache_file = patch.object(reasoning_cache, 'cache_path', return_value=Path(settings.name) / 'reasoning.sqlite3')
        cache_file.start()
        self.addCleanup(cache_file.stop)

    def transport(self, handler):
        return patch.object(model_client.httpx, "AsyncClient", side_effect=lambda **kw: self.client(**kw, transport=httpx.MockTransport(handler)))

    def test_explicit_user_key_file_has_priority_without_mutating_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(model_client, "ROOT", Path(directory)), patch.dict(os.environ, {}, clear=True):
            (Path(directory) / ".env.example").write_text("DEEPSEEK_API_KEY=example-test-key\nDEEPSEEK_MODEL=test-model\nUNRELATED_PASSWORD=not-loaded\n", encoding="utf-8")
            self.assertEqual(model_client.configured("DEEPSEEK_API_KEY"), "example-test-key")
            self.assertEqual(model_client.configured("UNRELATED_PASSWORD"), "")
            self.assertNotIn("DEEPSEEK_API_KEY", os.environ)
            (Path(directory) / ".env").write_text("DEEPSEEK_API_KEY=env-test-key\n", encoding="utf-8")
            self.assertEqual(model_client.configured("DEEPSEEK_API_KEY"), "env-test-key")
            os.environ["DEEPSEEK_API_KEY"] = "process-test-key"
            self.assertEqual(model_client.configured("DEEPSEEK_API_KEY"), "env-test-key")
            os.environ["DEEPSEEK_MODEL"] = "process-model"
            self.assertEqual(model_client.configured("DEEPSEEK_MODEL"), "process-model")
            (Path(directory) / ".env").write_text("DEEPSEEK_API_KEY=changeme\n", encoding="utf-8")
            self.assertEqual(model_client.configured("DEEPSEEK_API_KEY"), "example-test-key")

    def test_public_rag_is_hydrated_and_caller_cannot_inject_customer_text(self):
        payload = verify.build_safe_strategy_payload({"court_id": "CT01"}, {}, [{"id": "AXP01", "text": "홍비밀 010-5555-7777"}])
        source = next(ref for ref in payload["legal_references"] if ref.get("public_source_id") == "AXP01")
        self.assertIn("개인회생", source["excerpt"])
        self.assertNotIn("홍비밀", json.dumps(payload, ensure_ascii=False))
        source["excerpt"] += "홍비밀"
        with self.assertRaises(model_client.ModelClientError):
            verify.safe_strategy_messages(payload)

    def test_current_statute_chunk_is_rehydrated_from_registered_snapshot(self):
        source = {'id': 'L13', 'status': 'collected', 'title': '현행 법률', 'source_type': 'statute',
                  'url': 'https://www.law.go.kr/LSW/lsInfoR.do?lsiSeq=290631', 'sha256': 'public-file-hash',
                  'effective_date': '2026-10-02', 'chunks': [
                      {'id': 'L13:614', 'source_id': 'L13', 'text': '제614조의 현재 적용 법률 원문'}]}
        with patch.object(corpus, 'source_detail', side_effect=lambda source_id: source if source_id == 'L13' else None):
            payload = verify.build_safe_strategy_payload({'court_id': 'CT01'}, {}, [
                {'id': 'statute-579'}, {'id': 'statute-611'}, {'id': 'statute-614'},
                {'id': 'L13:614', 'source_id': 'L13', 'text': 'PRIVATE_CUSTOMER_DO_NOT_SEND'}])
            current = next(ref for ref in payload['legal_references'] if ref.get('public_chunk_id') == 'L13:614')
            self.assertEqual(current['excerpt'], '제614조의 현재 적용 법률 원문')
            self.assertEqual(current['effective_date'], '2026-10-02')
            self.assertNotIn('PRIVATE_CUSTOMER_DO_NOT_SEND', json.dumps(payload))
            verify.safe_strategy_messages(payload)
            source['chunks'][0]['text'] = '변경된 법률'
            with self.assertRaises(model_client.ModelClientError):
                verify.safe_strategy_messages(payload)

    async def test_raw_default_forces_local_even_with_external_preference(self):
        seen = []
        def handler(request):
            seen.append(request)
            self.assertEqual(request.url.host, "127.0.0.1")
            self.assertNotIn("external-secret", request.headers.get("Authorization", ""))
            self.assertIn("홍길동", request.content.decode())
            return httpx.Response(200, json=reply({"ok": True}))
        with patch.dict(os.environ, {"DEBTOFF_MODEL_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "external-secret", "OLLAMA_BASE_URL": "http://127.0.0.1:11434"}), self.transport(handler):
            result = await model_client.generate([{"role": "user", "content": "홍길동"}], {})
        self.assertFalse(result["external_processing"])
        self.assertEqual(len(seen), 1)

    async def test_public_https_ollama_is_rejected_before_transport(self):
        for endpoint in ("https://private-llama.example.com", "http://192.0.2.1:11434", "http://localhost.example.com:11434", "https://127.0.0.1@outside.example"):
            with self.subTest(endpoint=endpoint), patch.dict(os.environ, {"OLLAMA_BASE_URL": endpoint}), patch.object(model_client.httpx, "AsyncClient") as client:
                with self.assertRaises(ValueError):
                    await model_client.generate([{"role": "user", "content": "민감 정보"}], {})
                client.assert_not_called()

    def test_legacy_route_has_same_endpoint_policy(self):
        with patch.dict(os.environ, {"OLLAMA_BASE_URL": "https://outside.example"}):
            with self.assertRaises(ValueError):
                agent_engine._endpoint()

    def test_structured_payload_omits_all_identifiers_and_free_text(self):
        secret = "김비공개"
        case = {"client_name": secret, "id": secret, "court_id": "CT01", "case_type": "personal_rehabilitation",
                "address": "서울시 비공개로 123", "phone": "010-1234-5678", "account": "123-456-7890",
                "facts": [{"key": "monthly_income", "value": 2800000}, {"key": "employer", "value": secret}, {"key": "household_size", "value": "01012345678"}],
                "employment_type": secret, "summary": secret, "messages": [{"text": secret}]}
        calc = {"summary": {"net_monthly_income": 2800000, "name": secret}, "inputs": case,
                "blockers": [{"code": "MISSING_EVIDENCE", "message": secret}]}
        payload = verify.build_safe_strategy_payload(case, calc, [{"id": secret, "article": 614, "text": secret}])
        encoded = json.dumps(payload, ensure_ascii=False)
        for private in (secret, "서울시", "01012345678", "010-1234-5678", "123-456-7890"):
            self.assertNotIn(private, encoded)
        self.assertEqual(payload["numeric_facts"], {"monthly_income": 2800000})
        self.assertEqual(payload["legal_references"][0]["ref"], "LAW1")
        self.assertEqual(len(verify.safe_strategy_messages(payload)), 2)

    async def test_boundary_rebuilds_both_messages_and_schema(self):
        payload = verify.build_safe_strategy_payload({"monthly_income": 2800000}, {"monthly_payment": 900000}, [{"article": 614}])
        def handler(request):
            data = json.loads(request.content)
            self.assertEqual(request.url.host, "api.deepseek.com")
            self.assertNotIn("DO NOT TRANSMIT PRIVATE NAME", request.content.decode())
            self.assertEqual(data["thinking"], {"type": "enabled"})
            self.assertEqual(data["reasoning_effort"], "high")
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}", "reasoning_content": "NEVER STORE THIS"}}]})
        with patch.dict(os.environ, {"DEBTOFF_REASONING_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "synthetic-key"}), self.transport(handler):
            result = await model_client.generate([{"role": "user", "content": "DO NOT TRANSMIT PRIVATE NAME"}], {"description": "DO NOT TRANSMIT PRIVATE NAME"}, task_role="strategy_verification", structured_payload=payload)
        self.assertNotIn("NEVER STORE THIS", str(result))

    async def test_unsanitized_external_payload_and_numeric_strings_rejected(self):
        original = verify.build_safe_strategy_payload({"monthly_income": 2800000})
        for modify in (lambda p: p.update(client_name="홍길동"), lambda p: p["case_features"].update(employment_type="홍길동"), lambda p: p["numeric_facts"].update(monthly_income="01012345678"), lambda p: p["legal_references"].append({"ref": "홍길동", "law": "debtor_rehabilitation_act", "articles": [614]})):
            payload = copy.deepcopy(original)
            modify(payload)
            with patch.dict(os.environ, {"DEBTOFF_REASONING_PROVIDER": "deepseek"}), patch.object(model_client.httpx, "AsyncClient") as client:
                with self.assertRaises(model_client.ModelClientError):
                    await model_client.generate([], {}, task_role="strategy_verification", structured_payload=payload)
                client.assert_not_called()

    async def test_external_auth_failure_does_not_expose_credentials(self):
        def handler(request):
            return httpx.Response(401, json={"error": "synthetic-secret"})
        with patch.dict(os.environ, {"DEBTOFF_REASONING_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "synthetic-secret"}), self.transport(handler):
            result = await verify.run_strategy_verification({"monthly_income": 2800000}, {"monthly_payment": 900000}, [{"article": 614}])
        self.assertEqual(result["error"]["code"], "MODEL_AUTH_FAILED")
        self.assertFalse(result["passed"])
        self.assertNotIn("synthetic-secret", json.dumps(result))


class EvidenceLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        verify.clear_local_verification_cache()
        self.addCleanup(verify.clear_local_verification_cache)

    async def test_successful_batch_cache_reuses_only_exact_sources_items_and_model(self):
        with patch.object(model_client, 'generate', new=AsyncMock(return_value=reply(local_output()))) as generate:
            first = await verify.run_local_verification_batched('ocr', local_payload())
            second = await verify.run_local_verification_batched('ocr', local_payload())
            self.assertTrue(first['passed'] and second['passed'])
            self.assertEqual(second['cached_batch_count'], 1)
            self.assertEqual(generate.await_count, 1)
            changed = local_payload()
            changed['sources'][0]['version'] += 1
            await verify.run_local_verification_batched('ocr', changed)
            with patch.dict(os.environ, {'OLLAMA_MODEL': 'different-local-model'}):
                await verify.run_local_verification_batched('ocr', local_payload())
            self.assertEqual(generate.await_count, 3)
            second['findings'][0]['quote'] = 'tampered caller result'
            cached = await verify.run_local_verification_batched('ocr', local_payload())
            self.assertNotEqual(cached['findings'][0]['quote'], 'tampered caller result')

    async def test_failed_or_unavailable_checks_are_never_cached(self):
        for output in [reply(local_output(status='mismatch')), reply(local_output(), done_reason='length')]:
            with patch.object(model_client, 'generate', new=AsyncMock(return_value=output)) as generate:
                for _ in range(2):
                    result = await verify.run_local_verification_batched('ocr', local_payload())
                    self.assertFalse(result['passed'])
                self.assertEqual(generate.await_count, 2)

    async def test_cancelled_run_retains_only_completed_successful_batches(self):
        payload = local_payload()
        payload['sources'].append({'id': 'second', 'text': '채무 500만원'})
        for source in payload['sources']: source['text'] += '\n' + '원문 ' * 2100
        payload['items'].append({'id': 'debt', 'key': 'total_debt', 'value': 5000000, 'source_ids': ['second']})
        second = {'checks': [{'item_id': 'debt', 'status': 'supported', 'source_id': 'second', 'quote': '채무 500만원', 'reason': '금액과 항목 일치'}]}
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=[reply(local_output()), asyncio.CancelledError()])) as generate:
            with self.assertRaises(asyncio.CancelledError):
                await verify.run_local_verification_batched('ocr', payload)
            self.assertEqual(generate.await_count, 2)
        with patch.object(model_client, 'generate', new=AsyncMock(return_value=reply(second))) as generate:
            result = await verify.run_local_verification_batched('ocr', payload)
            self.assertTrue(result['passed'])
            self.assertEqual(result['cached_batch_count'], 1)
            self.assertEqual(generate.await_count, 1)

    async def test_timeout_stops_remaining_batches_with_honest_incomplete_progress(self):
        payload = local_payload()
        payload['sources'].append({'id': 'second', 'text': '채무 500만원'})
        for source in payload['sources']: source['text'] += '\n' + '원문 ' * 2100
        payload['items'].append({'id': 'debt', 'key': 'total_debt', 'value': 5000000, 'source_ids': ['second']})
        updates = []
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=asyncio.TimeoutError())) as generate:
            result = await verify.run_local_verification_batched('ocr', payload, updates.append)
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['checked_item_ids'], [])
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(result['unattempted_batch_count'], 1)
        self.assertEqual((updates[-1]['completed'], updates[-1]['total']), (1, 2))

    async def test_semantic_mismatch_does_not_skip_other_sources(self):
        payload = local_payload()
        payload['sources'].append({'id': 'second', 'text': '채무 500만원'})
        for source in payload['sources']: source['text'] += '\n' + '원문 ' * 2100
        payload['items'].append({'id': 'debt', 'key': 'total_debt', 'value': 5000000, 'source_ids': ['second']})
        second = {'checks': [{'item_id': 'debt', 'status': 'supported', 'source_id': 'second', 'quote': '채무 500만원', 'reason': '금액과 항목 일치'}]}
        with patch.object(model_client, 'generate', new=AsyncMock(side_effect=[reply(local_output(status='mismatch')), reply(second)])) as generate:
            result = await verify.run_local_verification_batched('ocr', payload)
        self.assertEqual(result['status'], 'needs_review')
        self.assertFalse(result['passed'])
        self.assertEqual(generate.await_count, 2)
        self.assertEqual(set(result['checked_item_ids']), {'income', 'debt'})
        self.assertEqual(result['unattempted_batch_count'], 0)

    async def test_multiple_documents_are_batched_without_losing_item_coverage(self):
        payload = {"sources": [{"id": "a", "text": "월 소득 280만원. " + "원문 " * 2100}, {"id": "b", "text": "채무 500만원. " + "다른 원문 " * 1300}],
                   "items": [{"id": "income", "key": "monthly_income", "value": 2800000, "source_ids": ["a"]}, {"id": "debt", "key": "total_debt", "value": 5000000, "source_ids": ["b"]}]}
        def answer(messages, schema, **kwargs):
            sent = json.loads(messages[1]["content"])
            item = sent["items"][0]
            return reply({"checks": [{"item_id": item["id"], "status": "supported", "source_id": item["source_ids"][0], "quote": "월 소득 280만원" if item["id"] == "income" else "채무 500만원", "reason": "원문 수치와 일치합니다."}]})
        updates = []
        with patch.object(model_client, "generate", new=AsyncMock(side_effect=answer)) as generate:
            result = await verify.run_local_verification_batched("ocr", payload, updates.append)
        self.assertTrue(result["passed"])
        self.assertEqual(result["batch_count"], 2)
        self.assertEqual(set(result["checked_item_ids"]), {"income", "debt"})
        self.assertEqual(updates[-1]["completed"], 2)
        self.assertEqual(generate.await_count, 2)

    async def test_complete_source_grounded_review_passes_with_provenance(self):
        with patch.object(model_client, "generate", new=AsyncMock(return_value=reply(local_output()))) as generate:
            result = await verify.run_local_verification("ocr", local_payload())
        self.assertTrue(result["passed"])
        self.assertEqual(result["source_refs"][0]["version"], 2)
        self.assertEqual(generate.call_args.kwargs["task_role"], "ocr")
        review_schema = generate.call_args.args[1]
        self.assertEqual(review_schema['properties']['checks']['minItems'], 1)
        self.assertEqual(review_schema['properties']['checks']['maxItems'], 1)
        self.assertEqual(len(result["input_sha256"]), 64)

    async def test_document_quote_choices_are_bound_to_the_original_source(self):
        payload = local_payload()
        payload['sources'].append({'id': 'calculation', 'kind': 'code_calculation', 'text': '{"months": 36}'})
        with patch.object(model_client, 'generate', new=AsyncMock(return_value=reply(local_output()))) as generate:
            result = await verify.run_local_verification('document', payload)
        self.assertTrue(result['passed'])
        schema = generate.call_args.args[1]
        variants = schema['properties']['checks']['items']['anyOf']
        originals = {source['id']: source['text'] for source in payload['sources']}
        for variant in variants:
            properties = variant['properties']
            source_id = properties['source_id'].get('const')
            if source_id:
                self.assertTrue(all(not quote or quote in originals[source_id]
                                    for quote in properties['quote']['enum']))
            else:
                self.assertNotIn('supported', properties['status']['enum'])

    async def test_unsupported_quote_number_and_source_never_pass(self):
        for changes in ({"quote": "없는 문구"}, {"source_id": "other-case"}, {"quote": "홍길동"}, {"status": "mismatch"}):
            with self.subTest(changes=changes), patch.object(model_client, "generate", new=AsyncMock(return_value=reply(local_output(**changes)))):
                result = await verify.run_local_verification("ocr", local_payload())
            self.assertFalse(result["passed"])

    async def test_missing_or_duplicate_item_coverage_blocks(self):
        payload = local_payload()
        payload["items"].append({"id": "other", "key": "total_debt", "value": 50000000})
        for output in (local_output(), {"checks": local_output()["checks"] * 2}):
            with patch.object(model_client, "generate", new=AsyncMock(return_value=reply(output))):
                result = await verify.run_local_verification("ocr", payload)
            self.assertFalse(result["passed"])
            self.assertEqual(result["error"]["code"], "INCOMPLETE_VERIFICATION_COVERAGE")

    async def test_document_verification_requires_every_prose_section(self):
        payload = local_payload()
        payload["draft"] = {"content": "소득은 월 280만원이며 재산은 없습니다."}
        payload["items"].append({"id": "section", "key": "draft_section", "value": payload["draft"]["content"]})
        with patch.object(model_client, "generate", new=AsyncMock(return_value=reply(local_output()))):
            result = await verify.run_local_verification("document", payload)
        self.assertFalse(result["passed"])

    async def test_unavailable_timeout_truncation_empty_and_oversize_fail_closed(self):
        for exc in (asyncio.TimeoutError(), httpx.ConnectError("unavailable")):
            with patch.object(model_client, "generate", new=AsyncMock(side_effect=exc)):
                result = await verify.run_local_verification("ocr", local_payload())
            self.assertEqual(result["status"], "unavailable")
            self.assertFalse(result["passed"])
        with patch.object(model_client, "generate", new=AsyncMock(return_value=reply(local_output(), done_reason="length"))):
            self.assertFalse((await verify.run_local_verification("ocr", local_payload()))["passed"])
        payload = local_payload()
        payload["sources"][0]["text"] *= 500
        with patch.object(model_client, "generate", new=AsyncMock()) as generate:
            result = await verify.run_local_verification("ocr", payload)
            empty = await verify.run_local_verification("ocr", {"sources": [], "items": []})
        generate.assert_not_called()
        self.assertFalse(empty["passed"])
        self.assertEqual(result["error"]["code"], "VERIFICATION_BATCH_REQUIRED")

    async def test_advanced_incomplete_evidence_cannot_pass(self):
        output = {"decision": "no_additional_risk_identified", "findings": []}
        with patch.object(model_client, "generate", new=AsyncMock(return_value=reply(output))):
            result = await verify.run_strategy_verification({"monthly_income": 1000000})
        self.assertFalse(result["passed"])

    async def test_advanced_invented_legal_reference_cannot_pass(self):
        output = {"decision": "review_required", "findings": [{"code": "income", "severity": "review_required", "reason": "소득을 대조해야 합니다.", "strategy": "소득자료를 추가 제출합니다.", "source_refs": ["MADE_UP_CASE"]}]}
        with patch.object(model_client, "generate", new=AsyncMock(return_value=reply(output))):
            result = await verify.run_strategy_verification({"monthly_income": 1000000}, {"monthly_payment": 10000}, [{"article": 614}])
        self.assertFalse(result["passed"])
        self.assertEqual(result["error"]["code"], "UNSUPPORTED_LEGAL_CITATION")

    async def test_ocr_calculation_mapping_retains_unknowns_and_rejects_silent_legal_assumptions(self):
        evidence = [
            {"path": "income.monthly_amount", "value": 2800000, "source_id": "doc:private-person", "quote": "월 소득 280만원"},
            {"path": "monthly_trustee_fee", "value": 0, "source_id": "doc:private-person", "quote": "연락처 010-1234-5678"},
            {"path": "recognized_household_size", "value": 1, "source_id": "doc2", "quote": "가족 1명"},
            {"path": "creditors.0.principal", "value": 999999999, "source_id": "doc2", "quote": "가족 1명"},
        ]
        sources = local_payload()["sources"] + [{"id": "doc2", "text": "가족 1명"}]
        with patch.object(model_client, "generate", new=AsyncMock(return_value=reply({"evidence": evidence}))) as generate:
            result = await verify.extract_calculation_inputs(sources, {"policy_id": "kr-rehab-2026-v1", "as_of": "2026-10-03"})
        self.assertEqual(result["inputs"]["income"]["monthly_amount"], 2800000)
        self.assertIsNone(result["inputs"]["monthly_trustee_fee"])
        self.assertIsNone(result["inputs"]["objection"])
        self.assertIsNone(result["inputs"]["recognized_household_size"])
        self.assertEqual(result["inputs"]["creditors"], [])
        self.assertEqual(generate.call_args.kwargs["task_role"], "calculation_extraction")
        self.assertEqual(len(result["evidence"]), 1)

    async def test_large_mapping_preserves_conflicts_and_ambiguous_entities_as_unknown(self):
        sources = [{"id": "a", "text": "A" * 7000}, {"id": "b", "text": "B" * 7000}]
        def mapped(source, context):
            source_id = source[0]['id']
            value = 100 if source_id == 'a' else 200
            return {'status': 'extracted', 'inputs': {'income': {'monthly_amount': value},
                'assets': [], 'creditors': [{'id': 'creditors-0', 'name': 'same bank', 'kind': 'unsecured', 'principal': 900, 'interest': 0}]},
                'evidence': [{'path':'income.monthly_amount','value':value,'source_id':source_id,'quote':str(value)},
                             {'path':'creditors.0.principal','value':900,'source_id':source_id,'quote':'900'}],
                'errors': [], 'input_sha256': source_id}
        with patch.object(verify, '_extract_calculation_batch', new=AsyncMock(side_effect=mapped)):
            result = await verify.extract_calculation_inputs(sources)
        self.assertEqual(result['status'], 'needs_review')
        self.assertIsNone(result['inputs']['income']['monthly_amount'])
        self.assertEqual(len(result['inputs']['creditors']), 2)
        self.assertTrue(all(row['principal'] is None for row in result['inputs']['creditors']))
        self.assertFalse(result['evidence'])
        self.assertEqual({error['code'] for error in result['errors']}, {'CONFLICTING_DOCUMENT_VALUES', 'ENTITY_IDENTITY_AMBIGUOUS'})


if __name__ == "__main__":
    unittest.main()
