# Railway 이전 구성

현재 로컬 시연을 유지하고 이후 공통 API와 프론트 2개를 따로 배포하도록 파일을 준비했다. 실제 배포·계정 생성·과금 서비스 변경은 수행하지 않았다.

| 서비스 | 이미지 | 필수 설정 |
| --- | --- | --- |
| API | `Dockerfile` | `DEBTOFF_DEMO_MODE=0`, 강한 직원/변호사 비밀번호, CORS 프론트 두 origin, 데이터 볼륨 `/data`, 모델 URL |
| 고객 프론트 | `Dockerfile.frontend` | `FRONTEND_APP=portal`, `DEBTOFF_API_ORIGIN=https://공통API도메인` |
| 사무소 프론트 | `Dockerfile.frontend` | `FRONTEND_APP=office`, 같은 API URL |
| 모델 서버 | 로컬 경계의 별도 실행 구성 | 원문 처리 허용 주소·모델 설치·CPU/메모리 처리량 검증 |

Railway는 Dockerfile을 탐지하며 사용자 지정 경로를 지정할 수 있다. 두 프론트 서비스의 빌드 설정에는 `Dockerfile.frontend`를 명시하고 API용 `railway.json` healthcheck를 그대로 상속하지 않도록 별도 서비스 설정을 사용한다. 프론트 healthcheck는 `/`, API는 `/api/health`다. [Railway Dockerfile 문서](https://docs.railway.com/builds/dockerfiles).

API는 단일 worker·단일 replica로 사용한다. SQLite DB, 파일, 작업 상태를 `/data`에 보존한다. 다중 인스턴스 확장 전 PostgreSQL·객체 저장소·작업 큐 전환이 필요하다. 볼륨은 실행 시 마운트되는 영속 저장소이며 실제 마운트 경로와 백업 정책을 설정해야 한다. [Railway Volumes](https://docs.railway.com/volumes).

신규 공식 원문 저장소는 `DEBTOFF_CORPUS_DIR=/data/corpus`로 같은 영속 볼륨에 둔다. 로컬에서 수집한 원문은 이미지에 포함되지 않으므로 초기 설치 후 직원 지식함의 수집 기능 또는 인증된 `POST /api/knowledge/ingest`로 생성해야 한다. 서버가 시작됐다는 이유만으로 수집이 완료된 것으로 표시하지 않는다. n8n 이식은 [AX API 계약](ax-pipeline.md)을 따른다.

원문 처리는 loopback 또는 `host.docker.internal`의 로컬 모델만 허용합니다. 임의 HTTPS 모델 주소로 원문을 전달하는 구성은 지원하지 않습니다. 외부 고급 검증은 익명 구조화 데이터 전용 경계를 사용합니다. [자동 처리와 개인정보 경계](ax-automation.md)를 확인하세요.

## 배포 전 확인할 현재 제약

Docker 파일은 작성되었지만 이 PC에서 이미지 빌드·Railway 실배포를 검증하지 않았다. 비시연 모드는 직원·변호사 인증만 제공하며 실제 고객 계정 초대/회수·MFA·비밀번호 변경·암호화 저장·감사 접근정책이 준비되지 않았다. 현재 `DEBTOFF_DEMO_MODE=0`만으로 실고객 서비스를 개시할 수 있다는 의미가 아니다.

법원·문서 등록부는 원본 조사자료의 비활성 상태를 보존한다. 별도 공식 PDF 원본과 버전 계산 정책을 추가했으며 계산안·서식은 사건별 변호사 검토를 거친다. 관할별 최신 접수요건 검토와 실제 법원 제출 활성화는 별도 범위다. `LAW_API_OC`는 새 앱의 환경변수에 명시적으로 제공하며 lawmaster의 비밀키를 복사하지 않는다.

한국어 OCR은 선택 빌드 인자 `ENABLE_KOREAN_OCR=1`로 패키지와 해시 고정 모델을 설치한다. 기본 이미지는 OCR 없이 시작하며 `/api/system`에 설치 필요 상태를 표시한다. 모델은 `/app/.local/ocr/models`에 이미지와 함께 저장되고 실행 중 내려받지 않는다. OCR CPU·메모리 여유와 180초 업로드 제한은 배포 환경에서 별도 측정해야 한다. 이 Docker 빌드는 아직 실행 검증하지 않았다. 공식 PDF/HWP 원본과 프롬프트·법률 정책은 `COPY data`, `COPY apps/api`에 포함된다.

보존용 실무 검토 원본 `data/reference_evidence/`, lawmaster, `.env`, `.local`, GPU 시험 로그는 Docker 이미지에 포함하지 않는다. `.dockerignore`는 필요한 앱 코드·비식별 등록부·설정만 허용한다.

## 로컬 백업과 복원

```powershell
python scripts/backup.py create .local/backups/case-backup.zip
python scripts/backup.py restore .local/backups/case-backup.zip --destination .local/restored-case-data
```

백업 파일명과 복원 폴더는 존재하지 않는 새 경로를 사용한다. 도구는 사건 DB 스냅샷과 사건이 참조하는 업로드·생성 PDF를 SHA-256으로 확인해 보관하고, 복원 후 DB 무결성을 확인하며 기존 로그인 세션을 폐기한다. 실행 중인 저장소 위에 복원하지 않는다. 복원 검사 후 별도 실행에서 `DEBTOFF_DATA_DIR`을 새 폴더로 지정한다.

이 백업 도구는 `.local` 전체 복제가 아니다. 공개 법률 corpus·법령 감시 상태·추론 캐시·환경 키는 별도 보존해야 한다. API를 정지한 상태에서 해당 경로를 별도 백업하는 운영 절차와 암호화·복구 훈련은 환경에 맞게 구성한다.
