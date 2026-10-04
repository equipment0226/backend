# 로컬 한국어 스캔 OCR

`apps/api/ocr_engine.py`가 텍스트 PDF와 스캔 PDF를 페이지별로 나누고, 스캔 페이지와 PNG/JPG는 한국어 PP-OCRv5 모델로 읽는다. 문서 내용은 외부 API로 보내지 않는다. 문자 검출, 방향 분류, 한국어 인식을 CPU ONNX Runtime에서 실행한다.

처음 한 번 `python scripts/setup_ocr.py`를 실행한다. 이 작업은 `requirements-ocr.txt`의 패키지를 `.local/ocr/python`에 설치하고, 공식 RapidOCR 모델 저장소에서 SHA256이 고정된 3개 모델을 `.local/ocr/models`에 받는다. 현재 모델 합계는 19,326,832바이트이며, 이 Windows 환경의 격리된 의존성 디렉터리는 약 363MB이다. 문서 처리 중에는 모델을 다운로드하지 않는다. 배포 환경은 `requirements-ocr.txt`를 설치한 뒤 `python scripts/setup_ocr.py --skip-install`로 모델만 준비할 수도 있다. 모델 폴더는 배포 이미지나 영속 볼륨에 포함해야 한다.

호출 계약은 `extract_pages(content: bytes, extension: str) -> list[dict]`다. 각 페이지에 `text`, `raw_text`, `extraction_method`, `confidence`, `lines`, `warnings`, `requires_review`가 있다. 페이지 본문과 판독 원문을 글자 수로 자르지 않는다(`text_truncated: false`). 각 OCR 줄은 원문, 신뢰도, 픽셀 좌표의 사각형 네 점을 보존한다. 텍스트 PDF도 줄과 PDF 좌표를 남기며 신뢰도는 임의로 만들지 않고 `null`이다. 같은 높이에 있는 표의 항목과 금액은 실제 좌표를 사용해 왼쪽부터 묶는다. 신뢰도 0.70 미만이거나 상충하는 줄은 팩터 추출용 `text`에서 제외하되 원문과 대안 판독을 남긴다. 금액과 성명은 높은 신뢰도여도 검증 전 후보값이다.

PDF의 원래 텍스트는 보존하고, 이미지들의 중복을 뺀 합집합 면적이 페이지의 25% 이상이면 페이지 전체를 OCR한다. 여러 조각으로 나뉜 스캔도 포함한다. 텍스트·이미지 혼합 페이지에서는 `native_text/native_lines`와 `ocr_text/ocr_lines`를 각각 보관하고, 좌표를 PDF 기준으로 맞춰 읽기 순서를 구성한다. 같은 위치의 내용이 다르면 텍스트를 덮어쓰지 않고 `native_ocr_conflicts`와 검토 상태를 남긴다. OCR이 실패해도 원래 PDF 텍스트와 좌표는 유지한다.

방향 분류가 정상 한국어를 뒤집는 경우를 줄이기 위해 낮은 신뢰도, 넓은 영역에 비해 비정상적으로 짧은 판독, 숫자와 `O/I/l`이 붙어 혼동될 수 있는 식별자를 실제 픽셀로 재판독한다. 페이지당 최대 6개 줄을 정방향으로 한 번씩만 읽는다. `recognition_candidates`에 최초·재판독 문구와 각각의 신뢰도를 남긴다. 명확하게 개선된 줄만 채택하며, 식별자나 금액의 판독이 충돌하면 자동 확정하지 않는다. 기대 성명·계좌·금액을 인식기에 주입하지 않는다.

기본 제한은 파일 10MB, PDF 100쪽, OCR 대상 20쪽, 전체 OCR 작업 180초, 이미지 4천만 픽셀, 렌더링 200dpi이다. `DEBTOFF_OCR_MAX_PAGES`, `DEBTOFF_OCR_TIMEOUT`, `DEBTOFF_OCR_DPI`로 지정할 수 있으며 코드에 상한이 있다.

OCR은 별도 숨김 프로세스에서 실행되고 시간 제한이 지나면 종료된다. 처리된 페이지는 유지한다. 나머지는 `timeout`, `page_limit`, `queue_busy`, `engine_unavailable`, `page_error` 등의 이유와 `manual_review` 상태를 반환한다. 업로드 요청을 무기한 붙잡거나 읽지 못한 부분을 모델로 추정해 채우지 않는다.

검증 명령과 실제 측정 결과(2026-10-04):

- `python -m unittest tests.test_ocr -v`: 본문 절단 방지, 혼합 페이지 보존, 표의 행 결합, 실패·제한·판독 충돌을 포함한 17개 회귀 검사.
- `python scripts/audit_ocr_sources.py`: 기존 가상 사건의 업로드 13건을 SQLite 읽기 전용으로 확인. 원본 해시 13건 일치, 텍스트 PDF 12건은 저장 본문과 완전히 동일, 급여 스캔은 주요 항목 8개 일치. [원문 대조 보고서](../reports/ocr-audit/stored-original-comparison.json). 별도로 예전 `synthetic_case`의 나머지 텍스트 PDF 6종도 본문·줄 좌표가 보존되는지 확인한다. [예전 자료 본문 대조](../reports/ocr-audit/original-native-comparison.json).
- `python scripts/benchmark_ocr_fields.py --phase final`: 김새봄 15종과 예전 김가온 급여 스캔 3쪽을 실제 처리. 83개 항목 중 80개 일치(수정 전 74개), 12.56초. 방향 오판으로 사라졌던 하단 안내 두 줄씩을 복구했다. 남은 3개는 김가온 급여 각 쪽의 `TEST-ACCOUNT-0O1`/`TEST-ACCOUNT-001` 판독 충돌이다. 올바른 후보도 보존되지만 자동 확정하지 않는다. [항목별 결과와 제한](../reports/ocr-audit/field-benchmark-final.json).
- `python scripts/benchmark_ocr_fields.py --retest-only --phase final`: 별도 이하늘 자료 15종·16쪽, 69/69 항목 일치, 3.90초. 텍스트 PDF 15쪽과 스캔 1쪽을 구분해 측정했다. [새 자료 결과](../reports/ocr-audit/retest-benchmark-final.json).

전체 추출 결과는 `.work/ocr-audit/fresh-extraction-final.json`, `original-extraction-final.json`, `retest-extraction-final.json`에 저장해 후속 구조화 검증에서 재사용한다. 벤치마크의 기대값은 결과 비교에만 쓰며 엔진에는 원본 바이트만 전달한다. 기존 `scripts/benchmark_ocr.py`는 성명·실수령액·좌표만 확인하는 좁은 검사이므로 전체 판독 성공의 근거로 사용하지 않는다.

문자가 정확하게 읽혀도 연 소득을 월 소득으로 잘못 해석하거나 채권·예금 항목을 구조화하지 못할 수 있다. 이 단계는 `document_facts.py`의 항목·단위·기간·근거 검증으로 별도 확인한다. 위 비율은 고정된 합성 자료의 항목 검사이며 구겨짐·흐림·필기·실제 기관별 서식의 일반 OCR 정확도를 의미하지 않는다.

진술서에는 별도로 `narrative_claims.py`를 적용한다. 인용에 같은 숫자가 존재한다는 사실만으로 통과시키지 않고, 실제 인용 조각의 자산·생활비·소득·공제 항목과 월간·연간 의미까지 대조한다. 만·천·억원 표기는 정확히 환산하지만 원문에 없는 월평균 계산은 하지 않는다. 실제 합성 출력에서 발견한 자산 240만원→생활비 240만원 오류는 `NARRATIVE_NUMERIC_ROLE_MISMATCH`로 차단된다. `python -m unittest tests.test_narrative_semantics tests.test_grounded_drafting -v`로 28개 검사를 실행한다. 새 검사 16개 중 2개는 모델 응답과 공개자료 어댑터를 대체해 생성 경로의 차단·1회 재작성 연결을 확인하며, 실제 모델 품질이나 문단 전체 의미의 정확도를 입증하지 않는다. 복합 지출 합계처럼 연결이 불명확한 문장은 검토 대상으로 남기고 후속 의미 검증을 유지한다. [캡처된 실제 출력의 재검사](../reports/ocr-audit/narrative-semantic-regression.json).

모델과 API 참고: [RapidOCR 공식 저장소](https://github.com/RapidAI/RapidOCR), [공식 모델 목록과 체크섬](https://github.com/RapidAI/RapidOCR/blob/main/python/rapidocr/default_models.yaml).
