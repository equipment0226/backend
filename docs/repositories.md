# 세 저장소의 역할

빚오프는 사건을 처리하는 서버, 고객이 사용하는 화면, 직원·변호사가 사용하는 화면을 각각 별도 저장소로 관리합니다. 고객 화면의 안내를 바꾸거나 사무소 화면을 개선할 때 담당 부분을 나눠 작업할 수 있도록 구성했습니다.

## 어떤 역할을 맡나요?

| 저장소 | 맡는 일 | 로컬 접속 주소 |
| --- | --- | --- |
| [backend](https://github.com/equipment0226/backend) | 로그인과 권한, 사건 기록, 상담·서류 처리, 계산, 문서 작성·검토 | http://localhost:8000 |
| [frontend_customer](https://github.com/equipment0226/frontend_customer) | 개인회생 안내, 상담 신청, 내 사건, 자료 제출, 알림과 문의 | http://localhost:5173 |
| [frontend_lawyerandstaff](https://github.com/equipment0226/frontend_lawyerandstaff) | 세부 상담, 자료 검증, 사건 진행 확인, 계산·쟁점·문서 검토 | http://localhost:5174 |

고객이 파일을 올리면 서버가 해당 사건에 저장하고 사무소 화면에 반영합니다. 직원이 보완을 요청하면 같은 사건 기록을 통해 고객의 요청 카드와 알림에 전달됩니다. 화면이 둘이어도 사건 내용은 한곳에서 관리합니다.

기능 설명서는 backend의 `docs`에 모아두고, 각 저장소의 README에는 해당 부분을 실행하는 방법을 안내합니다. 공개 범위는 각 GitHub 저장소의 설정을 따릅니다.

## 저장소에 들어가는 자료

코드, 공개 법률 자료와 법원 원본 서식, 명시적으로 만든 가상 테스트 자료를 포함합니다. 실제 고객의 업로드, 운영 DB, 작성 문서, 로그인 기록과 API 키는 올리지 않습니다.

환경설정 예시는 필요한 항목과 안전한 기본값으로 새로 만들었습니다. 기존 작업 폴더의 환경 파일에 실제 키가 있더라도 값을 복사하지 않습니다. 외부 AI 연결 설정은 서버에서 관리하고 고객·사무소 화면에는 키를 넣지 않습니다.

## 각각 실행하려면

서버를 먼저 준비하고 두 화면이 그 서버 주소를 보도록 설정합니다. 프론트 주소도 서버에 등록해야 정상적으로 연결됩니다. 로컬에서 기본 포트를 쓰면 위 표의 주소를 사용합니다.

설치를 담당하는 사람에게 필요한 명령은 다음과 같습니다. 자세한 의존성과 설정은 각 저장소 README를 따릅니다.

| 대상 | 실행 방법 |
| --- | --- |
| 백엔드 | Python 설치·의존성 준비 후 `python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000` |
| 고객 화면 | `npm ci` → `npm run build` → `npm start` |
| 직원·변호사 화면 | `npm ci` → `npm run build` → `npm start` |

프론트의 서버 주소 설정은 `DEBTOFF_API_ORIGIN`, 서버에서 허용할 화면 주소는 `DEBTOFF_CORS_ORIGINS`입니다. 서버 실행과 프론트 연결에 필요한 설정은 각 저장소 README에 따라 실행 환경에 전달합니다.

테스트 계정 자동 생성은 격리된 로컬에서만 사용합니다. 해당 설정은 `DEBTOFF_DEMO_MODE=1`, `DEBTOFF_START_PROFILE=fresh`이며 실제 운영에 그대로 공개하지 않습니다.

## 통합 작업 폴더의 변경을 옮기는 방법

현재 통합 작업 폴더에는 세 저장소에 필요한 파일을 골라 복사하는 도구가 있습니다. 기존 파일과 사람이 수정한 파일을 비교해, 로컬 설정이나 Git 기록을 덮지 않도록 확인합니다.

`python scripts/export_repositories.py --refresh`를 실행하면 `.work/repository-split/`의 세 작업 사본이 갱신됩니다. 이 명령 자체가 GitHub에 업로드하지는 않습니다. 변경 파일을 확인하고 필요한 검사와 비밀정보 제외 확인을 마친 뒤 커밋·업로드합니다.

복사 목록은 `export-manifest.json`, 실행 확인은 `validation.json`에 남습니다. 원격 업로드 결과는 통합 작업 폴더의 `reports/git-publication.json`에서 확인할 수 있습니다.
