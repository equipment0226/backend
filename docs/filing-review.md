# 문서 검토와 제출 기록

1차 작성본은 검토와 보완을 위한 문서다. 미확정 항목을 0원이나 임의의 판단으로 채워 제출 가능 상태로 바꾸지 않는다. 담당자가 필요한 사실·계산 판단·본문을 보완하면 새 PDF를 만들고 그 파일을 다시 AI 원문 대조한다. 최종 제출 패키지의 변호사 승인은 1차 초안 검토 기록과 구별한다.

## 내부 제출 패키지

`GET /api/cases/{case_id}/filing-readiness`는 준비 가능 여부, 최신 작성본 7종, 보완 사유를 반환한다. 서식은 개시신청서(D5100), 재산목록(D5101), 수입지출목록(D5103), 진술서(D5105), 변제계획안(D5110), 급여소득 증명서(D5115), 개인회생채권자목록(D5106)이다. 이는 현재 제품이 지원하는 기본 서식 묶음이며 개별 법원의 추가 첨부요구가 없다는 뜻이 아니다. 등록되지 않은 관할이나 필요한 공식 서식이 없는 경우에는 준비를 차단한다.

`POST /api/cases/{case_id}/filing-packages`는 `expected_version`, `document_ids`를 받는다. 빈 목록은 서버가 최신 서식 7종을 선택한다. 다음 조건이 모두 충족되어야 `prepared` 상태로 저장한다.

- 세부상담 완료, 미확정 사실·미결 쟁점·미완료 자료 및 보정 요구 없음
- 현재 자료 버전의 공식 서식 7종과 동일한 검증 계산 연결
- 필수 빈칸·출력 넘침 없음, 원본 서식·계산 재대조 일치
- 현재 PDF의 AI 원문·수치 대조 통과 및 PDF 해시 일치
- 적용 법률 변경 검토 완료

`GET /api/cases/{case_id}/filing-packages/{package_id}/download`는 현재 작성본을 ZIP으로 내려준다. PDF 이름은 수임번호와 서류명을 사용한다. 다운로드는 외부 전송이나 법원 접수가 아니다.

`POST /api/cases/{case_id}/filing-packages/{package_id}/approve`는 변호사만 사용할 수 있다. `expected_version`, 5자 이상의 `reason`, `final_checks_confirmed: true`가 필요하다. 이 확인에는 AI가 전부 확인했다고 보장하지 않는 서명·날인, 미매핑 빈칸, 별지 전체, 계산 판단 및 개별 제출요건 검토가 포함된다. 승인에는 자료 버전과 패키지·계산·AI 대조·PDF 해시를 고정한다. 화면은 `제출 준비`로 바뀐다.

자료나 판단이 바뀌면 패키지는 만료된다. 같은 자료 버전에서 새 PDF를 작성하거나 AI 검토 결과가 바뀌어도 기존 승인본과 일치하지 않으면 재승인이 필요하다. 승인된 검토 보고서(`bundles`, `kind=review_only`)는 제출 패키지를 대신할 수 없다.

## 외부 제출 후 접수증 기록

현재 실제 법원 전송 연동은 없다. 사용자가 외부 전자소송 등에서 제출한 뒤 접수증을 올려 내부 이력을 남긴다. 기록은 법원의 인가·면책을 의미하지 않는다.

`POST /api/cases/{case_id}/filing-packages/{package_id}/receipts`는 다음 multipart 필드를 받는다.

- `file`: PDF, TXT, PNG 또는 JPG 접수증, 최대 10MB
- `expected_version`, `court_case_number`, `reason`
- `person_confirmed`, `case_number_confirmed`, `receipt_confirmed`: 모두 true

직원 또는 변호사가 명의·사건번호·실제 접수를 확인한다. 로컬에서 읽은 원문에 개인회생 사건번호와 접수 표시가 있어야 하며 읽지 못한 쪽이 있으면 등록하지 않는다. 원본은 사건별 uploads 폴더에 저장하되 `documents` 대신 별도 `submission_receipts` 컬렉션에서 보관한다. 따라서 접수증 등록은 계산 입력 리비전을 올리거나 최종 승인을 무효화하지 않는다. 고객 증빙을 보완하는 기존 `/documents` 경로는 계속 입력과 승인을 무효화한다.

`POST /api/cases/{case_id}/submissions`는 `expected_version`, `bundle_id`(승인된 filing package ID), `receipt_document_id`(별도 접수증 ID), `court_case_number`를 받는다. 현재 승인본과 접수증 검증·파일 해시·사건번호가 일치할 때만 내부 접수 기록을 저장하고 `법원 제출`로 표시한다. `record_type=external_filing_receipt`, `external_transmission=false`, `court_approval_confirmed=false`를 저장한다. 접수증 파일은 `/submission-receipts/{receipt_id}/download`로 내려받는다.

접수증 원본도 백업·복원 해시 검증 대상이다. 누락한 접수증이 있는 백업은 정상 복원으로 처리하지 않는다.

검증: `python -X utf8 -m unittest tests.test_filing_gate tests.test_filing_api -v`. 실제 계산·공식 서식 미리보기와 격리된 SQLite/ASGI 경로를 사용한다. AI 결과와 PDF 내용은 이 제출 경계 테스트의 합성 입력이며 실제 모델 호출이나 외부 법원 전송은 없다.
