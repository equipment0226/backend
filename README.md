# 빚오프 backend

개인회생 사건·인증·상담·서류/OCR·법률계산·작성·검토 API입니다. 고객과 직원 화면은 별도 저장소에서 실행합니다. 이 저장소에는 운영 DB·고객 업로드·모델 가중치·API 키가 없습니다.

## 실행

Python 3.11 이상을 사용합니다. `python -m venv .venv`로 가상환경을 만든 뒤 활성화하고 `python -m pip install -r requirements-dev.txt`를 실행합니다. `.env.example`를 참고해 운영체제/배포 플랫폼 환경변수를 설정한 뒤 `python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000`으로 시작합니다. `.env` 파일은 자동으로 로드되지 않습니다.

- API 상태: http://localhost:8000/api/health
- API 문서: http://localhost:8000/docs
- 고객 화면 origin: http://localhost:5173
- 직원·변호사 화면 origin: http://localhost:5174

프론트 주소는 `DEBTOFF_CORS_ORIGINS`에 쉼표로 구분해 등록합니다. 비밀 API 키는 이 서버에만 설정합니다. 일반 운영에서는 계정 자동 생성을 하지 않습니다. 격리한 로컬 가상 테스트에서만 `DEBTOFF_DEMO_MODE=1`, `DEBTOFF_START_PROFILE=fresh`로 customer/customer, staff/staff, lawyer/lawyer 계정을 생성할 수 있습니다.

## OCR와 문서 출력

`python scripts/setup_ocr.py`로 로컬 한국어 OCR을 설치합니다. 외부 추론에 개인정보를 보내지 않는 경계와 로컬 모델 설정은 docs를 확인하세요. Linux 문서 출력에는 NanumMyeongjo 글꼴이 필요합니다(Docker에 포함). `DEBTOFF_KOREAN_FONT`로 설치된 글꼴 경로를 지정할 수도 있습니다.

`docker build --build-arg ENABLE_KOREAN_OCR=1 -t debtoff-backend .` 후 볼륨을 `/data`에 연결하여 실행합니다. Docker의 API 키·CORS·로컬 모델 접속 주소는 실행 환경변수로 주입합니다. 법원 실제 전송 기능은 없으며 접수증을 확인한 내부 제출 기록만 저장합니다.

## 검증과 자료

`python scripts/run_tests.py`는 별도 임시 DB로 테스트합니다. OCR 모델이 필요한 실제 이미지 검사는 OCR 설치 후 실행합니다. `examples/`는 모두 가상자료이며 운영 추출기는 manifest/expected 정답 파일을 읽지 않습니다. 과거 통합환경 브라우저 스크립트는 이 저장소에 포함하지 않습니다.

문서: [추출](docs/document-extraction.md), [제출 검토](docs/filing-review.md), [전체 문서](docs/README.md).
