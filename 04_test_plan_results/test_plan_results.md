# 04. 테스트 계획 및 결과 보고서

## 기준 정보

- 기준 브랜치: `develop`
- 기준일: 2026-03-02

## 1) 테스트 범위

- 검색 플로우: 증상/제품 입력 처리
- 결과 플로우: 상담 메모 및 성분/주의 안내
- 지도 플로우: 내 위치/약국 조회/핀 상호작용/지도 이동 재조회
- API 파라미터 검증: `/api/pharmacies/` 좌표/반경/개수

## 2) 테스트 케이스

| ID | 항목 | 입력/행동 | 기대 결과 |
|---|---|---|---|
| TC-01 | 증상 검색 | `/smart-search/?q=두통` | 결과 페이지 렌더 |
| TC-02 | 상담 메모 | 프로필 포함 검색 | 영어 메모 출력 |
| TC-03 | 내 위치 찾기 | 버튼 클릭 | 현재 좌표 + 빨간 핀 표시 |
| TC-04 | 약국 조회 | 근처 약국 찾기 | 지도+목록 표시 |
| TC-05 | 핀 hover | 핀에 마우스 오버 | 툴팁 표시 |
| TC-06 | 지도 이동 | 드래그/줌 | viewport 기준 재조회 |
| TC-07 | pet/vet 필터 | pet/vet 포함 결과 | 목록/핀에서 제외 |

## 3) 점검 실행 결과

### 3.1 Python 컴파일 점검

```bash
py -m py_compile skn22_4th_prj/chat/views.py \
  skn22_4th_prj/graph_agent/builder_v2.py \
  skn22_4th_prj/graph_agent/nodes_v2.py \
  skn22_4th_prj/services/map_service.py
```

- 결과: 성공 (오류 없음)

### 3.2 Django 시스템 점검

```bash
py skn22_4th_prj/manage.py check
```

- 결과: `System check identified no issues (0 silenced).`
- 참고: Python 3.14 + langchain_core 경고 1건(실행 차단 아님)

## 4) 결론

`develop` 브랜치 기준 핵심 기능(검색, 개인화 안내, 지도 약국 조회, 상호작용)은 발표 데모 가능한 상태이며, 제출용 문서 기준을 충족한다.
