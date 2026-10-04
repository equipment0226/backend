"""Case-grounded AX proposals, with separately labelled workflow rules and Llama.

The engine reads a single case snapshot and local public-source corpus. It does
not mutate the case, confirm a fact, calculate legal amounts, or send a request.
All proposals require an explicit staff decision in the application.
"""
from __future__ import annotations

import asyncio
from . import prompt_registry
import copy
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import agent_engine as runtime
from . import model_client

ROOT = Path(__file__).resolve().parents[2]
HARNESS_VERSION = "debtoff-ax-v3.2"
MODEL_TIMEOUT = 120
MODEL_MAX_TOKENS = 600
CASE_MAX_TOKENS = 220
_MODEL_GATE = threading.Lock()
_CACHE: dict[str, dict] = {}
INACTIVE = {"deleted", "superseded", "rejected", "old_version", "quarantined"}
CASE_KINDS = {"case_document", "party_statement", "case_record"}
FactorKey = Literal["client_name", "monthly_income", "income_period", "total_debt", "living_expenses",
    "assets_total", "household_size", "address", "employer", "employment_type", "housing_cost", "housing_deposit",
    "creditors", "dependents", "business_revenue", "business_expenses", "repayment_date", "payer", "fund_source",
    "family_legal_relation", "remaining_debt", "recent_borrowing", "fund_usage", "disposal_proceeds", "tax_arrears",
    "insurance_surrender", "prior_proceedings", "service_date", "account_scope"]


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=160)
    quote: str = Field(min_length=2, max_length=120)


class ModelFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=2, max_length=55)
    observation: str = Field(min_length=8, max_length=180)
    category: Literal["document_classification", "extraction_candidate", "review_question"]
    citations: list[Citation] = Field(min_length=1, max_length=2)
    factor_key: FactorKey | None = None
    value: int | str | None = None


class ModelAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[ModelFinding] = Field(min_length=1, max_length=4)


class IntakeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str | None
    quote: str = Field(max_length=100)


class IntakeFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: FactorKey
    value: int | str
    quote: str = Field(min_length=2, max_length=100)


class IntakeAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_name: IntakeIdentity
    region: IntakeIdentity
    case_type: Literal["personal_rehabilitation", "bankruptcy_review", "other", "unknown"]
    case_type_quote: str = Field(max_length=100)
    facts: list[IntakeFact] = Field(max_length=4)


class SelectedFactor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    key: FactorKey


class IntakeSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_name_source_id: str | None
    region_source_id: str | None
    case_type: Literal["personal_rehabilitation", "bankruptcy_review", "other", "unknown"]
    case_type_source_id: str | None
    factors: list[SelectedFactor] = Field(max_length=4)


class CaseSelectedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    factor_key: FactorKey | None
    document_type: Literal["payroll", "bank_statement", "debt_certificate", "income_certificate", "family_certificate", "lease", "court_order", "consultation"] | None


class CaseSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selections: list[CaseSelectedItem] = Field(min_length=1, max_length=2)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _text(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _tokens(value: str) -> set[str]:
    words = re.findall(r"[가-힣a-z0-9]{2,}", value.lower())
    return set(words) | {word[index:index + 2] for word in words for index in range(len(word) - 1)}


def _data(filename, fallback):
    try:
        return json.loads((ROOT / "data" / filename).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def _active(item: dict) -> bool:
    return not item.get("stale") and item.get("status") not in INACTIVE


def _source(source_id, kind, title, text, version=1, **details):
    return {"id": source_id, "kind": kind, "title": title, "text": text,
            "version": version, "source_sha256": _digest(text), **details}


def _windows(text: str, size=900, overlap=140):
    # Overlap retains headings with the table or sentence on either side of a cut.
    for offset in range(0, len(text), size - overlap):
        yield offset, text[offset:offset + size]


def case_sources(case: dict) -> list[dict]:
    """Preserve exact page text and distinguish claims from workflow metadata."""
    sources = []
    for document in case.get("documents", []):
        if not _active(document):
            continue
        doc_id = str(document["id"])
        title = document.get("filename") or document.get("name") or document.get("title") or "사건 자료"
        pages = document.get("page_texts") or []
        if not pages:
            pages = [{"page": 1, "text": document.get("text") or document.get("extracted_text") or ""}]
        has_text = False
        for index, page in enumerate(pages):
            page_text = _text(page.get("text", "") if isinstance(page, dict) else page)
            if not page_text.strip():
                continue
            has_text = True
            number = page.get("page", index + 1) if isinstance(page, dict) else index + 1
            for offset, excerpt in _windows(page_text):
                sources.append(_source(f"doc:{doc_id}:p{number}:{offset}", "case_document", title,
                    excerpt, document.get("version", 1), document_id=doc_id, page=number,
                    offset=offset, line_offset=page_text[:offset].count('\n'), status=document.get("status", "received"),
                    source_sha256=_digest(page_text)))
        if not has_text:
            sources.append(_source(f"document-state:{doc_id}", "workflow_record", title,
                f"문서: {title}; 문자 추출 결과 없음; 수신 상태: {document.get('status', 'received')}",
                document.get("version", 1), document_id=doc_id, status="text_unavailable"))
    for message in case.get("messages", []):
        # Never recycle an office's own request as evidence that the client did it.
        if message.get("role") != "client" or not _active(message):
            continue
        text = _text(message.get("text", ""))
        if text.strip():
            for offset, excerpt in _windows(text):
                sources.append(_source(f"message:{message.get('id', _digest(text)[:12])}:{offset}",
                    "party_statement", "고객 회신 원문", excerpt,
                    message.get("version", case.get("input_revision", 1)), status="unverified_statement"))
    consultation = case.get("consultation", {}) or {}
    if isinstance(consultation, dict) and _active(consultation):
        entries = [("notes", consultation.get("notes", ""))] + list((consultation.get("answers", {}) or {}).items())
        for key, value in entries:
            text = _text(value)
            for offset, excerpt in _windows(text):
                sources.append(_source(f"consultation:{key}:{offset}", "party_statement",
                    f"상담 진술 · {key}", excerpt, consultation.get("version", 1),
                    status="unverified_statement", field=key))
    for index, statement in enumerate(case.get("statements", [])):
        if not isinstance(statement, dict) or not _active(statement) or statement.get("role") in {"staff", "lawyer"}:
            continue
        text = _text(statement.get("text", statement.get("value", "")))
        for offset, excerpt in _windows(text):
            sources.append(_source(f"statement:{statement.get('id', index)}:{offset}", "party_statement",
                statement.get("title", "상담 진술"), excerpt, statement.get("version", 1), status="unverified_statement"))
    for fact in case.get("facts", []):
        if not _active(fact):
            continue
        value = fact.get("value") if fact.get("value") is not None else fact.get("claimed_value")
        if value is None:
            continue
        text = f"{fact.get('label', fact.get('key', '사실 후보'))}: {value}; 상태: {fact.get('status', 'unknown')}"
        sources.append(_source(f"fact:{fact['id']}", "case_record", fact.get("label", "기록된 사실 후보"),
            text, case.get("input_revision", 1), status=fact.get("status", "unknown"),
            field=fact.get("key"), numeric_value=value, evidence_ids=fact.get("evidence_ids", [])))
    for collection, title in (("requests", "자료 요청 기록"), ("deadlines", "기한 기록"), ("corrections", "보정 요구 기록")):
        for item in case.get(collection, []):
            if not _active(item):
                continue
            fields = {key: item.get(key) for key in ("title", "requirement", "period", "status", "due_date", "evidence_id", "source_document_id") if key in item}
            sources.append(_source(f"{collection}:{item['id']}", "workflow_record", item.get("title", title),
                json.dumps(fields, ensure_ascii=False), item.get("version", case.get("input_revision", 1)),
                status=item.get("status", "unconfirmed")))
    return sources


def build_context(case: dict, kind="case_review") -> dict:
    all_sources = case_sources(case)
    primary = [source for source in all_sources if source["kind"] in CASE_KINDS]
    query = "개인회생 신청 서류 소득 급여 증빙 보정 " + case.get("court_name", "")
    query += " " + " ".join(source["text"][:350] for source in primary[-8:])
    query += " " + " ".join(_text(item.get("requirement")) for item in case.get("corrections", []))[:500]
    terms = _tokens(query)
    for source in primary:
        source["score"] = len(terms & _tokens(source["text"]))
    # Diversify by document so a large PDF cannot crowd all client replies out.
    ranked = sorted(primary, key=lambda item: item.get("score", 0), reverse=True)
    selected = []
    groups = set()
    # The latest client reply is independently relevant, even with no query overlap.
    latest_reply = next((source for source in reversed(primary) if source["id"].startswith("message:")), None)
    if latest_reply:
        selected.append(latest_reply)
        groups.add(latest_reply["id"].rsplit(":", 1)[0])
    for source in ranked:
        group = source.get("document_id") or source["id"].rsplit(":", 1)[0]
        if group in groups or source in selected:
            continue
        selected.append(source)
        groups.add(group)
        if len(selected) >= 5:
            break
    # Reserve room for another page of a court order when only one document exists.
    for source in ranked:
        if len(selected) >= 5:
            break
        if source not in selected:
            selected.append(source)
    public_sources = []
    corpus_error = None
    try:
        from . import corpus
        chunks = corpus.search(query=query[:3500], court_id=case.get("court_id"), limit=4)
        for chunk in chunks:
            if not chunk.get("text") or not chunk.get("url"):
                continue
            if chunk.get("court_id") and chunk["court_id"] != case.get("court_id"):
                continue
            source = _source("official:" + str(chunk["id"]), "official_reference", chunk["title"],
                chunk["text"], chunk.get("sha256", chunk.get("content_hash", 1)),
                url=chunk["url"], public_source_id=chunk["source_id"], court_id=chunk.get("court_id"),
                page=chunk.get("page"), locator=chunk.get("locator"),
                retrieved_at=chunk.get("fetched_at", chunk.get("retrieved_at")),
                source_type=chunk.get("source_type"), effective_date=chunk.get("effective_date"),
                effective_date_status=chunk.get("effective_date_status", "requires_review"),
                content_hash=chunk.get("sha256", chunk.get("content_hash")),
                status="reference_requires_review", score=chunk.get("score", 0))
            public_sources.append(source)
    except (ImportError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        corpus_error = type(exc).__name__
    rules = _data("knowledge.json", [])
    rule_sources = []
    for rule in rules if isinstance(rules, list) else rules.get("items", []):
        if not _active(rule) or (rule.get("court_id") and rule["court_id"] != case.get("court_id")):
            continue
        rule_sources.append(_source("rule:" + rule["id"], "workflow_rule", rule["title"], rule["text"],
            scope=rule.get("scope", "design"), approved=bool(rule.get("approved")),
            source=rule.get("source"), status="planning_reference"))
    return {"case_id": case.get("id"), "case_version": case.get("version", 1),
            "input_revision": case.get("input_revision", 1), "kind": kind,
            "sources": selected + public_sources + rule_sources,
            "case_sources": all_sources, "model_case_sources": selected,
            "official_sources": public_sources, "rule_sources": rule_sources,
            "corpus_status": "available" if public_sources else "no_matches" if not corpus_error else "unavailable",
            "corpus_error": corpus_error, "retrieval": "case_page_lexical_and_local_public_corpus",
            "excluded_case_source_count": max(0, len(primary) - len(selected))}


def _ref(source: dict, quote=None) -> dict:
    reference = {"source_id": source["id"], "quote": quote or source["text"][:160],
                 "title": source["title"], "source_version": source["version"]}
    for key in ("document_id", "page", "offset", "url", "retrieved_at", "content_hash", "effective_date", "effective_date_status"):
        if source.get(key) is not None:
            reference[key] = source[key]
    return reference


def _quote_near(text: str, start: int, end: int, size=160) -> str:
    left = max(0, start - 35)
    return text[left:max(min(len(text), left + size), end)][:240]


def _public_refs(context: dict, query: str) -> list[dict]:
    terms = _tokens(query)
    ranked = sorted(context["official_sources"], key=lambda source: len(terms & _tokens(source["text"] + source["title"])), reverse=True)
    if not ranked:
        return []
    source = ranked[0]
    # A link alone, or an unrelated downloaded page, is never presented as support.
    keywords = [word for word in re.findall(r"[가-힣]{2,}", query) if len(word) >= 2]
    matches = [re.search(re.escape(word), source["text"]) for word in keywords]
    matches = [match for match in matches if match]
    if not matches:
        return []
    match = matches[0]
    reference = _ref(source, _quote_near(source["text"], match.start(), match.end()))
    reference["relationship"] = "검토 참고 · 해당 사건의 충족 여부나 법률 판단을 증명하지 않음"
    return [reference]


def _finding(context, key, title, observation, references, action, severity="review", rule_ids=(), query=""):
    return {"id": "ax-" + _digest([key, context["case_id"], context["input_revision"]])[:16],
            "title": title, "severity": severity, "observation": observation,
            "evidence_refs": references, "official_refs": _public_refs(context, query) if query else [],
            "rule_refs": [_ref(source) for source in context["rule_sources"] if source["id"] in {"rule:" + rule for rule in rule_ids}],
            "action": action, "origin": "rulebook", "review_status": "pending",
            "input_revision": context["input_revision"], "case_version": context["case_version"],
            "epistemic_status": "workflow_review_candidate"}


def _income_candidates(source: dict) -> list[dict]:
    if source.get("field") == "monthly_income" and isinstance(source.get("numeric_value"), (int, float)):
        return [{"amount": int(source["numeric_value"]), "basis": "unknown", "quote": source["text"][:160], "source": source}]
    # Tax-year revenue and annual income are intentionally not treated as monthly pay.
    text = source["text"]
    amount_pattern = r"(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>만원|만\s*원|원)"
    results = []
    for match in re.finditer(amount_pattern, text):
        near = text[max(0, match.start() - 45):min(len(text), match.end() + 15)]
        if not re.search(r"월\s*(?:평균\s*)?(?:소득|급여|급료|수입|보수)|월급|실수령|실지급|차인지급|급여|세후", near):
            continue
        if re.search(r"연간|연봉|귀속연도|연\s*소득|과세표준|사업\s*매출", near):
            continue
        try:
            amount = int(float(match.group("number").replace(",", "")) * (10000 if "만" in match.group("unit") else 1))
        except ValueError:
            continue
        basis = "net" if re.search(r"세후|실수령", near) else "gross" if "세전" in near else "unknown"
        results.append({"amount": amount, "basis": basis, "quote": _quote_near(text, match.start(), match.end()), "source": source})
    return results


def rulebook_findings(case: dict, context: dict) -> list[dict]:
    sources = context["case_sources"]
    primary = [source for source in sources if source["kind"] in CASE_KINDS]
    by_id = {source["id"]: source for source in sources}
    documents = {doc["id"]: doc for doc in case.get("documents", []) if _active(doc)}
    catalog = {item["id"]: item for item in _data("registry.json", {}).get("documents", [])}
    findings = []

    def add(key, title, observation, refs, action, **kwargs):
        findings.append(_finding(context, key, title, observation, refs, action, **kwargs))

    def catalog_satisfied(catalog_id):
        return any(item.get("catalog_id") == catalog_id and item.get("status") == "fulfilled" for item in case.get("requests", []))

    def request_action(catalog_id, reason):
        pending = next((item for item in case.get("requests", []) if item.get("catalog_id") == catalog_id and item.get("status") != "fulfilled"), None)
        if pending:
            return {"type": "review_task", "reason": "기존 자료 요청의 수신·범위·내용을 확인합니다. " + reason, "request_id": pending["id"]}
        return {"type": "document_request", "catalog_id": catalog_id, "period": "담당자 확인", "reason": reason,
                "requires_scope_confirmation": True}

    # Request state proves that verification is unfinished, not that an asset/income is absent.
    for request in case.get("requests", []):
        if request.get("status") in {"fulfilled", "cancelled", "deleted"}:
            continue
        record = by_id.get("requests:" + request["id"])
        if not record:
            continue
        active_docs = [documents[doc_id] for doc_id in request.get("document_ids", []) if doc_id in documents]
        if not active_docs:
            observation = "현재 요청에 연결된 유효한 파일이 없습니다. 다른 경로로 받은 자료나 발급 장애가 있는지 확인하고 요청 범위를 안내하세요."
            title = "미수신 자료 확인 · " + request["title"]
        else:
            observation = "연결된 파일을 수신했지만 요청은 충족 확정 전입니다. 대상자·기간·문서 종류와 원문 내용을 대조하세요."
            title = "수신 후 검증 · " + request["title"]
        add("request:" + request["id"], title, observation, [_ref(record)],
            {"type": "review_task", "reason": observation, "request_id": request["id"]},
            rule_ids=("K003", "K005"), query="신청 첨부서류 " + request["title"])

    candidates = [candidate for source in primary for candidate in _income_candidates(source)]
    income_pair = None
    for left in candidates:
        for right in candidates:
            if left["source"]["id"] == right["source"]["id"] or left["amount"] == right["amount"]:
                continue
            if left["source"].get("document_id") and left["source"].get("document_id") == right["source"].get("document_id"):
                continue
            if left["source"]["kind"] == "case_document" or right["source"]["kind"] == "case_document":
                income_pair = (left, right)
                break
        if income_pair:
            break
    if income_pair:
        left, right = income_pair
        known_difference = {left["basis"], right["basis"]} == {"net", "gross"}
        title = "소득 기준 대조 · 세전/세후 구분" if known_difference else "소득 진술·자료의 금액 후보 차이"
        observation = f"원문에서 {left['amount']:,}원과 {right['amount']:,}원 후보가 검색되었습니다. "
        observation += "세전과 세후가 구분되어 있어 단순 모순으로 판단하지 않습니다. " if known_difference else "대상 월·지급자·세전/세후가 같은지 먼저 확인하세요. "
        observation += "후보값으로 월 소득을 확정하지 않습니다."
        add("income-comparison", title, observation,
            [_ref(left["source"], left["quote"]), _ref(right["source"], right["quote"])],
            request_action("D36", "급여 자료와 상담 금액 후보의 차이를 대상 월·세전/세후 기준 및 급여 입금 원문으로 대조합니다."),
            severity="attention", rule_ids=("K002", "K006"), query="소득 급여 증명")

    family_paid = None
    for source in primary:
        for match in re.finditer(r"가족|어머니|아버지|부모|배우자|남편|아내|형제|누나|언니|동생", source["text"]):
            quote = _quote_near(source["text"], match.start(), match.end())
            if re.search(r"갚|완납|대신\s*(?:납부|변제)|대납", quote) and not re.search(r"갚지\s*않|갚은\s*적.*없|완납.*아니|완납.*못|대납.*없", quote):
                family_paid = (source, quote)
                break
        if family_paid:
            break
    if family_paid:
        source, quote = family_paid
        add("family-payment", "가족의 채무 변제 진술 · 지급 관계 확인",
            "가족이 채무를 갚았다는 진술 또는 기재가 있습니다. 완납 여부·실제 지급자·자금출처·가족에게 갚을 의무를 확인할 자료가 필요합니다. 채권 제외나 대위 발생을 확정하지 않습니다.",
            [_ref(source, quote)], request_action("D46", "가족의 대납 진술을 완납 확인 및 송금 원문과 대조하고 가족간 채무·대위 관계는 변호사 검토에 연결합니다."),
            severity="attention", rule_ids=("K002",), query="채권 채무 변제")

    # Explicit missing-document language only. A filename or generic office notice is not enough.
    missing_patterns = [("D36", r"통장\s*내역|계좌\s*(?:거래)?내역|거래\s*내역|급여\s*입금"),
                        ("D07", r"급여\s*명세서|급여명세"), ("D05", r"소득\s*금액\s*증명"),
                        ("D38", r"부채\s*증명|채무\s*확인서"), ("D03", r"가족\s*관계\s*증명")]
    requested_catalogs = {item["action"].get("catalog_id") for item in findings}
    for catalog_id, pattern in missing_patterns:
        if catalog_id not in catalog or catalog_id in requested_catalogs or catalog_satisfied(catalog_id):
            continue
        match_source = None
        for source in primary:
            for match in re.finditer(pattern, source["text"]):
                # Scan the same sentence to avoid transferring another document's absence.
                right = re.split(r"[。.!?\n]", source["text"][match.end():match.end() + 55])[0]
                if re.search(r"미제출|미수신|제출\s*하지\s*않|없(?:음|습|어|다)|누락|준비\s*못|못\s*받|발급\s*(?:대기|불가)|아직", right):
                    match_source = (source, _quote_near(source["text"], match.start(), match.end() + len(right)))
                    break
            if match_source:
                break
        if match_source:
            source, quote = match_source
            reason = "원문에 자료 미제출·발급 장애 기재가 있습니다. 다른 경로의 수신 여부와 필요한 기간을 확인하세요."
            add("missing:" + catalog_id, "부족자료 후보 · " + catalog[catalog_id]["name"], reason,
                [_ref(source, quote)], request_action(catalog_id, reason), rule_ids=("K003", "K005"),
                query=catalog[catalog_id]["name"] + " 첨부서류")
            requested_catalogs.add(catalog_id)

    for source in sources:
        if source.get("status") == "text_unavailable":
            add("unreadable:" + source["document_id"], "문자 추출 필요 · " + source["title"],
                "파일은 수신했으나 검색할 원문 텍스트가 없습니다. 스캔·이미지는 담당자의 원문 확인 또는 OCR 처리가 필요하며 AI 판독 완료로 표시하지 않습니다.",
                [_ref(source)], {"type": "review_task", "reason": "원문을 열어 스캔·암호화·추출 실패를 확인하고 읽을 수 있는 자료를 준비합니다.", "document_id": source["document_id"]}, rule_ids=("K003",))

    for deadline in case.get("deadlines", []):
        if deadline.get("status") in {"cancelled", "completed"}:
            continue
        if deadline.get("due_date") and deadline.get("evidence_id"):
            continue
        record = by_id.get("deadlines:" + deadline["id"])
        if record:
            add("deadline:" + deadline["id"], "송달 근거·보정 기한 확인",
                "기한 기록에 만료일 또는 송달 증거가 연결되지 않았습니다. 실제 송달일과 법원 원문의 기간을 확인한 후 담당자가 기한을 확정해야 합니다.",
                [_ref(record)], {"type": "review_task", "reason": "송달증거와 법원 원문을 연결하고 기한 및 대체 담당자를 확인합니다.", "deadline_id": deadline["id"]},
                severity="urgent", query="보정 송달 기간")

    for correction in case.get("corrections", []):
        if correction.get("status") in {"approved", "reviewed", "closed"} and correction.get("answer"):
            continue
        record = by_id.get("corrections:" + correction["id"])
        document_id = correction.get("source_document_id")
        court_page = next((source for source in primary if source.get("document_id") == document_id
                           and source.get("page") == correction.get("source_page", 1)), None)
        if not record:
            continue
        if not court_page:
            add("correction-source:" + correction["id"], "보정 요구의 법원 원문 연결",
                "보정 항목은 등록되었으나 해당 요구를 대조할 원문 페이지를 찾지 못했습니다. 요청 계좌·기간·항목을 원문에서 확인하세요.",
                [_ref(record)], {"type": "review_task", "reason": "보정권고·명령의 문서와 페이지를 연결합니다.", "correction_id": correction["id"]}, severity="attention")
        elif context["kind"] == "correction":
            requirement = correction.get("requirement") or correction.get("title", "보정 요구")
            answer = "[담당자 검토용 · 미확정 초안]\n법원 요구: " + requirement + "\n\n원문 대조: " + court_page["title"] + f" {court_page['page']}쪽\n요구 기간·계좌·대상: [원문 대조 후 입력]\n사실관계: [확인된 자료와 진술을 구분하여 입력]\n첨부 근거: [문서명·페이지·증명 범위 입력]\n미확보 자료 및 추가 확인: [담당자 입력]"
            add("correction-draft:" + correction["id"], "보정 답변 검토 틀 · " + correction.get("title", "법원 요구"),
                "등록된 법원 요구와 연결된 원문 페이지로 답변 검토 틀을 구성했습니다. 사실·소명·첨부 부분은 미확정이며 담당자 작성과 변호사 검토가 필요합니다.",
                [_ref(record), _ref(court_page)], {"type": "correction_draft", "correction_id": correction["id"], "answer": answer,
                    "reason": "법원 요구별 사실·첨부·미확보 항목을 확인한 후 초안으로 저장합니다."}, rule_ids=("K001", "K003"), query="보정 서류")
    return findings


FACTOR_LABELS = {"monthly_income": "월 소득", "total_debt": "채무 총액", "living_expenses": "월 생활비",
    "assets_total": "재산 총액", "household_size": "가구원 수", "address": "주소", "employer": "근무처",
    "employment_type": "소득 형태", "housing_cost": "월 주거비"}
FACTOR_LABELS.update({"client_name": "의뢰인 성명", "income_period": "소득 산정 기간", "housing_deposit": "임차보증금",
    "creditors": "채권자별 채무 내역", "dependents": "부양 진술", "business_revenue": "사업 수입", "business_expenses": "사업 필요경비",
    "repayment_date": "제3자 완납일", "payer": "제3자 지급자", "fund_source": "자금출처", "family_legal_relation": "가족간 약정 진술",
    "remaining_debt": "완납 후 잔존채무", "recent_borrowing": "최근 차입", "fund_usage": "자금 사용처", "disposal_proceeds": "재산 처분대금",
    "tax_arrears": "세금 체납", "insurance_surrender": "보험 해약환급금", "prior_proceedings": "과거 절차 이력",
    "service_date": "송달일 기재", "account_scope": "본인 계좌 범위"})
MONEY_PATTERN = r"(?<![\d,.\-−])\d[\d,]*(?:\.\d+)?\s*(?:억\s*)?(?:\d[\d,]*(?:\.\d+)?\s*)?(?:천\s*만|백\s*만|만)?\s*원"


def _money_value(text: str) -> int | None:
    compact = re.sub(r"[\s,]", "", text)
    match = re.fullmatch(r"(?:(\d+(?:\.\d+)?)억)?(?:(\d+(?:\.\d+)?)(천만|백만|만)?)?원", compact)
    if not match or not any((match.group(1), match.group(2))):
        return None
    result = float(match.group(1) or 0) * 100000000
    result += float(match.group(2) or 0) * {None: 1, "만": 10000, "백만": 1000000, "천만": 10000000}[match.group(3)]
    return int(result) if 0 <= result <= 10**13 else None


def _candidate(key: str, value, source: dict, quote: str, origin="rulebook", **extra):
    return {"id": "factor-" + _digest([key, value, source["id"], quote, origin])[:14],
            "key": key, "label": FACTOR_LABELS.get(key, key), "value": value, "quote": quote,
            "status": "candidate", "origin": origin, "source_type": source["kind"], "source_id": source["id"],
            "document_id": source.get("document_id"), "page": source.get("page"),
            "source_version": source.get("version", 1), "evidence_refs": [_ref(source, quote)], **extra}


def extract_factor_candidates(sources: list[dict]) -> list[dict]:
    """Conservative phrase/amount extraction; every value remains an unconfirmed candidate."""
    from . import document_facts
    typed = document_facts.extract(sources)
    typed_by_source = {}
    for row in typed:
        typed_by_source.setdefault(row['source_id'], []).append(row)
    candidates = []
    patterns = {
        "monthly_income": r"월\s*(?:평균\s*)?(?:소득|급여|수입|보수)|월급|실수령(?:액)?|실지급(?:액)?|차인지급(?:액)?|급여\s*(?:총액|합계)",
        # A bare debt label can describe one family loan or one creditor balance.
        # Only an explicit aggregate label is eligible for the case-wide total.
        "total_debt": r"(?:전체|총)\s*(?:채무액?|부채|빚|대출)|(?:채무액?|부채|빚|대출)\s*(?:은|는|이|가|:|：)?\s*(?:총액|총계|합계|전체|총)",
        "recent_borrowing": r"최근\s*(?:대출|차입)|신규\s*대출|카드론|현금서비스",
        "living_expenses": r"월\s*생활비|생활비|생계비",
        "assets_total": r"재산\s*(?:총액|합계)|총\s*재산|자산\s*(?:총액|합계)|총\s*자산",
        "housing_cost": r"월세|월\s*주거비|월\s*임대료",
        "housing_deposit": r"(?:임차|임대차|전세)?\s*보증금",
        "business_revenue": r"사업\s*(?:수입|매출)|월\s*매출|매출액",
        "business_expenses": r"사업\s*(?:경비|비용)|필요\s*경비",
        "insurance_surrender": r"해약\s*환급금|해지\s*환급금",
        "tax_arrears": r"체납\s*(?:액|세금|금액)|세금\s*체납|국세\s*체납|지방세\s*체납",
        "remaining_debt": r"잔존\s*채무|완납\s*후\s*잔액|남은\s*채무",
    }
    for source in sources:
        if source["kind"] not in CASE_KINDS:
            continue
        text = source["text"]
        for money in re.finditer(MONEY_PATTERN, text):
            if re.search(r"[-−]\s*$", text[max(0, money.start() - 4):money.start()]):
                continue
            amount = _money_value(money.group())
            if amount is None:
                continue
            left = text[max(0, money.start() - 40):money.start()]
            # Prefer the nearest labelled factor; otherwise one amount could become both
            # income and debt in a single short sentence.
            matches = [(key, match) for key, pattern in patterns.items() for match in re.finditer(pattern, left)]
            if not matches:
                continue
            key, label = max(matches, key=lambda pair: pair[1].end())
            tail = left[label.end():]
            if re.search(r"[。.!?\n]|\d.*원", tail) or len(tail) > 22:
                continue
            near = left[label.start():] + money.group()
            if key == "total_debt":
                label_start = money.start() - len(left) + label.start()
                prefix = re.split(r"[。.!?\n]", text[max(0, label_start-100):label_start])[-1]
                after_amount = re.split(r"[。.!?\n]", text[money.end():money.end()+35])[0]
                individual = (r"(?:가족|친족|친척|부모님?|어머니|아버지|배우자|남편|아내|누나|언니|동생|"
                              r"개별|당행|당사|해당\s*채권자|채권자\s*\d+|"
                              r"[^\s,.;]*(?:은행|카드|저축|캐피탈|대부)[^\s,.;]*)"
                              r"\s*(?:의|에게|에\s*대한|에게\s*진)?\s*$")
                conditional = r"만약|가정|추정|예상|(?:인정|확정|추가|포함|제외|합산|반영|변제|상환).{0,8}(?:되면|하면|한\s*경우|할\s*경우)"
                # Explicit aggregation still does not turn one creditor's total,
                # a component introduced by '중', or an if-clause into a fact.
                if re.search(individual, prefix) or re.search(r"(?:중|가운데|원금|이자)\s*", tail):
                    continue
                if re.search(conditional, prefix + near) or re.search(r"가정|추정|예상|일\s*수", after_amount):
                    continue
            if key == "monthly_income" and re.search(r"연봉|연간|연\s*소득|귀속|사업\s*매출", near):
                continue
            quote_start = max(0, money.start() - len(left) + label.start() - 8)
            quote = text[quote_start:min(len(text), money.end() + 12)]
            basis = "net" if re.search(r"세후|실수령|실지급|차인지급", quote) else "gross" if "세전" in quote else "unspecified"
            candidates.append(_candidate(key, amount, source, quote, amount_basis=basis))
        for match in re.finditer(r"(?:가구원|가족\s*(?:구성원|인원)?|세대원)\s*(?:수|은|는|이|가|:|：)?\s*(\d{1,2})\s*(?:명|인)|(?<!\d)(\d{1,2})\s*인\s*가구", text):
            value = int(match.group(1) or match.group(2))
            if 1 <= value <= 30:
                candidates.append(_candidate("household_size", value, source, match.group(),
                    limitation="가구원 수 후보이며 법원이 인정할 부양인원과 다릅니다."))
        for key, pattern in (("address", r"(?:거주지|거주\s*주소|주소)\s*[:：]\s*([^\n,;.]{3,70})"),
                             ("employer", r"(?:근무처|회사명|직장명|사업장명)\s*[:：]\s*([^\n,;.]{2,50})")):
            for match in re.finditer(pattern, text):
                value = match.group(1).strip().rstrip(".")
                if value and value not in {"미확인", "모름", "미상", "확인 필요"}:
                    candidates.append(_candidate(key, value, source, match.group()))
        # Employment is extracted from a complete scoped clause below. A bank
        # note saying '급여외 소득 없음' must never become '무직 진술'.
        if source.get("field") in FACTOR_LABELS and source.get("numeric_value") is not None:
            candidates.append(_candidate(source["field"], source["numeric_value"], source, source["text"]))
        explicit_labels = {
            "client_name": r"의뢰인|내담자|성명|성함|고객명",
            "income_period": r"소득\s*산정\s*기간|급여\s*산정\s*기간|지급\s*대상\s*기간|지급월|급여월",
            "creditors": r"채권자(?:별\s*(?:내역|채무|잔액))?|채권\s*내역",
            "dependents": r"부양\s*대상|부양가족|실제\s*부양|양육\s*현황",
            "repayment_date": r"완납일|대납일|대신\s*갚은\s*날",
            "payer": r"지급자|대납자|송금인|송금자",
            "fund_source": r"자금\s*출처|송금\s*원천|완납\s*자금",
            "family_legal_relation": r"가족간\s*(?:약정|관계)|대여\s*약정|증여\s*약정|반환\s*약정",
            "remaining_debt": r"잔존\s*채무|완납\s*후\s*잔액|남은\s*채무",
            "recent_borrowing": r"최근\s*차입|최근\s*대출|차입\s*내역|신규\s*대출",
            "fund_usage": r"자금\s*사용처|대출금\s*사용처|처분대금\s*사용처|최종\s*사용처",
            "disposal_proceeds": r"재산\s*처분|처분\s*대금|매각\s*대금|매각\s*내역",
            "tax_arrears": r"세금\s*체납|체납\s*내역|국세\s*체납|지방세\s*체납",
            "prior_proceedings": r"과거\s*절차|과거\s*회생|과거\s*파산|면책\s*이력|이전\s*사건",
            "service_date": r"송달일|실제\s*송달일|송달\s*일자",
            "account_scope": r"본인\s*계좌|전체\s*계좌|계좌\s*범위|계좌\s*목록",
        }
        for key, label in explicit_labels.items():
            for match in re.finditer(r"(?:" + label + r")\s*[:：]\s*([^\n;]{1,120})", text):
                value = match.group(1).strip()
                # A labelled unknown is a gap, never a usable factor or zero amount.
                if value in {"미상", "모름", "미확인", "확인 필요", "알 수 없음"}:
                    continue
                if key == "client_name":
                    person = re.match(r"[가-힣]{2,5}(?=\s|[,.;]|$)", value)
                    if not person:
                        continue
                    value = person.group()
                if key in {"repayment_date", "service_date", "income_period"}:
                    value = value[:80]
                candidates.append(_candidate(key, value, source, match.group(),
                    limitation="원문에 기재된 진술 후보이며 법률관계나 실제 충족 여부를 확정하지 않습니다."))
        payer = re.search(r"(어머니|아버지|부모님|배우자|남편|아내|누나|언니|동생|형)(?:이|가|께서)?[^.\n]{0,30}?(?:대신\s*)?(?:갚았습니다|갚아|갚았|완납했|대납했)", text)
        if payer:
            candidates.append(_candidate("payer", payer.group(1), source, payer.group(),
                limitation="지급자로 언급된 가족의 관계 후보입니다. 대위·증여·대여를 추정하지 않습니다."))
    # Typed document values take precedence over broad phrase matches. Annual
    # income, one creditor's balance and payroll deductions keep their own keys.
    filtered = []
    for candidate in candidates:
        rows = typed_by_source.get(candidate['source_id'], [])
        if candidate['key'] in {row['key'] for row in rows}:
            continue
        if candidate['key'] == 'monthly_income' and any(row['key'] in {'income_gross', 'income_net'} for row in rows):
            continue
        if candidate['key'] == 'total_debt' and candidate.get('document_id') and any(
                row['key'] == 'creditor_total' and row.get('scope') == 'creditor' for row in rows):
            continue
        filtered.append(candidate)
    candidates = filtered
    lookup = {source['id']: source for source in sources}
    for row in typed:
        source = lookup[row['source_id']]
        extra = {key: value for key, value in row.items() if key not in {
            'id', 'key', 'value', 'quote', 'source_id', 'document_id', 'page', 'source_type', 'source_version', 'status', 'origin'}}
        candidates.append(_candidate(row['key'], row['value'], source, row['quote'], origin=document_facts.VERSION,
                                     typed_fact_id=row['id'], **extra))
    for source_id, rows in typed_by_source.items():
        monthly = [row for row in rows if row['key'] in {'income_net', 'income_gross'} and row.get('frequency') == 'monthly']
        # Multiple document periods stay separate; a gross figure must not win
        # over the corresponding net figure because it occurred later in OCR.
        for row in monthly:
            if row['key'] == 'income_gross' and any(other['key'] == 'income_net' and
                    (other.get('period_start'), other.get('period_end')) == (row.get('period_start'), row.get('period_end')) for other in monthly):
                continue
            candidates.append(_candidate('monthly_income', row['value'], lookup[source_id], row['quote'], origin=document_facts.VERSION,
                typed_fact_id=row['id'], amount_basis=row.get('basis'), frequency='monthly',
                period_start=row.get('period_start'), period_end=row.get('period_end'),
                line_start=row['line_start'], line_end=row['line_end'], limitation='해당 문서·기간의 월 소득 후보이며 인정 소득 확정 전입니다.'))
    # Overlapping retrieval windows retain the same source document and exact quote once.
    result, seen = [], set()
    for candidate in candidates:
        identity = (candidate['key'], candidate['typed_fact_id']) if candidate.get('typed_fact_id') else (candidate["key"], _text(candidate["value"]), candidate.get("document_id") or candidate["source_id"].rsplit(":", 1)[0], candidate["quote"])
        if identity not in seen:
            seen.add(identity)
            result.append(candidate)
    return result


def check_documents(case: dict, candidates: list[dict]) -> list[dict]:
    classifications = [("급여명세서", "D07", r"급여\s*명세|급료\s*명세|실지급액|차인지급액"),
        ("소득금액증명", "D05", r"소득\s*금액\s*증명"), ("계좌 거래내역", "D36", r"거래\s*내역|입출금\s*내역|거래일.*출금.*입금"),
        ("부채증명서", "D38", r"부채\s*증명|채무\s*잔액\s*확인|채무\s*확인서"),
        ("가족관계증명서", "D03", r"가족\s*관계\s*증명"), ("주민등록표 등본", "D01", r"주민\s*등록.*등본"),
        ("임대차계약서", "D33", r"임대차\s*계약"), ("보정권고·명령", None, r"보정\s*권고|보정\s*명령")]
    result = []
    for document in case.get("documents", []):
        if not _active(document):
            continue
        pages = document.get("page_texts") or []
        text = "\n".join(_text(page.get("text", "") if isinstance(page, dict) else page) for page in pages)
        text = text or document.get("text") or document.get("extracted_text") or ""
        classification, catalog_id, class_quote = "문서 종류 미확인", None, None
        title = document.get("filename") or document.get("name") or document.get("title") or ""
        meeting = re.search(r"상담\s*(?:회의록|기록|일지)|회의록", text[:150])
        if meeting and re.search(r"상담|회의록", title):
            classification, class_quote = "상담 회의록", meeting.group()
        else:
            from .document_facts import classification as typed_classification
            typed_class = typed_classification(text)
            if typed_class:
                classification, catalog_id, class_quote = typed_class['classification'], typed_class['catalog_id'], typed_class['quote']
            for label, cid, pattern in classifications:
                if typed_class:
                    break
                match = re.search(pattern, text[:250])
                if not match:
                    continue
                sentence = re.split(r"[.\n]", text[match.start():match.end() + 45])[0]
                if re.search(r"미제출|제출\s*전|제출\s*필요|요청|없음|미확보", sentence):
                    continue
                classification, catalog_id, class_quote = label, cid, match.group()
                break
        linked = [request for request in case.get("requests", []) if document["id"] in request.get("document_ids", []) or request["id"] == document.get("request_id")]
        related = [request for request in case.get("requests", []) if catalog_id and request.get("catalog_id") == catalog_id]
        requests = linked or related
        missing = []
        status = "needs_more"
        if not text.strip():
            status, reason = "unreadable", "문자 원문이 없어 내용·문서 종류·값을 판독할 수 없습니다."
            missing = ["읽을 수 있는 원문 또는 OCR 결과"]
        else:
            person = re.search(r"(?:성명|이름|성함)\s*[:：]?\s+([가-힣]{2,5})(?:\s|$|[.,;])", text)
            expected_name = case.get("client_name", "")
            if person and expected_name and "미확인" not in expected_name and person.group(1) != expected_name:
                status, reason = "identity_conflict", f"문서의 성명 기재가 사건 의뢰인과 다릅니다. 대상자와 가족 자료 여부를 확인해야 합니다."
                missing = ["대상자 일치 또는 관계 설명"]
            else:
                if not catalog_id and classification == "문서 종류 미확인":
                    missing.append("문서 종류")
                if not expected_name or expected_name not in text:
                    missing.append("의뢰인과 자료 대상자 대조")
                if not requests:
                    missing.append("해당 자료 요청 및 필요한 범위 연결")
                for request in requests:
                    period = request.get("period", "")
                    period_dates = re.findall(r"\d{4}[-./]\d{1,2}(?:[-./]\d{1,2})?", period)
                    if not period_dates or not all(date in text for date in period_dates):
                        missing.append("요청 기간·범위 대조")
                    if catalog_id and request.get("catalog_id") != catalog_id:
                        missing.append("요청 서류 종류와 원문 분류 대조")
                status = "needs_more" if missing else "sufficient_candidate"
                reason = "원문 종류·이름·요청 기간 문자열이 대응합니다. 충분성 후보이며 담당자 검증 전입니다." if not missing else "문서 종류와 값 후보를 추출했으나 " + "·".join(dict.fromkeys(missing)) + "이 필요합니다."
        result.append({"document_id": document["id"], "classification": classification, "catalog_id": catalog_id,
            "classification_quote": class_quote, "origin": "rulebook", "coverage_status": status,
            "reason": reason, "missing": list(dict.fromkeys(missing)), "matched_request_ids": [item["id"] for item in requests],
            "extracted_fact_ids": [item["id"] for item in candidates if item.get("document_id") == document["id"]]})
    return result


def _workflow_findings(case, context, extracted_facts):
    try:
        from .rulebook import evaluate_case
        evaluation = evaluate_case({**case, "extraction_candidates": extracted_facts})
    except ImportError:
        return [], {"status": "unavailable", "matched_rules": [], "required_documents": []}
    findings = []
    requested = {item.get("catalog_id") for item in case.get("requests", []) if item.get("status") != "cancelled"}
    for rule in evaluation.get("matched_rules", []):
        references = []
        for trigger in rule.get("trigger_refs", []):
            quote = trigger.get("quote", "")
            source = next((item for item in context["case_sources"] if quote and quote in item["text"]
                and (not trigger.get("document_id") or item.get("document_id") == trigger["document_id"])), None)
            if source:
                references.append(_ref(source, quote))
        if not references:
            continue
        for need in rule.get("required_documents", []):
            if need["catalog_id"] in requested:
                continue
            requested.add(need["catalog_id"])
            item = _finding(context, "workflow:" + rule["id"] + ":" + need["catalog_id"],
                need.get("name", "상황별 자료") + " · " + rule["title"],
                rule["reason"], references[:2], {"type": "document_request", "catalog_id": need["catalog_id"],
                    "period": need.get("period", "담당자 확인"), "reason": need["reason"], "requires_scope_confirmation": True},
                query=need.get("name", "첨부 서류"))
            item["workflow_rule_ids"] = [rule["id"]]
            item["factor_keys"] = need.get("factor_keys", [])
            findings.append(item)
    return findings, evaluation


def _intake_baseline(text: str) -> dict:
    source = _source("intake:transcript", "party_statement", "상담 회의록 원문", text, status="unverified_statement")
    facts = extract_factor_candidates([source])
    name_match = re.search(r"(?:의뢰인|내담자|고객명|성명|성함|이름)\s*(?:은|는|:|：)?\s*([가-힣]{2,5})(?=\s|[,.;]|입니다|씨|님|이고|$)", text)
    if not name_match:
        name_match = re.search(r"([가-힣]{2,4})\s*(?:씨|님)\s*(?:은|는|이|가)?", text)
    if not name_match:
        name_match = re.search(r"(?:저는|이름은)\s*([가-힣]{2,5})(?:이라고|입니다)", text)
    invalid_names = {"미확인", "담당자", "변호사", "아버지", "어머니", "의뢰인"}
    client_name = name_match.group(1) if name_match and name_match.group(1) not in invalid_names else None
    regions = "서울|부산|대구|인천|광주|대전|울산|세종|수원|성남|안양|평택|용인|고양|의정부|강릉|춘천|원주|청주|천안|전주|창원|제주|경기|강원|충북|충남|전북|전남|경북|경남"
    region_match = re.search(r"(?:거주지|거주\s*주소|주소|지역)\s*[:：]?\s*(" + regions + r")(?:특별시|광역시|특별자치시|시|도)?", text)
    if not region_match:
        region_match = re.search(r"(" + regions + r")(?:특별시|광역시|시|도)?(?:\s*[가-힣]{1,8}(?:구|동|시))?\s*(?:에|에서)?\s*(?:거주|살고|사는)", text)
    region = region_match.group(1) if region_match else None
    case_type = "bankruptcy_review" if re.search(r"개인\s*파산|파산\s*(?:상담|신청|검토|하고)|면책\s*상담", text) else "personal_rehabilitation" if re.search(r"개인\s*회생|회생\s*(?:상담|신청|검토|하고)", text) else "unknown"
    return {"client_name": client_name, "region": region, "case_type": case_type, "extracted_facts": facts,
            "identity_evidence": {"client_name": name_match.group() if client_name else None,
                                  "region": region_match.group() if region_match else None}, "origin": "rulebook"}


def _intake_spans(text: str) -> list[dict]:
    spans = []
    for part in re.split(r"(?<=[.!?])\s+|(?<!\d),(?!\d)|\n|(?<=이고)\s+|(?<=이며)\s+", text[:11000]):
        part = part.strip()
        if not part:
            continue
        for offset, excerpt in _windows(part, size=450, overlap=40):
            spans.append(_source("T" + str(len(spans) + 1), "party_statement", "상담 원문 구간", excerpt, status="unverified_statement"))
            if len(spans) >= 40:
                return spans
    return spans


def _resolve_intake_selection(output: IntakeSelection, spans: list[dict]) -> tuple[IntakeAnalysis, list[str]]:
    lookup = {source["id"]: source for source in spans}
    errors = []
    identities = {}
    for field in ("client_name", "region"):
        source_id = getattr(output, field + "_source_id")
        value, quote = None, ""
        if source_id:
            source = lookup.get(source_id)
            if not source:
                errors.append("UNKNOWN_SOURCE_ID")
            else:
                extracted = _intake_baseline(source["text"])
                value = extracted[field]
                quote = extracted["identity_evidence"].get(field) or ""
                if not value:
                    errors.append("IDENTITY_SOURCE_MISMATCH")
        identities[field] = IntakeIdentity(value=value, quote=quote)
    source = lookup.get(output.case_type_source_id)
    quote = ""
    resolved_case_type = output.case_type
    if source:
        term = re.search(r"개인\s*회생|회생|개인\s*파산|파산|면책", source["text"])
        quote = _quote_near(source["text"], term.start(), term.end(), 90) if term else source["text"][:90]
    if (resolved_case_type == "personal_rehabilitation" and "회생" not in quote) or (resolved_case_type == "bankruptcy_review" and not re.search(r"파산|면책", quote)) or (resolved_case_type != "unknown" and not quote):
        errors.append("CASE_TYPE_SOURCE_MISMATCH")
        resolved_case_type, quote = "unknown", ""
    facts = []
    for selection in output.factors:
        source = lookup.get(selection.source_id)
        if not source:
            errors.append("UNKNOWN_SOURCE_ID")
            continue
        candidates = [item for item in extract_factor_candidates([source]) if item["key"] == selection.key]
        distinct = {_text(item["value"]) for item in candidates}
        if len(distinct) != 1:
            errors.append("FACTOR_SOURCE_MISMATCH")
            continue
        candidate = candidates[0]
        facts.append(IntakeFact(key=selection.key, value=candidate["value"], quote=candidate["quote"][:100]))
    resolved = IntakeAnalysis(**identities, case_type=resolved_case_type, case_type_quote=quote, facts=facts)
    return resolved, list(dict.fromkeys(errors))


def _validate_intake(output: IntakeAnalysis, text: str) -> list[str]:
    errors = []
    for identity in (output.client_name, output.region):
        if identity.value is not None and (identity.quote not in text or not identity.quote or identity.value not in identity.quote):
            errors.append("UNGROUNDED_IDENTITY")
    if output.case_type != "unknown" and (not output.case_type_quote or output.case_type_quote not in text):
        errors.append("UNGROUNDED_CASE_TYPE")
    # A case type is the stated consultation intention, not model legal eligibility.
    if output.case_type == "personal_rehabilitation" and "회생" not in output.case_type_quote:
        errors.append("CASE_TYPE_NOT_STATED")
    if output.case_type == "bankruptcy_review" and not re.search(r"파산|면책", output.case_type_quote):
        errors.append("CASE_TYPE_NOT_STATED")
    for fact in output.facts:
        if fact.quote not in text:
            errors.append("QUOTE_MISMATCH")
            continue
        if isinstance(fact.value, int):
            if fact.key == "household_size":
                if str(fact.value) not in re.findall(r"\d+", fact.quote) or not re.search(r"가족|가구|세대|동거", fact.quote):
                    errors.append("UNGROUNDED_FACTOR")
            else:
                values = {_money_value(match.group()) for match in re.finditer(MONEY_PATTERN, fact.quote)}
                if fact.value not in values:
                    errors.append("UNGROUNDED_NUMBER")
        elif fact.value not in fact.quote:
            from .document_facts import employment_observations
            if fact.key != "employment_type" or not any(row['value'] == fact.value for row in employment_observations(fact.quote)):
                errors.append("UNGROUNDED_FACTOR")
        if fact.key == "monthly_income" and re.search(r"연봉|연간|연\s*소득|매출", fact.quote) and not re.search(r"월급|월\s*소득|실수령", fact.quote):
            errors.append("INCOME_PERIOD_MISMATCH")
    return list(dict.fromkeys(errors))


async def analyze_intake(text: str) -> dict:
    """Read a meeting transcript, extract draft facts, and evaluate preparation rules."""
    started = time.perf_counter()
    baseline = _intake_baseline(text)
    config = model_client.provider_config()
    model_label = "DeepSeek" if config["provider"] == "deepseek" else "Llama"
    metrics = {**config, "harness_version": HARNESS_VERSION,
               "timeout_seconds": MODEL_TIMEOUT, "max_output_tokens": MODEL_MAX_TOKENS,
               "max_model_calls": 1, "model_status": "disabled", "input_characters": len(text), "cached": False}
    metrics['prompt']=prompt_registry.metadata('intake')
    error = None
    use_model = os.getenv("DEBTOFF_AX_MODEL", "on").lower() not in {"off", "disabled", "0", "false"}
    if not text.strip():
        metrics["model_status"] = "no_case_evidence"
        error = {"code": "EMPTY_TRANSCRIPT", "message": "상담 회의록 원문을 입력하세요."}
    elif use_model and _MODEL_GATE.acquire(blocking=False):
        try:
            spans = _intake_spans(text)
            schema = IntakeSelection.model_json_schema()
            schema["$defs"]["SelectedFactor"]["properties"]["source_id"]["enum"] = [source["id"] for source in spans]
            for field in ("client_name_source_id", "region_source_id", "case_type_source_id"):
                schema["properties"][field] = {"enum": [None] + [source["id"] for source in spans]}
            raw = await model_client.generate([
                        {"role": "system", "content": prompt_registry.instruction('intake')},
                        {"role": "user", "content": json.dumps([{"id": source["id"], "text": source["text"]} for source in spans], ensure_ascii=False)},
                    ], schema, timeout=MODEL_TIMEOUT, max_tokens=MODEL_MAX_TOKENS)
            metrics.update(prompt_tokens=raw.get("prompt_eval_count"), completion_tokens=raw.get("eval_count"),
                generation_seconds=round(raw.get("eval_duration", 0) / 1e9, 3), load_seconds=round(raw.get("load_duration", 0) / 1e9, 3),
                done_reason=raw.get("done_reason"))
            if raw.get("done_reason") == "length" or not raw.get("done", False):
                metrics["model_status"] = "failed"
                error = {"code": "TRUNCATED_MODEL_OUTPUT", "message": "Llama 출력이 중단되어 회의록의 규칙 추출 후보만 사용합니다."}
            else:
                selection = IntakeSelection.model_validate_json(raw["message"]["content"])
                output, discarded = _resolve_intake_selection(selection, spans)
                # Validate each selected field independently. A wrong case-type span
                # must not hide separately grounded name or income candidates.
                defects = _validate_intake(output, text[:11000])
                if not output.facts and not output.client_name.value and not output.region.value and output.case_type == "unknown":
                    defects.append("NO_GROUNDED_MODEL_FIELDS")
                metrics["discarded_model_fields"] = discarded
                metrics["partial_output"] = bool(discarded)
                if defects:
                    metrics["model_status"] = "failed"
                    error = {"code": "EVIDENCE_VALIDATION_FAILED", "checks": defects, "message": "회의록 원문 인용·값 검증을 통과하지 못해 규칙 추출만 표시합니다."}
                else:
                    source = _source("intake:transcript", "party_statement", "상담 회의록 원문", text, status="unverified_statement")
                    model_origin = "deepseek" if config["provider"] == "deepseek" else "llama3"
                    model_facts = [_candidate(fact.key, fact.value, source, fact.quote, model_origin,
                        extraction_method="model_field_selection_and_deterministic_value_parser") for fact in output.facts]
                    model_keys = {(fact["key"], _text(fact["value"])) for fact in model_facts}
                    baseline["extracted_facts"] = model_facts + [fact for fact in baseline["extracted_facts"] if (fact["key"], _text(fact["value"])) not in model_keys]
                    for field in ("client_name", "region"):
                        identity = getattr(output, field)
                        if identity.value:
                            baseline[field] = identity.value
                            baseline["identity_evidence"][field] = identity.quote
                            baseline.setdefault("field_origins", {})[field] = model_origin
                    if output.case_type != "unknown":
                        baseline["case_type"] = output.case_type
                        baseline.setdefault("field_origins", {})["case_type"] = model_origin
                    baseline["origin"] = model_origin + "_and_rulebook"
                    metrics["model_status"] = "completed"
        except (asyncio.TimeoutError, httpx.TimeoutException):
            metrics["model_status"] = "failed"
            error = {"code": "MODEL_TIMEOUT", "message": "회의록 Llama 판독이 120초 한도를 초과했습니다. 원문의 규칙 추출 후보를 표시합니다."}
        except ValidationError:
            metrics["model_status"] = "failed"
            error = {"code": "SCHEMA_VALIDATION_FAILED", "message": "회의록 Llama 응답 형식이 올바르지 않아 규칙 추출 후보만 표시합니다."}
        except (model_client.ModelClientError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            metrics["model_status"] = "failed"
            error = model_client.failure_details(exc)
        finally:
            _MODEL_GATE.release()
    elif use_model:
        metrics["model_status"] = "failed"
        error = {"code": "MODEL_BUSY", "message": "다른 자료를 판독 중이어서 규칙 추출 후보를 먼저 표시합니다."}
    # Region is only a routing candidate. The law-office validates actual jurisdiction.
    city = baseline["region"] or ""
    courts = _data("registry.json", {}).get("courts", [])
    court = next((item for item in courts if city and city[:2] in item.get("name", "")), None)
    provisional = {"id": "intake-preview", "client_name": baseline["client_name"], "court_id": court["id"] if court else None,
        "summary": text[:1000], "consultation": {"notes": text, "answers": {}}, "documents": [], "requests": [],
        "facts": [{"id": fact["key"], "key": fact["key"], "label": fact["label"], "claimed_value": fact["value"], "value": None, "status": "candidate"} for fact in baseline["extracted_facts"]],
        "extraction_candidates": baseline["extracted_facts"]}
    document_needs, matched_rules, missing_factors = [], [], []
    try:
        from .rulebook import evaluate_case
        evaluation = evaluate_case(provisional)
        matched_rules = evaluation.get("matched_rules", [])
        missing_factors = evaluation.get("missing_factors", [])
        for need in evaluation.get("required_documents", []):
            document_needs.append({**need, "period": need.get("period") or "담당자 확인", "quote": need.get("quote", ""), "origin": "rulebook"})
    except (ImportError, KeyError, TypeError, ValueError, AttributeError):
        keys = {fact["key"] for fact in baseline["extracted_facts"]}
        for catalog_id, reason, key in [("D07", "월 급여 진술과 지급 기준 확인", "monthly_income"),
                                      ("D36", "급여 입금 및 거래 원문 대조", "monthly_income"),
                                      ("D38", "채권자별 채무 잔액 대조", "total_debt"),
                                      ("D33", "주거비와 임대차 관계 확인", "housing_cost")]:
            if key in keys:
                candidate = next(fact for fact in baseline["extracted_facts"] if fact["key"] == key)
                document_needs.append({"catalog_id": catalog_id, "reason": reason, "period": "담당자 확인", "quote": candidate["quote"], "origin": "rulebook"})
    missing = []
    if not baseline["client_name"]:
        missing.append("의뢰인 성명")
    if not baseline["region"]:
        missing.append("실제 거주지역·주소 및 관할 확인")
    if baseline["case_type"] == "unknown":
        missing.append("회생·파산 등 상담 희망 사건 구분")
    if not any(fact["key"] == "monthly_income" for fact in baseline["extracted_facts"]):
        missing.append("현재 월 소득·지급 기준")
    if not any(fact["key"] == "total_debt" for fact in baseline["extracted_facts"]):
        missing.append("채무 총액과 채권자별 내역")
    labels = {"personal_rehabilitation": "개인회생 상담", "bankruptcy_review": "개인파산·면책 검토", "unknown": "사건 구분 미확인", "other": "기타 법률상담"}
    if error:
        error["message"] = error["message"].replace("Llama", model_label)
    metrics.update(wall_seconds=round(time.perf_counter() - started, 3),
        extracted_factor_count=len(baseline["extracted_facts"]), model_factor_count=sum(fact["origin"] in {"llama3", "deepseek"} for fact in baseline["extracted_facts"]),
        llama_factor_count=sum(fact["origin"] == "llama3" for fact in baseline["extracted_facts"]))
    return {**baseline, "status": "needs_review" if metrics["model_status"] == "completed" else "rulebook_only",
        "court_id": court["id"] if court else None, "court_name": court.get("name") if court else None,
        "court_status": "jurisdiction_candidate_requires_review",
        "summary": f"{baseline['client_name'] or '성명 미확인'} · {baseline['region'] or '거주지 미확인'} · {labels[baseline['case_type']]} · 원문에서 팩터 후보 {len(baseline['extracted_facts'])}건 추출",
        "document_needs": document_needs, "matched_rules": matched_rules, "missing_factors": missing_factors,
        "missing_information": missing, "issues": [{"title": item, "status": "needs_review"} for item in missing],
        "metrics": metrics, "error": error,
        "limitations": ["상담 진술에서 추출한 초안 후보입니다. 사람·관할·사건 구분은 법률 자격 판정이 아닙니다.",
                        "모델은 문장 ID와 팩터 종류를 선택하고 코드는 해당 원문의 값·금액 단위를 읽습니다. 독립적인 법률 검증이 아닙니다.",
                        "회의록 전체에서 규칙 후보를 찾고 앞 11,000자의 최대 40개 구간을 모델에 전달합니다. 금액의 법적 채택은 담당자 검토 대상입니다."]}


def validate_model_output(output: ModelAnalysis, prompt_sources: list[dict]) -> list[str]:
    lookup = {source["id"]: source for source in prompt_sources}
    problems = []
    for finding in output.findings:
        cited = []
        quoted_text = []
        for citation in finding.citations:
            source = lookup.get(citation.source_id)
            if source is None:
                problems.append("UNKNOWN_SOURCE_ID")
            elif citation.quote not in source["text"]:
                problems.append("QUOTE_MISMATCH")
            else:
                cited.append(source)
                quoted_text.append(citation.quote)
        if not any(source["kind"] in CASE_KINDS for source in cited):
            problems.append("CASE_EVIDENCE_REQUIRED")
        content = finding.title + " " + finding.observation
        # Numeric grounding is checked against quoted passages, not another page.
        numbers = set(re.findall(r"\d+(?:\.\d+)?", content.replace(",", "")))
        quoted_numbers = set(re.findall(r"\d+(?:\.\d+)?", " ".join(quoted_text).replace(",", "")))
        if numbers - quoted_numbers:
            problems.append("UNGROUNDED_NUMBER")
        if not re.search(r"[가-힣]{2,}", finding.observation):
            problems.append("KOREAN_OUTPUT_REQUIRED")
        if re.search(r"(?:인가|면책|승인).{0,6}(?:확정|보장|완료)|승소\s*확률|자동\s*제출|채권\s*제외\s*확정", content):
            problems.append("UNSUPPORTED_LEGAL_CONCLUSION")
        # Do not confuse the presence of a magic word such as '확인' with semantic
        # validation. Factual observations remain unverified candidates in the UI.
        if finding.factor_key and finding.value is not None:
            # Factor values need their own quoted numerical/semantic support.
            if len(quoted_text) != 1:
                problems.append("FACTOR_SINGLE_QUOTE_REQUIRED")
            else:
                fact = IntakeFact(key=finding.factor_key, value=finding.value, quote=quoted_text[0][:100])
                probe = IntakeAnalysis(client_name=IntakeIdentity(value=None, quote=""),
                    region=IntakeIdentity(value=None, quote=""), case_type="unknown", case_type_quote="", facts=[fact])
                problems.extend(_validate_intake(probe, quoted_text[0]))
    return list(dict.fromkeys(problems))


def _model_sources(context: dict) -> list[dict]:
    # The newest upload is first. Identical transcript text in a document and a
    # consultation record is sent once. Public law text is retrieved separately
    # for rule references; this short model call only classifies case evidence.
    selected = context["model_case_sources"]
    documents = list(reversed([source for source in selected if source["kind"] == "case_document"]))
    others = [source for source in selected if source["kind"] != "case_document"]
    result, seen = [], set()
    for source in documents + others:
        identity = _digest(source["text"])
        if identity in seen:
            continue
        seen.add(identity)
        result.append({**source, "original_id": source["id"], "id": "S" + str(len(result) + 1), "text": source["text"][:420]})
        if len(result) >= 3:
            break
    return result


def _resolve_case_selection(output: CaseSelection, sources: list[dict]) -> tuple[list[ModelFinding], list[str]]:
    lookup = {source["id"]: source for source in sources}
    classifications = {
        "payroll": ("급여명세서", r"급여\s*명세|실지급액|차인지급액"),
        "bank_statement": ("계좌 거래내역", r"거래\s*내역|입출금\s*내역"),
        "debt_certificate": ("부채증명서", r"부채\s*증명|채무\s*잔액\s*확인|채무\s*확인서"),
        "income_certificate": ("소득금액증명", r"소득\s*금액\s*증명"),
        "family_certificate": ("가족관계증명서", r"가족\s*관계\s*증명"),
        "lease": ("임대차계약서", r"임대차\s*계약"),
        "court_order": ("보정권고·명령", r"보정\s*(?:권고|명령)"),
        "consultation": ("상담 기록", r"상담|회의록"),
    }
    findings, discarded, seen = [], [], set()
    for selected in output.selections:
        source = lookup.get(selected.source_id)
        if not source:
            discarded.append("UNKNOWN_SOURCE_ID")
            continue
        if selected.factor_key:
            candidates = [item for item in extract_factor_candidates([source]) if item["key"] == selected.factor_key]
            if len({_text(item["value"]) for item in candidates}) != 1:
                discarded.append("FACTOR_SOURCE_MISMATCH")
                continue
            candidate = candidates[0]
            key = (source["original_id"], selected.factor_key)
            if key in seen:
                continue
            seen.add(key)
            findings.append(ModelFinding(title=FACTOR_LABELS[selected.factor_key] + " 추출 후보",
                observation="AI가 선택한 원문 구간에서 해당 팩터 값을 읽었습니다. 기간·대상·기준을 대조하세요.",
                category="extraction_candidate", factor_key=selected.factor_key, value=candidate["value"],
                citations=[Citation(source_id=selected.source_id, quote=candidate["quote"][:100])]))
        elif selected.document_type:
            label, pattern = classifications[selected.document_type]
            match = re.search(pattern, source["text"][:200])
            if source["kind"] != "case_document" or not match:
                discarded.append("CLASSIFICATION_SOURCE_MISMATCH")
                continue
            if selected.document_type != "consultation" and re.search(r"상담|회의록", source["title"]):
                discarded.append("CLASSIFICATION_SOURCE_MISMATCH")
                continue
            # Mentioning a missing payslip in a letter does not make that letter a payslip.
            sentence = re.split(r"[.\n]", source["text"][match.start():match.end() + 35])[0]
            if re.search(r"미제출|제출\s*전|제출\s*필요|요청|없음|미확보", sentence):
                discarded.append("CLASSIFICATION_SOURCE_MISMATCH")
                continue
            key = (source["original_id"], selected.document_type)
            if key in seen:
                continue
            seen.add(key)
            findings.append(ModelFinding(title=label + " 분류 후보", observation="AI가 문서 종류를 선택했습니다. 원문 대상자·발급기간과 요청 범위를 확인하세요.",
                category="document_classification", citations=[Citation(source_id=selected.source_id, quote=_quote_near(source["text"], match.start(), match.end(), 100))]))
        else:
            discarded.append("EMPTY_SELECTION")
    return findings, list(dict.fromkeys(discarded))


async def _run_model(context: dict, metrics: dict) -> tuple[list[dict], dict | None]:
    sources = _model_sources(context)
    if not context["model_case_sources"]:
        metrics["model_status"] = "no_case_evidence"
        return [], {"code": "NO_CASE_EVIDENCE", "message": "고객 진술이나 읽을 수 있는 업로드 원문이 없어 Llama 판독을 실행하지 않았습니다."}
    if os.getenv("DEBTOFF_AX_MODEL", "on").lower() in {"off", "disabled", "0", "false"}:
        metrics["model_status"] = "disabled"
        return [], None
    if not _MODEL_GATE.acquire(blocking=False):
        metrics["model_status"] = "failed"
        return [], {"code": "MODEL_BUSY", "message": "로컬 모델이 다른 원문을 검토 중입니다. 규칙 검토 결과를 먼저 확인하세요."}
    started = time.perf_counter()
    metrics["model_status"] = "running"
    try:
        schema = CaseSelection.model_json_schema()
        schema["$defs"]["CaseSelectedItem"]["properties"]["source_id"]["enum"] = [source["id"] for source in sources]
        prompt = [{key: source[key] for key in ("id", "kind", "title", "text") if key in source} for source in sources]
        messages = [
            {"role": "system", "content": prompt_registry.instruction('case_evidence')},
            {"role": "user", "content": json.dumps({"task": context["kind"], "sources": prompt}, ensure_ascii=False)},
        ]
        raw = await model_client.generate(messages, schema, timeout=MODEL_TIMEOUT, max_tokens=CASE_MAX_TOKENS)
        metrics.update(model_source_count=len(sources), model_input_characters=sum(len(source["text"]) for source in sources),
            extraction_method="model_field_selection_and_deterministic_value_parser")
        metrics.update(prompt_tokens=raw.get("prompt_eval_count"), completion_tokens=raw.get("eval_count"),
            load_seconds=round(raw.get("load_duration", 0) / 1e9, 3),
            generation_seconds=round(raw.get("eval_duration", 0) / 1e9, 3), done_reason=raw.get("done_reason"))
        metrics["tokens_per_second"] = round(raw.get("eval_count", 0) / (raw.get("eval_duration", 0) / 1e9), 2) if raw.get("eval_duration") else None
        telemetry = await runtime.model_status() if metrics["provider"] == "ollama" else {"status": "external_api", "loaded": []}
        model = next((entry for entry in telemetry.get("loaded", []) if entry["name"] == metrics["model"]), {})
        metrics.update(gpu_in_use=model.get("gpu_in_use"), size_vram=model.get("size_vram"), runtime_status=telemetry["status"])
        if raw.get("done_reason") == "length" or not raw.get("done", False):
            metrics["model_status"] = "failed"
            return [], {"code": "TRUNCATED_MODEL_OUTPUT", "message": "Llama 출력이 토큰 한도에서 중단되어 모델 후보를 채택하지 않았습니다."}
        selection = CaseSelection.model_validate_json(raw["message"]["content"])
        resolved, discarded = _resolve_case_selection(selection, sources)
        metrics.update(discarded_model_fields=discarded, partial_output=bool(discarded))
        if not resolved:
            metrics["model_status"] = "failed"
            return [], {"code": "EVIDENCE_VALIDATION_FAILED", "checks": discarded,
                "message": "모델이 선택한 문서·팩터와 실제 원문이 대응하지 않아 후보를 채택하지 않았습니다."}
        output = ModelAnalysis(findings=resolved)
        problems = validate_model_output(output, sources)
        if problems:
            metrics["model_status"] = "failed"
            return [], {"code": "EVIDENCE_VALIDATION_FAILED", "checks": problems,
                        "message": "Llama 후보의 원문 인용·숫자·표현 검증에 실패했습니다. 규칙 결과만 표시합니다."}
        by_id = {source["id"]: source for source in sources}
        findings = []
        for index, candidate in enumerate(output.findings):
            refs, official_refs = [], []
            for citation in candidate.citations:
                source = {**by_id[citation.source_id], "id": by_id[citation.source_id]["original_id"]}
                ref = _ref(source, citation.quote)
                (official_refs if source["kind"] == "official_reference" else refs).append(ref)
            item = _finding(context, "llama:" + str(index) + candidate.title, candidate.title,
                candidate.observation, refs, {"type": "review_task", "reason": candidate.observation})
            model_origin = "deepseek" if metrics["provider"] == "deepseek" else "llama3"
            item.update(origin=model_origin, category=candidate.category, official_refs=official_refs,
                        epistemic_status="unverified_model_candidate", semantic_validation_status="human_review_required")
            if candidate.factor_key and candidate.value is not None:
                citation = candidate.citations[0]
                source = {**by_id[citation.source_id], "id": by_id[citation.source_id]["original_id"]}
                item["extracted_fact"] = _candidate(candidate.factor_key, candidate.value, source, citation.quote, model_origin,
                    extraction_method="model_field_selection_and_deterministic_value_parser")
            findings.append(item)
        metrics["model_status"] = "completed"
        return findings, None
    except (asyncio.TimeoutError, httpx.TimeoutException):
        metrics["model_status"] = "failed"
        return [], {"code": "MODEL_TIMEOUT", "message": "Llama 응답이 120초 한도를 초과했습니다. 규칙 검토 결과를 먼저 확인하세요."}
    except ValidationError:
        metrics["model_status"] = "failed"
        return [], {"code": "SCHEMA_VALIDATION_FAILED", "message": "Llama가 검토 후보의 JSON 형식을 지키지 못했습니다. 모델 결과를 채택하지 않았습니다."}
    except (model_client.ModelClientError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        metrics["model_status"] = "failed"
        return [], model_client.failure_details(exc)
    finally:
        metrics["model_wall_seconds"] = round(time.perf_counter() - started, 3)
        _MODEL_GATE.release()


async def analyze_case(case: dict, kind: str = "case_review") -> dict:
    started = time.perf_counter()
    original_digest = _digest(case)
    snapshot = copy.deepcopy(case)
    context = build_context(snapshot, kind)
    findings = rulebook_findings(snapshot, context)
    extracted_facts = extract_factor_candidates(context["case_sources"])
    workflow_findings, workflow = _workflow_findings(snapshot, context, extracted_facts)
    already_proposed = {finding["action"].get("catalog_id") for finding in findings}
    findings += [finding for finding in workflow_findings if finding["action"].get("catalog_id") not in already_proposed]
    config = model_client.provider_config()
    model_label = "DeepSeek" if config["provider"] == "deepseek" else "Llama"
    metrics = {**config, "harness_version": HARNESS_VERSION,
        "timeout_seconds": MODEL_TIMEOUT, "max_output_tokens": CASE_MAX_TOKENS, "max_model_calls": 1,
        "concurrency_limit": 1, "cached": False, "case_source_count": len(context["case_sources"]),
        "retrieved_source_count": len(context["model_case_sources"]) + len(context["official_sources"]),
        "official_source_count": len(context["official_sources"]), "corpus_status": context["corpus_status"],
        "rulebook_finding_count": len(findings), "excluded_case_source_count": context["excluded_case_source_count"],
        "context_sha256": _digest(context), "semantic_validation_status": "human_review_required"}
    metrics['prompt']=prompt_registry.metadata('case_evidence')
    cache_key = _digest([context, config, metrics['prompt'], os.getenv("OLLAMA_BASE_URL"), os.getenv("DEBTOFF_AX_MODEL", "on")])
    if cache_key in _CACHE:
        cached = copy.deepcopy(_CACHE[cache_key])
        cached["metrics"].update(cached=True, wall_seconds=round(time.perf_counter() - started, 3))
        return cached
    model_findings, error = await _run_model(context, metrics)
    if error:
        error["message"] = error["message"].replace("로컬 Llama", model_label).replace("Llama", model_label)
    findings += model_findings
    model_facts = [item["extracted_fact"] for item in model_findings if item.get("extracted_fact")]
    model_keys = {(item["key"], _text(item["value"]), item.get("document_id")) for item in model_facts}
    extracted_facts = model_facts + [item for item in extracted_facts if (item["key"], _text(item["value"]), item.get("document_id")) not in model_keys]
    document_checks = check_documents(snapshot, extracted_facts)
    metrics.update(extracted_factor_count=len(extracted_facts), document_check_count=len(document_checks))
    metrics.update(llama_finding_count=sum(item["origin"] == "llama3" for item in model_findings),
                   model_finding_count=len(model_findings), wall_seconds=round(time.perf_counter() - started, 3))
    status = "needs_review" if model_findings else "rulebook_only" if findings else "manual_review"
    if _digest(case) != original_digest:
        findings = []
        status = "stale"
        extracted_facts, document_checks = [], []
        error = {"code": "STALE_INPUT", "message": "실행 중 사건 자료가 바뀌었습니다. 현재 근거로 다시 검토하세요."}
    cited_ids = {ref["source_id"] for finding in findings for ref in finding["evidence_refs"]}
    retrieved = context["sources"][:]
    for source in context["case_sources"]:
        if source["id"] in cited_ids and not any(item["id"] == source["id"] for item in retrieved):
            retrieved.append(source)
    summary = f"업무 규칙 후보 {metrics['rulebook_finding_count']}건 · {model_label} 판독 후보 {len(model_findings)}건"
    if metrics["model_status"] == "failed":
        summary += " · 모델 판독 실패"
    elif metrics["model_status"] in {"disabled", "no_case_evidence"}:
        summary += " · 모델 판독 미실행"
    result = {"status": status, "case_id": context["case_id"], "case_version": context["case_version"],
        "input_revision": context["input_revision"], "kind": kind, "summary": summary,
        "findings": findings, "extracted_facts": extracted_facts, "document_checks": document_checks,
        "workflow": workflow,
        "retrieved_sources": retrieved, "metrics": metrics, "error": error,
        "steps": [
            {"id": "case_retrieval", "title": "사건 원문·고객 회신 검색", "status": "completed", "detail": f"사건 자료 구간 {len(context['case_sources'])}개 · 모델 전달 {len(context['model_case_sources'])}개"},
            {"id": "official_retrieval", "title": "법원·정부 공개 원문 검색", "status": "completed" if context["official_sources"] else "unavailable", "detail": f"관할·공통 자료 {len(context['official_sources'])}개 · {context['corpus_status']}"},
            {"id": "rulebook", "title": "업무 규칙 대조", "status": "completed", "detail": f"규칙 후보 {metrics['rulebook_finding_count']}건"},
            {"id": "llama", "title": model_label + " 한국어 판독", "status": metrics["model_status"], "detail": f"검증 통과 후보 {len(model_findings)}건"},
            {"id": "review", "title": "직원 검토 후 업무 반영", "status": "pending", "detail": "서류 요청·검토 과제·초안은 검토하여 적용합니다."},
        ],
        "limitations": ["원문 인용 일치와 숫자 검증은 의미적 타당성·법률 정확성을 보장하지 않습니다.",
            "규칙 검토는 등록된 업무 통제이며 Llama 추론 성과와 구분합니다. 법률 규칙 승인이나 사실 확정이 아닙니다.",
            "공식 자료는 수집 당시 본문입니다. 관할·시행일·현재 적용 여부는 담당자가 확인해야 합니다.",
            "전체 사건을 읽은 결과가 아닙니다. 페이지 검색으로 선택한 원문 구간만 모델에 전달합니다."]}
    if metrics["model_status"] in {"completed", "disabled"} and status != "stale":
        if len(_CACHE) >= 40:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[cache_key] = copy.deepcopy(result)
    return result
