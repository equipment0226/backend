# 현재 문서 안내

실행 흐름은 [단계 계약](../apps/api/workflow_contract.py), 실행 코드는 `automation.py`가 정본입니다. 상태·검증 범위는 실제 실행 보고서로 확인합니다.

| 문서 | 내용 |
| --- | --- |
| [새 테스트 안내](testing.md) | 새 계정·빈 사건 목록에서 고객 신청, 가상자료, 알림·검증 확인 |
| [분리 저장소](repositories.md) | 백엔드·고객·직원/변호사 독립 실행·배포와 개인정보 제외 |
| [문서 항목 추출](document-extraction.md) | 문서별 의미·단위·기간·계좌·대출과 원문 근거 보존 |
| [구조](architecture.md) | 여덟 단계 입출력·검증·재개와 모듈 책임 |
| [자동 처리와 검증](ax-automation.md) | 개인정보 경계·재요청·작성·법령 갱신·비용·결과 축적 |
| [고객 포털](portal.md) | 공개 안내·내 사건 권한·동의 후기·상담과 직원 연결 |
| [법원별 요청](court-request-rules.md) | 서류 범위·기간·옵션·공식 근거 |
| [법률계산](legal-calculation.md) | 산식·입력 증빙·지원 범위 |
| [법원 서식](court-forms.md) | 원본 PDF·폰트·배치·필수값과 버전 |
| [문서 업무](document-workflow.md) | 계산 연결·작성·승인·재검증 |
| [보완·최종 승인·제출 기록](filing-review.md) | 수정본 AI 재검토·변호사 승인·접수증 연결 |
| [OCR](ocr.md) | 원문 판독·페이지·오류 처리 |
| [프롬프트](prompts.md) | 업무별 입력과 검증 경계 |
| [실행 환경](runtime-analysis.md) | 설치·호출 설정·실제 검증 자료 |
| [배포](deployment.md) | 로컬·배포·백업과 복원 |
| [구현 상태](implementation-status.md) | 현재 기능 안내와 기존 요구 추적의 구분 |
| [접수 API](ax-pipeline.md) | 상담 접수·후보·초안 API의 상세 계약 |

`data/registry.json`과 `data/requirements.json`은 기존 기획에서 가져온 출처·요구 ID를 보존합니다. 초기 설계 파일의 주장이나 예정 기능을 현행 구현으로 해석하지 않습니다. 보존할 실무 검토 원본은 `data/reference_evidence/`에 있습니다.

단계별 입력·출력·검증·수정 한도·체크포인트를 명시하는 구조는 [triz_backend STAGES](https://github.com/equipment0226/triz_backend/blob/main/docs/STAGES.md)와 [LEARNING](https://github.com/equipment0226/triz_backend/blob/main/docs/LEARNING.md)을 참고했습니다. 해당 프로젝트의 전체 단계나 학습 엔진을 이식한 것은 아닙니다.
