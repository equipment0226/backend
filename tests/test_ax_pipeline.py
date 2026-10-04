"""Synthetic rule/draft tests and HTTP/SQLite orchestration tests; no live model calls."""
import copy
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch


class DeferredPool:
    """Preserve queued/running boundaries without racing a real model thread."""
    def __init__(self):
        self.tasks = []

    def submit(self, function, *args):
        self.tasks.append((function, args))

    def drain(self):
        while self.tasks:
            function, args = self.tasks.pop(0)
            function(*args)


class RuleDraftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Delay API/store imports until discovery has loaded test_workflow's environment.
        from apps.api import ax_service, domain, drafting, rulebook
        cls.ax, cls.domain, cls.drafting, cls.rules = ax_service, domain, drafting, rulebook

    def make_case(self, text="합성 상담: 월 급여 280만 원이며 개인회생을 상담합니다."):
        case = self.domain.new_case("홍테스트", "CT01", "서울회생법원", text, True)
        case.update(case_type="personal_rehabilitation", case_type_label="개인회생",
                    consultation={"notes": text, "answers": {}}, extraction_candidates=[])
        return case

    def test_rule_trigger_document_factor_and_draft_mapping(self):
        case = self.make_case("월 급여를 받는 회사원입니다. 월세 보증금이 있고 가족이 대신 갚은 카드대금이 있습니다.")
        result = self.rules.evaluate_case(case)
        matched = {r["id"]: r for r in result["matched_rules"]}
        self.assertTrue({"WF01", "WF02", "WF04", "WF05"} <= set(matched))
        self.assertTrue({"D07", "D08", "D36", "D33", "D45", "D46"} <= {d["catalog_id"] for d in result["required_documents"]})
        self.assertIn("fund_source", matched["WF05"]["missing_factors"])
        self.assertIn("creditors", matched["WF05"]["draft_targets"])
        self.assertIn(matched["WF05"]["trigger_refs"][0]["quote"].strip(), case["consultation"]["notes"])
        self.assertFalse(matched["WF05"]["legal_determination"])

    def test_explicit_negation_and_superseded_document_do_not_trigger(self):
        case = self.make_case("가족이 대신 갚은 적 없습니다. 직장에 관한 추가 확인이 필요합니다.")
        case["documents"] = [{"id": "old", "text": "최근 대출과 자동차 처분", "status": "superseded"}]
        ids = {r["id"] for r in self.rules.evaluate_case(case)["matched_rules"]}
        self.assertNotIn("WF05", ids)
        self.assertNotIn("WF06", ids)
        self.assertNotIn("WF15", ids)

    def test_invalid_or_foreign_document_quotes_are_not_candidates(self):
        case = self.make_case()
        case["documents"] = [{"id": "valid", "text": "월 급여 280만 원", "status": "received"},
                             {"id": "old", "text": "월 급여 999만 원", "status": "superseded"}]
        inputs = [
            {"key": "monthly_income", "value": 2800000, "document_id": "valid", "quote": "월 급여 280만 원"},
            {"key": "monthly_income", "value": 9990000, "document_id": "old", "quote": "월 급여 999만 원"},
            {"key": "total_debt", "value": 99999999, "document_id": "valid", "quote": "없는 채무 원문"},
            {"key": "assets_total", "value": 100000, "document_id": "different-case-doc", "quote": "월 급여 280만 원"},
        ]
        self.ax.enrich_candidates(case, inputs)
        self.ax.enrich_candidates(case, inputs)
        self.assertEqual(len(case["extraction_candidates"]), 1)
        self.assertEqual(case["extraction_candidates"][0]["value"], 2800000)
        self.assertEqual(case["extraction_candidates"][0]["status"], "candidate")

    def test_chunked_consultation_and_client_message_candidates_are_retained(self):
        case = self.make_case("월 급여 280만 원이라고 말씀드립니다.")
        case["consultation"]["answers"] = {"housing": "월세 60만 원입니다."}
        case["messages"] = [{"id": "reply-1", "role": "client", "text": "총 채무 4200만 원입니다."},
                            {"id": "staff-1", "role": "staff", "text": "재산 총액 9000만 원"}]
        candidates = [
            {"key": "monthly_income", "value": 2800000, "source_id": "consultation:notes:0", "quote": "월 급여 280만 원"},
            {"key": "housing_cost", "value": 600000, "source_id": "consultation:housing:0", "quote": "월세 60만 원"},
            {"key": "total_debt", "value": 42000000, "source_id": "message:reply-1:0", "quote": "총 채무 4200만 원"},
            {"key": "assets_total", "value": 90000000, "source_id": "message:staff-1:0", "quote": "재산 총액 9000만 원"},
            {"key": "assets_total", "value": 999, "source_id": "message:other-reply:0", "quote": "총 채무 4200만 원"},
        ]
        self.ax.enrich_candidates(case, candidates)
        self.assertEqual({c["source_id"] for c in case["extraction_candidates"]},
                         {"consultation:notes:0", "consultation:housing:0", "message:reply-1:0"})
        self.assertTrue(all(c["status"] == "candidate" for c in case["extraction_candidates"]))
        isolated = copy.deepcopy(case)
        isolated["extraction_candidates"] = []
        isolated["consultation"]["status"] = "quarantined"
        isolated["messages"][0]["status"] = "quarantined"
        self.ax.enrich_candidates(isolated, candidates)
        self.assertEqual(isolated["extraction_candidates"], [])

    def test_superseded_candidates_do_not_fill_drafts_or_remove_missing_fields(self):
        case = self.make_case()
        case["documents"] = [{"id": "old", "text": "월 급여 999만 원", "status": "superseded"}]
        case["extraction_candidates"] = [{"id": "c", "key": "monthly_income", "value": 9990000,
                                            "status": "accepted", "document_id": "old", "source_id": "doc:old", "quote": "월 급여 999만 원"}]
        case["facts"][0].update(value=9990000, status="confirmed", evidence_ids=["old"])
        draft = self.drafting.generate(case)
        fields = [f for section in draft["sections"] for f in section["fields"] if f["key"] == "monthly_income"]
        self.assertTrue(all(f["value"] is None for f in fields))
        self.assertIn("monthly_income", case["rule_evaluation"]["missing_factors"])
        self.assertFalse(draft["source_refs"])

    def test_unknown_is_not_zero_and_conflicts_are_retained(self):
        case = self.make_case()
        case["documents"] = [{"id": "a", "text": "급여 280만 원", "status": "received"},
                             {"id": "b", "text": "급여 310만 원", "status": "received"}]
        case["extraction_candidates"] = [
            {"key": "monthly_income", "value": amount, "document_id": doc, "source_id": "doc:" + doc,
             "status": "candidate", "source_type": "case_document", "quote": "급여"}
            for amount, doc in [(2800000, "a"), (3100000, "b")]]
        draft = self.drafting.generate(case)
        fields = {f["key"]: f for section in draft["sections"] if section["id"] != "repayment_plan" for f in section["fields"]}
        self.assertEqual(fields["monthly_income"]["status"], "conflict")
        self.assertIsNone(fields["total_debt"]["value"])
        self.assertIn("monthly_income", draft["conflicting_factors"])
        self.assertEqual(self.drafting.value_text(None), "미확인")

    def test_out_of_scope_draft_has_no_rehabilitation_repayment_plan(self):
        for case_type in ("other", "bankruptcy_review", "unknown"):
            case = self.make_case("상속과 채무에 관한 상담이며 신청 방향은 미정입니다.")
            case.update(case_type=case_type, case_type_label="절차 방향 검토")
            draft = self.drafting.generate(case)
            self.assertNotIn("repayment_plan", {s["id"] for s in draft["sections"]})
            self.assertNotIn("개인회생 신청서", draft["sections"][0]["title"])

    def test_new_draft_supersedes_old_and_identity_changes_fingerprint(self):
        case = self.make_case()
        old = self.drafting.generate(case)
        before = self.ax.fingerprint(case)
        case["court_id"] = "CT03"
        self.assertNotEqual(before, self.ax.fingerprint(case))
        newer = self.drafting.generate(case, "관할 후보 변경")
        self.assertTrue(old["stale"])
        self.assertFalse(newer["stale"])
        self.assertGreater(newer["version"], old["version"])


class AXPipelineAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from apps.api import ax_engine, main, store
        cls.main, cls.store, cls.engine = main, store, ax_engine
        cls.temp = tempfile.TemporaryDirectory(prefix="debtoff-ax-pipeline-")
        cls.data_patch = patch.object(store, "DATA_DIR", Path(cls.temp.name))
        cls.data_patch.start()
        cls.auto_patch = patch.dict("os.environ", {"DEBTOFF_AUTO_AX": "1"})
        cls.auto_patch.start()
        cls.pool = DeferredPool()
        cls.pool_patch = patch.object(main, "POOL", cls.pool)
        cls.pool_patch.start()
        main.LOGIN_ATTEMPTS.clear()
        cls.client_context = TestClient(main.app)
        cls.client = cls.client_context.__enter__()
        cls.headers = {}
        for role in ("staff", "lawyer", "client"):
            response = cls.client.post("/api/auth/login", json={"username": "demo-" + role, "password": "debtoff-demo"})
            if response.status_code != 200:
                raise AssertionError(response.text)
            cls.headers[role] = {"Authorization": "Bearer " + response.json()["token"]}

    @classmethod
    def tearDownClass(cls):
        cls.pool.tasks.clear()
        cls.client_context.__exit__(None, None, None)
        cls.pool_patch.stop()
        cls.auto_patch.stop()
        cls.data_patch.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.pool.tasks.clear()
        self.text = "내담자는 홍테스트입니다. 서울 마포구에 거주하며 개인회생을 상담합니다. 월 급여 280만 원이고 월세를 납부합니다."
        self.intake_response = {"client_name": "홍테스트", "region": "서울 마포구", "court_id": "CT01", "case_type": "personal_rehabilitation", "summary": self.text,
                                "status": "needs_review", "metrics": {"model_status": "completed", "model": "mock-only"}, "missing_information": ["정확한 채무 잔액 확인"], "document_needs": [],
                                "extracted_facts": [{"key": "monthly_income", "label": "월 소득", "value": 2800000, "quote": "월 급여 280만 원", "source_type": "meeting"}]}
        self.intake_patch = patch.object(self.engine, "analyze_intake", AsyncMock(side_effect=lambda text: copy.deepcopy(self.intake_response)))
        self.intake_mock = self.intake_patch.start()
        async def analyzed(case, kind="case_review"):
            doc = case["documents"][-1]
            wrong_person = "김다른" in doc["text"]
            return {"status": "needs_review", "findings": [], "metrics": {"model_status": "completed", "model": "mock-only"},
                    # analyze_case returns the current whole-case extraction snapshot,
                    # including the retained consultation and newly arrived document.
                    "extracted_facts": [{"key": "monthly_income", "label": "월 소득", "value": 2800000, "quote": "월 급여 280만 원", "source_id": "consultation:notes:0", "source_type": "party_statement"},
                                        {"key": "monthly_income", "label": "월 소득", "value": 3100000, "quote": "월 급여 310만 원", "document_id": doc["id"], "source_type": "case_document"}],
                    "document_checks": [{"document_id": doc["id"], "coverage_status": "identity_conflict" if wrong_person else "needs_more", "person_status": "mismatch" if wrong_person else "matched"}]}
        self.analysis_patch = patch.object(self.engine, "analyze_case", AsyncMock(side_effect=analyzed))
        self.analysis_patch.start()
        self.verification_patch = patch('apps.api.verification.run_local_verification_batched', AsyncMock(return_value={
            'status': 'unavailable', 'passed': False, 'checked_item_ids': [], 'findings': []}))
        self.verification_patch.start()

    def tearDown(self):
        self.verification_patch.stop()
        self.pool.tasks.clear()
        self.analysis_patch.stop()
        self.intake_patch.stop()

    def intake(self, legacy_draft=True):
        response = self.client.post("/api/intake-runs", json={"text": self.text}, headers=self.headers["staff"])
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        self.assertEqual(response.json()["status"], "queued")
        self.pool.drain()
        finished = self.client.get("/api/intake-runs/" + run_id, headers=self.headers["staff"])
        self.assertEqual(finished.status_code, 200, finished.text)
        self.assertEqual(finished.json()["status"], "completed", finished.text)
        self.assertNotIn("input_text", finished.json())
        case = self.client.get("/api/cases/" + finished.json()["result"]["case_id"], headers=self.headers["staff"]).json()
        # Existing review/download tests exercise historical drafts. New intake now
        # waits for all required documents; seed an explicitly historical fixture.
        if legacy_draft:
            from apps.api import drafting
            case = self.store.mutate(case['id'], case['version'], {'name': '합성 시험', 'role': 'staff'},
                                     'test.historical_draft', lambda c: drafting.generate(c, '과거 초안 호환 시험'))
        return case, run_id

    def upload(self, case, wrong=False):
        text = ("성명: 김다른" if wrong else "성명: 홍테스트") + " 급여명세서. 월 급여 310만 원. 최근 대출이 있습니다."
        request = next(r for r in case["requests"] if r["catalog_id"] == "D07")
        response = self.client.post("/api/cases/" + case["id"] + "/documents", headers=self.headers["staff"],
                                    data={"expected_version": case["version"], "request_id": request["id"]},
                                    files={"file": ("synthetic-payroll.txt", text.encode(), "text/plain")})
        self.assertEqual(response.status_code, 200, response.text)
        self.pool.drain()
        return self.client.get("/api/cases/" + case["id"], headers=self.headers["staff"]).json()

    def test_intake_makes_requests_and_waits_for_documents_before_draft(self):
        case, run_id = self.intake(legacy_draft=False)
        self.assertEqual(case["client_name"], "홍테스트")
        self.assertEqual(case["court_id"], "CT01")
        self.assertTrue({"D07", "D08", "D33"} <= {r["catalog_id"] for r in case["requests"]})
        self.assertEqual(case["extraction_candidates"][0]["value"], 2800000)
        self.assertEqual(case["drafts"], [])
        self.assertEqual(case['ax_pipeline']['stage'], 'collecting')
        self.assertEqual(self.intake_mock.await_count, 1)

    def test_upload_reanalyzes_and_invalidates_draft_until_documents_complete(self):
        case, _ = self.intake()
        old_id = case["drafts"][-1]["id"]
        updated = self.upload(case)
        self.assertTrue(next(d for d in updated["drafts"] if d["id"] == old_id)["stale"])
        self.assertEqual(updated["drafts"][-1]["id"], old_id)
        self.assertIn("D44", {r["catalog_id"] for r in updated["requests"]})
        self.assertIn(updated['ax_pipeline']['stage'], ('collecting', 'verification_waiting'))
        runs = self.client.get("/api/cases/" + case["id"] + "/ax-runs", headers=self.headers["staff"]).json()["runs"]
        self.assertTrue(runs)
        result = self.client.get("/api/ax-runs/" + runs[0]["id"], headers=self.headers["staff"])
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotEqual(result.json()["status"], "stale")

    def test_identity_conflict_is_quarantined_and_does_not_enter_draft(self):
        case, _ = self.intake()
        updated = self.upload(case, wrong=True)
        doc = updated["documents"][-1]
        self.assertEqual(doc["status"], "quarantined")
        self.assertFalse(any(c.get("document_id") == doc["id"] and c["status"] != "quarantined" for c in updated["extraction_candidates"]))
        self.assertFalse(any(ref.get("document_id") == doc["id"] for ref in updated["drafts"][-1]["source_refs"]))
        self.assertNotIn("D44", {r["catalog_id"] for r in updated["requests"]})

    def test_new_extraction_snapshot_supersedes_obsolete_parser_candidate(self):
        case, _ = self.intake()
        from apps.api import store
        def add_old(c):
            c['extraction_candidates'].append({'id':'obsolete-factor','key':'total_debt','value':5000000,'quote':'최근 대출','source_id':'old-parser','status':'candidate'})
        case=store.mutate(case['id'],case['version'],{'name':'synthetic test','role':'test'},'test.old_parser',add_old)
        updated=self.upload(case)
        self.assertEqual(next(c for c in updated['extraction_candidates'] if c['id']=='obsolete-factor')['status'],'superseded')
        fields=[f for s in updated['drafts'][-1]['sections'] for f in s['fields']]
        self.assertFalse(any(f['key']=='total_debt' and f['value']==5000000 for f in fields))

    def test_lawyer_only_review_stale_rejection_and_real_docx_download(self):
        case, _ = self.intake()
        draft = case["drafts"][-1]
        base = "/api/cases/" + case["id"] + "/drafts/" + draft["id"]
        payload = {"expected_version": case["version"], "decision": "approve", "reason": "합성 자료의 누락 및 출처를 확인한 1차 검토"}
        denied = self.client.post(base + "/review", headers=self.headers["staff"], json=payload)
        self.assertEqual(denied.json()["code"], "LAWYER_ONLY")
        download = self.client.get(base + "/download?format=docx", headers=self.headers["staff"])
        self.assertEqual(download.status_code, 200, download.text[:100] if download.status_code != 200 else "")
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            self.assertIn("word/document.xml", archive.namelist())
            self.assertIn("홍테스트", archive.read("word/document.xml").decode())
        approved = self.client.post(base + "/review", headers=self.headers["lawyer"], json=payload)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["drafts"][-1]["status"], "reviewed")
        self.assertIn("법원 제출 승인과 별도", approved.json()["drafts"][-1]["review"]["scope"])
        updated = self.upload(approved.json())
        stale = self.client.post(base + "/review", headers=self.headers["lawyer"], json={**payload, "expected_version": updated["version"]})
        self.assertEqual(stale.json()["code"], "STALE_DRAFT")

    def test_client_cannot_see_internal_automation_or_download_draft(self):
        case, run_id = self.intake()
        user = {"name": "합성 테스트", "role": "staff"}
        self.store.mutate(case["id"], case["version"], user, "test.assign_client", lambda c: c.update(client_user_id="client"))
        visible = self.client.get("/api/cases/" + case["id"], headers=self.headers["client"])
        self.assertEqual(visible.status_code, 200, visible.text)
        for field in ("drafts", "extraction_candidates", "ax_runs", "automation", "rule_evaluation", "intake_analysis", "consultation"):
            self.assertNotIn(field, visible.json())
        self.assertEqual(visible.json()["documents"], [])
        self.assertEqual(self.client.get("/api/intake-runs/" + run_id, headers=self.headers["client"]).status_code, 403)
        url = "/api/cases/" + case["id"] + "/drafts/" + case["drafts"][-1]["id"] + "/download"
        self.assertEqual(self.client.get(url, headers=self.headers["client"]).status_code, 403)
        self.assertEqual(self.client.post("/api/intake-runs", json={"text": self.text}, headers=self.headers["client"]).status_code, 403)

    def test_draft_hash_mismatch_cannot_be_approved(self):
        case, _ = self.intake()
        user = {"name": "합성 테스트", "role": "staff"}
        edited = self.store.mutate(case["id"], case["version"], user, "test.tamper", lambda c: c["drafts"][-1]["sections"][0].update(content="해시와 다른 변조된 내용"))
        url = "/api/cases/" + case["id"] + "/drafts/" + case["drafts"][-1]["id"] + "/review"
        response = self.client.post(url, headers=self.headers["lawyer"], json={"expected_version": edited["version"], "decision": "approve", "reason": "변조된 초안의 해시 검사 시험"})
        self.assertEqual(response.json()["code"], "DRAFT_HASH")

    def classification_payload(self, case, **changes):
        return {"expected_version": case["version"], "client_name": case["client_name"],
                "region": case.get("region") or "", "court_id": case["court_id"],
                "case_type": case.get("case_type", "personal_rehabilitation"),
                "reason": "상담 원문을 다시 대조한 사건 분류 검토", **changes}

    def test_client_cannot_edit_intake_identity_region_or_type(self):
        case, _ = self.intake()
        before = copy.deepcopy(case)
        path = "/api/cases/" + case["id"] + "/intake-review"
        response = self.client.post(path, headers=self.headers["client"],
                                    json=self.classification_payload(case, client_name="변경시도", court_id="CT03", case_type="other"))
        self.assertEqual(response.status_code, 403, response.text)
        after = self.store.get_case(case["id"])
        for key in ("client_name", "court_id", "region", "case_type", "version", "input_revision"):
            self.assertEqual(before[key], after[key])

    def test_classification_correction_stales_old_and_removes_repayment_section(self):
        case, _ = self.intake()
        old = case["drafts"][-1]["id"]
        response = self.client.post("/api/cases/" + case["id"] + "/intake-review", headers=self.headers["staff"],
                                    json=self.classification_payload(case, court_id="CT03", region="부산광역시", case_type="other"))
        self.assertEqual(response.status_code, 200, response.text)
        updated = response.json()
        self.assertEqual(updated["court_id"], "CT03")
        self.assertEqual(updated["case_type"], "other")
        self.assertGreater(updated["input_revision"], case["input_revision"])
        self.assertTrue(next(d for d in updated["drafts"] if d["id"] == old)["stale"])
        current = updated["drafts"][-1]
        self.assertNotIn("repayment_plan", {s["id"] for s in current["sections"]})
        self.assertNotIn("개인회생 신청서", current["sections"][0]["title"])
        self.assertEqual(updated["intake_review"]["actor"], "담당 직원")

    def test_case_classification_does_not_activate_or_modify_legal_registry(self):
        registry_path = self.store.ROOT / "data/registry.json"
        original_bytes = registry_path.read_bytes()
        original = self.client.get("/api/registry", headers=self.headers["staff"]).json()
        self.assertFalse(original["meta"]["operational"])
        case, _ = self.intake()
        response = self.client.post("/api/cases/" + case["id"] + "/intake-review", headers=self.headers["staff"],
                                    json=self.classification_payload(case, court_id="CT02", region="수원시", case_type="personal_rehabilitation"))
        self.assertEqual(response.status_code, 200, response.text)
        after = self.client.get("/api/registry", headers=self.headers["staff"]).json()
        self.assertEqual(original_bytes, registry_path.read_bytes())
        self.assertEqual(original["courts"], after["courts"])
        self.assertEqual(original["rules"], after["rules"])
        self.assertFalse(after["meta"]["operational"])

    def test_monetary_candidate_correction_requires_integer_not_boolean_or_string(self):
        case, _ = self.intake()
        candidate = next(c for c in case["extraction_candidates"] if c["key"] == "monthly_income")
        path = "/api/cases/" + case["id"] + "/extraction-candidates/" + candidate["id"] + "/review"
        for invalid in (True, "3100000", 3100000.5, -1):
            response = self.client.post(path, headers=self.headers["staff"], json={"expected_version": case["version"], "decision": "correct", "reason": "합성 급여 원문의 금액을 다시 대조함", "value": invalid})
            self.assertEqual(response.status_code, 422, response.text)
            stored = self.store.get_case(case["id"])
            self.assertEqual(stored["version"], case["version"])
            self.assertEqual(next(c for c in stored["extraction_candidates"] if c["id"] == candidate["id"])["value"], 2800000)
        valid = self.client.post(path, headers=self.headers["staff"], json={"expected_version": case["version"], "decision": "correct", "reason": "합성 급여 원문의 금액을 다시 대조함", "value": 3100000})
        self.assertEqual(valid.status_code, 200, valid.text)
        corrected = next(c for c in valid.json()["extraction_candidates"] if c["id"] == candidate["id"])
        self.assertEqual(corrected["value"], 3100000)
        self.assertEqual(corrected["original_value"], 2800000)
        self.assertEqual(corrected["status"], "accepted")

    def test_name_change_quarantines_prior_identity_and_excludes_old_text_and_amounts(self):
        case, _ = self.intake()
        case = self.upload(case)
        def add_old_identity_evidence(c):
            c["consultation"]["notes"] += " 기존내담자전용경위_모의"
            c["messages"].append({"id": "old-person-reply", "role": "client", "text": "홍테스트 고객의 전용경위_모의입니다."})
            c["facts"][0].update(value=5100000, status="confirmed", evidence_ids=[c["documents"][-1]["id"]])
        case = self.store.mutate(case["id"], case["version"], {"name": "합성 테스트", "role": "staff"},
                                 "test.old_identity", add_old_identity_evidence)
        response = self.client.post("/api/cases/" + case["id"] + "/intake-review", headers=self.headers["staff"],
                                    json=self.classification_payload(case, client_name="김새이름"))
        self.assertEqual(response.status_code, 200, response.text)
        updated = response.json()
        self.assertEqual(updated["client_name"], "김새이름")
        self.assertTrue(all(d["status"] == "quarantined" for d in updated["documents"]))
        previous_ids = {c["id"] for c in case["extraction_candidates"]}
        self.assertTrue(all(c["status"] == "quarantined" for c in updated["extraction_candidates"] if c["id"] in previous_ids))
        corrected_name = next(c for c in updated["extraction_candidates"] if c["key"] == "client_name" and c["status"] == "accepted")
        self.assertEqual(corrected_name["value"], "김새이름")
        self.assertEqual(corrected_name["original_value"], "홍테스트")
        self.assertEqual(corrected_name["origin"], "human_correction")
        self.assertTrue(all(f["status"] == "quarantined" for f in updated["facts"]))
        self.assertEqual(updated["consultation"]["status"], "quarantined")
        self.assertEqual(updated["messages"][-1]["status"], "quarantined")
        draft = updated["drafts"][-1]
        values = [field["value"] for section in draft["sections"] for field in section["fields"] if field["key"] == "monthly_income"]
        self.assertTrue(values)
        self.assertTrue(all(value is None for value in values))
        self.assertTrue(all(ref["source_id"] == "intake_review" for ref in draft["source_refs"]))
        rendered = json.dumps(draft, ensure_ascii=False)
        for old_value in ("홍테스트", "2800000", "3100000", "5100000", "기존내담자전용경위_모의", "전용경위_모의"):
            self.assertNotIn(old_value, rendered)
        source_texts = "\n".join(s["text"] for s in self.engine.case_sources(updated))
        for old_value in ("홍테스트", "280만", "310만", "5100000", "전용경위_모의"):
            self.assertNotIn(old_value, source_texts)
        from apps.api import rulebook
        self.assertNotIn("WF02", {r["id"] for r in rulebook.evaluate_case(updated)["matched_rules"]})


if __name__ == "__main__":
    unittest.main()
