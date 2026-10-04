# 스캔·공식 서식·검토 승인 계산

2026-10-03 로컬 구현. 기존 AX의 상담·문서 판독과 규칙 엔진에 아래 단계를 연결한다. 고객 포털과 사무소 화면은 같은 업로드 API를 사용하고, 계산·서식·프롬프트 본문은 직원 권한에만 공개한다.

## 자동 진행과 상세 검토

요청 서류 제출·검토가 완료되면 원문 검증→구조화 자료→계산/전략→1차 작성→AI 문서 검토까지 자동 진행한다. 미확정 계산·판단은 공란과 보완 목록으로 남겨 담당자가 초안과 함께 확인한다. 다음 단계는 담당자 수정·AI 재검토→변호사 최종 승인→접수증을 근거로 제출 기록이다. 단계의 정본은 `workflow_contract.py`이며 자세한 조건은 [자동 처리](ax-automation.md)에 있다. 아래 순서는 상세 화면에서 같은 정보를 확인·수정하는 수동 경로다. 1차 작성 전에 매 단계의 승인 클릭을 요구하지 않는다.

## 수동 확인 순서

1. `examples/synthetic_case/00_상담회의록_가상자료.txt`를 상담 입력에 넣는다.
2. 생성된 사건의 자료 검증에서 `documents/` 파일을 올린다. 급여 PDF는 이미지로만 이루어진 3쪽이다. 모델 호출 없이 로컬 OCR이 쪽별 본문·좌표·신뢰도를 추출한다.
3. 기존 AX가 본문에서 이름·종류·기간·금액 후보를 찾는다. 저신뢰 OCR 행은 자동 근거에서 제외한다. 원문 대조와 요청 완비 전에는 작성본을 확정하지 않는다.
4. 직원이 증빙을 대조하고 법률 계산의 소득·자산·채권·판단 근거를 입력한다. 가상 예시 채우기는 테스트 값이며 증빙 검증 상태를 바꾸지 않는다.
5. 계산은 원 단위 정수와 Decimal로 수행한다. 확인되지 않은 값은 0원으로 바꾸지 않는다. 증빙 미확인·미결 쟁점이면 계산 미리보기와 차단 사유를 반환한다.
6. 변호사가 계산안의 입력·적용 기준·수치·근거를 검토 승인한다. 이 상태는 법원 인가와 구분한다.
7. 법원 서식에서 원본을 선택해 자동 배치된 값·빈칸·출처를 확인한다. 현재 입력의 승인 계산 또는 자동 검증을 통과한 비만료 계산을 연결한다. 공식 원본 PDF와 작성한 PDF를 각각 내려받는다. 서명·동의 선택란은 자동 작성하지 않는다.

D5105 새 버전 작성은 기본적으로 채무 발생 경위·지급 곤란 사정·향후 변제계획을 근거별로 작성하고 독립 검증한다. 현재 사실·작성 정책·법률 버전이 일치하는 검증 문단만 재사용한다. 의미상 검증 실패에는 피드백 수정을 한 번 허용하고 시간초과/사용 불가는 보류한다. 기존 상담이나 오래된 짧은 초안을 복사해 대체하지 않는다.

기본 요청은 `fields.statement`를 보내지 않는다. 직원이 직접 수정 옵션을 선택해 비어 있지 않은 값을 보낸 경우만 수동 진술로 받으며 별도 검토가 필요하다. 자동 작성 실패의 409 응답에는 `detail.code`, `message`, 저장된 `case_version`이 포함된다. 다음 시도는 이 최신 버전을 사용한다. 실패 이력·알림은 저장하지만 새 PDF는 만들지 않는다. 문장 검증과 PDF 배치·작성값·원본 조항 검증은 별도다.

가족 대납 자료에는 추가 가족채권 후보가 있다. 기본 금융채무 7,800만원 예시는 그 관계를 최종 확정한 값이 아니다. 포함 판단을 하면 가족채권을 추가하고 총채무와 배분을 다시 계산한다. 명의 불일치·기간 부족 파일은 정상 자료를 올린 뒤 따로 시험한다.

## n8n에서 재사용할 API

Bearer 세션과 사건 권한을 동일하게 적용한다. 쓰기에는 직전 사건의 `expected_version`이 필요하며 충돌은 409다. 승인 노드는 변호사의 명시적 검토를 받은 뒤 호출한다.

| 작업 | 메서드·경로 | 입력·결과 |
| --- | --- | --- |
| 스캔 수신 | `POST /api/cases/{id}/documents` | multipart `file`, `request_id`, `expected_version`; OCR 결과 포함 사건 |
| 판독·초안 자동 분석 | `POST /api/cases/{id}/ax-runs` | 기존 비동기 실행; 실행 ID로 진행 조회 |
| 계산 스키마·근거 | `GET /api/legal-calculation/schema` | 정책·예제·필수 입력 |
| 검토 계산 | `POST /api/cases/{id}/legal-calculations` | `{expected_version, inputs}`; 저장된 계산 포함 사건 |
| 계산 승인 | `POST /api/cases/{id}/legal-calculations/{calc}/approve` | `{expected_version, reason}`; 변호사 전용 |
| 회차별 배분 CSV | `GET /api/cases/{id}/legal-calculations/{calc}/download` | UTF-8 BOM CSV |
| 원본 서식 목록 | `GET /api/court-forms?court_id=CT01` | 지원 법원·템플릿·좌표·출처·버전 |
| 배치할 값 미리보기 | `GET /api/court-forms/{template}/preview?case_id={id}&calculation_id={calc}` | 필수 빈칸·출처·검토 상태; 계산 연결은 선택 |
| 서식 생성 | `POST /api/cases/{id}/court-documents` | `{expected_version,template_id,fields,calculation_id}` |
| 수정본 AI 재검토 | `POST /api/cases/{id}/court-documents/{doc}/verify` | `{expected_version,reason}`; 로컬 원문·PDF 대조, 승인과 별도 |
| 서식 검토 승인 | `POST /api/cases/{id}/court-documents/{doc}/approve` | `{expected_version,reason}`; PDF·원본·내용·사건 리비전 검사 |
| PDF 받기 | `GET /api/cases/{id}/court-documents/{doc}/download` | 생성 당시 PDF; SHA-256 검증 |
| 공식 빈 양식 받기 | `GET /api/court-forms/{template}/original?format=pdf` | 버전 고정 공식 PDF; `format=hwp`로 HWP 원본 |
| 프롬프트 | `GET /api/prompts` | 3개 지시문과 버전·해시 |
| 합성 파일 받기 | `GET /api/testing/sample-bundle` | 테스트 ZIP |

최종 제출 패키지는 `GET /api/cases/{id}/filing-readiness`로 누락·검증 조건을 확인하고 `POST /api/cases/{id}/filing-packages`로 준비한다. 변호사는 `/filing-packages/{package}/approve`에 `expected_version`, `reason`, `final_checks_confirmed:true`를 보내 서명·동의·별지·법원 추가 요구까지 최종 확인한다. 외부 제출 후 전용 `/filing-packages/{package}/receipts`에 접수증을 업로드하고 `/submissions`로 내부 제출 기록을 저장한다. 접수증 multipart 필드는 `file`, `expected_version`, `court_case_number`, `reason`, `person_confirmed`, `case_number_confirmed`, `receipt_confirmed`이다. 원문·계산·PDF·적용 법률이 변경되면 이전 승인으로 제출 처리할 수 없다. 접수증의 형식·명의·사건번호 확인도 필요하다. 실제 법원 전송 API는 연결되어 있지 않다.

프롬프트는 모델의 근거 선택에 사용한다. 법률 산식·업무 규칙·OCR·승인 권한은 코드와 버전 정책으로 실행하며 LLM이 수치나 승인 조건을 바꿀 수 없다. 사건 자료 변경은 기존 계산·문서·승인을 만료시킨다.

공식 공통 PDF 7종을 15개 관할에 매핑한 상태다. 15개 법원별 개별 서식을 모두 승인한 것은 아니다. 부산 2026-07 자료제출목록은 별도 템플릿이다. PDF에 작성값·근거 대조표를 덧붙이고 제출 전 분리 검토하도록 표시한다. HWP 원본의 편집 및 실제 법원 제출 자동화는 포함하지 않는다.
