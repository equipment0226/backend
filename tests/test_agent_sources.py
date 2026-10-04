import asyncio
import copy
import json
import os
import unittest
from unittest.mock import patch

import httpx

from apps.api import agent_engine as agent
from apps.api import legal_sources as legal


def sample_case():
    return {"id": "test-case", "input_revision": 1, "documents": [{"id": "doc1", "version": 1, "status": "uploaded", "text": "월 소득 2,800,000원. 통장내역 미제출."}], "facts": [], "registry_controls": [{"id": "control1", "text": "소득 증빙은 담당자가 확인한다."}]}


def sample_output():
    return {"proposals": [{"title": "급여 확인", "observation": "월 소득 2,800,000원 기재의 기준을 확인해야 합니다.", "category": "extraction_candidate", "citations": [{"source_id": "doc:doc1", "quote": "월 소득 2,800,000원"}]}]}


class EvidenceTests(unittest.TestCase):
    def test_other_case_and_superseded_sources_are_not_retrieved(self):
        case = sample_case()
        case["documents"].append({"id": "old", "text": "과거 자료", "status": "superseded"})
        context = agent.build_context(case)
        self.assertNotIn("doc:old", [source["id"] for source in context["sources"]])
        self.assertEqual({source.get("document_id") for source in context["sources"] if source["kind"] == "case_document"}, {"doc1"})

    def test_exact_quote_and_grounded_amount_pass(self):
        self.assertEqual(agent.validate_output(agent.Analysis.model_validate(sample_output()), agent.build_context(sample_case())), [])

    def test_customer_reply_is_retained_as_unverified_statement(self):
        case = sample_case()
        case["messages"] = [{"id": "reply", "role": "client", "text": "가족이 카드 대금을 갚았어요."}]
        case["documents"] *= 4
        context = agent.build_context(case)
        source = next(source for source in context["sources"] if source["id"] == "message:reply")
        self.assertEqual(source["kind"], "party_statement")
        self.assertEqual(source["status"], "unverified_statement")

    def test_unknown_source_wrong_quote_and_invented_number_fail(self):
        for mutate, expected in [
            (lambda output: output["proposals"][0]["citations"][0].update(source_id="doc:other-case"), "UNKNOWN_SOURCE_ID"),
            (lambda output: output["proposals"][0]["citations"][0].update(quote="없는 인용"), "QUOTE_MISMATCH"),
            (lambda output: output["proposals"][0].update(observation="소득은 9999999원입니다."), "UNGROUNDED_NUMBER"),
        ]:
            output = sample_output()
            mutate(output)
            self.assertIn(expected, agent.validate_output(agent.Analysis.model_validate(output), agent.build_context(sample_case())))

    def test_reference_rule_cannot_alone_prove_case_fact(self):
        output = sample_output()
        output["proposals"][0].update(observation="자료를 확인해야 합니다.", citations=[{"source_id": "rule:control1", "quote": "소득 증빙은 담당자가 확인한다."}])
        self.assertIn("CASE_EVIDENCE_REQUIRED", agent.validate_output(agent.Analysis.model_validate(output), agent.build_context(sample_case())))


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        agent._CACHE.clear()
        legal._CACHE.clear()
        self.client_class = httpx.AsyncClient

    def client_factory(self, handler):
        return lambda **kwargs: self.client_class(**{**kwargs, "transport": httpx.MockTransport(handler)})

    async def test_no_evidence_goes_to_manual_review(self):
        result = await agent.run_analysis({"id": "empty", "documents": []})
        self.assertEqual(result["status"], "manual_review")
        self.assertEqual(result["error"]["code"], "NO_CASE_EVIDENCE")

    async def test_real_harness_schema_citations_metrics_and_cache(self):
        requests = []
        def handler(request):
            requests.append(request.url.path)
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": [{"name": "llama3:latest", "size_vram": 0}]})
            return httpx.Response(200, json={"done": True, "done_reason": "stop", "message": {"content": json.dumps(sample_output(), ensure_ascii=False)}, "eval_count": 20, "eval_duration": 2_000_000_000})
        with patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://127.0.0.1:11434", "OLLAMA_MODEL": "llama3:latest"}), patch.object(agent.httpx, "AsyncClient", side_effect=self.client_factory(handler)):
            result = await agent.run_analysis(sample_case())
            cached = await agent.run_analysis(sample_case())
        self.assertEqual(result["status"], "needs_review")
        self.assertFalse(result["metrics"]["gpu_in_use"])
        self.assertEqual(result["proposals"][0]["source_versions"], {"doc:doc1": 1})
        self.assertTrue(cached["metrics"]["cached"])
        self.assertEqual(requests.count("/api/chat"), 1)

    async def test_stale_input_is_never_released(self):
        case = sample_case()
        def handler(request):
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": []})
            case["input_revision"] = 2
            return httpx.Response(200, json={"done": True, "done_reason": "stop", "message": {"content": json.dumps(sample_output())}})
        with patch.object(agent.httpx, "AsyncClient", side_effect=self.client_factory(handler)):
            result = await agent.run_analysis(case)
        self.assertEqual(result["error"]["code"], "STALE_INPUT")
        self.assertFalse(result["proposals"])

    async def test_law_unconfigured_and_target_allowlist(self):
        with patch.dict(os.environ, {"LAW_API_OC": ""}):
            self.assertEqual((await legal.search_legal("개인회생"))["status"], "unconfigured")
            self.assertEqual((await legal.search_legal("개인회생", "http://attacker.invalid"))["status"], "invalid_target")

    async def test_law_credentials_are_redacted_and_metadata_only(self):
        credential = "synthetic-test-credential"
        def handler(request):
            self.assertEqual(request.url.host, "www.law.go.kr")
            self.assertEqual(request.url.params["OC"], credential)
            return httpx.Response(200, json={"LawSearch": {"totalCnt": 1, "law": {"법령일련번호": "123", "법령명한글": "채무자 회생 " + credential, "법령상세링크": "https://untrusted.invalid/" + credential}}})
        with patch.dict(os.environ, {"LAW_API_OC": credential}), patch.object(legal.httpx, "AsyncClient", side_effect=self.client_factory(handler)):
            result = await legal.search_legal("채무자 회생")
            cached = await legal.search_legal("채무자 회생")
        self.assertEqual(result["status"], "ok")
        self.assertNotIn(credential, json.dumps(result))
        self.assertTrue(result["results"][0]["url"].startswith("https://www.law.go.kr/LSW/"))
        self.assertEqual(result["results"][0]["content_status"], "metadata_only")
        self.assertTrue(cached["cached"])


if __name__ == "__main__":
    unittest.main()
