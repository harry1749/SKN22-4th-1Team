# 03. 시스템 구성도 (System Architecture)

## 기준 정보

- 기준 브랜치: `develop`
- 실행 서버: Django ASGI + Uvicorn

## 1) 아키텍처 개요

- Backend: Django (ASGI)
- Workflow: LangGraph (`graph_agent`)
- AI: OpenAI (`services/ai_service_v2.py`)
- Data: Supabase, openFDA, KR DUR
- Map: Google Places + OSM fallback

## 2) 컴포넌트 구성도

사용자가 증상(예: "머리가 아프고 열이 나요")을 질의했을 때, 시스템이 정보를 수집하고 답변을 출력할 때까지의 과정입니다.

```mermaid
sequenceDiagram
    participant MAP as Google Maps API
    participant U as User (Client)
    participant V as Django View
    participant R as LangGraph Router
    participant API as Supabase & FDA/DUR API
    participant LLM as GPT-4o-mini

    U->>V: 1. 자연어 증상 검색
    V->>R: 2. Graph Agent에 질의 및 사용자 세션 정보 전달
    
    R->>LLM: 3. 사용자 의도 및 상태 분류
    LLM-->>R: 분석 결과 반환
    
    Note left of API: Data Retrieval (병렬 처리)
    R->>API: 4. 유저 프로필 조회 및 성분탐색
    API-->>R: 추천 성분 및 DUR 반환
    
    R->>LLM: 5. 수집 제약 데이터 기반 답변 요청
    LLM-->>R: 환자 맞춤형 최종 답변
    
    R-->>V: 6. 생성 결과 반환 (답변 + 성분)
    V-->>U: 7. 결과 화면(HTML) 출력
    
    Note right of MAP: 주변 약국 탐색
    U->>V: [A] 사용자 장치 위치 전달 및 약국 요청
    V->>MAP: [B] 반경 내 약국 탐색
    MAP-->>V: 약국 위치 리스트 반환
    V-->>U: [C] 지도 마커 및 약국 목록 렌더링
```

## 3) 핵심 URL

- `/` 메인
- `/smart-search/` 통합 검색
- `/smart-search-products/` 성분별 제품 추천 페이지
- `/api/pharmacies/` 위치 기반 약국 API (`lat,lng,radius,limit`)
- `/api/symptom-products/` 성분별 제품 API

## 4) 시퀀스 요약

### 증상 검색
1. 사용자 입력 → `smart_search`
2. LangGraph 분류/검색/DUR 처리
3. 결과 페이지 렌더
4. 후속 제품 API 비동기 조회

### 약국 지도
1. 브라우저 Geolocation으로 좌표 확보
2. `/api/pharmacies/` 호출
3. Google 실패 시 OSM fallback
4. 지도 핀/목록 렌더
5. 지도 이동 시 viewport 기준 재조회

## 5) 구현 포인트

- 상담 메모: 영어 생성 + 프로필 필드 영어 번역
- 지도: 현재 위치 빨간 마커, 약국 핀 hover 툴팁
- 필터: pet/vet 장소 제외
