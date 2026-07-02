# CL-IC

공공데이터를 기반으로 육아 인프라(놀이/의료/교육/치안/생활환경)를 행정동·주소 단위로 지수화하고, 예산과 조건에 맞는 육아친화 주거지를 지도에서 추천해주는 Streamlit 서비스입니다. (부트캠프 2차 팀 프로젝트, 팀명 CL-ICKER, 4인 팀, 14일 개발)

## 내 역할 - 팀장

- **프로젝트 총괄**: 문제 정의, 팀 업무 분담, 일정 관리, 발표/보고서 구조 총괄. Google Sheets 기반 업무 분담·일정 관리 체계를 직접 설계해 팀 운영에 활용
- **C-LCI 지수 설계 주도**: 논문 근거를 바탕으로 교육·의료·안전·놀이·생활환경 5개 인프라 영역의 가중치 산출 로직을 직접 설계
- **놀이/친구 인프라 데이터 수집·정제**: 키즈카페·놀이터·도서관 등 놀이/친구 카테고리 공공데이터 수집 및 전처리 직접 담당
- **Streamlit 앱 직접 구현**: Choropleth 지도 시각화를 포함한 Streamlit UI를 직접 개발

## 기술 스택

Python · Streamlit · Pandas · Folium/Plotly(지도 시각화) · Kakao API(주소 지오코딩) · Rasterio/Shapely(공간 데이터 처리)

## 데모

**Choropleth 지도 전환**

![choropleth 지도 전환](assets/demo/demo_1_choropleth_map.gif)

**지도 클릭 기반 위치 분석**

![지도 클릭 분석](assets/demo/demo_3_map_click_analysis.gif)

**카카오 API 기반 주소 → 좌표 변환**

![주소-좌표 변환](assets/demo/demo_4_address_to_coord.gif)

추천 결과 화면 예시는 `assets/demo/screenshot_result_*.png`, 행정동 상·하위 비교는 `assets/demo/screenshot_ranking_compare.png`를 참고해 주세요.

## 작업 흐름

1. **데이터 수집 및 정제**: 놀이(키즈카페·놀이터·도서관), 교육, 의료(소아과·백신접종률), 치안(CCTV·파출소), 생활환경(공원·미세먼지 등) 공공데이터 정제 (`notebooks/` 참고, 카테고리별 전처리 노트북 22종)
2. **동별 지수 산출**: 카테고리별 인프라 지수를 정규화·가중치 적용하여 행정동 단위 C-LCI 지수로 산출 (`data/infra_index/` — 세부 카테고리별 지수 → 카테고리별 동별 지수 → 최종 합산)
3. **지도 시각화
