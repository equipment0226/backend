# 법원별 서류 요청과 검증

`data/court_request_rules.json`은 상담 추출 범위를 변경하지 않고 기존 서류 후보를 관할·기관·계좌·기간별 요청으로 나눈다. 법정 필수서류, 대체 가능한 서류, 법원이 추가로 명할 수 있는 서류, 사전 준비자료를 `requirement_kind`로 구별한다.

확인일은 2026-10-03이다. 법원별 규칙은 확인된 공개자료의 범위이며 전국 모든 재판부 실무를 확인했다는 의미가 아니다. 개별 사건 보정명령이 있으면 해당 원문과 요청 범위를 우선 연결해야 한다.

## 확인한 법원별 근거

| 법원 | 적용 원문 | 구현한 주요 범위 |
| --- | --- | --- |
| 서울회생법원 | [실무준칙 제402호](https://slb.scourt.go.kr/rel/information/qna/practice_rule.pdf), PDF 228~231쪽 | 급여증빙 대체 통장 6개월, 월별 수입상황보고서 1년, 추가명령 가능한 예금거래 1년 |
| 수원회생법원 | [실무준칙 제402호](https://swb.scourt.go.kr/swb/info/info_02/practice_rule_swb.pdf?v=2), PDF 99~100쪽 | 급여 대체자료와 필수자료 구별, 추가자료의 기간 구별 |
| 부산회생법원 | [2026-07-01 시행 자료제출목록](https://file.scourt.go.kr/AttachDownload?path=004&file=1782801689285_154129.pdf&downFile=%282026.+7.+1.+%EC%8B%9C%ED%96%89%29+%EB%B6%80%EC%82%B0%ED%9A%8C%EC%83%9D%EB%B2%95%EC%9B%90+%EC%9E%90%EB%A3%8C%EC%A0%9C%EC%B6%9C%EB%AA%A9%EB%A1%9D.pdf&seqnum=), 1~4쪽 | 전체 은행거래 1년 또는 계좌통합 상세조회, 급여입금 2년, 사업 세무증빙 3년, 지방세 전국·모든 세목 5년; 가족·혼인 상세와 본인·제3자 주민번호 표시 구별 |
| 광주회생법원 | [실무준칙 제402호](https://gjb.scourt.go.kr/gjb/info/info_01/practice_rule_gjb.pdf?v=1), 인쇄 187~190쪽 | 급여 대체자료 6개월, 수입상황보고서 1년, 추가명령 가능한 예금거래 1년 |
| 그 외 등록된 11개 법원 | 세부 원문 미확인 | 전국 공통 기준과 명시적으로 지정된 사건 범위만 적용하며 `coverage=unverified` 유지 |

관공서 서류 발급일은 [개인회생사건 처리지침 제4조 제1항의 공식 안내](https://www.easylaw.go.kr/CSP/CnpClsMain.laf?ccfNo=2&cciNo=1&cnpClsNo=1&csmSeq=1286)에 따라 원칙 2개월로 구분한다. 증명 대상기간과 발급일은 별개이며 특별한 사정에 따른 예외가 존재한다. 부산 목록의 2개월 발급 유의사항은 별도로 기록한다.

JSON의 각 출처는 URL·확인일·조항/쪽·적용 법원·원문 해시를 보유한다. `scripts/build_court_request_policy.py`는 검토한 명시적 매핑에서 JSON을 재생성한다. 원문을 자동 추론하여 규칙을 바꾸는 스크립트가 아니다.

## 요청과 이력

`apps.api.court_rules.plan(case, required_documents, as_of=None)`은 기존 규칙 평가 결과를 받아 요청 계획을 반환한다. 입력 `financial_accounts`, `creditors`, `insurance_policies`, `employers`에 존재하는 기관을 분리한다. `document_scopes`는 같은 종류의 서류에 여러 기간을 명시할 때 사용한다. 자유문장 계좌정보를 임의로 계좌 ID로 바꾸지 않는다.

`scope_key`는 법원·서류종류·기관·계좌키·기간·목적·발급옵션을 반영한다. 제목에는 계좌번호 끝자리만 사용하고 전체 계좌번호는 요청 제목이나 외부 검증 데이터에 넣지 않는다.

`reconcile()`은 동일 범위를 중복 생성하지 않는다. 불필요해진 요청은 철회 이력을 남기고 제출파일을 보존한다. 사유가 있는 수동 철회는 같은 범위에 계속 적용되며, 관할·계좌·기간이 달라지면 새 범위로 재평가한다. 기존 `workflow_rulebook` 요청은 범위가 확실히 같을 때만 제출 연결을 승계한다. 범위를 알 수 없는 기존 제출은 별도 확인 대상으로 남기며 여러 계좌 요청을 일괄 완료 처리하지 않는다.

### 소득유형에 따른 요청 범위

2026-10-04 수정에서 `income_eligibility.business_only_document_ids`로 사업자 등록·매출·사업경비·사업 세무자료의 자동 요청 조건을 분리했다. 현재 사업·프리랜서 소득 또는 급여와 사업의 겸업 근거가 있을 때만 이 분기를 적용한다. 급여만 확인된 사건에 과거 자동 생성된 `WF03` 요청이 남아 있어도 그 요청 자체를 사업소득의 근거로 사용하지 않는다. 소득유형이 불명확하면 `INCOME_TYPE_UNCONFIRMED`로 세부상담 확인을 요구한다. 소득금액증명처럼 급여소득자에게도 쓰이는 공통 자료는 사업자 전용으로 분류하지 않는다.

상담의 “사업소득, 세금 체납, 과거 이력은 없습니다”처럼 나열된 부정문을 문장 단위로 처리한다. 미확인·질문, 근무처 사업자등록번호, 빈 서식 제목은 현재 사업의 근거가 아니다. 급여와 사업소득이 함께 명시된 경우에는 양쪽 증빙 분기를 유지한다. 연금 가입내역과 실제 연금 수급내역, 지방세 증명서 제목과 실제 체납도 구별한다. 이는 기존 자료 준비 규칙의 문맥 오류를 수정한 것이며 새 제출 의무나 법률 판단을 추가하지 않는다.

재조정 시 필요 없어진 자동 요청은 철회 이력을 남기고 원본 업로드를 보존한다. 이미 완료한 요청은 완료 상태와 함께 `no_longer_required`를 기록한다. 직원 수동 요청과 개별 법원 명령은 자동 철회하지 않는다. 관련 회귀검증은 `tests/test_income_request_routing.py`에 있다.

## 검증과 자동화

`validate_metadata(request, document, as_of=None)`는 기관·계좌키·시작/종료일·발급일·발급옵션을 비교한다. 요청기간보다 짧거나 기관/계좌가 다르면 재요청 사유를 반환한다. 필수 판독값이 없으면 `metadata_required`이며 통과가 아니다.

발급일 검증은 고정된 요청 계획일과 현재 서울 날짜 중 늦은 날을 기준으로 한다. 오래된 계획 이후 새로 발급한 서류를 미래 문서로 오판하지 않으며, 실제 오늘 이후의 발급일은 허용하지 않는다. 테스트는 `as_of`로 검증일을 고정할 수 있다.

자동화의 로컬 판독은 요청 범위에 한해 기관명·사전에 등록한 계좌번호·날짜가 명시된 조회기간·발급일 등을 읽고 원문 인용을 저장한다. 첫 거래와 마지막 거래 날짜만으로 기간 전체가 포함되었다고 추정하지 않는다. 결정적 비교와 원문 의미 검증을 함께 통과해야 자동 완료한다. 판독 불가·기관 미확인·미검증 법원 기준은 검토 상태로 남는다.

직원용 API는 다음과 같다. 모두 사건 접근권한과 `expected_version`을 검사한다.

- `POST /api/cases/{case_id}/document-scopes`: 기관·계좌·기간 변경과 요청 재조정
- `POST /api/cases/{case_id}/documents/{doc_id}/metadata`: 원문 판독값과 검토사유 기록
- `POST /api/cases/{case_id}/requests/{request_id}/withdraw`: 사유 있는 수동 철회
- `POST /api/cases/{case_id}/notifications/{notice_id}/read`: 본인에게 허용된 알림 읽음 처리
- `POST /api/cases/{case_id}/court-outcomes`: 담당 변호사가 검증된 법원 원문과 생성 문서 버전을 연결

고객 응답에서는 전체 계좌키, 내부 판독값, 검증 원문, 직원용 알림과 전략 데이터를 제외한다.

## 판례와 검색 자료

`data/legal_ax_sources.json`과 `data/legal_research/manifest.json`은 다운로드 대상과 실제 원문 해시·텍스트 해시·수집시각을 구분한다. `scripts/collect_ax_legal_sources.py`는 공식 HTTPS 출처만 내려받고 고객정보를 전송하지 않는다. `corpus.ingest_local_sources()`는 다운로드한 원본의 해시를 다시 확인한 뒤 기존 검색 색인을 보존하면서 추가한다.

추가한 실제 판례는 [2010마1179](https://www.law.go.kr/LSW/precInfoP.do?precSeq=150036), [2015마657](https://www.law.go.kr/LSW/precInfoP.do?precSeq=179701), [2014마1255](https://www.law.go.kr/LSW/precInfoP.do?precSeq=177702)이다. 각각 편파변제·청산가치, 보정 시정기회·배우자재산, 인가 후 수행가능성 심리를 다룬다. 세 사건 모두 파기환송이므로 인가 성공 사례로 분류하지 않는다. 판례 당시의 구법상 기간을 현행 계산 상수로 사용하지 않는다.

[2018마7459](https://www.law.go.kr/LSW/precInfoP.do?precSeq=225433)는 확정된 변경계획을 완납한 채무자의 면책결정이 유지된 실제 유리한 결과이다. 별도 면책불허가 사유는 여전히 검토해야 한다. 신규 신청 인가 사례나 승인된 신청서 원본으로 분류하지 않고 `outcome=discharge_affirmed`, `not_an_approval_sample=true`로 보관한다.

`data/ax_legal_precedents.json`에 쟁점, 필요한 증빙, 검토할 보완전략과 적용해서는 안 되는 결론을 별도로 기록했다. 현행 채무한도와 변제기간은 [제579조](https://www.law.go.kr/LSW/lsLinkCommonInfo.do?chrClsCd=010202&lsJoLnkSeq=1023847839), [제611조](https://law.go.kr/LSW/lsLinkCommonInfo.do?chrClsCd=010202&lsJoLnkSeq=1024710777)의 2026-10-02 시행판을 대조했다.

검증 명령: `python -X utf8 -m unittest tests.test_court_rules tests.test_corpus tests.test_automation_api -v`.

## 정기 법령 변경 확인

`data/legal_watch_policy.json`에 공식 출처 11개와 원문 기준 해시가 있다. `apps.api.legal_watch.run_once()`는 기본 24시간 간격, 하루 20개 출처·40회 HTTP 요청, 출처당 최대 2회 시도로 제한한다. 리다이렉트와 재시도도 요청 수에 포함하며 수동 실행도 하루 한도를 넘지 않는다. 조건부 요청의 ETag·Last-Modified를 사용한다. 앱의 스케줄러 활성화는 `DEBTOFF_LEGAL_WATCH=1`이다.

법률의 현행 조문 링크가 JavaScript 틀만 반환하면 공식 응답의 법령 일련번호와 시행일을 읽어 공식 본문 엔드포인트를 요청한다. JavaScript를 실행하거나 임의 외부 URL을 따라가지 않는다. 동일 시행본을 참조하는 조문들은 한 번 받은 본문을 공유한다. 메뉴만 있는 HTTP 200 응답은 법령 수집 성공으로 표시하지 않는다.

제579조 채무한도 두 값과 제611조 변제기간 두 값만 자동 변경할 수 있다. 숫자를 제외한 조문 전체가 검토한 본문과 일치하고 공식 시행일이 확인되어야 한다. 다른 문언·법원 규칙 변경은 필요한 후속 작업과 함께 검토 보류로 전달한다. 미래 시행본은 대기하고 기존 본문을 검색에 유지한다. 접수 후 법이 변경된 사건은 부칙·경과규정 적용 검토를 요구한다.

수집 기록·실패·예산·이전 적용 버전은 `store.DATA_DIR/legal_watch`에 보존한다. 실패 시 마지막 정상 원문을 유지한다. 수동 법령 수집도 `assess_collected_sources()` 검증을 거치며, 새 원문이 들어왔지만 아직 규칙 검증이 끝나지 않았다면 사건 생성 단계가 대기한다. 단순 공백이나 화면 배너 변화는 법률 본문 변경으로 취급하지 않는다.

2026-10-03 실제 확인 결과는 `reports/legal-watch-validation.json`에 기록했다. 저장된 31개 공식 자료 모두 본문이 있고, 정기 확인 대상 11개도 기준 본문과 일치했다. 원문의 공개 수집 이력과 사건별 최신 법 적용 판단은 구별한다. 오프라인 검증: `python -X utf8 -m unittest discover -s tests -p test_legal_watch.py`.
