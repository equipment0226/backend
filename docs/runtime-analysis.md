# 실행 환경과 검증 자료

현재 루트 앱은 `scripts/dev.py`가 동일 Python으로 API와 두 화면 서버를 실행합니다. `lawmaster`는 별도 프로젝트이며 루트 실행에서 그 가상환경을 사용하지 않습니다.

2026-10-04부터 이 작업 폴더는 [새 테스트 프로필](testing.md)을 사용합니다. 이전 사건을 백업 후 초기화했으며, `.local/start-profile.json`이 `fresh`이면 실행기가 새 계정과 빈 사건 시작을 선택합니다. 계정은 `accounts` 테이블에 비밀번호 해시로 보관하고 재시작 시 기존 비밀번호를 덮어쓰지 않습니다. 회귀시험 실행기는 별도 저장소에서 `DEBTOFF_START_PROFILE=demo`를 지정하고 fresh 시험만 프로필을 개별 전환합니다.

같은 날 고객이 직접 생성한 수임번호 `2026-000001`은 상담 접수 단계로 전환했습니다. 제출된 11개 원본과 기존 처리 이력을 보존하고 세부 상담 전 미제출 자동 요청 12건은 철회했습니다. 직원·변호사가 세부 상담을 요청한 뒤 상담 기록을 저장하면 추출이 시작됩니다. 이 변경 전 SQLite 백업은 `.local/backups/before-consultation-gate-20261004.sqlite3`에 보관합니다. 현재 상태는 [실행 중 API 검증](../reports/consultation-live-state.json)에 기록했습니다. 과거의 빈 사건 검증 결과는 초기화 당시의 스냅샷입니다.

## 실행 구성

Python 의존성은 `requirements.txt`, React 빌드는 `apps/frontend/package-lock.json`을 기준으로 설치합니다. 판독 설치·모델 준비는 [OCR 문서](ocr.md)를 따릅니다. 기존 `.local`과 `.env`를 개발 캐시처럼 삭제하지 않습니다.

원문 판독·서류/문서 점검·근거 서술은 로컬 llama3 경로를 사용합니다. 큰 JSON 하나를 계속 재작성하는 대신 문단 단위로 입력·출력 크기를 제한합니다. 작성 제한시간은 `DEBTOFF_LOCAL_WRITING_TIMEOUT`으로 조정하며 기본 240초, 허용 범위 30~600초입니다. 수동 진술서 화면의 요청 대기 한도는 10분입니다. 제한시간 확대가 CPU 처리량이나 작성 성공을 보장하지 않습니다.

외부 검증은 익명화된 고급 판단에 한정합니다. API 키는 유효한 `.env`, 사용자 `.env.example`, 프로세스 환경 순서로 선택합니다. 다른 설정은 환경→`.env`→`.env.example` 순서입니다. 키·프롬프트 원문을 보고서나 UI에 출력하지 않습니다. 캐시·호출 한도와 공식 법령 갱신은 [자동 처리 문서](ax-automation.md)에 설명합니다.

## 검증 자료

| 자료 | 확인 범위 |
| --- | --- |
| `reports/test-results.json` | 격리 저장소의 현재 회귀시험 결과; 모델 대역 포함 |
| `reports/ax-live-verification.json` | 합성 원문 로컬 호출·익명 외부 호출의 실제 결과 |
| `reports/statement-authoring-live.json` | 최신 근거 진술 작성의 실제 상태·소요시간·실패 사유 |
| [실제 포털 상담](../reports/portal-chat-live.json) | 합성 질문의 실제 로컬 안내 연결·외부 미전송 확인 |
| `reports/court-form-typography.json` | 원본 PDF 폰트·대체 글꼴·작성칸 배치와 출력 검사 |
| `reports/legal-watch-validation.json` | 공식 출처 감시·적용 제한의 검증 |
| `reports/browser-runtime-readonly.json` | 실행 중인 화면의 실제 저장 원문·문서 배치·다운로드 조회 |
| `reports/ui-automation/` | 격리된 합성 응답으로 확인한 화면·알림·입력 |
| `reports/portal-experience/` | 공개/본인 사건 경계·후기 동의 철회·상담 상태·모바일/동작 줄이기 화면 검사 |
| `reports/browser-fresh-start.json` | 실제 새 계정 3개 로그인·예시 불러오기·자료 다운로드, 사건 생성 없이 빈 상태 유지 |
| `reports/fresh-start-state.json` | 새 계정과 사건·분석·생성·법원 결과 원장의 0건 상태 |
| `reports/browser-portal-application.json` | 격리된 고객 신청·동의·중복 방지·알림·카드 높이 검증 |
| `reports/office-readability.json` | 격리된 업무 화면의 한국어 표시·알림·사이드바 정렬 검증 |
| `reports/office-consultation.json` | 격리된 상담 신청 원문·세부 상담 요청·기록 저장·수정 흐름 |
| `reports/consultation-live-state.json` | 실제 세 역할 로그인·상담 대기 상태·기존 11개 파일 보존·요청 철회 |

```powershell
python scripts/run_tests.py
python scripts/browser_ax_automation.py
python scripts/browser_fresh_start.py
```

실제 작성에서 형식·메타데이터·근거 불일치로 보류된 실행은 실패로 남깁니다. 이전 작성 버전의 성공, 대역 시험 성공, 파일 생성 여부만으로 새 작성 버전의 실제 검증 성공을 추정하지 않습니다. 과거 `llama-*-benchmark.json`의 GPU/CPU 관측은 해당 시점의 환경 기록이며 현재 장치 성능이나 실문서 SLA가 아닙니다.

현재 `reports/statement-authoring-live.json`은 `local-grounded-statement-v3`의 세 문단 작성 완료와 독립 원문 검증 통과를 기록합니다. 합성 사건으로 실제 로컬 호출을 확인한 결과이며, 작성 PDF는 `.work/court_typography/D5105-grounded-v3.pdf`에 보존합니다. 이 한 사례의 통과를 모든 실문서의 작성 성공률로 일반화하지 않습니다.

## 보존과 정리

초기 설계 묶음·임시 출력·사용하지 않는 `lawmaster/.venv`와 웹 의존성은 정리했습니다. 재설치는 [별도 프로젝트 안내](../lawmaster/README.md)를 따릅니다. 실행 소스·테스트·원본 법원 서식·공식 원문·사건 데이터·기존 출력·보고서·고유 설계 변경은 보존했습니다. 삭제 대상과 보존 이동의 SHA-256은 [정리 기록](../reports/workspace-cleanup.json)에 있습니다.

추가로 오래된 `.work` 합성 시험 데이터·수집 임시 파일·일회성 도구를 정리했습니다. 정리 당시 최종 PDF 비교 자료와 `ax-final-tests.log`, 진행 중인 `portal-live-*`를 보존했습니다. 이후 회귀시험의 격리 데이터와 로그는 다시 생성되며, 검증 결과는 `reports`에 보존합니다.
