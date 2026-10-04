# 문서 원문에서 구조화한 관측

`apps/api/document_facts.py`는 법률 판단 없이 증빙의 항목·숫자·기간을 읽는다. `extract(sources)`의 입력은 원문 source 목록이며, 정답 manifest나 테스트 사례의 이름·금액을 운영 코드에서 읽지 않는다.

각 결과에는 `key`, 한글 `label`, `value`, `quote`, `source_id`, `document_id`, `page`, `line_start`, `line_end`를 보관한다. 금액은 `unit=KRW`, `basis`, `frequency`, `period_start`, `period_end`, `as_of`, `institution`, `account_key`, `loan_key`를 가능한 범위에서 함께 보관한다. 원문에 12개월이라고 적힌 경우에만 `period_months=12`가 붙는다. 천원·만원 단위 환산에는 `unit_multiplier`, `source_unit_quote`, `unit_source_id`를 남긴다.

잔액 기준일(`balance_date`)과 조회일(`query_date`)은 종류와 정확한 인용을 붙여 `as_of`로 사용한다. 발급일은 `issued_at`에 별도로 보관한다. 발급일만 있는 자료에서 잔액 기준일을 추정하지 않으며, 늦게 재발급된 과거 잔액이 최신 잔액을 덮어쓰지 않는다.

지원 원문 범위는 주민등록·가족관계, 계좌통합조회, 부채증명, 원천징수, 재직·건강보험·연금가입, 지적조회·지방세, 보험가입·해약환급금, 거주확인·생활비/채무 경위 진술, 은행거래표, 급여명세다. 증빙에 없는 항목을 자동으로 0으로 만들지 않는다. 명시된 부양가족 없음·소유 없음은 `explicit_absence`로 구분한다. 가구원 수와 법률상 인정 부양인원은 다르다.

- 연간 총급여·실수령 합계·공제는 월 소득과 별도로 저장한다. 명시된 월평균과 월 실수령만 기존 월소득 후보로 투영한다.
- “급여외 소득 없음”, “사업소득 없음”, “현재 무직 아님”은 무직으로 판정하지 않는다. 급여자료의 근로소득 표시는 해당 증빙 기간의 관측이다.
- 은행표는 급여 입금·생활비 출금·상환 출금을 월별로 구분한다. 중복 검색 구간의 같은 행을 다시 더하지 않는다.
- 채권자의 원금·이자·합계, 예금의 기관·계좌·기준일을 유지한다. 가족 차용금 잔액과 약정 진술은 별도 관측으로 두며 금융기관 채무에 임의 합산하지 않는다.
- 불명확하거나 충돌하는 값은 최종 판단을 대신하지 않는다. `evidence_mapping.py`가 원문을 다시 읽어 동일성·단위·집계 근거를 대조하고, 계산 가정과 법률 승인은 별도로 유지한다.

`ax_engine.extract_factor_candidates`가 모든 typed 관측을 기존 추출 후보에 연결한다. 직원 화면은 해당 문서·페이지·인용을 열어 확인·정정할 수 있다. 관측 생성 자체는 사실 확정이나 AI 의미 검증 통과를 뜻하지 않는다.

검증: `python -X utf8 -m unittest tests.test_document_facts tests.test_ax_engine tests.test_income_request_routing tests.test_evidence_mapping_integrity -v`. 두 독립 가상사건의 전체 15종 원본, 실제 이미지 급여 OCR, 부정 표현, 연간/월간 분리, 표와 단위, 여러 채권자·계좌, 중복·충돌·가족채무 분리를 대조한다.
