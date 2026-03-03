# 🌐 Global Drug Safety Intelligence

해외 체류 중인 한국인을 위한 **증상 기반 OTC 의약품 안전 가이드** 시스템입니다.  
미국 FDA 데이터와 한국 식약처 DUR(의약품 사용 검토) 데이터를 결합하여, 사용자의 증상 및 건강 프로파일을 고려한 맞춤형 의약품 정보를 AI로 제공합니다.

---

## 🏗️ 프로젝트 구조

```
drug_information/
│
├── api_fastapi/              # 메인 FastAPI 서버 (AI 파이프라인)
│   ├── main2.py              # ★ 서버 진입점 (uvicorn 실행 대상)
│   │
│   ├── graph_agent/          # LangGraph AI 워크플로우
│   │   ├── builder_v2.py     #   그래프 노드 연결 및 컴파일
│   │   ├── nodes_v2.py       #   각 노드 함수 (분류→FDA→DUR→답변 생성)
│   │   └── state.py          #   그래프 공유 상태 스키마
│   │
│   ├── services/             # 외부 API 및 비즈니스 로직
│   │   ├── ai_service_v2.py  #   OpenAI GPT 연동 (분류·답변·번역)
│   │   ├── drug_service.py   #   FDA API 조회 + KR DUR DB 검색
│   │   ├── supabase_service.py #  Supabase DUR 데이터 조회 및 캐싱
│   │   ├── map_service.py    #   미국 OTC 제품 검색 (FDA 활용)
│   │   ├── auth_service.py   #   JWT 인증
│   │   └── user_service.py   #   사용자 건강 프로파일 조회
│   │
│   ├── routers/              # FastAPI 라우터
│   │   ├── auth_router.py    #   /auth/* (로그인·회원가입)
│   │   ├── drug_router.py    #   /api/drugs/* (의약품 검색)
│   │   └── user_router.py    #   /api/users/* (프로파일 관리)
│   │
│   ├── prompts/              # LLM 프롬프트 템플릿
│   │   ├── system_prompts.py #   의도 분류(Intent) 프롬프트
│   │   └── answer_prompts_v2.py # 성분별 안전 분류·답변 생성 프롬프트
│   │
│   └── templates/            # Jinja2 HTML 템플릿
│       └── symptom_result.html # 증상 검색 결과 페이지
│
├── backend_django/           # Django ORM (사용자 인증 + DUR DB 관리)
│   ├── core/                 #   Django 설정 (settings.py)
│   └── drugs/                #   DurMaster, DrugPermitInfo 모델 정의
│
├── data_pipeline/            # 데이터 수집 및 DB 동기화 스크립트
│   ├── dur_unified_collector.py    # 공공API → MySQL DUR 데이터 수집
│   ├── drug_enrichment_collector.py # 의약품 성분명 보강 수집
│   ├── sync_to_supabase.py         # MySQL → Supabase 동기화
│   └── supabase_schema.sql         # Supabase 테이블 DDL
│
├── .env                      # 환경 변수 (★ repo에 포함 안 됨 → 직접 생성 필요)
├── requirements.txt          # Python 의존성
└── README.md
```

---

## ⚙️ AI 파이프라인 흐름

```
사용자 입력 (증상 텍스트)
    │
    ▼
[classify_node]  → GPT가 의도 분류 (증상 추천 / 제품 검색 / 일반 질문)
    │
    ▼
[retrieve_fda_node]  → FDA API에서 해당 증상의 OTC 성분 목록 수집
    │                   실패 시 AI 동의어 확장 → 재검색 → AI 직접 추천 순으로 폴백
    ▼
[retrieve_dur_node]  → Supabase에서 성분별 KR DUR 정보 조회
    │
    ▼
[generate_symptom_answer_node]
    ├─ GPT: 성분별 can_take 판단 + 이유 + DUR 경고 유형 분류
    └─ FDA: can_take=true 성분에 대해 OTC 브랜드 제품 병렬 조회
    │
    ▼
[symptom_result.html] → 성분 카드 UI (안전 배지 / 제품명 아코디언)
```

---

### 1. Python 가상환경 생성 및 의존성 설치

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate

# 3. 필수 패키지 설치
pip install -r requirements.txt
```

### 2. MySQL 데이터베이스 생성

```sql
CREATE DATABASE drug_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'drug'@'localhost' IDENTIFIED BY 'drug';
GRANT ALL PRIVILEGES ON drug_db.* TO 'drug'@'localhost';
FLUSH PRIVILEGES;
```

> `DB_NAME`, `DB_USER`, `DB_PASSWORD`는 아래 `.env` 설정과 일치해야 합니다.

### 3. `.env` 파일 생성

루트(`drug_information/`) 에 `.env` 파일을 생성하세요.

```env
# ★ 필수 - OpenAI
OPENAI_API_KEY=sk-proj-...

# ★ 필수 - Supabase (DUR 데이터 저장소)
SUPABASE_URL=https://<your-project>.supabase.co/
SUPABASE_KEY=<your-supabase-anon-key>

# ★ 필수 - MySQL (Django ORM)
DB_NAME=drug_db
DB_USER=drug
DB_PASSWORD=drug
DB_HOST=127.0.0.1
DB_PORT=3306

# 선택 - 한국 공공데이터포털 DUR API 키 (데이터 수집 시에만 필요)
KR_API_KEY=<your-kr-api-key>

# 선택 - Django 시크릿 키 (배포 시 반드시 변경)
DJANGO_SECRET_KEY=django-insecure-replace-this-in-production

# 선택 - LangSmith 모니터링
LANGSMITH_API_KEY=lsv2_pt_...
```

### 4. Supabase 테이블 생성

[Supabase 콘솔](https://supabase.com) → SQL Editor에서 아래 파일을 실행하세요.

```bash
# 파일 위치
data_pipeline/supabase_schema.sql
```

### 5. Django 마이그레이션 (사용자 인증 DB 구성)

```bash
cd backend_django
python manage.py makemigrations
python manage.py migrate
cd ..
```

**Step 3: 초기 데이터 수집 (Data Collection)**
공공데이터포털 API를 통해 DUR 데이터를 수집하여 DB에 적재합니다.
*   주의: `KR_API_KEY`가 `.env`에 올바르게 설정되어 있어야 합니다.
*   시간이 다소 소요될 수 있습니다.

```bash
cd data_pipeline

# 한국 DUR 데이터 수집 → MySQL 저장
python dur_unified_collector.py

# 의약품 성분명 보강
python drug_enrichment_collector.py

# MySQL → Supabase 동기화
python sync_to_supabase.py
```

> **처음 Supabase를 세팅하는 경우**: KR API 키 없이도 Supabase에 DUR 데이터를 직접 CSV로 import할 수 있습니다. [공공데이터포털](http://www.data.go.kr)에서 DUR 품목정보 데이터를 다운받아 활용하세요.

### 6. 서버 실행

```bash
cd api_fastapi
uvicorn main2:app --reload --port 8000
```

브라우저에서 `http://localhost:8000` 접속

---

## 🔑 환경 변수 요약

| 변수명              | 필수 여부           | 설명                                 |
| ------------------- | ------------------- | ------------------------------------ |
| `OPENAI_API_KEY`    | ★ 필수              | gpt-4.1-nano 호출용                   |
| `SUPABASE_URL`      | ★ 필수              | Supabase 프로젝트 URL                |
| `SUPABASE_KEY`      | ★ 필수              | Supabase anon public key             |
| `DB_NAME`           | ★ 필수              | MySQL 데이터베이스명                 |
| `DB_USER`           | ★ 필수              | MySQL 사용자                         |
| `DB_PASSWORD`       | ★ 필수              | MySQL 비밀번호                       |
| `DB_HOST`           | 필수                | DB 호스트 (기본: `127.0.0.1`)        |
| `DB_PORT`           | 필수                | DB 포트 (기본: `3306`)               |
| `KR_API_KEY`        | 데이터 수집 시 필요 | 공공데이터포털 DUR API 키            |
| `DJANGO_SECRET_KEY` | 선택                | Django 시크릿 키 (배포 시 필수 변경) |
| `LANGSMITH_API_KEY` | 선택                | LangGraph 파이프라인 모니터링        |

---

## ️ 기술 스택

| 분류          | 기술                                           |
| ------------- | ---------------------------------------------- |
| API 서버      | FastAPI + Uvicorn                              |
| AI 워크플로우 | LangGraph (비순환 그래프 파이프라인)           |
| LLM           | OpenAI gpt-4.1-nano                             |
| 데이터베이스  | MySQL (Django ORM) + Supabase (PostgreSQL)     |
| 인증          | JWT (python-jose)                              |
| 외부 API      | 미국 FDA Open API, 한국 공공데이터포털 DUR API |

---

## 📝 주의사항

- 본 시스템은 **참고용 정보 제공 목적**이며, 의료 진단이나 처방을 대체하지 않습니다.
- 복용 전 반드시 현지 약사 또는 의사에게 상담받으시기 바랍니다.
- `.env` 파일과 `db.sqlite3`, `*.pyc` 파일은 `.gitignore`에 포함하세요.
