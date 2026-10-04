# 세 저장소로 실행하기

| 저장소 | 역할 | 기본 주소 |
| --- | --- | --- |
| [backend](https://github.com/equipment0226/backend) | 인증·사건·상담·서류·OCR·계산·작성·검토 API | http://localhost:8000 |
| [frontend_customer](https://github.com/equipment0226/frontend_customer) | 고객 포털·상담 신청·자료 제출·알림 | http://localhost:5173 |
| [frontend_lawyerandstaff](https://github.com/equipment0226/frontend_lawyerandstaff) | 직원·변호사 사건 업무공간 | http://localhost:5174 |

각 저장소에는 자체 README와 Dockerfile, 안전한 환경변수 예시가 있습니다. 프론트는 각각 `npm ci`, `npm run build`, `npm start`로 독립 실행하며 `DEBTOFF_API_ORIGIN`을 서버 실행 환경에서 주입합니다. 백엔드는 `python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000`으로 실행합니다. 설치·운영 계정·OCR·글꼴 요건은 각 저장소 README에 명시합니다.

백엔드 `DEBTOFF_CORS_ORIGINS`에 두 프론트의 origin을 등록합니다. 모델 API 키는 백엔드에만 설정합니다. 프론트는 API 키나 모델에 직접 접근하지 않습니다. 테스트용 계정 자동 생성은 격리한 로컬에서 `DEBTOFF_DEMO_MODE=1`, `DEBTOFF_START_PROFILE=fresh`일 때만 활성화합니다.

Git에는 운영 DB·고객 업로드·생성 문서·로컬 모델·기존 환경변수 값을 넣지 않습니다. 원래 통합 작업 폴더의 `.env.example`에도 실제 키가 있을 수 있어, 분리 저장소용 예시는 변수명과 안전한 기본값으로 새로 만듭니다. 키 값은 빈칸이며 현지 환경에서 입력합니다. 공개 법률 자료와 명시적인 가상 테스트 자료만 포함합니다.

통합 작업 폴더에서는 `python scripts/export_repositories.py`로 `.work/repository-split/`에 검토 가능한 세 작업 사본을 만듭니다. `--refresh`는 이전 내보내기 파일의 해시를 확인한 뒤 갱신하며 사람이 수정한 파일을 덮지 않고 `.git`과 로컬 설정을 보존합니다. 생성기는 원격 저장소를 변경하지 않습니다. 내보내기 목록과 SHA256은 `export-manifest.json`, 독립 실행 검증은 `validation.json`, 원격 게시 결과는 통합 폴더의 `reports/git-publication.json`에서 확인합니다.
