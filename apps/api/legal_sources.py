"""Small credential-redacting adapter inspired by lawmaster's OfficialLawClient."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

import httpx

API_URL = "https://www.law.go.kr/DRF/lawSearch.do"
GUIDE_URL = "https://open.law.go.kr/LSO/openApi/guideList.do"
_CACHE: dict[tuple[str, str, str], tuple[float, dict]] = {}
MAX_BYTES = 2_000_000
CACHE_SECONDS = 900


def _result(status: str, message: str, **extra) -> dict:
    return {"status": status, "message": message, "results": [], "source": "국가법령정보센터", "guide_url": GUIDE_URL, "applicability_status": "not_reviewed", "cached": False, **extra}


def _clean(value, credential: str) -> str:
    return re.sub(r"<[^>]+>", "", str(value or "")).replace(credential, "[REDACTED]")[:500]


async def search_legal(query: str, target: str = "law") -> dict:
    """Fetch metadata only. Receiving a row never confirms legal applicability."""
    query = str(query).strip()
    if target not in {"law", "prec"}:
        return _result("invalid_target", "법령(law) 또는 판례(prec)만 검색할 수 있습니다.")
    if not 2 <= len(query) <= 200:
        return _result("invalid_query", "검색어는 2~200자로 입력하세요.")
    credential = os.getenv("LAW_API_OC", "").strip()
    if not credential:
        return _result("unconfigured", "LAW_API_OC가 설정되지 않았습니다. 국가법령정보 공동활용 이용 승인과 인증값을 등록하세요.")
    key = (target, query, hashlib.sha256(credential.encode()).hexdigest())
    cached = _CACHE.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return {**copy.deepcopy(cached[1]), "cached": True}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5), follow_redirects=False) as client:
            async with client.stream("GET", API_URL, params={"OC": credential, "target": target, "type": "JSON", "query": query, "display": 15, "page": 1}) as response:
                if response.status_code in {401, 403}:
                    return _result("auth_failed", "공식 API 인증 또는 이용 승인을 확인하세요.")
                if response.status_code == 429:
                    return _result("rate_limited", "공식 API 요청 한도에 도달했습니다.")
                if response.status_code != 200:
                    return _result("unavailable", "공식 API에 연결하지 못했습니다.")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        return _result("response_too_large", "공식 API 응답이 크기 한도를 초과했습니다.")
        transport_hash = hashlib.sha256(raw).hexdigest()
        redacted = bytes(raw).replace(credential.encode(), b"[REDACTED]")
        if "미신청".encode() in redacted:
            return _result("auth_failed", "이 서비스의 목록 조회 승인을 확인하세요.")
        body = json.loads(redacted)
        root = body.get("LawSearch" if target == "law" else "PrecSearch", {})
        if not isinstance(root, dict) or not ("totalCnt" in root or target in root):
            return _result("invalid_response", "공식 API의 응답 형식 또는 인증 승인을 확인하세요.")
        rows = root.get(target, []) or []
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            return _result("invalid_response", "공식 API 목록 형식을 확인하지 못했습니다.")
        results = []
        for row in rows[:15]:
            if not isinstance(row, dict):
                continue
            source_id = str(row.get("법령일련번호" if target == "law" else "판례일련번호", ""))
            if not re.fullmatch(r"[0-9]{1,20}", source_id):
                continue
            path, param = ("lsInfoP.do", "lsiSeq") if target == "law" else ("precInfoP.do", "precSeq")
            results.append({"id": f"{target}:{source_id}", "source_id": source_id, "kind": target, "title": _clean(row.get("법령명한글" if target == "law" else "사건명"), credential), "url": f"https://www.law.go.kr/LSW/{path}?{param}={source_id}", "effective_on": _clean(row.get("시행일자"), credential), "decided_on": _clean(row.get("선고일자"), credential), "court": _clean(row.get("법원명"), credential), "case_number": _clean(row.get("사건번호"), credential), "content_status": "metadata_only", "applicability_status": "not_reviewed"})
        result = _result("ok", "공식 목록을 수신했습니다. 원문·시행일·사건 적용성은 별도 확인이 필요합니다.", results=results, total=int(root.get("totalCnt", len(results))), retrieved_at=datetime.now(timezone.utc).isoformat(), transport_sha256=transport_hash)
        if len(_CACHE) >= 100:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (time.monotonic(), copy.deepcopy(result))
        return result
    except httpx.TimeoutException:
        return _result("timeout", "공식 API 응답 시간이 초과되었습니다. 수동 검색을 이용하세요.")
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return _result("unavailable", "공식 API 응답을 확인하지 못했습니다. 수동 검색을 이용하세요.")
