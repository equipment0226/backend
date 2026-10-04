import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from apps.api import ax_engine as ax, corpus, model_client


def case_fixture():
    return {"id": "ax-test", "version": 2, "input_revision": 3, "client_name": "홍테스트",
        "court_id": "CT01", "court_name": "서울회생법원", "documents": [
            {"id": "pay", "filename": "급여자료.txt", "version": 1, "status": "received", "page_texts": [
                {"page": 1, "text": "급여명세서\n성명: 홍테스트\n월 소득 2,800,000원. 통장내역은 미제출."}]}],
        "facts": [], "requests": [], "messages": [], "deadlines": [], "corrections": []}


def model_reply(source_id="S1", quote="월 소득 2,800,000원"):
    return {"done": True, "done_reason": "stop", "message": {"content": json.dumps({"findings": [
        {"title": "소득 기재 확인", "observation": "급여의 월 소득 기재를 확인해야 합니다.",
         "category": "extraction_candidate", "citations": [{"source_id": source_id, "quote": quote}],
         "factor_key": "monthly_income", "value": 2800000}]}, ensure_ascii=False)},
        "eval_count": 80, "prompt_eval_count": 150, "eval_duration": 2_000_000_000}


def selection_reply(source_id="S1"):
    result = model_reply()
    result["message"]["content"] = json.dumps({"selections": [
        {"source_id": source_id, "factor_key": "monthly_income", "document_type": None},
        {"source_id": source_id, "factor_key": None, "document_type": "payroll"}]})
    return result


class CaseGroundingTests(unittest.TestCase):
    def setUp(self):
        self.corpus = patch.object(corpus, "search", return_value=[])
        self.corpus.start()
        self.addCleanup(self.corpus.stop)

    def test_office_notice_and_case_summary_do_not_become_facts(self):
        case = {"id": "no-evidence", "summary": "가족이 카드 대금을 갚았다.",
                "messages": [{"id": "staff", "role": "staff", "text": "월 소득 900만원. 가족이 갚은 채무 증빙을 주세요."}]}
        context = ax.build_context(case)
        self.assertEqual(context["model_case_sources"], [])
        self.assertFalse(ax.rulebook_findings(case, context))

    def test_pages_preserved_and_archived_document_excluded(self):
        case = case_fixture()
        case["documents"][0]["page_texts"].append({"page": 7, "text": "보정권고 지급자와 자금출처를 확인하십시오."})
        case["documents"].append({"id": "old", "status": "superseded", "text": "월 소득 900만원"})
        sources = ax.case_sources(case)
        self.assertTrue(any(source.get("page") == 7 for source in sources))
        self.assertFalse(any(source.get("document_id") == "old" for source in sources))

    def test_latest_client_reply_retained_with_many_documents(self):
        case = case_fixture()
        case["documents"] = [{**case["documents"][0], "id": f"doc{index}"} for index in range(9)]
        case["messages"] = [{"id": "latest", "role": "client", "text": "어머니가 카드 대금을 갚았습니다."}]
        context = ax.build_context(case)
        self.assertTrue(any(source["id"].startswith("message:latest") for source in context["model_case_sources"]))

    def test_family_payment_triggers_on_client_quote_but_not_explicit_denial(self):
        for text, expected in (("어머니가 카드 대금을 갚았습니다.", True), ("어머니가 카드 대금을 갚지 않았습니다.", False)):
            case = {"id": "family", "messages": [{"id": "reply", "role": "client", "text": text}]}
            findings = ax.rulebook_findings(case, ax.build_context(case))
            self.assertEqual(any("가족의 채무" in item["title"] for item in findings), expected)

    def test_income_difference_preserves_bases_and_exact_two_sources(self):
        case = case_fixture()
        case["documents"][0]["page_texts"][0]["text"] = "월 급여 세전 2,800,000원"
        case["consultation"] = {"notes": "월 소득 세후 260만원", "answers": {}}
        findings = ax.rulebook_findings(case, ax.build_context(case))
        finding = next(item for item in findings if "소득 기준" in item["title"])
        self.assertIn("단순 모순", finding["observation"])
        self.assertEqual(len(finding["evidence_refs"]), 2)

    def test_annual_tax_income_not_compared_as_monthly_pay(self):
        case = case_fixture()
        case["documents"][0]["page_texts"][0]["text"] = "연간 급여 소득 30,000,000원"
        case["consultation"] = {"notes": "월 소득 260만원", "answers": {}}
        self.assertFalse(any("소득 진술" in item["title"] for item in ax.rulebook_findings(case, ax.build_context(case))))

    def test_existing_document_request_is_reviewed_without_duplicate_request(self):
        case = case_fixture()
        case["requests"] = [{"id": "existing", "title": "통장내역", "catalog_id": "D36", "status": "requested", "document_ids": []}]
        findings = ax.rulebook_findings(case, ax.build_context(case))
        self.assertFalse(any(item["action"].get("catalog_id") == "D36" for item in findings))
        self.assertTrue(any(item["action"].get("request_id") == "existing" for item in findings))

    def test_document_absence_is_not_transferred_to_previous_sentence(self):
        case = case_fixture()
        case["documents"][0]["page_texts"][0]["text"] = "통장내역은 제출했습니다. 급여명세서는 미제출입니다."
        findings = ax.rulebook_findings(case, ax.build_context(case))
        catalog_ids = {item["action"].get("catalog_id") for item in findings}
        self.assertIn("D07", catalog_ids)
        self.assertNotIn("D36", catalog_ids)

    def test_unreadable_scan_and_unconfirmed_service_become_tasks(self):
        case = case_fixture()
        case["documents"][0]["page_texts"] = []
        case["deadlines"] = [{"id": "due", "title": "기한", "status": "unconfirmed", "due_date": None}]
        findings = ax.rulebook_findings(case, ax.build_context(case))
        self.assertTrue(any("문자 추출" in item["title"] for item in findings))
        self.assertTrue(any(item["severity"] == "urgent" for item in findings))

    def test_identity_conflict_and_unreadable_file_are_not_sufficient(self):
        case = case_fixture()
        case["documents"][0]["page_texts"][0]["text"] = "급여명세서\n성명: 김다른\n실지급액 280만원"
        self.assertEqual(ax.check_documents(case, [])[0]["coverage_status"], "identity_conflict")
        case["documents"][0]["page_texts"] = []
        self.assertEqual(ax.check_documents(case, [])[0]["coverage_status"], "unreadable")

    def test_transcript_mentioning_unsubmitted_payslip_stays_transcript(self):
        case = case_fixture()
        document = case["documents"][0]
        document["filename"] = "상담 회의록.txt"
        document["page_texts"][0]["text"] = "합성 상담 회의록\n급여명세서와 채권자별 잔액 증명은 제출 전입니다."
        self.assertEqual(ax.check_documents(case, [])[0]["classification"], "상담 회의록")


class IntakeExtractionTests(unittest.TestCase):
    def test_model_selects_spans_but_money_conversion_and_quotes_are_server_grounded(self):
        text = "의뢰인: 홍길동\n주소: 서울 강남구\n개인회생 상담입니다.\n월 소득 280만원\n채무 총액 7,500만원\n월세 70만원"
        spans = ax._intake_spans(text)
        selection = ax.IntakeSelection(client_name_source_id="T1", region_source_id="T2", case_type="personal_rehabilitation",
            case_type_source_id="T3", factors=[ax.SelectedFactor(source_id="T4", key="monthly_income"),
            ax.SelectedFactor(source_id="T5", key="total_debt"), ax.SelectedFactor(source_id="T6", key="housing_cost")])
        output, defects = ax._resolve_intake_selection(selection, spans)
        self.assertFalse(defects)
        self.assertFalse(ax._validate_intake(output, text))
        self.assertEqual([fact.value for fact in output.facts], [2800000, 75000000, 700000])
        selection.factors[0].source_id = "T6"
        _, defects = ax._resolve_intake_selection(selection, spans)
        self.assertIn("FACTOR_SOURCE_MISMATCH", defects)
        selection.factors[0].source_id = "T4"
        selection.case_type_source_id = "T1"
        output, defects = ax._resolve_intake_selection(selection, spans)
        self.assertIn("CASE_TYPE_SOURCE_MISMATCH", defects)
        self.assertEqual(output.case_type, "unknown")
        self.assertEqual(len(output.facts), 3)
        self.assertFalse(ax._validate_intake(output, text))

    def test_family_factors_are_labelled_claims_without_inventing_legal_relationship(self):
        text = "어머니가 카드 대금을 대신 갚았습니다.\n지급자: 김예시\n완납일: 2026-09-01\n자금출처: 어머니 예금\n가족간 약정: 대여인지 증여인지 아직 정하지 않음\n잔존채무: 미확인"
        result = ax._intake_baseline(text)
        facts = result["extracted_facts"]
        keys = {fact["key"] for fact in facts}
        self.assertTrue({"payer", "repayment_date", "fund_source", "family_legal_relation"} <= keys)
        self.assertNotIn("remaining_debt", keys)
        self.assertTrue(all(fact["status"] == "candidate" and fact["quote"] in text for fact in facts))

    def test_meeting_text_maps_identity_region_case_and_real_amount_units(self):
        text = "의뢰인: 홍길동. 주소: 서울 강남구. 개인회생 상담. 월 소득 세후 280만원, 채무 총액 1억 2천만원. 월세 70만원. 3인 가구."
        result = ax._intake_baseline(text)
        values = {item["key"]: item["value"] for item in result["extracted_facts"]}
        self.assertEqual((result["client_name"], result["region"], result["case_type"]), ("홍길동", "서울", "personal_rehabilitation"))
        self.assertEqual(values["monthly_income"], 2800000)
        self.assertEqual(values["total_debt"], 120000000)
        self.assertEqual(values["housing_cost"], 700000)
        self.assertEqual(values["household_size"], 3)
        self.assertEqual(values["address"], "서울 강남구")
        self.assertTrue(all(item["quote"] in text for item in result["extracted_facts"]))

    def test_unknown_income_is_not_zero_and_annual_income_not_monthly(self):
        result = ax._intake_baseline("소득은 미상입니다. 연봉 3000만원. 개인회생 상담입니다.")
        self.assertIsNone(result["client_name"])
        self.assertIsNone(result["region"])
        self.assertNotIn("monthly_income", {item["key"] for item in result["extracted_facts"]})

    def test_negative_amount_is_not_inverted_to_positive_income(self):
        result = ax._intake_baseline("월 소득 -2,800,000원. 사업 매출 - 300만원.")
        self.assertFalse(any(fact["key"] in {"monthly_income", "business_revenue"} for fact in result["extracted_facts"]))

    def test_individual_card_loan_does_not_replace_total_debt(self):
        result = ax._intake_baseline("전체 채무는 4500만원입니다. 최근 대출로 카드론 500만원을 받았고 생활비로 사용했습니다.")
        self.assertEqual([fact["value"] for fact in result["extracted_facts"] if fact["key"] == "total_debt"], [45000000])
        self.assertEqual([fact["value"] for fact in result["extracted_facts"] if fact["key"] == "recent_borrowing"], [5000000])

    def test_synthetic_meeting_family_component_does_not_become_case_total(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / 'examples/synthetic_case/00_상담회의록_가상자료.txt').read_text(encoding='utf-8')
        result = ax._intake_baseline(text)
        totals = [fact for fact in result['extracted_facts'] if fact['key'] == 'total_debt']
        self.assertEqual([fact['value'] for fact in totals], [78000000])
        self.assertTrue(all(fact['quote'] in text for fact in totals))
        self.assertEqual(next(f['value'] for f in result['extracted_facts'] if f['key']=='monthly_income'), 2800000)

    def test_synthetic_family_pdf_does_not_assert_individual_or_conditional_total(self):
        import pymupdf
        root = Path(__file__).resolve().parents[1]
        with pymupdf.open(root / 'examples/synthetic_case/documents/07_가족대납_가상자료.pdf') as document:
            sources = [ax._source(f'family:p{i+1}', 'case_document', '가족 대납 사실확인', page.get_text(sort=True),
                                 document_id='family', page=i+1) for i, page in enumerate(document)]
        facts = ax.extract_factor_candidates(sources)
        self.assertNotIn('total_debt', {fact['key'] for fact in facts})
        self.assertIn('payer', {fact['key'] for fact in facts})
        self.assertIn('fund_source', {fact['key'] for fact in facts})

    def test_only_explicit_case_aggregates_can_supply_total_debt(self):
        rejected = ['가족 채무 잔액: 200만원', '가족 채무 총액 200만원',
                    '예시은행A 채무 총액 500만원', '당행 대출 합계 500만원',
                    '총채무 중 가족에게 진 빚은 200만원', '총대출 원금 7500만원',
                    '가족채무를 포함하면 총채무는 8000만원입니다.',
                    '총채무는 8000만원으로 추정됩니다.']
        for text in rejected:
            with self.subTest(text=text):
                self.assertNotIn('total_debt', {f['key'] for f in ax._intake_baseline(text)['extracted_facts']})
        for text in ['채무는 총 8000만원입니다.', '전체 채무 8000만원', '부채 합계: 8000만원']:
            with self.subTest(text=text):
                self.assertEqual([f['value'] for f in ax._intake_baseline(text)['extracted_facts'] if f['key']=='total_debt'], [80000000])

    def test_model_cannot_select_family_component_as_case_total(self):
        spans = ax._intake_spans('가족 채무 잔액: 200만원')
        selection = ax.IntakeSelection(client_name_source_id=None, region_source_id=None,
            case_type='unknown', case_type_source_id=None,
            factors=[ax.SelectedFactor(source_id='T1', key='total_debt')])
        output, defects = ax._resolve_intake_selection(selection, spans)
        self.assertFalse(output.facts)
        self.assertIn('FACTOR_SOURCE_MISMATCH', defects)

    def test_document_selector_resolves_grounded_factor_and_classification(self):
        source = {"id": "S1", "original_id": "doc:pay:p1:0", "kind": "case_document", "title": "급여명세서.txt", "text": "급여명세서\n월 소득 280만원", "version": 1}
        selection = ax.CaseSelection.model_validate_json(selection_reply()["message"]["content"])
        resolved, discarded = ax._resolve_case_selection(selection, [source])
        self.assertFalse(discarded)
        self.assertEqual(len(resolved), 2)
        self.assertEqual(resolved[0].value, 2800000)
        self.assertFalse(ax.validate_model_output(ax.ModelAnalysis(findings=resolved), [source]))

    def test_missing_document_mention_is_not_a_document_classification(self):
        source = {"id": "S1", "original_id": "doc:notes:p1:0", "kind": "case_document", "title": "상담 회의록.txt", "text": "급여명세서는 미제출입니다.", "version": 1}
        selection = ax.CaseSelection(selections=[ax.CaseSelectedItem(source_id="S1", factor_key=None, document_type="payroll")])
        resolved, discarded = ax._resolve_case_selection(selection, [source])
        self.assertFalse(resolved)
        self.assertIn("CLASSIFICATION_SOURCE_MISMATCH", discarded)

    def test_model_identity_and_converted_amount_require_original_quote(self):
        output = ax.IntakeAnalysis.model_validate({"client_name": {"value": "홍길동", "quote": "홍길동"},
            "region": {"value": None, "quote": ""}, "case_type": "unknown", "case_type_quote": "",
            "facts": [{"key": "monthly_income", "value": 2800000, "quote": "월 소득 280만원"}]})
        self.assertEqual(ax._validate_intake(output, "홍길동 월 소득 280만원"), [])
        self.assertIn("UNGROUNDED_IDENTITY", ax._validate_intake(output, "김길동 월 소득 280만원"))
        output.facts[0].value = 3900000
        self.assertIn("UNGROUNDED_NUMBER", ax._validate_intake(output, "홍길동 월 소득 280만원"))

    def test_reference_or_workflow_quote_cannot_prove_a_model_fact(self):
        output = ax.ModelAnalysis.model_validate(json.loads(model_reply()["message"]["content"]))
        sources = [{"id": "S1", "kind": "official_reference", "text": "월 소득 2,800,000원"}]
        self.assertIn("CASE_EVIDENCE_REQUIRED", ax.validate_model_output(output, sources))

    def test_quote_and_unquoted_numeric_claim_are_rejected(self):
        output = ax.ModelAnalysis.model_validate(json.loads(model_reply()["message"]["content"]))
        sources = [{"id": "S1", "kind": "case_document", "text": "월 소득 2,800,000원. 다른 숫자 99."}]
        output.findings[0].observation = "소득 99원을 확인해야 합니다."
        self.assertIn("UNGROUNDED_NUMBER", ax.validate_model_output(output, sources))
        output.findings[0].citations[0].quote = "없는 원문"
        self.assertIn("QUOTE_MISMATCH", ax.validate_model_output(output, sources))


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ax._CACHE.clear()
        patches = [patch.dict(os.environ, {"DEBTOFF_MODEL_PROVIDER": "ollama", "DEBTOFF_AX_MODEL": "on"}),
                   patch.object(corpus, "search", return_value=[]),
                   patch.object(ax.runtime, "model_status", new=AsyncMock(return_value={"status": "available", "loaded": []}))]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_real_contract_validates_model_factor_and_leaves_case_unchanged(self):
        case = case_fixture()
        before = copy.deepcopy(case)
        with patch.object(model_client, "generate", new=AsyncMock(return_value=selection_reply())) as generate:
            result = await ax.analyze_case(case)
            cached = await ax.analyze_case(case)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["metrics"]["model_status"], "completed")
        self.assertEqual(result["case_version"], 2)
        self.assertTrue(any(fact["origin"] == "llama3" and fact["value"] == 2800000 for fact in result["extracted_facts"]))
        self.assertEqual(case, before)
        self.assertTrue(cached["metrics"]["cached"])
        self.assertEqual(generate.await_count, 1)

    async def test_rulebook_survives_model_failure_without_ai_success(self):
        with patch.object(model_client, "generate", new=AsyncMock(side_effect=httpx.ConnectError("offline"))):
            result = await ax.analyze_case(case_fixture())
        self.assertEqual(result["status"], "rulebook_only")
        self.assertEqual(result["metrics"]["model_status"], "failed")
        self.assertTrue(result["extracted_facts"])
        self.assertTrue(all(finding["origin"] == "rulebook" for finding in result["findings"]))

    async def test_no_client_evidence_skips_model_instead_of_repeating_office_text(self):
        case = {"id": "office-only", "messages": [{"id": "office", "role": "staff", "text": "가족 완납 자료 주세요."}]}
        with patch.object(model_client, "generate", new=AsyncMock()) as generate:
            result = await ax.analyze_case(case)
        generate.assert_not_awaited()
        self.assertEqual(result["metrics"]["model_status"], "no_case_evidence")

    async def test_disabled_model_keeps_truthful_origin_and_intake_document_mapping(self):
        with patch.dict(os.environ, {"DEBTOFF_AX_MODEL": "off"}), patch.object(model_client, "generate", new=AsyncMock()) as generate:
            result = await ax.analyze_intake("의뢰인: 홍길동. 주소: 서울. 개인회생 상담. 직장인 월 소득 280만원, 채무 총액 7500만원.")
        generate.assert_not_awaited()
        self.assertEqual(result["metrics"]["model_status"], "disabled")
        self.assertEqual(result["origin"], "rulebook")
        self.assertIn("D07", {item["catalog_id"] for item in result["document_needs"]})

    async def test_stale_input_cannot_release_facts_or_document_checks(self):
        case = case_fixture()
        async def change_input(*args, **kwargs):
            case["input_revision"] += 1
            return selection_reply()
        with patch.object(model_client, "generate", side_effect=change_input):
            result = await ax.analyze_case(case)
        self.assertEqual(result["status"], "stale")
        self.assertFalse(result["findings"])
        self.assertFalse(result["extracted_facts"])
        self.assertFalse(result["document_checks"])

    async def test_truncated_response_is_not_accepted_even_when_partial_json_parses(self):
        reply = selection_reply()
        reply["done_reason"] = "length"
        with patch.object(model_client, "generate", new=AsyncMock(return_value=reply)):
            result = await ax.analyze_case(case_fixture())
        self.assertEqual(result["error"]["code"], "TRUNCATED_MODEL_OUTPUT")
        self.assertEqual(result["metrics"]["model_finding_count"], 0)

    async def test_raw_intake_remains_local_when_external_reasoning_is_configured(self):
        secret = "test-secret-never-log"
        client_class = httpx.AsyncClient
        def handler(request):
            self.assertIn(request.url.host, {"127.0.0.1", "localhost", "::1", "host.docker.internal"})
            self.assertNotIn(secret, request.headers.get("Authorization", ""))
            return httpx.Response(401, json={"error": "bad credential"})
        with patch.dict(os.environ, {"DEBTOFF_MODEL_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": secret}), patch.object(model_client.httpx, "AsyncClient", side_effect=lambda **kw: client_class(**kw, transport=httpx.MockTransport(handler))):
            result = await ax.analyze_intake("의뢰인: 홍길동. 월 소득 280만원.")
        self.assertEqual(result["error"]["code"], "MODEL_AUTH_FAILED")
        self.assertNotIn(secret, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
