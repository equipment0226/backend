"""Small provider boundary shared by intake and document analysis.

Credentials are read only into memory, never returned in metadata or written.
Reference-project credentials require an explicit local opt-in environment flag.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

import httpx

from .agent_engine import _endpoint

ROOT = Path(__file__).resolve().parents[2]


class ModelClientError(Exception):
    def __init__(self, code, message, http_status=None):
        self.code, self.message, self.http_status = code, message, http_status
        super().__init__(code)


def failure_details(exc):
    if isinstance(exc, ModelClientError):
        return {"code": exc.code, "message": exc.message, "http_status": exc.http_status}
    return {"code": "MODEL_UNAVAILABLE", "message": "모델 판독을 완료하지 못했습니다. 연결 상태와 모델 설정을 확인하세요.", "error_type": type(exc).__name__}


LOCAL_ROLES = {"local", "consultation", "ocr", "document", "document_selection", "calculation_extraction", "grounded_drafting"}
CONFIG_KEYS = {"DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEBTOFF_REASONING_PROVIDER",
               "DEBTOFF_MODEL_PROVIDER", "OLLAMA_BASE_URL", "OLLAMA_MODEL", "OLLAMA_API_KEY"}


def configured(name, default=""):
    """Secret-preserving settings with the user's explicit local key override.

    Only model settings are eligible for the user's explicit example-file
    fallback. Values are never copied into telemetry, logs or child processes.
    """
    # The user explicitly supplied the example-file key. For this secret only,
    # a non-placeholder local file overrides a stale inherited process secret.
    # Other settings preserve ordinary environment precedence.
    if name in os.environ and name != "DEEPSEEK_API_KEY":
        return os.environ[name]
    if name not in CONFIG_KEYS:
        return default
    for filename in (".env", ".env.example"):
        try:
            for line in (ROOT / filename).read_text(encoding="utf-8-sig").splitlines():
                if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == name:
                    value = value.strip().strip('"').strip("'")
                    if name == "DEEPSEEK_API_KEY" and (not value or value.lower() in {"your_api_key", "your-api-key", "your_deepseek_api_key", "changeme", "replace-me", "sk-..."} or value.startswith("${")):
                        continue
                    return value
        except OSError:
            continue
    return os.getenv(name, default)


def provider_config(task_role="local") -> dict:
    # Raw customer data never follows the global/external provider preference.
    if task_role not in LOCAL_ROLES | {"strategy_verification"}:
        raise ValueError("UNSUPPORTED_MODEL_ROLE")
    provider = "ollama"
    if task_role == "strategy_verification":
        advanced_default = "deepseek" if configured("DEEPSEEK_API_KEY") else configured("DEBTOFF_MODEL_PROVIDER", "ollama")
        provider = configured("DEBTOFF_REASONING_PROVIDER", advanced_default).lower()
    if provider not in {"ollama", "deepseek"}:
        raise ValueError("UNSUPPORTED_MODEL_PROVIDER")
    return {"provider": provider, "model": configured("DEEPSEEK_MODEL", "deepseek-flash") if provider == "deepseek" else configured("OLLAMA_MODEL", "llama3:latest"),
            "external_processing": provider == "deepseek"}


def _deepseek_key() -> str:
    key = configured("DEEPSEEK_API_KEY")
    if key:
        return key
    if os.getenv("DEBTOFF_USE_REFERENCE_CREDENTIALS", "").lower() not in {"1", "true", "yes"}:
        raise ModelClientError("MODEL_CREDENTIAL_MISSING", "DeepSeek API 키가 설정되지 않았습니다.")
    path = ROOT / "lawmaster" / ".env"
    values = {}
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() in {"DEEPSEEK_API_KEY", "LLM_API_KEY"}:
                values[name.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        raise ModelClientError("MODEL_CREDENTIAL_MISSING", "DeepSeek API 키를 읽을 수 없습니다.") from None
    key = values.get("DEEPSEEK_API_KEY") or values.get("LLM_API_KEY")
    if not key:
        raise ModelClientError("MODEL_CREDENTIAL_MISSING", "DeepSeek API 키가 설정되지 않았습니다.")
    return key


def _active_legal_signature(structured_payload=None):
    """Invalidate cached reviews on public-policy edits or effective boundaries."""
    today = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    structured_payload = structured_payload or {}
    court_id = structured_payload.get('case_features', {}).get('court_id')
    public_ids = {ref.get('public_source_id') for ref in structured_payload.get('legal_references', []) if ref.get('public_source_id')}
    signature = {}
    for name in ('legal_calculation_rules.json', 'court_request_rules.json', 'legal_ax_sources.json',
                 'legal_research/manifest.json', 'ax_legal_precedents.json'):
        try:
            raw = (ROOT / 'data' / name).read_bytes()
            data = json.loads(raw)
            if name == 'court_request_rules.json' and isinstance(data, dict):
                court = data.get('courts', {}).get(court_id, {})
                source_ids = set(court.get('source_ids', []))
                data = {'court_id': court_id, 'court': court,
                        'sources': {key: source for key, source in data.get('sources', {}).items() if key in source_ids},
                        **{key: data.get(key) for key in ('scope', 'government_document_ids', 'split', 'precedence')}}
            elif name == 'legal_calculation_rules.json' and court_id != 'CT01' and isinstance(data, dict):
                data = {key: value for key, value in data.items() if not key.startswith('seoul_')}
                data['sources'] = [source for source in data.get('sources', []) if not source.get('id', '').startswith('seoul-')]
            elif name == 'legal_ax_sources.json' and isinstance(data, dict):
                data = {'sources': [source for source in data.get('sources', []) if source.get('id') in public_ids]}
            elif name == 'ax_legal_precedents.json' and isinstance(data, dict):
                data = {'use_policy': data.get('use_policy'), 'precedents': [source for source in data.get('precedents', []) if source.get('id') in public_ids]}
            if name.endswith('manifest.json') and isinstance(data, dict):
                # Merely downloading identical law again must not trigger a
                # paid rewrite; content/version/effectivity changes must.
                data = {'sources': [{key: source.get(key) for key in ('id', 'sha256', 'text_sha256',
                    'url', 'status', 'effective_date', 'court_id', 'source_type')}
                    for source in data.get('sources', []) if source.get('id') in public_ids]}
            raw = json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
            signature[name] = {'sha256': hashlib.sha256(raw).hexdigest()}
            if isinstance(data, dict):
                start, end = data.get('effective_from'), data.get('valid_through')
                signature[name]['in_effect'] = (not start or str(start) <= today) and (not end or today <= str(end))
        except (OSError, ValueError, TypeError):
            signature[name] = {'status': 'unavailable'}
    try:
        from .legal_watch import dependencies_signature
        signature['active_court_overlay'] = dependencies_signature({'court_id': court_id})
    except (ImportError, OSError, ValueError, TypeError, KeyError):
        signature['active_court_overlay'] = 'unavailable'
    return signature


async def generate(messages: list[dict], schema: dict, timeout=120, max_tokens=600,
                   *, task_role="local", structured_payload=None,
                   review_stage='strategy', review_attempt='auto', local_context_tokens=None) -> dict:
    config = provider_config(task_role)
    if task_role != 'strategy_verification' or config['provider'] != 'deepseek':
        return await _generate_uncached(messages, schema, timeout, max_tokens,
                                        task_role=task_role, structured_payload=structured_payload,
                                        local_context_tokens=local_context_tokens)
    from .verification import safe_strategy_messages, StrategyReview
    from . import reasoning_cache
    # Privacy validation precedes even a cache read: no caller can smuggle a raw
    # prompt through a nominally anonymous role or reuse an invalid-law snapshot.
    bounded_messages = safe_strategy_messages(structured_payload)
    fixed_schema = StrategyReview.model_json_schema()
    try:
        key, stage_key = reasoning_cache.identity(safe_payload=structured_payload,
            messages=bounded_messages, schema=fixed_schema, model=config['model'], provider=config['provider'],
            stage=review_stage, legal_signature=_active_legal_signature(structured_payload))
    except reasoning_cache.ReasoningLimitError as exc:
        raise ModelClientError(exc.code, exc.message) from None
    def validate(raw):
        if not raw.get('done') or raw.get('done_reason') != 'stop':
            return None
        try:
            result = StrategyReview.model_validate_json(raw['message']['content'])
        except (ValueError, TypeError, KeyError):
            return None
        refs = {ref['ref'] for ref in structured_payload['legal_references']}
        if result.decision == 'insufficient_evidence' or any(set(f.source_refs) - refs for f in result.findings):
            return None
        # Keep only the validated anonymous answer and numeric usage metadata.
        clean = {key: raw[key] for key in ('provider', 'model', 'external_processing', 'done', 'done_reason',
            'prompt_eval_count', 'eval_count', 'eval_duration', 'load_duration', 'request_wall_seconds') if key in raw}
        clean['message'] = {'content': result.model_dump_json()}
        usage = raw.get('usage') or {}
        clean['usage'] = {key: value for key, value in usage.items() if key in {
            'prompt_tokens', 'completion_tokens', 'total_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'} and type(value) is int}
        reasoning = (usage.get('completion_tokens_details') or {}).get('reasoning_tokens')
        if type(reasoning) is int:
            clean['usage']['completion_tokens_details'] = {'reasoning_tokens': reasoning}
        return clean
    try:
        return await reasoning_cache.run(key=key, stage_key=stage_key, stage=review_stage,
            operation=lambda: _generate_uncached(bounded_messages, fixed_schema, timeout, max_tokens,
                task_role=task_role, structured_payload=structured_payload),
            validate=validate, timeout=timeout, attempt=review_attempt)
    except reasoning_cache.ReasoningLimitError as exc:
        raise ModelClientError(exc.code, exc.message) from None


async def _generate_uncached(messages: list[dict], schema: dict, timeout=120, max_tokens=600,
                   *, task_role="local", structured_payload=None, local_context_tokens=None) -> dict:
    config = provider_config(task_role)
    if task_role == "strategy_verification":
        # Do not trust caller-provided messages or schemas, even when it claims
        # to have masked them. Rebuild the entire external request at the boundary.
        from .verification import safe_strategy_messages, StrategyReview
        messages = safe_strategy_messages(structured_payload)
        schema = StrategyReview.model_json_schema()
    started = time.perf_counter()
    if config["provider"] == "deepseek":
        # Fixed official destination: an arbitrary base URL cannot receive this credential.
        endpoint = "https://api.deepseek.com/chat/completions"
        headers = {"Authorization": "Bearer " + _deepseek_key()}
        bounded_messages = [dict(message) for message in messages]
        bounded_messages[0]["content"] += "\nReturn one JSON object conforming to this JSON schema: " + json.dumps(schema, ensure_ascii=False)
        payload = {"model": config["model"], "messages": bounded_messages,
                   "response_format": {"type": "json_object"}, "stream": False,
                   # https://api-docs.deepseek.com/guides/thinking_mode/
                   "thinking": {"type": "enabled"}, "reasoning_effort": "high", "max_tokens": max_tokens}
    else:
        if local_context_tokens is not None and (task_role not in LOCAL_ROLES or type(local_context_tokens) is not int or not 2048 <= local_context_tokens <= 8192):
            raise ValueError('INVALID_LOCAL_CONTEXT_LIMIT')
        context_tokens=local_context_tokens or 8192
        # UTF-8 bytes conservatively bound tokens, including constrained schema
        # and chat framing. Do not let the runtime silently trim source messages.
        input_bound=sum(len(str(message.get('content','')).encode('utf-8')) for message in messages)
        input_bound+=len(json.dumps(schema,ensure_ascii=False,separators=(',',':')).encode('utf-8'))+512
        available=context_tokens-input_bound
        if available < min(max_tokens,128):
            raise ModelClientError('LOCAL_CONTEXT_LIMIT','원문 전체와 검증 형식을 보존할 수 있도록 입력 범위를 더 나누어야 합니다.')
        output_limit=min(max_tokens,available)
        endpoint = _endpoint() + "/api/chat"
        local_key = configured("OLLAMA_API_KEY")
        headers = {"Authorization": "Bearer " + local_key} if local_key else {}
        payload = {"model": config["model"], "messages": messages, "format": schema,
                   "stream": False, "keep_alive": "5m",
                   # Keep the same trained context and loader options across
                   # consultation/OCR/writing calls so the CPU runner is reusable.
                   # Explicit mmap avoids anonymous model-copy pressure on this
                   # 16GB Windows host. Smaller prefill batches reduce scratch RAM.
                   # Official option handling: github.com/ollama/ollama/blob/main/server/sched.go
                   "options": {"temperature": 0, "num_ctx": context_tokens,
                               "num_predict": output_limit, "use_mmap": True, "num_batch": 128}}
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10), follow_redirects=False,
                                 trust_env=False, headers=headers) as client:
        response = await asyncio.wait_for(client.post(endpoint, json=payload), timeout=timeout)
        if response.status_code in {401, 403}:
            raise ModelClientError("MODEL_AUTH_FAILED", "모델 API가 인증을 거부했습니다. 유효한 API 키와 권한을 확인하세요.", response.status_code)
        if response.status_code == 402:
            raise ModelClientError("MODEL_BALANCE_REQUIRED", "모델 API 사용 잔액을 확인하세요.", 402)
        if response.status_code == 429:
            raise ModelClientError("MODEL_RATE_LIMIT", "모델 API 요청 한도에 도달했습니다. 잠시 후 다시 실행하세요.", 429)
        response.raise_for_status()
        raw = response.json()
    if config["provider"] == "ollama":
        return {**raw, **config, "request_wall_seconds": round(time.perf_counter() - started, 3),
                'context_tokens':context_tokens,'input_token_upper_bound':input_bound,'output_token_limit':output_limit}
    choice = raw["choices"][0]
    usage = raw.get("usage", {})
    # Never retain provider reasoning_content; only the requested structured answer.
    return {**config, "done": choice.get("finish_reason") == "stop", "done_reason": choice.get("finish_reason"),
            "message": {"content": choice.get("message", {}).get("content") or ""},
            "prompt_eval_count": usage.get("prompt_tokens"), "eval_count": usage.get("completion_tokens"),
            "eval_duration": 0, "load_duration": 0,
            "request_wall_seconds": round(time.perf_counter() - started, 3),
            "response_model": raw.get("model"), "usage": usage}
