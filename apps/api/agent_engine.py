"""Bounded local Llama drafting with lexical retrieval and deterministic evidence checks.

The model cannot change case facts, calculate legal amounts, or release a filing.
This module is independent of persistence and HTTP routes.
"""
from __future__ import annotations

import asyncio
from . import prompt_registry
import copy
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

ROOT = Path(__file__).resolve().parents[2]
HARNESS_VERSION = "debtoff-evidence-v1"
_MODEL_LOCK = asyncio.Semaphore(1)
_CACHE: dict[str, dict] = {}


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=120)
    quote: str = Field(min_length=1, max_length=180)


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=80)
    observation: str = Field(min_length=1, max_length=220)
    category: Literal["extraction_candidate", "review_question", "document_request"]
    citations: list[Citation] = Field(min_length=1, max_length=3)


class Analysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposals: list[Proposal] = Field(min_length=1, max_length=2)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _tokens(value: str) -> set[str]:
    words = re.findall(r"[가-힣A-Za-z0-9]{2,}", value.lower())
    return set(words) | {word[i:i+2] for word in words for i in range(len(word)-1)}


def _knowledge() -> list[dict]:
    path = ROOT / "data" / "knowledge.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else data.get("items", data.get("sources", []))
    except (OSError, ValueError, AttributeError):
        return []


def build_context(case: dict, kind: str = "case_review") -> dict:
    """Only the supplied case is eligible; never retrieve another case's documents."""
    query = " ".join([kind, case.get("summary", ""), case.get("court_name", ""), _text(case.get("issues", []))[:1000], _text(case.get("corrections", []))[:600]])
    terms = _tokens(query)
    sources = []
    for doc in case.get("documents", []):
        if doc.get("stale") or doc.get("status") in {"deleted", "superseded", "rejected", "old_version"}:
            continue
        text = doc.get("text") or doc.get("extracted_text")
        if not text and doc.get("page_texts"):
            pages = doc["page_texts"]
            text = "\n".join(_text(p.get("text", "") if isinstance(p, dict) else p) for p in pages)
        if not text:
            continue
        text = _text(text)
        # Limit each excerpt and preserve exact offsets for the review display.
        chunks = [(offset, text[offset:offset+700]) for offset in range(0, min(len(text), 21000), 700)]
        offset, excerpt = max(chunks, key=lambda row: len(terms & _tokens(row[1])))
        sources.append({"id": "doc:" + str(doc["id"]), "document_id": doc["id"], "kind": "case_document", "title": doc.get("name", doc.get("filename", doc.get("title", "사건 자료"))), "text": excerpt, "version": doc.get("version", 1), "status": doc.get("status", "unverified"), "offset": offset, "source_sha256": _digest(text), "score": len(terms & _tokens(excerpt))})
    for fact in case.get("facts", []):
        # A recorded fact remains a claim unless a human has confirmed it.
        value = fact.get("value", fact.get("claimed_value"))
        if value is None:
            value = fact.get("claimed_value")
        if value is not None:
            text = f"{fact.get('label', fact.get('key', '사실'))}: {value}; 상태: {fact.get('status', 'unknown')}; 확인 이유: {fact.get('reason', '')}"
            sources.append({"id": "fact:" + str(fact["id"]), "kind": "case_record", "title": fact.get("label", "사건 기록"), "text": text[:700], "version": case.get("input_revision", 1), "status": fact.get("status", "unknown"), "score": 0, "source_sha256": _digest(text)})
    for message in case.get("messages", [])[-6:]:
        text = _text(message.get("text", "")).strip()
        if not text or not message.get("id"):
            continue
        sources.append({"id": "message:" + str(message["id"]), "kind": "party_statement" if message.get("role") == "client" else "office_note", "title": "고객 회신 원문" if message.get("role") == "client" else "사무실 기록 원문", "text": text[:700], "version": message.get("version", case.get("input_revision", 1)), "status": "unverified_statement", "score": len(terms & _tokens(text)), "source_sha256": _digest(text)})
    sources.sort(key=lambda source: (source["score"], source["kind"] == "case_document"), reverse=True)
    selected = sources[:3]
    # Keep one customer reply so a later reply cannot disappear behind uploaded documents.
    statement = next((source for source in sources if source["kind"] == "party_statement"), None)
    if statement and statement not in selected:
        selected = selected[:2] + [statement]
    rules = case.get("registry_controls", case.get("rules", [])) or _knowledge()
    rule_sources = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict) or rule.get("active") is False or rule.get("stale"):
            continue
        if rule.get("court_id") and rule["court_id"] != case.get("court_id"):
            continue
        if rule.get("status") in {"superseded", "retired", "old_version"}:
            continue
        text = _text(rule.get("text") or rule.get("content") or rule.get("description") or rule.get("rule") or "")
        if not text:
            continue
        rule_sources.append({"id": "rule:" + str(rule.get("id", index)), "kind": "workflow_rule", "title": rule.get("title", "검토 통제 규칙"), "text": text[:500], "version": rule.get("version", 1), "source": rule.get("source", rule.get("source_file", "제공 기획 자료")), "source_url": rule.get("url", rule.get("source_url")), "source_type": rule.get("source_type", "planning_reference"), "scope": rule.get("scope", "design"), "approved": bool(rule.get("approved", False)), "status": "reference_requires_review", "score": len(terms & _tokens(text)), "source_sha256": _digest(rule)})
    rule_sources.sort(key=lambda source: source["score"], reverse=True)
    selected += rule_sources[:1]
    return {"case_id": case.get("id"), "input_revision": case.get("input_revision", 1), "kind": kind, "sources": selected, "excluded_case_source_count": max(0, len(sources) - 3), "retrieval": "lexical_keywords_and_korean_bigrams", "harness_version": HARNESS_VERSION}


def validate_output(output: Analysis, context: dict) -> list[str]:
    sources = {source["id"]: source for source in context["sources"]}
    problems = []
    for proposal in output.proposals:
        cited = []
        for citation in proposal.citations:
            source = sources.get(citation.source_id)
            if not source:
                problems.append("UNKNOWN_SOURCE_ID")
            elif citation.quote not in source["text"]:
                problems.append("QUOTE_MISMATCH")
            else:
                cited.append(source)
        if not any(source["kind"] in {"case_document", "case_record", "party_statement", "office_note"} for source in cited):
            problems.append("CASE_EVIDENCE_REQUIRED")
        numbers = set(re.findall(r"\d+(?:\.\d+)?", (proposal.title + " " + proposal.observation).replace(",", "")))
        evidence_numbers = set(re.findall(r"\d+(?:\.\d+)?", " ".join(source["text"] for source in cited).replace(",", "")))
        if numbers - evidence_numbers:
            problems.append("UNGROUNDED_NUMBER")
        if re.search(r"(?:인가|면책|승인)(?:가|이|은|는)?\s*(?:확정|보장|완료)|승소\s*확률|자동\s*제출|채권\s*제외\s*확정", proposal.observation):
            problems.append("UNSUPPORTED_LEGAL_CONCLUSION")
    return list(dict.fromkeys(problems))


def _endpoint() -> str:
    from .model_client import configured
    url = configured("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("INVALID_MODEL_ENDPOINT")
    # HTTPS alone is not a privacy boundary: a public Ollama-compatible server
    # would receive complete consultation and document contents. Raw processing
    # is restricted to this machine (or the Docker host on this machine).
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1", "host.docker.internal"}:
        raise ValueError("INVALID_MODEL_ENDPOINT")
    return url


async def model_status() -> dict:
    try:
        headers = {"Authorization": "Bearer " + os.environ["OLLAMA_API_KEY"]} if os.getenv("OLLAMA_API_KEY") else {}
        async with httpx.AsyncClient(timeout=3, follow_redirects=False, trust_env=False, headers=headers) as client:
            response = await client.get(_endpoint() + "/api/ps")
            response.raise_for_status()
            models = response.json().get("models", [])
            return {"status": "available", "model": os.getenv("OLLAMA_MODEL", "llama3:latest"), "loaded": [{"name": model.get("name"), "size": model.get("size", 0), "size_vram": model.get("size_vram", 0), "gpu_in_use": model.get("size_vram", 0) > 0} for model in models]}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return {"status": "unavailable", "model": os.getenv("OLLAMA_MODEL", "llama3:latest"), "loaded": []}


async def run_analysis(case: dict, kind: str = "case_review") -> dict:
    started = time.perf_counter()
    context = build_context(case, kind)
    metrics = {"model": os.getenv("OLLAMA_MODEL", "llama3:latest"), "harness_version": HARNESS_VERSION, "cached": False, "input_revision": context["input_revision"], "concurrency_limit": 1, "timeout_seconds": 90, "retrieved_source_count": len(context["sources"]), "retrieved_characters": sum(len(source["text"]) for source in context["sources"]), "excluded_case_source_count": context["excluded_case_source_count"], "context_sha256": _digest(context)}
    metrics['prompt']=prompt_registry.metadata('legacy_review')

    def result(status, proposals=None, error=None):
        metrics["wall_seconds"] = round(time.perf_counter() - started, 3)
        return {"status": status, "proposals": proposals or [], "metrics": copy.deepcopy(metrics), "retrieved_sources": context["sources"], "input_revision": context["input_revision"], "error": error, "semantic_validation_status": "human_review_required", "limitations": ["담당자 검토 전 제안입니다. 사실 확정·법률 판단·계산·제출을 자동 실행하지 않습니다.", "인용 구간·숫자 일치는 주장 의미와 한국법 적용의 타당성을 보장하지 않습니다. 담당자가 문맥과 판단을 검토해야 합니다."]}

    if not any(source["kind"] in {"case_document", "case_record", "party_statement", "office_note"} for source in context["sources"]):
        return result("manual_review", error={"code": "NO_CASE_EVIDENCE", "message": "읽을 수 있는 사건 원문이나 기록을 먼저 등록하세요. 이미지·스캔은 문자 추출 또는 수동 확인이 필요합니다."})
    try:
        endpoint = _endpoint()
    except ValueError:
        return result("failed", error={"code": "INVALID_MODEL_ENDPOINT", "message": "모델 연결 주소를 확인하세요."})
    cache_key = _digest({"context": context, "model": metrics["model"], "endpoint": endpoint, "prompt":metrics['prompt']})
    if cache_key in _CACHE:
        cached = copy.deepcopy(_CACHE[cache_key])
        cached["metrics"].update(cached=True, wall_seconds=round(time.perf_counter() - started, 3))
        return cached
    acquired = False
    try:
        await asyncio.wait_for(_MODEL_LOCK.acquire(), timeout=1)
        acquired = True
        schema = Analysis.model_json_schema()
        schema["$defs"]["Citation"]["properties"]["source_id"]["enum"] = [source["id"] for source in context["sources"]]
        prompt_sources = [{key: source[key] for key in ("id", "kind", "title", "text", "status", "source_type", "scope", "approved") if key in source} for source in context["sources"]]
        messages = [
            {"role": "system", "content": prompt_registry.instruction('legacy_review')},
            {"role": "user", "content": json.dumps({"task": kind, "sources": prompt_sources}, ensure_ascii=False)},
        ]
        headers = {"Authorization": "Bearer " + os.environ["OLLAMA_API_KEY"]} if os.getenv("OLLAMA_API_KEY") else {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=5), follow_redirects=False, trust_env=False, headers=headers) as client:
            response = await asyncio.wait_for(client.post(endpoint + "/api/chat", json={"model": metrics["model"], "stream": False, "format": schema, "keep_alive": "5m", "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 256}, "messages": messages}), timeout=90)
            response.raise_for_status()
            raw = response.json()
        metrics.update(prompt_tokens=raw.get("prompt_eval_count"), completion_tokens=raw.get("eval_count"), load_seconds=round(raw.get("load_duration", 0)/1e9, 3), generation_seconds=round(raw.get("eval_duration", 0)/1e9, 3), done_reason=raw.get("done_reason"))
        metrics["tokens_per_second"] = round(raw.get("eval_count", 0) / (raw.get("eval_duration", 0)/1e9), 2) if raw.get("eval_duration") else None
        telemetry = await model_status()
        matching = next((model for model in telemetry.get("loaded", []) if model["name"] == metrics["model"]), {})
        metrics.update(gpu_in_use=matching.get("gpu_in_use"), size_vram=matching.get("size_vram"), runtime_status=telemetry["status"])
        if raw.get("done_reason") == "length" or not raw.get("done", False):
            return result("manual_review", error={"code": "TRUNCATED_MODEL_OUTPUT", "message": "모델 출력 한도에 도달했습니다. 검토 후보를 채택하지 않고 수동 검토로 넘겼습니다."})
        output = Analysis.model_validate_json(raw["message"]["content"])
        defects = validate_output(output, context)
        if defects:
            return result("manual_review", error={"code": "EVIDENCE_VALIDATION_FAILED", "checks": defects, "message": "원문 근거·숫자 검증을 통과하지 못했습니다. 담당자가 원문을 확인해야 합니다."})
        if case.get("input_revision", 1) != context["input_revision"] or _digest(build_context(case, kind)) != _digest(context):
            return result("manual_review", error={"code": "STALE_INPUT", "message": "실행 중 사건 입력이 변경되어 제안을 보류했습니다."})
        proposals = []
        for proposal in output.proposals:
            item = proposal.model_dump()
            item.update(status="needs_review", epistemic_status="unverified_candidate", input_revision=context["input_revision"], evidence_ids=[source["document_id"] for source in context["sources"] if source.get("document_id") and source["id"] in {citation.source_id for citation in proposal.citations}], source_versions={citation.source_id: next(source["version"] for source in context["sources"] if source["id"] == citation.source_id) for citation in proposal.citations})
            proposals.append(item)
        final = result("needs_review", proposals)
        if len(_CACHE) >= 50:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[cache_key] = copy.deepcopy(final)
        return final
    except (asyncio.TimeoutError, httpx.TimeoutException):
        code = "MODEL_TIMEOUT" if acquired else "MODEL_BUSY"
        return result("manual_review", error={"code": code, "message": "모델 응답을 기다리는 동안 시간 한도에 도달했습니다. 담당자 검토를 계속할 수 있습니다."})
    except ValidationError:
        return result("manual_review", error={"code": "SCHEMA_VALIDATION_FAILED", "message": "모델 출력 형식 검증에 실패했습니다. 원문을 직접 확인하세요."})
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return result("failed", error={"code": "MODEL_UNAVAILABLE", "message": "로컬 모델을 호출하지 못했습니다. Ollama 실행과 연결 주소를 확인하세요."})
    finally:
        if acquired:
            _MODEL_LOCK.release()
