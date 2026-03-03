# Global Drug Safety Intelligence

해외 체류 중인 한국인을 위한 증상 기반 OTC 의약품 안전 가이드 프로젝트입니다.  
미국 FDA 데이터와 한국 DUR 정보를 결합해 증상/제품 기반 안내를 제공합니다.

## 기술 스택

- Backend: Django (ASGI)
- AI Orchestration: LangGraph
- External APIs: OpenAI, FDA Open API, Supabase, Google Maps (optional)
- Local DB: SQLite (`db.sqlite3`, Django 기본 관리용)

## 프로젝트 구조

```text
.
├── skn22_4th_prj/
│   ├── manage.py
│   ├── run_uvicorn.py
│   ├── chat/
│   ├── drug/
│   ├── users/
│   ├── services/
│   ├── graph_agent/
│   ├── prompts/
│   ├── templates/
│   └── skn22_4th_prj/   # settings.py, urls.py, asgi.py, wsgi.py
├── data_pipeline/
├── mysql/
└── requirements.txt
```

## 빠른 시작

### 1) Python 준비

- 권장: Python 3.11+
- Windows에서 `No module named pip`가 뜨면:

```powershell
py -m ensurepip --upgrade
```

### 2) 가상환경 생성 및 의존성 설치

프로젝트 루트(`SKN22-4th-1Team`)에서 실행:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -r requirements.txt
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install -r requirements.txt
```

### 3) `.env` 설정

루트 경로(`SKN22-4th-1Team/.env`)에 생성:

```env
OPENAI_API_KEY=...
SUPABASE_URL=https://<your-project>.supabase.co
SUPABASE_KEY=<your-anon-or-service-key>

# Optional
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
GOOGLE_MAPS_API_KEY=<maps-js-key>
KR_API_KEY=<public-data-key>
DJANGO_SECRET_KEY=<django-secret>
LANGSMITH_API_KEY=<langsmith-key>
LANGCHAIN_PROJECT=skn22-4th-django
```

### 4) 마이그레이션

```powershell
cd skn22_4th_prj
python manage.py migrate
```

### 5) 서버 실행 (권장)

```powershell
cd skn22_4th_prj
python run_uvicorn.py
```

또는 직접 실행:

```powershell
cd skn22_4th_prj
python -m uvicorn skn22_4th_prj.asgi:application --host 0.0.0.0 --port 8000 --reload
```

접속 주소:

- `http://127.0.0.1:8000/`
- `http://localhost:8000/`

참고: `0.0.0.0`은 바인딩 주소이며 브라우저 접속 주소로는 보통 `localhost` 또는 `127.0.0.1`을 사용합니다.

## 주요 URL

- `/` : 메인 페이지
- `/smart-search/` : 증상/제품 통합 검색
- `/api/pharmacies/` : 주변 약국 조회 API
- `/api/symptom-products/` : 증상 기반 성분별 제품 API
- `/auth/register/`, `/auth/login/`, `/auth/logout/`
- `/user/profile/`
- `/drug/search/`
- `/drug/us-roadmap/`

호환 경로도 유지됩니다:

- `/drugs/*`
- `/api/drugs/*`

## 환경 점검 스크립트

```powershell
cd skn22_4th_prj
python check_env.py
python check_tables.py
```

## 자주 발생하는 실행 오류

### 1) `No module named 'django'`

원인:

- 가상환경 미활성화
- `requirements.txt` 미설치
- 다른 Python 인터프리터로 실행

해결:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn skn22_4th_prj.asgi:application --reload --port 8000
```

### 2) `No module named pip`

해결:

```powershell
py -m ensurepip --upgrade
python -m pip install -U pip
```

### 3) 서버는 켜졌는데 페이지가 안 열림

점검:

- 접속 주소를 `http://localhost:8000/` 또는 `http://127.0.0.1:8000/`로 입력했는지 확인
- 포트 충돌 여부 확인 (`8000` 사용 중이면 다른 포트로 실행)
- 터미널 로그에 `Error loading ASGI app` 또는 Traceback이 있는지 확인

## 주의사항

- 본 서비스는 의료 진단/처방을 대체하지 않습니다.
- 복용 전 의사/약사 상담이 필요합니다.
- `.env`, `db.sqlite3` 등 민감 파일은 버전 관리에서 제외하세요.
