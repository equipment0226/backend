"""Download the allowlisted official sources and write an auditable local report."""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.api import corpus


if __name__ == "__main__":
    result = asyncio.run(corpus.ingest_all())
    report = {"stats": result["stats"], "sources": result["sources"], "sample_searches": [{"query": query, "court_id": court, "results": [{k: c[k] for k in ("id", "source_id", "title", "locator", "url", "court_id", "sha256", "score")} for c in corpus.search(query, court)]} for query, court in [("개인회생 급여소득자 제출 서류", "CT01"), ("영업소득자 소득금액증명", "CT02"), ("청산가치 배우자 재산", "CT03")]], "signature": corpus.corpus_signature(), "limitation": "실제 다운로드 원문을 검색합니다. 시행일·법적 적용 여부·현행성 검증은 수집 성공과 별개입니다."}
    report_path = ROOT / "reports/legal-ingestion.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["stats"], ensure_ascii=False, indent=2))
    for source in result["sources"]:
        print(source["id"], source["status"], source.get("chunk_count", 0), (source.get("error") or "")[:180])
