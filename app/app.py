import json
import math
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import numpy as np
import pandas as pd
import requests
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
import rasterio
from rasterio.mask import mask
from rasterio.warp import transform_geom
from shapely.geometry import shape, mapping, Point, box
from shapely.ops import unary_union
from shapely.prepared import prep
import folium
from streamlit_folium import st_folium
from branca.element import Element

def format_kor_price(x):
    if x >= 100000000:
        return f"{x/100000000:.1f}억"
    elif x >= 10000:
        return f"{x/10000:.0f}만"
    else:
        return str(int(x))

st.set_page_config(page_title="C-LCI 지도", layout="wide")

BASE_DIR = Path(__file__).resolve().parent

KAKAO_API_KEY = os.environ.get("KAKAO_API_KEY", "")

COLOR_SCALE = ["#FAF4D9","#F1E7B4", "#F0CF3F"]

FACILITY_CATEGORY_COLORS = {
    "교육": "#f322d0",
    "놀이": "#ff7f0e",
    "안전": "#d62728",
    "의료복지": "#2ca02c",
    "환경생활": "#6628a0",
}

SAFETY_EXCLUDED_IN_SUMMARY = {"교통사고", "CCTV", "안전벨", "-"}


def haversine(lat1, lng1, lat2, lng2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def haversine_vectorized(lat, lng, lat_arr, lng_arr):
    if len(lat_arr) == 0:
        return np.array([], dtype=float)

    lat_arr = np.asarray(lat_arr, dtype=float)
    lng_arr = np.asarray(lng_arr, dtype=float)

    lat1 = np.radians(float(lat))
    lng1 = np.radians(float(lng))
    lat2 = np.radians(lat_arr)
    lng2 = np.radians(lng_arr)

    dlat = lat2 - lat1
    dlng = lng2 - lng1

    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlng / 2.0) ** 2
    return 6371000.0 * 2.0 * np.arctan2(np.sqrt(a), np.sqrt(1.0 - a))


def add_coord_cache(df, lat_col="위도", lng_col="경도"):
    if df is None or df.empty:
        return df

    out = df.copy()
    if lat_col in out.columns:
        out[lat_col] = pd.to_numeric(out[lat_col], errors="coerce")
    if lng_col in out.columns:
        out[lng_col] = pd.to_numeric(out[lng_col], errors="coerce")
    if lat_col in out.columns and lng_col in out.columns:
        out = out.dropna(subset=[lat_col, lng_col]).copy()
        out["_lat_np"] = out[lat_col].to_numpy(dtype=float)
        out["_lng_np"] = out[lng_col].to_numpy(dtype=float)
    return out


def rough_bbox_mask(lat, lng, lat_arr, lng_arr, radius):
    if len(lat_arr) == 0:
        return np.array([], dtype=bool)
    lat_arr = np.asarray(lat_arr, dtype=float)
    lng_arr = np.asarray(lng_arr, dtype=float)

    lat_delta = radius / 111320.0
    cos_lat = max(0.1, math.cos(math.radians(float(lat))))
    lng_delta = radius / (111320.0 * cos_lat)

    return (
        (lat_arr >= float(lat) - lat_delta) &
        (lat_arr <= float(lat) + lat_delta) &
        (lng_arr >= float(lng) - lng_delta) &
        (lng_arr <= float(lng) + lng_delta)
    )


def get_distance_series(df, lat, lng, lat_col="위도", lng_col="경도", radius=None):
    if df is None or df.empty or lat_col not in df.columns or lng_col not in df.columns:
        return df.iloc[0:0].copy(), np.array([], dtype=float)

    if "_lat_np" in df.columns and "_lng_np" in df.columns:
        lat_arr = df["_lat_np"].to_numpy(dtype=float)
        lng_arr = df["_lng_np"].to_numpy(dtype=float)
    else:
        lat_arr = pd.to_numeric(df[lat_col], errors="coerce").to_numpy(dtype=float)
        lng_arr = pd.to_numeric(df[lng_col], errors="coerce").to_numpy(dtype=float)

    valid_mask = ~np.isnan(lat_arr) & ~np.isnan(lng_arr)
    work_df = df.loc[valid_mask].copy()
    lat_arr = lat_arr[valid_mask]
    lng_arr = lng_arr[valid_mask]

    if radius is not None:
        bbox_mask = rough_bbox_mask(lat, lng, lat_arr, lng_arr, radius)
        work_df = work_df.loc[bbox_mask].copy()
        lat_arr = lat_arr[bbox_mask]
        lng_arr = lng_arr[bbox_mask]

    dists = haversine_vectorized(lat, lng, lat_arr, lng_arr)
    return work_df, dists


def make_circle_points(lat, lng, radius_m=1000, steps=72):
    points_lat, points_lng = [], []
    earth_radius = 6371000
    lat_rad = math.radians(lat)
    lng_rad = math.radians(lng)
    ang_dist = radius_m / earth_radius
    for i in range(steps + 1):
        bearing = 2 * math.pi * i / steps
        new_lat = math.asin(
            math.sin(lat_rad) * math.cos(ang_dist)
            + math.cos(lat_rad) * math.sin(ang_dist) * math.cos(bearing)
        )
        new_lng = lng_rad + math.atan2(
            math.sin(bearing) * math.sin(ang_dist) * math.cos(lat_rad),
            math.cos(ang_dist) - math.sin(lat_rad) * math.sin(new_lat)
        )
        points_lat.append(math.degrees(new_lat))
        points_lng.append(math.degrees(new_lng))
    return points_lat, points_lng


def score_to_color(score):
    if score >= 80: return "#fcf811ff"
    if score >= 60: return "#ffe600cc"
    if score >= 40: return "#f7c948"
    if score >= 20: return "#f4a460"
    return "#d73027"


def normalize(value, v_min, v_max, reverse=False):
    if v_max == v_min: return 0.0
    value = max(v_min, min(v_max, value))
    norm = (value - v_min) / (v_max - v_min)
    return round(1 - norm if reverse else norm, 4)


def find_col(df, candidates):
    for c in candidates:
        if c in df.columns: return c
    return None


def format_price_kor(value):
    if pd.isna(value): return "가격미상"
    try: value = int(float(value))
    except: return "가격미상"
    eok = value // 100000000
    remainder = value % 100000000
    cheon = remainder // 10000000
    man = remainder // 10000
    if eok > 0:
        if cheon > 0: return f"{eok}억 {cheon}천만 원"
        if man > 0: return f"{eok}억 {man:,}만 원"
        return f"{eok}억 원"
    if man >= 1000: return f"{man // 1000}천 {man % 1000:,}만 원"
    if man > 0: return f"{man:,}만 원"
    if value >= 1000: return f"{value:,}원"
    return f"{value}원"



def clean_str(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def pick_existing_col(df, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None


def normalize_sido_name(value):
    value = clean_str(value)
    if not value:
        return ""
    if "서울" in value:
        return "서울특별시"
    if "인천" in value:
        return "인천광역시"
    if "경기" in value:
        return "경기도"
    return value


def infer_sido_from_text(value):
    value = clean_str(value)
    if not value:
        return ""
    if "서울" in value:
        return "서울특별시"
    if "인천" in value:
        return "인천광역시"
    if "경기" in value:
        return "경기도"
    return ""


def normalize_gu_name(value):
    value = clean_str(value)
    if not value:
        return ""
    parts = value.split()
    return parts[-1]


def make_region_key(sido, gu):
    sido = normalize_sido_name(sido)
    gu = normalize_gu_name(gu)
    if not gu:
        return ""
    return f"{sido}__{gu}" if sido else gu


def build_geo_row_keys(props):
    sido = normalize_sido_name(props.get("sidonm", ""))
    gu = normalize_gu_name(props.get("sggnm", ""))
    dong = clean_str(props.get("동이름", ""))
    region_key = make_region_key(sido, gu)
    score_key = f"{region_key}__{dong}" if region_key and dong else ""
    legacy_key = f"{gu} {dong}".strip()
    return sido, gu, dong, region_key, score_key, legacy_key



def get_dong_score(dong_name, dong_scores_df, selected_gu=None, selected_sido=None):
    if dong_scores_df is None or dong_scores_df.empty or not dong_name:
        return None

    work = dong_scores_df.copy()
    dong_name = clean_str(dong_name)
    work["행정동"] = work["행정동"].astype(str).str.strip()
    matched = work[work["행정동"] == dong_name].copy()

    if matched.empty:
        return None

    if selected_gu and "시군구정규화" in matched.columns:
        matched = matched[matched["시군구정규화"] == normalize_gu_name(selected_gu)].copy()

    if selected_sido and "시도정규화" in matched.columns:
        temp = matched[matched["시도정규화"] == normalize_sido_name(selected_sido)].copy()
        if not temp.empty:
            matched = temp

    if matched.empty:
        return None

    score = pd.to_numeric(matched.iloc[0].get("100점 환산"), errors="coerce")
    return None if pd.isna(score) else round(float(score), 1)


def get_coord(address):
    url = "https://dapi.kakao.com/v2/local/search/address.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_API_KEY}"}
    try:
        res = requests.get(url, headers=headers, params={"query": address}, timeout=5)
        docs = res.json().get("documents", [])
        if docs: return float(docs[0]["y"]), float(docs[0]["x"])
    except Exception as e:
        st.error(f"주소 좌표 변환 실패: {e}")
    return None, None


def get_dong(address):
    url = "https://dapi.kakao.com/v2/local/search/address.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_API_KEY}"}
    try:
        res = requests.get(url, headers=headers, params={"query": address}, timeout=5)
        docs = res.json().get("documents", [])
        if docs:
            addr = docs[0].get("road_address") or docs[0].get("address")
            if addr: return addr.get("region_3depth_h_name") or addr.get("region_3depth_name", "")
    except Exception as e:
        st.error(f"행정동 조회 실패: {e}")
    return ""


def get_full_address_from_coord(lat, lng):
    url = "https://dapi.kakao.com/v2/local/geo/coord2address.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_API_KEY}"}
    try:
        res = requests.get(url, headers=headers, params={"x": lng, "y": lat}, timeout=5)
        docs = res.json().get("documents", [])
        if docs:
            road = docs[0].get("road_address")
            addr = docs[0].get("address")
            if road:
                main = " ".join([str(road.get(k, "")).strip() for k in ["address_name"] if str(road.get(k, "")).strip()])
                building = str(road.get("building_name", "")).strip()
                if building and building not in main:
                    return f"{main} ({building})"
                return main
            if addr:
                return str(addr.get("address_name", "")).strip()
    except:
        pass
    return ""


DISPLAY_CATEGORY_MAP = {
    "교육": "교육/학군",
    "놀이": "놀이/친구",
    "안전": "안전/치안",
    "의료복지": "의료/복지",
    "환경생활": "환경/생활",
}

REVERSE_CATEGORY_MAP = {v: k for k, v in DISPLAY_CATEGORY_MAP.items()}


def to_display_category(value):
    return DISPLAY_CATEGORY_MAP.get(value, value)


def filter_summary_facilities(df):
    if df is None or df.empty:
        return df
    out = df.copy()
    safety_mask = out["category"].eq("안전")
    exclude_mask = out["name"].astype(str).isin(SAFETY_EXCLUDED_IN_SUMMARY) | out["type"].astype(str).isin(SAFETY_EXCLUDED_IN_SUMMARY)
    return out[~(safety_mask & exclude_mask)].copy()


def infer_name_col(df):
    candidates = [
        "시설명", "기관명", "상호명", "사업장명", "명칭", "이름", "장소명", "공원명", "역명", "정류장명",
        "도서관명", "병원명", "요양기관명", "약국명", "아파트명", "단지명", "어린이집명", "학교명", "학원명",
        "센터명", "지점명", "업소명", "매장명", "문화시설명", "시설", "name", "NAME"
    ]
    for c in candidates:
        if c in df.columns:
            return c
    for c in df.columns:
        if any(k in str(c) for k in ["명", "이름", "상호", "기관", "시설", "장소", "역", "정류장", "센터", "업소"]):
            return c
    return None


def get_access_score(lat, lng, df, lat_col, lng_col, radius):
    try:
        if df.empty or lat_col not in df.columns or lng_col not in df.columns:
            return 0.0
        temp, dists = get_distance_series(df, lat, lng, lat_col, lng_col, radius=radius)
        if len(dists) == 0:
            return 0.0
        f = dists[dists <= radius]
        if len(f) == 0:
            return 0.0
        f = np.where(f == 0, 1.0, f)
        return float((1.0 / f).sum())
    except Exception:
        return 0.0

def count_within(lat, lng, df, lat_col, lng_col, radius):
    try:
        if df.empty or lat_col not in df.columns or lng_col not in df.columns:
            return 0
        _, dists = get_distance_series(df, lat, lng, lat_col, lng_col, radius=radius)
        if len(dists) == 0:
            return 0
        return int((dists <= radius).sum())
    except Exception:
        return 0

def nearest_dist(lat, lng, df, lat_col, lng_col):
    try:
        if df.empty or lat_col not in df.columns or lng_col not in df.columns:
            return 9999999
        _, dists = get_distance_series(df, lat, lng, lat_col, lng_col, radius=None)
        if len(dists) == 0:
            return 9999999
        return float(dists.min())
    except Exception:
        return 9999999

def park_area_score_fn(lat, lng, df_park, radius=1000):
    try:
        if df_park.empty:
            return 0.0
        df = df_park.copy()
        lat_col  = find_col(df, ["위도", "lat", "latitude", "y", "Y"])
        lng_col  = find_col(df, ["경도", "lng", "longitude", "x", "X"])
        area_col = find_col(df, ["공원면적", "area", "면적"])
        if not all([lat_col, lng_col, area_col]):
            return 0.0

        df = add_coord_cache(df, lat_col, lng_col)
        if area_col in df.columns:
            df[area_col] = pd.to_numeric(df[area_col], errors="coerce").fillna(0)

        temp, dists = get_distance_series(df, lat, lng, lat_col, lng_col, radius=radius)
        if len(dists) == 0:
            return 0.0

        temp = temp.copy()
        temp["거리"] = dists
        temp = temp[temp["거리"] <= radius].copy()
        if temp.empty:
            return 0.0

        temp["거리"] = temp["거리"].replace(0, 1)
        return float((temp[area_col] / temp["거리"]).sum())
    except Exception:
        return 0.0


DEM_DIR = BASE_DIR / "DEM"


def classify_walk_difficulty(avg_slope_deg):
    if avg_slope_deg is None or pd.isna(avg_slope_deg):
        return None
    if avg_slope_deg < 2:
        return "매우 쉬움"
    if avg_slope_deg < 5:
        return "쉬움"
    if avg_slope_deg < 8:
        return "보통"
    if avg_slope_deg < 12:
        return "다소 어려움"
    return "어려움"


def get_dong_geometry(selected_sido, selected_gu, selected_dong, dong_geojson):
    if not selected_dong:
        return None
    for feat in dong_geojson["features"]:
        props = feat["properties"]
        if (
            normalize_sido_name(props.get("sidonm", "")) == normalize_sido_name(selected_sido) and
            normalize_gu_name(props.get("sggnm", "")) == normalize_gu_name(selected_gu) and
            clean_str(props.get("동이름", "")) == clean_str(selected_dong)
        ):
            return shape(feat["geometry"])
    return None


@st.cache_data(show_spinner=False)
def get_dong_avg_slope_and_walkability(selected_sido, selected_gu, selected_dong, dong_geojson):
    dong_geom = get_dong_geometry(selected_sido, selected_gu, selected_dong, dong_geojson)
    if dong_geom is None or not DEM_DIR.exists():
        return None, None

    slope_values = []
    for img_path in sorted(DEM_DIR.glob('*/*.img')):
        try:
            with rasterio.open(img_path) as src:
                geom_src = transform_geom('EPSG:4326', src.crs, mapping(dong_geom), precision=6)
                geom_shape = shape(geom_src)
                if geom_shape.is_empty or not geom_shape.intersects(box(*src.bounds)):
                    continue

                data, out_transform = mask(src, [geom_src], crop=True, filled=False)
                arr = data[0].astype('float64')

                if np.ma.isMaskedArray(arr):
                    arr = arr.filled(np.nan)

                nodata = src.nodata
                if nodata is not None:
                    arr[arr == nodata] = np.nan

                valid = np.isfinite(arr)
                if not valid.any():
                    continue

                xres = abs(out_transform.a)
                yres = abs(out_transform.e)
                if xres == 0 or yres == 0:
                    continue

                gy, gx = np.gradient(arr, yres, xres)
                slope_deg = np.degrees(np.arctan(np.sqrt(gx ** 2 + gy ** 2)))
                slope_valid = slope_deg[np.isfinite(slope_deg)]
                if slope_valid.size > 0:
                    slope_values.append(slope_valid)
        except Exception:
            continue

    if not slope_values:
        return None, None

    merged = np.concatenate(slope_values)
    if merged.size == 0:
        return None, None

    avg_slope = round(float(np.nanmean(merged)), 2)
    return avg_slope, classify_walk_difficulty(avg_slope)


def get_dong_slope_info(selected_sido, selected_gu, selected_dong, dong_geojson):
    avg_slope, walk_difficulty = get_dong_avg_slope_and_walkability(
        selected_sido, selected_gu, selected_dong, dong_geojson
    )
    return {
        "dong_avg_slope": avg_slope,
        "walk_difficulty": walk_difficulty,
    }


FILE_CONFIG = [
    {"path": "교육학군/[교육] 방과후돌봄교실.csv", "kind": "csv", "encoding": "cp949", "name_col": None,      "lat_col": "위도", "lng_col": "경도", "type_name": "방과후돌봄교실", "category": "교육"},
    {"path": "교육학군/[교육] 중학교.csv",         "kind": "csv", "encoding": "cp949", "name_col": "학교명",  "lat_col": "위도", "lng_col": "경도", "type_name": "중학교",       "category": "교육"},
    {"path": "교육학군/[교육] 초등학교.csv",        "kind": "csv", "encoding": "cp949", "name_col": "학교명",  "lat_col": "위도", "lng_col": "경도", "type_name": "초등학교",      "category": "교육"},
    {"path": "교육학군/[교육] 학원.csv",            "kind": "csv", "encoding": "cp949", "name_col": "학원명",  "lat_col": "위도", "lng_col": "경도", "type_name": "학원",         "category": "교육"},
    {"path": "교육학군/어린이집.csv",               "kind": "csv", "encoding": "cp949", "name_col": None,      "lat_col": "위도", "lng_col": "경도", "type_name": "어린이집",      "category": "교육"},
    {"path": "놀이친구/[놀이] 공공도서관.xlsx",     "kind": "excel", "name_col": None,   "lat_col": "위도", "lng_col": "경도", "type_name": "공공도서관",   "category": "놀이"},
    {"path": "놀이친구/[놀이] 놀이터.xlsx",         "kind": "excel", "name_col": None,   "lat_col": "위도", "lng_col": "경도", "type_name": "놀이터",       "category": "놀이"},
    {"path": "놀이친구/[놀이] 키즈카페.xlsx",       "kind": "excel", "name_col": None,   "lat_col": "위도", "lng_col": "경도", "type_name": "키즈카페",     "category": "놀이"},
    {"path": "안전치안/[안전]경찰서위치.xlsx",       "kind": "excel", "name_col": None,   "lat_col": "위도", "lng_col": "경도", "type_name": "경찰서",       "category": "안전"},
    {"path": "안전치안/[안전]교통사고빈번지역.xlsx", "kind": "excel", "name_col": None,   "lat_col": "위도", "lng_col": "경도", "type_name": "교통사고",     "category": "안전"},
    {"path": "안전치안/[안전]지구대파출소위치.xlsx", "kind": "excel", "name_col": None,   "lat_col": "위도", "lng_col": "경도", "type_name": "파출소",       "category": "안전"},
    {"path": "안전치안/[치안]cctv위치.xlsx",        "kind": "excel", "name_col": None,   "lat_col": "위도", "lng_col": "경도", "type_name": "CCTV",         "category": "안전"},
    {"path": "안전치안/[치안]알람벨위치.xlsx",      "kind": "excel", "name_col": None,   "lat_col": "위도", "lng_col": "경도", "type_name": "안전벨",       "category": "안전"},
    {"path": "의료복지/[복지]_문화시설_총합_최종.xlsx",      "kind": "excel", "name_col": None, "lat_col": "위도", "lng_col": "경도", "type_name": "문화시설",     "category": "의료복지"},
    {"path": "의료복지/[복지]통합지역아동센터_최종.xlsx",    "kind": "excel", "name_col": None, "lat_col": "위도", "lng_col": "경도", "type_name": "지역아동센터", "category": "의료복지"},
    {"path": "의료복지/[의료]병원_최종.xlsx",               "kind": "excel", "name_col": None, "lat_col": "위도", "lng_col": "경도", "type_name": "병원",         "category": "의료복지"},
    {"path": "의료복지/[의료]주변소아과중복제거.xlsx",       "kind": "excel", "name_col": None, "lat_col": "위도", "lng_col": "경도", "type_name": "소아과",       "category": "의료복지"},
    {"path": "환경생활/[생활]_대형마트모음.xlsx",    "kind": "excel", "name_col": None,   "lat_col": "위도", "lng_col": "경도", "type_name": "대형마트",     "category": "환경생활"},
    {"path": "환경생활/[생활]버스정류장.xlsx",       "kind": "excel", "name_col": None,   "lat_col": "위도", "lng_col": "경도", "type_name": "버스정류장",   "category": "환경생활"},
    {"path": "환경생활/[생활]지하철_최종.xlsx",      "kind": "excel", "name_col": None,   "lat_col": "위도", "lng_col": "경도", "type_name": "지하철",       "category": "환경생활"},
    {"path": "환경생활/[환경]_공원(면적포함).csv",   "kind": "csv",   "encoding": "utf-8", "name_col": None, "lat_col": "위도", "lng_col": "경도", "type_name": "공원",         "category": "환경생활"},
    {"path": "환경생활/[환경] 미세먼지.csv",         "kind": "csv",   "encoding": "cp949", "name_col": None, "lat_col": "위도", "lng_col": "경도", "type_name": "미세먼지",     "category": "환경생활"},
    {"path": "환경생활/[환경]유흥업소.xlsx",         "kind": "excel", "name_col": None,   "lat_col": "위도", "lng_col": "경도", "type_name": "유흥업소",     "category": "환경생활"},
]


@st.cache_data
def load_geojson():
    geojson_path = BASE_DIR / "fianl.geojson"
    score_path   = BASE_DIR / "[최종] 동별전체합계 - 순위 포함.xlsx"

    with open(geojson_path, encoding="utf-8") as f:
        data = json.load(f)

    score_df = pd.read_excel(score_path)
    if "행정동" in score_df.columns:
        score_df["행정동"] = score_df["행정동"].astype(str).str.strip()
    else:
        score_df["행정동"] = ""

    gu_col = pick_existing_col(score_df, ["시군구", "sggnm", "구", "시군구명"])
    sido_col = pick_existing_col(score_df, ["시도", "시도명", "sidonm", "광역시도", "시도_nm"])
    if gu_col is None:
        score_df["시군구정규화"] = ""
    else:
        score_df["시군구정규화"] = score_df[gu_col].apply(normalize_gu_name)

    if sido_col is not None:
        score_df["시도정규화"] = score_df[sido_col].apply(normalize_sido_name)
    else:
        base_for_sido = score_df[gu_col] if gu_col is not None else ""
        score_df["시도정규화"] = pd.Series(base_for_sido).apply(infer_sido_from_text)

    score_df["100점 환산"] = pd.to_numeric(score_df.get("100점 환산", 0), errors="coerce").fillna(0)
    score_df["region_key"] = score_df.apply(lambda r: make_region_key(r.get("시도정규화", ""), r.get("시군구정규화", "")), axis=1)
    score_df["score_key"] = score_df.apply(
        lambda r: f"{r['region_key']}__{clean_str(r.get('행정동', ''))}" if clean_str(r.get("행정동", "")) else "",
        axis=1,
    )
    score_df["legacy_key"] = score_df.apply(
        lambda r: f"{clean_str(r.get('시군구정규화', ''))} {clean_str(r.get('행정동', ''))}".strip(),
        axis=1,
    )

    score_map = (
        score_df[score_df["score_key"] != ""]
        .drop_duplicates(subset=["score_key"], keep="first")
        .set_index("score_key")["100점 환산"]
        .to_dict()
    )
    legacy_counts = score_df["legacy_key"].value_counts(dropna=False).to_dict()
    legacy_unique_df = score_df[
        (score_df["legacy_key"] != "") &
        (score_df["legacy_key"].map(legacy_counts) == 1)
    ].drop_duplicates(subset=["legacy_key"], keep="first")
    legacy_score_map = legacy_unique_df.set_index("legacy_key")["100점 환산"].to_dict()

    for feature in data["features"]:
        p = feature["properties"]
        sido, gu, dong, region_key, score_key, legacy_key = build_geo_row_keys(p)
        p["sidonm"] = sido
        p["sggnm"] = gu
        p["동이름"] = dong
        p["region_key"] = region_key
        p["display_region"] = f"{sido} {gu}".strip()
        p["score_key"] = score_key
        p["legacy_key"] = legacy_key
        p["score"] = float(score_map.get(score_key, legacy_score_map.get(legacy_key, 0)))

    return data




@st.cache_data
def load_dong_scores():
    score_path = BASE_DIR / "[최종] 동별전체합계 - 순위 포함.xlsx"
    base_cols = ["행정동", "100점 환산", "순위", "시도정규화", "시군구정규화"]
    if not score_path.exists():
        return pd.DataFrame(columns=base_cols)
    try:
        df = pd.read_excel(score_path)
        if "행정동" in df.columns:
            df["행정동"] = df["행정동"].astype(str).str.strip()
        else:
            df["행정동"] = ""

        gu_col = pick_existing_col(df, ["시군구", "sggnm", "구", "시군구명"])
        sido_col = pick_existing_col(df, ["시도", "시도명", "sidonm", "광역시도", "시도_nm"])

        if gu_col is not None:
            df["시군구정규화"] = df[gu_col].apply(normalize_gu_name)
        else:
            df["시군구정규화"] = ""

        if sido_col is not None:
            df["시도정규화"] = df[sido_col].apply(normalize_sido_name)
        else:
            base_for_sido = df[gu_col] if gu_col is not None else ""
            df["시도정규화"] = pd.Series(base_for_sido).apply(infer_sido_from_text)

        if "100점 환산" in df.columns:
            df["100점 환산"] = pd.to_numeric(df["100점 환산"], errors="coerce").fillna(0)
        else:
            df["100점 환산"] = 0
        if "순위" in df.columns:
            df["순위"] = pd.to_numeric(df["순위"], errors="coerce")
        else:
            df["순위"] = np.nan

        return df[base_cols].copy()
    except:
        return pd.DataFrame(columns=base_cols)




@st.cache_data
def build_gu_geojson(dong_geojson):
    gu_shapes = {}
    for feat in dong_geojson["features"]:
        props = feat["properties"]
        sido = normalize_sido_name(props.get("sidonm", ""))
        gu = normalize_gu_name(props.get("sggnm", ""))
        region_key = make_region_key(sido, gu)
        if not region_key:
            continue
        geom = shape(feat["geometry"])
        gu_shapes.setdefault(region_key, {"sido": sido, "gu": gu, "geoms": []})
        gu_shapes[region_key]["geoms"].append(geom)

    features = []
    for region_key, info in gu_shapes.items():
        merged = unary_union(info["geoms"])
        features.append({
            "type": "Feature",
            "geometry": mapping(merged),
            "properties": {
                "region_key": region_key,
                "sidonm": info["sido"],
                "sggnm": info["gu"],
                "display_region": f"{info['sido']} {info['gu']}".strip(),
                "score": 1,
            }
        })
    return {"type": "FeatureCollection", "features": features}



@st.cache_data
def load_facilities():
    dfs, load_logs = [], []
    for cfg in FILE_CONFIG:
        full_path = BASE_DIR / cfg["path"]
        try:
            df = pd.read_csv(full_path, encoding=cfg.get("encoding", "cp949"))                 if cfg["kind"] == "csv" else pd.read_excel(full_path)

            lat_col  = cfg["lat_col"]
            lng_col  = cfg["lng_col"]
            name_col = cfg.get("name_col") or infer_name_col(df)

            if lat_col not in df.columns or lng_col not in df.columns:
                load_logs.append(f"누락 컬럼: {full_path.name}")
                continue

            temp = pd.DataFrame()
            temp["위도"]     = pd.to_numeric(df[lat_col], errors="coerce")
            temp["경도"]     = pd.to_numeric(df[lng_col], errors="coerce")
            temp["type"]     = cfg["type_name"]
            temp["category"] = cfg["category"]
            if name_col and name_col in df.columns:
                temp["name"] = df[name_col].astype(str).str.strip().replace("", pd.NA).fillna(cfg["type_name"])
            else:
                temp["name"] = cfg["type_name"]

            if cfg["type_name"] == "CCTV":
                temp["name"] = "CCTV"
                temp["type"] = "-"
            elif cfg["type_name"] == "안전벨":
                temp["name"] = "안전벨"
                temp["type"] = "-"

            temp = add_coord_cache(temp, "위도", "경도")
            dfs.append(temp[["name", "위도", "경도", "type", "category", "_lat_np", "_lng_np"]])
        except Exception as e:
            load_logs.append(f"로드 실패: {full_path.name} -> {e}")

    all_df = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame(
        columns=["name", "위도", "경도", "type", "category"]
    )
    return all_df, load_logs


@st.cache_data
def load_houses():
    house_dir = BASE_DIR / "house"
    if not house_dir.exists():
        return pd.DataFrame(columns=["name","도로명","위도","경도","주택유형","거래유형","평균금액","시군구"])

    frames = []
    for file_path in sorted(house_dir.glob("*.xlsx")):
        try:
            df = pd.read_excel(file_path)
        except: continue
        if not all(c in df.columns for c in ["위도","경도"]): continue

        temp = pd.DataFrame()
        temp["위도"]   = pd.to_numeric(df["위도"],   errors="coerce")
        temp["경도"]   = pd.to_numeric(df["경도"],   errors="coerce")
        temp["도로명"] = df["도로명"].astype(str) if "도로명" in df.columns else ""
        temp["평균금액"] = pd.to_numeric(df["평균금액"], errors="coerce") if "평균금액" in df.columns else None
        temp["시군구"] = df["시군구"].astype(str) if "시군구" in df.columns else ""
        temp["주택유형"] = df["주택유형"].astype(str) if "주택유형" in df.columns else "주택"
        temp["name"]   = df["단지명"].astype(str) if "단지명" in df.columns else temp["도로명"].fillna("").replace("","주택")

        fname = file_path.stem
        temp["거래유형"] = "매매" if "매매" in fname else ("전세" if "전세" in fname else "기타")
        temp = add_coord_cache(temp, "위도", "경도")
        frames.append(temp[["name","도로명","위도","경도","주택유형","거래유형","평균금액","시군구","_lat_np","_lng_np"]])

    if not frames:
        return pd.DataFrame(columns=["name","도로명","위도","경도","주택유형","거래유형","평균금액","시군구"])
    return pd.concat(frames, ignore_index=True).drop_duplicates()


@st.cache_data
def load_infra():
    d = {}

    def rc(path, enc="utf-8"):
        full = BASE_DIR / path
        if not full.exists(): return pd.DataFrame()
        try: return pd.read_csv(full, encoding=enc)
        except: return pd.DataFrame()

    def re(path):
        full = BASE_DIR / path
        if not full.exists(): return pd.DataFrame()
        try: return pd.read_excel(full)
        except: return pd.DataFrame()

    # 교육
    d["elementary"]   = rc("교육학군/[교육] 초등학교.csv",      "cp949")
    d["middle"]       = rc("교육학군/[교육] 중학교.csv",        "cp949")
    d["academy"]      = rc("교육학군/[교육] 학원.csv",          "utf-8")
    d["daycare"]      = rc("교육학군/어린이집.csv",              "cp949")
    d["after_school"] = rc("교육학군/[교육] 방과후돌봄교실.csv", "cp949")

    # 놀이
    d["library"]    = re("놀이친구/[놀이] 공공도서관.xlsx")
    d["playground"] = re("놀이친구/[놀이] 놀이터.xlsx")
    d["kids_cafe"]  = re("놀이친구/[놀이] 키즈카페.xlsx")
    d["child_pop"]  = re("놀이친구/[친구] 0~18세 인구통계.xlsx")

    # 아동비율 계산
    if not d["child_pop"].empty:
        남자cols = [f'{i}세남자' for i in range(19)]
        여자cols = [f'{i}세여자' for i in range(19)]
        d["child_pop"]["아동수"] = \
            d["child_pop"][[c for c in 남자cols if c in d["child_pop"].columns]].sum(axis=1) + \
            d["child_pop"][[c for c in 여자cols if c in d["child_pop"].columns]].sum(axis=1)
        if "남자" in d["child_pop"].columns and "여자" in d["child_pop"].columns:
            d["child_pop"]["총인구"] = d["child_pop"]["남자"] + d["child_pop"]["여자"]
            d["child_pop"]["아동비율"] = d["child_pop"]["아동수"] / d["child_pop"]["총인구"].replace(0, float("nan"))

    d["police"]   = re("안전치안/[안전]경찰서위치.xlsx")
    d["accident"] = re("안전치안/[안전]교통사고빈번지역.xlsx")
    d["sub_s"]    = re("안전치안/[안전]지구대파출소위치.xlsx")
    d["cctv"]     = re("안전치안/[치안]cctv위치.xlsx")
    d["bell"]     = re("안전치안/[치안]알람벨위치.xlsx")

    d["hospital"]   = re("의료복지/[의료]병원_최종.xlsx")
    d["pediatrics"] = re("의료복지/[의료]주변소아과중복제거.xlsx")
    d["culture"]    = re("의료복지/[복지]_문화시설_총합_최종.xlsx")
    d["welfare"]    = re("의료복지/[복지]통합지역아동센터_최종.xlsx")

    if not d["pediatrics"].empty:
        if "좌표(Y)" in d["pediatrics"].columns:
            d["pediatrics"]["위도"] = pd.to_numeric(d["pediatrics"]["좌표(Y)"], errors="coerce")
        if "좌표(X)" in d["pediatrics"].columns:
            d["pediatrics"]["경도"] = pd.to_numeric(d["pediatrics"]["좌표(X)"], errors="coerce")

    if not d["welfare"].empty:
        if "Y좌표값" in d["welfare"].columns:
            d["welfare"]["위도"] = pd.to_numeric(d["welfare"]["Y좌표값"], errors="coerce")
        if "X좌표값" in d["welfare"].columns:
            d["welfare"]["경도"] = pd.to_numeric(d["welfare"]["X좌표값"], errors="coerce")

    d["mart"]    = re("환경생활/[생활]_대형마트모음.xlsx")
    d["bus"]     = re("환경생활/[생활]버스정류장.xlsx")
    d["subway"]  = re("환경생활/[생활]지하철_최종.xlsx")
    d["park"]    = rc("환경생활/[환경]_공원(면적포함).csv", "utf-8")
    d["dust"]    = rc("환경생활/[환경] 미세먼지.csv", "cp949")
    d["harmful"] = re("환경생활/[환경]유흥업소.xlsx")

    if not d["cctv"].empty:
        if "WGS84위도" in d["cctv"].columns and "위도" not in d["cctv"].columns:
            d["cctv"]["위도"] = pd.to_numeric(d["cctv"]["WGS84위도"], errors="coerce")
        if "WGS84경도" in d["cctv"].columns and "경도" not in d["cctv"].columns:
            d["cctv"]["경도"] = pd.to_numeric(d["cctv"]["WGS84경도"], errors="coerce")

    if not d["subway"].empty:
        if "역위도" in d["subway"].columns:
            d["subway"]["위도"] = pd.to_numeric(d["subway"]["역위도"], errors="coerce")
        if "역경도" in d["subway"].columns:
            d["subway"]["경도"] = pd.to_numeric(d["subway"]["역경도"], errors="coerce")

    if not d["mart"].empty:
        if "y좌표값" in d["mart"].columns and "위도" not in d["mart"].columns:
            d["mart"]["위도"] = pd.to_numeric(d["mart"]["y좌표값"], errors="coerce")
        if "x좌표값" in d["mart"].columns and "경도" not in d["mart"].columns:
            d["mart"]["경도"] = pd.to_numeric(d["mart"]["x좌표값"], errors="coerce")

    coord_keys = ["elementary","middle","academy","daycare","after_school","library","playground",
                  "kids_cafe","police","accident","sub_s","cctv","bell","hospital","pediatrics",
                  "culture","welfare","mart","bus","subway","park","harmful"]
    for key in coord_keys:
        if key in d and not d[key].empty:
            lat_col = find_col(d[key], ["위도", "WGS84위도", "역위도", "좌표(Y)", "Y좌표값", "y좌표값", "lat", "latitude", "y", "Y"])
            lng_col = find_col(d[key], ["경도", "WGS84경도", "역경도", "좌표(X)", "X좌표값", "x좌표값", "lng", "longitude", "x", "X"])
            if lat_col and lng_col:
                d[key] = add_coord_cache(d[key], lat_col, lng_col)

    return d


@st.cache_data
def load_child_density():
    infra = load_infra()
    child_pop = infra.get("child_pop", pd.DataFrame()).copy()

    base_cols = ["시도정규화", "시군구정규화", "시군구명", "읍면동명", "아동수", "총인구", "아동밀집도(%)"]
    if child_pop.empty:
        return pd.DataFrame(columns=base_cols)

    if "시군구명" in child_pop.columns:
        child_pop["시군구명"] = child_pop["시군구명"].astype(str).str.strip()
    else:
        child_pop["시군구명"] = ""

    if "읍면동명" in child_pop.columns:
        child_pop["읍면동명"] = child_pop["읍면동명"].astype(str).str.strip()
    else:
        child_pop["읍면동명"] = ""

    sido_col = pick_existing_col(child_pop, ["시도명", "시도", "광역시도", "sidonm"])
    if sido_col is not None:
        child_pop["시도정규화"] = child_pop[sido_col].apply(normalize_sido_name)
    else:
        child_pop["시도정규화"] = child_pop["시군구명"].apply(infer_sido_from_text)
    child_pop["시군구정규화"] = child_pop["시군구명"].apply(normalize_gu_name)

    if "아동수" not in child_pop.columns:
        boy_cols = [f"{i}세남자" for i in range(19) if f"{i}세남자" in child_pop.columns]
        girl_cols = [f"{i}세여자" for i in range(19) if f"{i}세여자" in child_pop.columns]
        child_pop["아동수"] = child_pop[boy_cols].sum(axis=1) + child_pop[girl_cols].sum(axis=1)

    if "총인구" not in child_pop.columns:
        child_pop["남자"] = pd.to_numeric(child_pop.get("남자", 0), errors="coerce").fillna(0)
        child_pop["여자"] = pd.to_numeric(child_pop.get("여자", 0), errors="coerce").fillna(0)
        child_pop["총인구"] = child_pop["남자"] + child_pop["여자"]

    child_pop["아동수"] = pd.to_numeric(child_pop["아동수"], errors="coerce").fillna(0)
    child_pop["총인구"] = pd.to_numeric(child_pop["총인구"], errors="coerce").fillna(0)

    child_pop["아동밀집도(%)"] = np.where(
        child_pop["총인구"] > 0,
        (child_pop["아동수"] / child_pop["총인구"]) * 100,
        0
    )

    return child_pop[base_cols].copy()



def calc_score(lat, lng, dong_name, radius=1000):
    """
    점수 계산은 팀원들의 개별 계산식(두 번째 코드 파일) 기준으로 맞춤.
    radius 파라미터는 UI의 주변 시설 표시에는 그대로 쓰이지만,
    카테고리 점수는 각 항목별 고정 반경을 사용한다.
    """
    infra  = load_infra()
    scores = {}

    try:
        cctv_lat = find_col(infra["cctv"], ["WGS84위도", "위도", "lat"])
        cctv_lng = find_col(infra["cctv"], ["WGS84경도", "경도", "lng"])
        bell_lat = find_col(infra["bell"], ["위도", "lat"])
        bell_lng = find_col(infra["bell"], ["경도", "lng"])

        n_police   = count_within(lat, lng, infra["police"],   "위도", "경도", 3000)
        n_sub      = count_within(lat, lng, infra["sub_s"],    "위도", "경도", 2000)
        n_accident = count_within(lat, lng, infra["accident"], "위도", "경도", 1000)
        s_cctv     = get_access_score(lat, lng, infra["cctv"], cctv_lat, cctv_lng, 1000)
        s_alarm    = get_access_score(lat, lng, infra["bell"], bell_lat, bell_lng, 1000)

        v1 = normalize(n_police, 0, 2)
        v2 = normalize(n_sub,    0, 7)
        v3 = normalize(s_cctv,   0, 0.8)
        v4 = normalize(s_alarm,  0, 0.8)
        scores["안전/치안"] = round((v1 + v2 + v3 + v4) / 4 * 100 - n_accident * 3, 2)
    except:
        scores["안전/치안"] = 0

    try:
        s_elem  = get_access_score(lat, lng, infra["elementary"],   "위도", "경도", 1000)
        n_elem  = count_within    (lat, lng, infra["elementary"],   "위도", "경도", 1000)
        s_mid   = get_access_score(lat, lng, infra["middle"],       "위도", "경도", 1500)
        n_mid   = count_within    (lat, lng, infra["middle"],       "위도", "경도", 1500)
        n_acad  = count_within    (lat, lng, infra["academy"],      "위도", "경도", 500)
        n_day   = count_within    (lat, lng, infra["daycare"],      "위도", "경도", 500)
        n_after = count_within    (lat, lng, infra["after_school"], "위도", "경도", 1000)

        v1 = normalize(s_elem,  0, 0.0115)
        v2 = normalize(n_elem,  0, 5)
        v3 = normalize(s_mid,   0, 0.0090)
        v4 = normalize(n_mid,   0, 7)
        v5 = normalize(n_acad,  0, 52)
        v6 = normalize(n_day,   0, 11)
        v7 = normalize(n_after, 0, 5)
        scores["교육/학군"] = round((v1 + v2 + v3 + v4 + v5 + v6 + v7) / 7 * 100, 2)
    except:
        scores["교육/학군"] = 0

    try:
        s_hosp = get_access_score(lat, lng, infra["pediatrics"], "위도", "경도", 2000)
        s_welf = get_access_score(lat, lng, infra["welfare"],    "위도", "경도", 2000)
        v1 = normalize(s_hosp, 0, 0.03)
        v2 = normalize(s_welf, 0, 0.01)
        scores["의료/복지"] = round((v1 + v2) / 2 * 100, 2)
    except:
        scores["의료/복지"] = 0

    try:
        dist_play = nearest_dist(lat, lng, infra["playground"], "위도", "경도")
        dist_lib  = nearest_dist(lat, lng, infra["library"],    "위도", "경도")
        cnt_kids  = count_within(lat, lng, infra["kids_cafe"],  "위도", "경도", 1000)

        pop_row = pd.DataFrame()
        if not infra["child_pop"].empty and "읍면동명" in infra["child_pop"].columns:
            pop_row = infra["child_pop"][infra["child_pop"]["읍면동명"].astype(str).str.strip() == str(dong_name).strip()]
            if len(pop_row) == 0 and dong_name:
                kw = str(dong_name).rstrip("동가").rstrip("0123456789")
                if kw:
                    pop_row = infra["child_pop"][infra["child_pop"]["읍면동명"].astype(str).str.contains(kw, na=False)]

        raw_pop = float(pop_row["아동비율"].mean()) if len(pop_row) > 0 and "아동비율" in pop_row.columns else 0.0

        v1 = normalize(dist_play, 0, 3000, reverse=True)
        v2 = normalize(dist_lib,  0, 5000, reverse=True)
        v3 = normalize(cnt_kids,  0, 5)
        v4 = normalize(raw_pop,   0.01, 0.29)
        scores["놀이/친구"] = round((v1 + v2 + v3 + v4) / 4 * 100, 2)
    except:
        scores["놀이/친구"] = 0

    life_score = 0.0
    env_score  = 0.0

    try:
        subway_lat = find_col(infra["subway"], ["역위도", "위도", "lat"])
        subway_lng = find_col(infra["subway"], ["역경도", "경도", "lng"])
        bus_lat    = find_col(infra["bus"],    ["위도", "lat"])
        bus_lng    = find_col(infra["bus"],    ["경도", "lng"])
        mart_lat   = find_col(infra["mart"],   ["위도", "y좌표값", "lat"])
        mart_lng   = find_col(infra["mart"],   ["경도", "x좌표값", "lng"])

        cnt_sub  = count_within(lat, lng, infra["subway"], subway_lat, subway_lng, 1000)
        cnt_bus  = count_within(lat, lng, infra["bus"],    bus_lat,    bus_lng,     500)
        cnt_mart = count_within(lat, lng, infra["mart"],   mart_lat,   mart_lng,   1000)

        v1 = normalize(cnt_sub,  0, 3)
        v2 = normalize(cnt_bus,  0, 20)
        v3 = normalize(cnt_mart, 0, 5)
        life_score = round((v1 + v2 + v3) / 3 * 100, 2)
    except:
        life_score = 0.0

    try:
        s_park  = park_area_score_fn(lat, lng, infra["park"], radius=1000)
        cnt_yuh = count_within(lat, lng, infra["harmful"], "위도", "경도", 500)

        dust_row = pd.DataFrame()
        if not infra["dust"].empty and "동" in infra["dust"].columns:
            dust_row = infra["dust"][infra["dust"]["동"].astype(str).str.strip() == str(dong_name).strip()]
            if len(dust_row) == 0 and dong_name:
                kw = str(dong_name).rstrip("동가").rstrip("0123456789")
                if kw:
                    dust_row = infra["dust"][infra["dust"]["동"].astype(str).str.strip().str.contains(kw, na=False)]

        raw_dust = float(dust_row["점수"].mean()) if len(dust_row) > 0 and "점수" in dust_row.columns else 5.0

        v1 = normalize(s_park,   0, 700)
        v2 = normalize(raw_dust, 0, 10.19)
        v3 = round(1 - normalize(cnt_yuh, 0, 5), 4)
        env_score = round((v1 + v2 + v3) / 3 * 100, 2)
    except:
        env_score = 0.0

    scores["생활/환경"] = round((life_score + env_score) / 2, 2)
    return scores


def calc_total(scores, weights):
    total_w = sum(weights.values())
    if total_w == 0:
        return 0.0
    return round(sum(scores.get(k, 0) * weights.get(k, 0) for k in scores) / total_w, 2)


def calc_weighted_display_scores(scores, weights, base_weight=5):
    weighted_scores = {}
    category_count = len(scores) if scores else len(weights)
    total_w = sum(weights.values())

    if category_count == 0:
        return weighted_scores

    if total_w == 0:
        return {key: 0.0 for key in scores}

    equal_ratio = 1.0 / float(category_count)

    for key, value in scores.items():
        current_ratio = float(weights.get(key, base_weight)) / float(total_w)
        ratio_factor = current_ratio / equal_ratio
        weighted_value = float(value) * ratio_factor
        weighted_scores[key] = round(max(0.0, min(100.0, weighted_value)), 2)
    return weighted_scores


@st.cache_data(show_spinner=False, max_entries=20000)
def calc_score_cached(lat, lng, dong_name, radius=1000):
    return calc_score(lat, lng, dong_name, radius=radius)


def make_gu_map(gu_geojson, selected_region=None):
    rows = [{
        "region_key": f["properties"]["region_key"],
        "display_region": f["properties"].get("display_region", f["properties"].get("sggnm", "")),
        "구분": 1
    } for f in gu_geojson["features"]]
    df = pd.DataFrame(rows)
    fig = px.choropleth_mapbox(
        df,
        geojson=gu_geojson,
        locations="region_key",
        featureidkey="properties.region_key",
        color="구분",
        color_continuous_scale=[[0, "#F1E7B4"], [1, "#F1E7B4"]],
        range_color=[0, 1],
        mapbox_style="carto-positron",
        zoom=9.4,
        center={"lat": 37.55, "lon": 126.98},
        opacity=0.45,
        hover_name="display_region",
        hover_data={"구분": False, "region_key": False},
    )
    fig.update_layout(
        margin={"r": 0, "t": 0, "l": 0, "b": 0},
        height=620,
        coloraxis_showscale=False,
    )
    return fig




def make_dong_map(dong_geojson, selected_sido, selected_gu, result=None, houses=None, radius_m=1000, dong_scores_df=None):
    rows, features_filtered = [], []
    region_label = f"{selected_sido} {selected_gu}".strip()

    for feat in dong_geojson["features"]:
        props = feat["properties"]
        if (
            normalize_sido_name(props.get("sidonm", "")) == normalize_sido_name(selected_sido) and
            normalize_gu_name(props.get("sggnm", "")) == normalize_gu_name(selected_gu)
        ):
            rows.append({"key": props["key"], "동이름": clean_str(props["동이름"])})
            features_filtered.append(feat)
    if not rows:
        return None

    df = pd.DataFrame(rows)
    if dong_scores_df is not None and not dong_scores_df.empty:
        df["동이름"] = df["동이름"].astype(str).str.strip()
        work_scores = dong_scores_df.copy()
        work_scores = work_scores[work_scores["시군구정규화"] == normalize_gu_name(selected_gu)].copy()
        if selected_sido and "시도정규화" in work_scores.columns:
            temp_scores = work_scores[work_scores["시도정규화"] == normalize_sido_name(selected_sido)].copy()
            if not temp_scores.empty:
                work_scores = temp_scores
        df = df.merge(work_scores, left_on="동이름", right_on="행정동", how="left")
        df["score"] = pd.to_numeric(df.get("100점 환산", 0), errors="coerce").fillna(0)
        
    else:
        df["score"] = 0
        

    df = df.sort_values(["score", "동이름"], ascending=[False, True]).reset_index(drop=True)
    df["구내순위"] = np.arange(1, len(df) + 1)
    if len(df) == 1:
        df["구내색상점수"] = 100.0
    else:
        df["구내색상점수"] = ((len(df) - df["구내순위"]) / (len(df) - 1) * 100).round(1)

    filtered_geojson = {"type": "FeatureCollection", "features": features_filtered}
    lats, lons = [], []
    for feat in features_filtered:
        centroid = shape(feat["geometry"]).centroid
        lats.append(centroid.y)
        lons.append(centroid.x)
    center_lat = sum(lats) / len(lats) if lats else 37.55
    center_lon = sum(lons) / len(lons) if lons else 126.98

    fig = px.choropleth_mapbox(
        df,
        geojson=filtered_geojson,
        locations="key",
        featureidkey="properties.key",
        color="구내색상점수",
        color_continuous_scale=COLOR_SCALE,
        range_color=[0, 100],
        mapbox_style="carto-positron",
        zoom=11.5,
        center={"lat": center_lat, "lon": center_lon},
        opacity=0.62,
        hover_name="동이름",
        hover_data={
            "score": True,
            "구내순위": True,

            "구내색상점수": False,
            "key": False,
            "동이름": False,
        },
        labels={"score": "동별 점수", "구내순위": f"{region_label} 내 순위"},
    )
    fig.update_layout(
        margin={"r": 0, "t": 0, "l": 0, "b": 0},
        height=620,
        coloraxis_colorbar=dict(title=f"{region_label} 내 순위", tickvals=[0, 25, 50, 75, 100], thickness=12, len=0.5),
        clickmode="event+select",
    )

    if result:
        circle_lats, circle_lngs = make_circle_points(result["lat"], result["lng"], radius_m=radius_m)
        fig.add_trace(go.Scattermapbox(
            lat=circle_lats, lon=circle_lngs, mode="lines", fill="toself",
            fillcolor="rgba(31,119,180,0.12)",
            line=dict(width=2, color="rgba(31,119,180,0.5)"),
            name=f"반경 {radius_m}m", hoverinfo="skip",
        ))
        fig.add_trace(go.Scattermapbox(
            lat=[result["lat"]], lon=[result["lng"]], mode="markers+text",
            marker=dict(size=12, color="#d60000", symbol="circle"),
            text=[f"C-LCI {result['total']}점"],
            textposition="top right", textfont=dict(size=13, color="#d60000"),
            name="선택 위치",
            hovertemplate=f"{result.get('address','선택 위치')}<br>C-LCI {result['total']}점<extra></extra>",
        ))

        if houses is not None and not houses.empty:
            plot_houses = houses.copy()
            for col in ["name","주택유형","거래유형","도로명","시군구","거리(m)"]:
                if col not in plot_houses.columns: plot_houses[col] = "-"
            if "평균금액" not in plot_houses.columns: plot_houses["평균금액"] = None
            plot_houses["평균금액표시"] = plot_houses["평균금액"].apply(format_price_kor)
            customdata = plot_houses[["name","주택유형","거래유형","도로명","평균금액표시","시군구","거리(m)"]].to_numpy()
            fig.add_trace(go.Scattermapbox(
                lat=plot_houses["위도"], lon=plot_houses["경도"], mode="markers",
                marker=dict(size=9, color="#1f77b4", opacity=0.85),
                text=plot_houses["name"], customdata=customdata,
                name="매물(단지)",
                hovertemplate=(
                    "<b>%{customdata[0]}</b><br>"
                    "유형: %{customdata[1]} / %{customdata[2]}<br>"
                    "도로명: %{customdata[3]}<br>"
                    "평균금액: %{customdata[4]}<br>"
                    "시군구: %{customdata[5]}<br>"
                    "거리: %{customdata[6]}m<extra></extra>"
                ),
            ))

        selected_types = get_selected_facility_types()
        if selected_types:
            nearby_fac = get_nearby(result["lat"], result["lng"], facilities, radius=radius_m)
            nearby_fac = nearby_fac[nearby_fac["type"].isin(selected_types)].copy()
            if not nearby_fac.empty:
                for cat_value, cat_df in nearby_fac.groupby("category", sort=False):
                    custom_fac = cat_df[["name", "type", "category", "거리(m)"]].to_numpy()
                    marker_color = FACILITY_CATEGORY_COLORS.get(cat_value, "#2ca02c")
                    fig.add_trace(go.Scattermapbox(
                        lat=cat_df["위도"], lon=cat_df["경도"], mode="markers",
                        marker=dict(size=8, color=marker_color, opacity=0.9),
                        customdata=custom_fac, name=to_display_category(cat_value),
                        hovertemplate=(
                            "<b>%{customdata[0]}</b><br>"
                            "종류: %{customdata[1]}<br>"
                            "카테고리: %{customdata[2]}<br>"
                            "거리: %{customdata[3]}m<extra></extra>"
                        ),
                    ))
    return fig




def get_dong_feature_by_point(lat, lng, dong_geojson, selected_sido=None, selected_gu=None):
    point = Point(lng, lat)
    for feat in dong_geojson["features"]:
        props = feat["properties"]
        if selected_sido and normalize_sido_name(props.get("sidonm", "")) != normalize_sido_name(selected_sido):
            continue
        if selected_gu and normalize_gu_name(props.get("sggnm", "")) != normalize_gu_name(selected_gu):
            continue
        geom = shape(feat["geometry"])
        if geom.contains(point) or geom.touches(point):
            return feat
    return None


def _dong_fill_color(score):
    try:
        score = float(score)
    except Exception:
        score = 0.0
    if score >= 80:
        return "#F0CF3F"
    if score >= 60:
        return "#F1E7B4"
    return "#FAF4D9"

def make_dong_click_map(
    dong_geojson,
    selected_sido,
    selected_gu,
    result=None,
    houses=None,
    radius_m=1000,
    dong_scores_df=None,
    selected_categories=None
):
    rows, features_filtered = [], []
    region_label = f"{selected_sido} {selected_gu}".strip()

    for feat in dong_geojson["features"]:
        props = feat["properties"]
        if (
            normalize_sido_name(props.get("sidonm", "")) == normalize_sido_name(selected_sido)
            and normalize_gu_name(props.get("sggnm", "")) == normalize_gu_name(selected_gu)
        ):
            rows.append({"key": props["key"], "동이름": clean_str(props["동이름"])})
            features_filtered.append(json.loads(json.dumps(feat)))

    if not rows:
        return None

    df = pd.DataFrame(rows)

    if dong_scores_df is not None and not dong_scores_df.empty:
        df["동이름"] = df["동이름"].astype(str).str.strip()
        work_scores = dong_scores_df.copy()
        work_scores = work_scores[
            work_scores["시군구정규화"] == normalize_gu_name(selected_gu)
        ].copy()

        if selected_sido and "시도정규화" in work_scores.columns:
            temp_scores = work_scores[
                work_scores["시도정규화"] == normalize_sido_name(selected_sido)
            ].copy()
            if not temp_scores.empty:
                work_scores = temp_scores

        df = df.merge(work_scores, left_on="동이름", right_on="행정동", how="left")
        df["score"] = pd.to_numeric(df.get("100점 환산", 0), errors="coerce").fillna(0)
    else:
        df["score"] = 0

    df = df.sort_values(["score", "동이름"], ascending=[False, True]).reset_index(drop=True)
    df["구내순위"] = np.arange(1, len(df) + 1)

    if len(df) == 1:
        df["구내색상점수"] = 100.0
    else:
        df["구내색상점수"] = ((len(df) - df["구내순위"]) / (len(df) - 1) * 100).round(1)

    score_info = df.set_index("key")[["동이름", "score", "구내순위", "구내색상점수"]].to_dict("index")

    lats, lons = [], []
    for feat in features_filtered:
        info = score_info.get(feat["properties"].get("key"), {})
        feat["properties"].update(info)
        centroid = shape(feat["geometry"]).centroid
        lats.append(centroid.y)
        lons.append(centroid.x)

    center_lat = sum(lats) / len(lats) if lats else 37.55
    center_lon = sum(lons) / len(lons) if lons else 126.98

    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=12,
        tiles="CartoDB positron",
        control_scale=False
    )

    def add_facility_legend(m, selected_categories):
        if not selected_categories:
            return

        reverse_display_map = {
            "교육/학군": "교육",
            "놀이/친구": "놀이",
            "안전/치안": "안전",
            "의료/복지": "의료복지",
            "환경/생활": "환경생활",
        }

        rows_html = []
        for display_name in selected_categories:
            key = reverse_display_map.get(display_name)
            color = FACILITY_CATEGORY_COLORS.get(key, "#999999")

            rows_html.append(f"""
                <div style="display:flex; align-items:center; margin-bottom:6px;">
                    <span style="
                        display:inline-block;
                        width:12px;
                        height:12px;
                        background:{color};
                        border-radius:50%;
                        margin-right:8px;
                        flex-shrink:0;
                    "></span>
                    <span>{display_name}</span>
                </div>
            """)

        legend_html = f"""
        <div style="
            position: fixed;
            top: 12px;
            right: 12px;
            z-index: 9999;
            background-color: rgba(255,255,255,0.96);
            border: 1.5px solid #bdbdbd;
            border-radius: 10px;
            padding: 10px 12px;
            font-size: 13px;
            line-height: 1.3;
            box-shadow: 0 2px 6px rgba(0,0,0,0.18);
            min-width: 145px;
        ">
            <div style="font-weight:700; margin-bottom:8px;">시설 카테고리</div>
            {''.join(rows_html)}
        </div>
        """
        m.get_root().html.add_child(Element(legend_html))

    add_facility_legend(m, selected_categories)

    folium.GeoJson(
        {"type": "FeatureCollection", "features": features_filtered},
        style_function=lambda feature: {
            "fillColor": _dong_fill_color(feature["properties"].get("구내색상점수", 0)),
            "color": "#666666",
            "weight": 1.2,
            "fillOpacity": 0.7,
        },
        tooltip=folium.GeoJsonTooltip(
            fields=["동이름", "score", "구내순위"],
            aliases=["행정동", "C-LCI 점수", "구내순위"],
            localize=True,
            sticky=False,
            labels=True,
            style=(
                "background-color: white; color: #222; font-family: Arial; "
                "font-size: 12px; padding: 8px;"
            ),
        ),
    ).add_to(m)

    if result and "lat" in result and "lng" in result:
        folium.Circle(
            location=[result["lat"], result["lng"]],
            radius=radius_m,
            color="#3186cc",
            weight=2,
            fill=True,
            fill_opacity=0.08,
        ).add_to(m)

        folium.CircleMarker(
            location=[result["lat"], result["lng"]],
            radius=6,
            color="#ff1d1d",
            fill=True,
            fill_color="#ff1d1d",
            fill_opacity=1.0,
            tooltip="선택 지점",
        ).add_to(m)

        selected_types = get_selected_facility_types()
        if selected_types:
            facilities, _ = load_facilities()
            nearby_fac = get_nearby(result["lat"], result["lng"], facilities, radius=radius_m)
            nearby_fac = nearby_fac[nearby_fac["type"].isin(selected_types)].copy()

            if not nearby_fac.empty:
                for _, row in nearby_fac.iterrows():
                    marker_color = FACILITY_CATEGORY_COLORS.get(row["category"], "#2ca02c")
                    tooltip_text = (
                        f"<b>{row['name']}</b><br>"
                        f"종류: {row['type']}<br>"
                        f"카테고리: {to_display_category(row['category'])}<br>"
                        f"거리: {int(row['거리(m)'])}m"
                    )

                    folium.CircleMarker(
                        location=[row["위도"], row["경도"]],
                        radius=5,
                        color=marker_color,
                        fill=True,
                        fill_color=marker_color,
                        fill_opacity=0.9,
                        tooltip=folium.Tooltip(tooltip_text),
                    ).add_to(m)

    if houses is not None and not houses.empty:
        plot_houses = houses.copy()
        if "평균금액" in plot_houses.columns:
            plot_houses["평균금액표시"] = plot_houses["평균금액"].apply(format_price_kor)
        else:
            plot_houses["평균금액표시"] = "가격미상"

        for _, row in plot_houses.iterrows():
            tooltip_text = (
                f"<b>{row.get('name', '-')}</b><br>"
                f"유형: {row.get('주택유형', '-')} / {row.get('거래유형', '-')}<br>"
                f"도로명: {row.get('도로명', '-')}<br>"
                f"평균금액: {row.get('평균금액표시', '가격미상')}"
            )

            folium.CircleMarker(
                location=[row["위도"], row["경도"]],
                radius=5,
                color="#1f77b4",
                fill=True,
                fill_color="#1f77b4",
                fill_opacity=0.85,
                tooltip=folium.Tooltip(tooltip_text),
            ).add_to(m)

    return m


def set_view_by_point(lat, lng, dong_geojson):
    point = Point(lng, lat)
    for feat in dong_geojson["features"]:
        geom = shape(feat["geometry"])
        if geom.contains(point) or geom.touches(point):
            dong_name = clean_str(feat["properties"].get("동이름", ""))
            st.session_state.selected_sido = normalize_sido_name(feat["properties"].get("sidonm", ""))
            st.session_state.selected_gu = normalize_gu_name(feat["properties"].get("sggnm", ""))
            st.session_state.selected_dong = dong_name
            st.session_state.view_mode = "dong"
            return feat
    return None



def get_nearby(lat, lng, facilities, radius=1000):
    if facilities.empty:
        return pd.DataFrame(columns=["name","위도","경도","type","category","거리(m)"])
    temp, dists = get_distance_series(facilities, lat, lng, "위도", "경도", radius=radius)
    if len(dists) == 0:
        return pd.DataFrame(columns=["name","위도","경도","type","category","거리(m)"])
    temp = temp.copy()
    temp["거리(m)"] = dists
    nearby = temp[temp["거리(m)"] <= radius].copy()
    nearby["거리(m)"] = nearby["거리(m)"].round(1)
    return nearby.sort_values("거리(m)")

def get_nearby_houses(lat, lng, houses, radius=1000):
    if houses.empty:
        return pd.DataFrame(columns=["name","도로명","위도","경도","주택유형","거래유형","평균금액","시군구","거리(m)"])
    temp, dists = get_distance_series(houses, lat, lng, "위도", "경도", radius=radius)
    if len(dists) == 0:
        return pd.DataFrame(columns=["name","도로명","위도","경도","주택유형","거래유형","평균금액","시군구","거리(m)"])
    temp = temp.copy()
    temp["거리(m)"] = dists
    nearby = temp[temp["거리(m)"] <= radius].copy()
    nearby["거리(m)"] = nearby["거리(m)"].round(1)
    return nearby.sort_values("거리(m)")

def get_selected_facility_types():
    selected = []
    category_map = {
        "교육/학군": "교육",
        "놀이/친구": "놀이",
        "안전/치안": "안전",
        "의료/복지": "의료복지",
        "환경/생활": "환경생활",
    }
    for label, cat_value in category_map.items():
        if st.session_state.get(f"cat_{label}", False):
            types = st.session_state.get(f"types_{cat_value}", [])
            if cat_value == "안전":
                types = [t for t in types if t not in ["CCTV", "안전벨", "-"]]
            selected.extend(types)
    return selected


def get_dong_polygon(selected_sido, selected_gu, selected_dong, dong_geojson):
    for feat in dong_geojson["features"]:
        if normalize_sido_name(feat["properties"].get("sidonm", "")) == normalize_sido_name(selected_sido) and normalize_gu_name(feat["properties"].get("sggnm", "")) == normalize_gu_name(selected_gu) and feat["properties"].get("동이름") == selected_dong:
            return shape(feat["geometry"])
    return None


def get_houses_in_dong(selected_sido, selected_gu, selected_dong, dong_geojson, houses_df, budget=None):
    columns = ["name","도로명","위도","경도","주택유형","거래유형","평균금액","시군구"]
    if houses_df is None or houses_df.empty or not selected_gu or not selected_dong:
        return pd.DataFrame(columns=columns)

    poly = get_dong_polygon(selected_sido, selected_gu, selected_dong, dong_geojson)
    if poly is None:
        return pd.DataFrame(columns=columns)

    temp = houses_df.dropna(subset=["위도", "경도"]).copy()
    if temp.empty:
        return pd.DataFrame(columns=columns)

    minx, miny, maxx, maxy = poly.bounds
    temp = temp[
        (temp["경도"] >= minx) & (temp["경도"] <= maxx) &
        (temp["위도"] >= miny) & (temp["위도"] <= maxy)
    ].copy()
    if temp.empty:
        return pd.DataFrame(columns=columns)

    prepared_poly = prep(poly)
    points = [Point(float(lon), float(lat)) for lon, lat in zip(temp["경도"].to_numpy(), temp["위도"].to_numpy())]
    mask = np.array([prepared_poly.contains(pt) for pt in points], dtype=bool)
    temp = temp.loc[mask].copy()

    if budget is not None and budget > 0 and "평균금액" in temp.columns:
        price_series = pd.to_numeric(temp["평균금액"], errors="coerce")
        temp = temp[price_series <= budget].copy()

    if temp.empty:
        return pd.DataFrame(columns=columns)

    temp = temp.drop_duplicates(subset=["name", "도로명"]).copy()
    return temp.sort_values(["평균금액", "name"], ascending=[True, True]).reset_index(drop=True)


def recommend_houses_in_dong(selected_sido, selected_gu, selected_dong, dong_geojson, houses_df, weights, budget=None, limit=5):
    temp = get_houses_in_dong(selected_sido, selected_gu, selected_dong, dong_geojson, houses_df, budget=budget)
    if temp.empty:
        return pd.DataFrame(columns=["name","도로명","평균금액","C-LCI 점수","주택유형","거래유형"])

    temp = temp.head(120).copy()
    recs = []
    for _, row in temp.iterrows():
        lat = float(row["위도"])
        lng = float(row["경도"])
        scores = calc_score_cached(lat, lng, selected_dong)
        total = calc_total(scores, weights)
        recs.append({
            "name": row.get("name", "주택"),
            "도로명": row.get("도로명", ""),
            "평균금액": row.get("평균금액", None),
            "C-LCI 점수": total,
            "주택유형": row.get("주택유형", "주택"),
            "거래유형": row.get("거래유형", "-"),
        })
    out = pd.DataFrame(recs).drop_duplicates(subset=["name", "도로명"]) if recs else pd.DataFrame()
    if out.empty:
        return pd.DataFrame(columns=["name","도로명","평균금액","C-LCI 점수","주택유형","거래유형"])
    return out.sort_values(["C-LCI 점수", "평균금액"], ascending=[False, True]).head(limit)

def recommend_houses_near_point(lat, lng, dong_name, houses_df, weights, radius=1000, budget=None):
    if houses_df is None or houses_df.empty:
        return pd.DataFrame(columns=["순위","아파트명","도로명","평균금액","C-LCI 점수","주택유형","거래유형","거리(m)"])

    temp = get_nearby_houses(lat, lng, houses_df, radius=radius).copy()
    if temp.empty:
        return pd.DataFrame(columns=["순위","아파트명","도로명","평균금액","C-LCI 점수","주택유형","거래유형","거리(m)"])

    if budget is not None and budget > 0 and "평균금액" in temp.columns:
        price_series = pd.to_numeric(temp["평균금액"], errors="coerce")
        temp = temp[price_series <= budget].copy()

    if temp.empty:
        return pd.DataFrame(columns=["순위","아파트명","도로명","평균금액","C-LCI 점수","주택유형","거래유형","거리(m)"])

    recs = []
    for row in temp.itertuples(index=False):
        house_lat = float(row.위도)
        house_lng = float(row.경도)

        use_dong = dong_name or get_dong(str(getattr(row, "도로명", "")))

        scores = calc_score_cached(house_lat, house_lng, use_dong, radius=radius)
        total = calc_total(scores, weights)

        recs.append({
            "아파트명": getattr(row, "name", "주택"),
            "도로명": getattr(row, "도로명", ""),
            "평균금액": getattr(row, "평균금액", None),
            "C-LCI 점수": total,
            "주택유형": getattr(row, "주택유형", "주택"),
            "거래유형": getattr(row, "거래유형", "-"),
            "거리(m)": getattr(row, "거리_m", None)  
        })

    out = pd.DataFrame(recs).drop_duplicates(subset=["아파트명", "도로명"]) if recs else pd.DataFrame()
    if out.empty:
        return pd.DataFrame(columns=["순위","아파트명","도로명","평균금액","C-LCI 점수","주택유형","거래유형","거리(m)"])

    out = out.sort_values(["C-LCI 점수", "평균금액", "거리(m)"], ascending=[False, True, True]).reset_index(drop=True)
    out.insert(0, "순위", out.index + 1)
    return out

if "view_mode"       not in st.session_state: st.session_state.view_mode       = "gu"
if "selected_sido"   not in st.session_state: st.session_state.selected_sido   = None
if "selected_gu"     not in st.session_state: st.session_state.selected_gu     = None
if "selected_dong"   not in st.session_state: st.session_state.selected_dong   = None
if "result"          not in st.session_state: st.session_state.result          = None
if "nearby_houses"   not in st.session_state: st.session_state.nearby_houses   = pd.DataFrame()
if "dong_map_houses" not in st.session_state: st.session_state.dong_map_houses = pd.DataFrame()
if "dong_recommendations" not in st.session_state: st.session_state.dong_recommendations = pd.DataFrame()
if "search_address"  not in st.session_state: st.session_state.search_address  = ""
if "weights"         not in st.session_state: st.session_state.weights = {
    "안전/치안": 5, "교육/학군": 5, "의료/복지": 5, "놀이/친구": 5, "생활/환경": 5,
}
if "budget"          not in st.session_state: st.session_state.budget = 0
if "dong_search_applied" not in st.session_state: st.session_state.dong_search_applied = False
if "last_map_click"   not in st.session_state: st.session_state.last_map_click   = None

dong_geojson   = load_geojson()
gu_geojson     = build_gu_geojson(dong_geojson)
dong_scores_df = load_dong_scores()
facilities, load_logs = load_facilities()
houses = load_houses()
child_density_df = load_child_density()

def refresh_dong_house_views(force=False):
    selected_sido = st.session_state.get("selected_sido")
    selected_gu = st.session_state.get("selected_gu")
    selected_dong = st.session_state.get("selected_dong")
    budget = st.session_state.get("budget", 0)
    weights = st.session_state.get("weights", {})
    applied = bool(st.session_state.get("dong_search_applied", False))

    result = st.session_state.get("result") or {}
    if not selected_dong:
        selected_dong = clean_str(result.get("dong", ""))
        if selected_dong:
            st.session_state.selected_dong = selected_dong

    if (not selected_sido or not selected_gu) and result.get("lat") is not None and result.get("lng") is not None:
        set_view_by_point(float(result["lat"]), float(result["lng"]), dong_geojson)
        selected_sido = st.session_state.get("selected_sido")
        selected_gu = st.session_state.get("selected_gu")

    if force:
        applied = True
        st.session_state.dong_search_applied = True

    if selected_sido and selected_gu and selected_dong and applied:
        st.session_state.dong_map_houses = get_houses_in_dong(
            selected_sido, selected_gu, selected_dong, dong_geojson, houses, budget=budget
        )
        st.session_state.dong_recommendations = recommend_houses_in_dong(
            selected_sido, selected_gu, selected_dong, dong_geojson, houses, weights, budget=budget, limit=5
        )
    else:
        st.session_state.dong_map_houses = pd.DataFrame()
        st.session_state.dong_recommendations = pd.DataFrame()

st.title("C-LCI | 양육 친화 주거지 탐색 서비스")
st.markdown(
    "<p style='font-size:22px; color:gray; margin-top:-10px;'>데이터 기반 생활 인프라 분석으로 우리 동네 양육 환경을 확인하세요.</p>",
    unsafe_allow_html=True
)
with st.sidebar:
    if st.session_state.view_mode == "dong":
        if st.button("← 전체 지도로", use_container_width=True):
            st.session_state.view_mode     = "gu"
            st.session_state.selected_sido = None
            st.session_state.selected_gu   = None
            st.session_state.selected_dong = None
            st.session_state.result        = None
            st.session_state.nearby_houses = pd.DataFrame()
            st.session_state.dong_map_houses = pd.DataFrame()
            st.session_state.dong_recommendations = pd.DataFrame()
            st.session_state.dong_search_applied = False
            st.rerun()
        st.divider()

    st.header("가중치 설정")
    st.caption("기준값: 5 | 높을수록 해당 영역 비중 증가")
    for key in st.session_state.weights.keys():
        st.session_state.weights[key] = st.slider(key, 0, 10, st.session_state.weights[key], step=1)

    total_w = sum(st.session_state.weights.values())
    if total_w > 0:
        st.caption("비율: " + " | ".join(
            f"{k} {int(v/total_w*100)}%" for k, v in st.session_state.weights.items()
        ))

    st.session_state.budget = st.number_input(
        "예산 입력 (원)", min_value=0, value=int(st.session_state.budget), step=10000000,
        help="선택한 동 안에서 예산 이하 아파트를 추천합니다."
    )
    search_clicked = st.button("동 내 예산 맞춤 추천 검색", use_container_width=True)
    if search_clicked:
        refresh_dong_house_views(force=True)

    st.divider()
    st.markdown("**시설 지도 표시**")
    category_label_map = REVERSE_CATEGORY_MAP
    for label, cat_value in category_label_map.items():
        st.checkbox(label, key=f"cat_{label}")
        if st.session_state.get(f"cat_{label}", False):
            type_options = sorted(facilities[facilities["category"] == cat_value]["type"].dropna().unique().tolist())
            if cat_value == "안전":
                type_options = [t for t in type_options if t not in ["CCTV", "안전벨", "-"]]
            st.multiselect(
                f"{label} 시설 종류", type_options, default=type_options,
                key=f"types_{cat_value}", label_visibility="collapsed"
            )

    selected_categories = [
        label
        for label in REVERSE_CATEGORY_MAP.keys()
        if st.session_state.get(f"cat_{label}", False)
    ]

    st.divider()
    st.markdown("**동 내 예산 맞춤 추천**")
    if st.session_state.selected_dong:
        st.caption(f"선택 동: {st.session_state.selected_dong}")
        if not st.session_state.dong_search_applied:
            st.caption("예산 입력 후 검색 버튼을 눌러 추천 결과를 확인하세요.")
        elif st.session_state.dong_recommendations.empty:
            st.info("선택한 동에서 예산 조건에 맞는 추천 아파트가 없습니다.")
        else:
            rec_sidebar = st.session_state.dong_recommendations.copy()
            rec_sidebar["평균금액"] = rec_sidebar["평균금액"].apply(format_price_kor)
            rec_sidebar.insert(0, "순위", np.arange(1, len(rec_sidebar) + 1))
            rec_sidebar = rec_sidebar.rename(columns={"name": "아파트명"})
            st.dataframe(
                rec_sidebar[["순위", "아파트명", "평균금액", "C-LCI 점수", "주택유형", "거래유형"]],
                use_container_width=True,
                hide_index=True,
            )
            st.caption(f"지도에는 예산 이하 매물 {len(st.session_state.dong_map_houses):,}개만 표시됩니다.")
    else:
        st.caption("동을 클릭한 뒤 검색 버튼을 누르면 해당 동 기준 추천 아파트 상위 5개가 표시됩니다.")

    st.divider()
    if houses is not None and not houses.empty:
        st.success(f"매물 데이터: {len(houses):,}개")
    else:
        st.error("주택 데이터를 찾을 수 없습니다.")

current_radius = st.session_state.get("main_radius_slider", 1000)
if st.session_state.result:
    r_lat  = st.session_state.result["lat"]
    r_lng  = st.session_state.result["lng"]
    r_dong = st.session_state.result.get("dong", "")
    new_scores = calc_score_cached(r_lat, r_lng, r_dong, radius=current_radius)
    new_total  = calc_total(new_scores, st.session_state.weights)
    st.session_state.result["scores"] = new_scores
    st.session_state.result["display_scores"] = calc_weighted_display_scores(new_scores, st.session_state.weights)
    st.session_state.result["total"]  = new_total
    st.session_state.result["dong_score"] = get_dong_score(r_dong, dong_scores_df, st.session_state.get("selected_gu"), st.session_state.get("selected_sido"))
    st.session_state.result.update(
        get_dong_slope_info(
            st.session_state.get("selected_sido"),
            st.session_state.get("selected_gu"),
            r_dong,
            dong_geojson,
        )
    )
    st.session_state.nearby_houses = get_nearby_houses(r_lat, r_lng, houses, radius=current_radius)

refresh_dong_house_views()

col_addr, col_btn = st.columns([5, 1])
with col_addr:
    address = st.text_input(
        "주소 검색", value=st.session_state.search_address,
        placeholder="예) 서울특별시 강남구 테헤란로 152",
        label_visibility="collapsed",
    )
with col_btn:
    search_btn = st.button("검색", use_container_width=True)

if search_btn:
    st.session_state.search_address = address.strip()
    if not address.strip():
        st.warning("주소를 입력해주세요.")
    else:
        lat, lng = get_coord(address.strip())
        if lat is None:
            st.warning("주소를 찾지 못했어요.")
        else:
            feat = set_view_by_point(lat, lng, dong_geojson)

            if feat is not None:
                dong_name = clean_str(feat["properties"].get("동이름", ""))
                selected_sido = normalize_sido_name(feat["properties"].get("sidonm", ""))
                selected_gu = normalize_gu_name(feat["properties"].get("sggnm", ""))
            else:
                dong_name = clean_str(get_dong(address.strip()))
                selected_sido = st.session_state.get("selected_sido")
                selected_gu = st.session_state.get("selected_gu")
                st.session_state.selected_dong = dong_name

            with st.spinner("점수 계산 중."):
                scores = calc_score_cached(lat, lng, dong_name, radius=current_radius)
                total  = calc_total(scores, st.session_state.weights)
                nearby = get_nearby(lat, lng, facilities, radius=current_radius)
                nearby_houses = get_nearby_houses(lat, lng, houses, radius=current_radius)

                st.session_state.result = {
                    "total": total,
                    "scores": scores,
                    "display_scores": calc_weighted_display_scores(scores, st.session_state.weights),
                    "nearby": nearby,
                    "lat": lat,
                    "lng": lng,
                    "address": get_full_address_from_coord(lat, lng) or address.strip(),
                    "dong": dong_name,
                    "dong_score": get_dong_score(
                        dong_name,
                        dong_scores_df,
                        selected_gu,
                        selected_sido
                    ),
                    **get_dong_slope_info(
                        selected_sido,
                        selected_gu,
                        dong_name,
                        dong_geojson,
                    ),
                }
                st.session_state.nearby_houses = nearby_houses

                st.session_state.dong_search_applied = False
                st.session_state.dong_map_houses = pd.DataFrame()
                st.session_state.dong_recommendations = pd.DataFrame()

            st.rerun()

col_map, col_result = st.columns([3, 1])

with col_result:
    st.subheader("C-LCI 결과")
    if st.session_state.result:
        result = st.session_state.result
        total  = result["total"]
        badge_color = score_to_color(total)
        st.markdown(
            f"""<div style="background:{badge_color};color:#111;border-radius:10px;
                padding:14px 16px;text-align:center;font-size:22px;font-weight:700;
                margin-bottom:10px;">C-LCI {total:.1f}점</div>""",
            unsafe_allow_html=True,
        )
        if result.get("address"): st.caption(f"{result['address']}")
        st.write(f"**행정동**: {result.get('dong', '-')}")
        dong_score = result.get("dong_score")
        st.write(f"**동 점수**: {dong_score:.1f}점" if isinstance(dong_score, (int, float)) else "**동 점수**: -")
        dong_avg_slope = result.get("dong_avg_slope")
        walk_difficulty = result.get("walk_difficulty")
        st.write(
            f"**동 평균 경사**: {dong_avg_slope:.2f}°"
            if isinstance(dong_avg_slope, (int, float)) else "**동 평균 경사**: -"
        )
        st.write(f"**보행난이도**: {walk_difficulty}" if walk_difficulty else "**보행난이도**: -")
        st.divider()
        st.markdown("**카테고리별 점수**")
        category_scores_to_show = result.get("display_scores", result["scores"])
        for k, v in category_scores_to_show.items():
            bar_color = score_to_color(v)
            st.markdown(
                f"""<div style="margin-bottom:10px;">
                  <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px;">
                    <span>{k}</span><span style="font-weight:700;">{v}점</span>
                  </div>
                  <div style="width:100%;height:10px;background:#e5e5e5;border-radius:999px;">
                    <div style="width:{max(0,min(100,v))}%;height:10px;background:{bar_color};border-radius:999px;"></div>
                  </div>
                </div>""",
                unsafe_allow_html=True,
            )
    else:
        if st.session_state.view_mode == "gu":
            st.info("구를 클릭하면\n상세 지도로\n전환됩니다")
        else:
            st.info("동 또는 주택 점을 클릭해\nC-LCI 점수를 확인하세요")

with col_map:
    if st.session_state.view_mode == "gu":
        st.caption("구를 클릭하면 동 단위 지도로 전환됩니다.")
        fig   = make_gu_map(gu_geojson, make_region_key(st.session_state.get("selected_sido"), st.session_state.get("selected_gu")))
        event = st.plotly_chart(fig, use_container_width=True, on_select="rerun", key="gu_map")
        if event and event.get("selection") and event["selection"].get("points"):
            clicked_region = event["selection"]["points"][0].get("location")
            if clicked_region:
                selected_sido, selected_gu = clicked_region.split("__", 1) if "__" in clicked_region else ("", clicked_region)
                st.session_state.selected_sido = selected_sido
                st.session_state.selected_gu   = selected_gu
                st.session_state.selected_dong = None
                st.session_state.view_mode     = "dong"
                st.session_state.dong_map_houses = pd.DataFrame()
                st.session_state.dong_recommendations = pd.DataFrame()
                st.session_state.dong_search_applied = False
                st.rerun()
    else:
        selected_sido = st.session_state.selected_sido
        selected_gu = st.session_state.selected_gu
        st.caption(f"{selected_sido} {selected_gu} — 동 클릭 또는 주택 점 클릭 가능")
        houses_for_map = st.session_state.nearby_houses
        if st.session_state.selected_dong and st.session_state.dong_search_applied:
            houses_for_map = st.session_state.dong_map_houses
        folium_map = make_dong_click_map(
            dong_geojson,
            selected_sido,
            selected_gu,
            st.session_state.result,
            houses_for_map,
            current_radius,
            dong_scores_df,
            selected_categories=selected_categories
)
        if folium_map is not None:
            map_data = st_folium(
                folium_map,
                use_container_width=True,
                height=620,
                key="dong_click_map",
                returned_objects=["last_clicked"],
            )
            clicked = (map_data or {}).get("last_clicked")
            if clicked:
                click_sig = (round(clicked.get("lat", 0.0), 7), round(clicked.get("lng", 0.0), 7))
                if st.session_state.get("last_map_click") != click_sig:
                    st.session_state.last_map_click = click_sig
                    clat, clng = float(clicked["lat"]), float(clicked["lng"])
                    feat = get_dong_feature_by_point(clat, clng, dong_geojson, selected_sido, selected_gu)
                    if feat is not None:
                        prev_sido = st.session_state.get("selected_sido")
                        prev_gu = st.session_state.get("selected_gu")
                        prev_dong = st.session_state.get("selected_dong")
                        prev_applied = bool(st.session_state.get("dong_search_applied", False))

                        dong_name = feat["properties"].get("동이름", "")
                        new_sido = normalize_sido_name(feat["properties"].get("sidonm", ""))
                        new_gu = normalize_gu_name(feat["properties"].get("sggnm", ""))

                        same_dong = (
                            normalize_sido_name(prev_sido) == normalize_sido_name(new_sido)
                            and normalize_gu_name(prev_gu) == normalize_gu_name(new_gu)
                            and clean_str(prev_dong) == clean_str(dong_name)
                        )

                        st.session_state.selected_sido = new_sido
                        st.session_state.selected_gu = new_gu
                        st.session_state.selected_dong = dong_name
                        if not same_dong:
                            st.session_state.dong_map_houses = pd.DataFrame()
                            st.session_state.dong_recommendations = pd.DataFrame()
                            st.session_state.dong_search_applied = False
                        with st.spinner("점수 계산 중..."):
                            scores = calc_score_cached(clat, clng, dong_name, radius=current_radius)
                            total = calc_total(scores, st.session_state.weights)
                            nearby = get_nearby(clat, clng, facilities, radius=current_radius)
                            nearby_houses = get_nearby_houses(clat, clng, houses, radius=current_radius)
                            st.session_state.result = {
                                "total": total, "scores": scores, "display_scores": calc_weighted_display_scores(scores, st.session_state.weights), "nearby": nearby,
                                "lat": clat, "lng": clng, "address": get_full_address_from_coord(clat, clng) or dong_name,
                                "dong": dong_name, "dong_score": get_dong_score(dong_name, dong_scores_df, st.session_state.get("selected_gu"), st.session_state.get("selected_sido")),
                                **get_dong_slope_info(
                                    st.session_state.get("selected_sido"),
                                    st.session_state.get("selected_gu"),
                                    dong_name,
                                    dong_geojson,
                                ),
                            }
                            st.session_state.nearby_houses = nearby_houses
                            if same_dong and prev_applied:
                                refresh_dong_house_views(force=True)
                        st.rerun()
    st.select_slider(
        "분석 반경 설정 (미터)",
        options=[500, 600, 700, 800, 900, 1000],
        value=current_radius,
        key="main_radius_slider",
    )


if st.session_state.selected_sido and st.session_state.selected_gu and not dong_scores_df.empty:
    st.divider()
    region_label = f"{st.session_state.selected_sido} {st.session_state.selected_gu}".strip()
    st.subheader(f"{region_label} 동별 점수 비교")

    dongs_in_gu = list(dict.fromkeys([
        feat["properties"].get("동이름", "").strip()
        for feat in dong_geojson["features"]
        if normalize_sido_name(feat["properties"].get("sidonm", "")) == normalize_sido_name(st.session_state.selected_sido)
        and normalize_gu_name(feat["properties"].get("sggnm", "")) == normalize_gu_name(st.session_state.selected_gu)
    ]))

    chart_df = dong_scores_df[dong_scores_df["행정동"].isin(dongs_in_gu)].copy()
    chart_df = chart_df[chart_df["시군구정규화"] == normalize_gu_name(st.session_state.selected_gu)].copy()
    temp_chart_df = chart_df[chart_df["시도정규화"] == normalize_sido_name(st.session_state.selected_sido)].copy()
    if not temp_chart_df.empty:
        chart_df = temp_chart_df
    chart_df["행정동"] = chart_df["행정동"].astype(str).str.strip()
    chart_df["100점 환산"] = pd.to_numeric(chart_df["100점 환산"], errors="coerce").fillna(0)

    chart_df = chart_df.sort_values(["행정동", "100점 환산"], ascending=[True, False])
    chart_df = chart_df.drop_duplicates(subset=["행정동"], keep="first")
    chart_df = chart_df.sort_values(["100점 환산", "행정동"], ascending=[False, True]).reset_index(drop=True)

    if not chart_df.empty:
        chart_df["행정동"] = chart_df["행정동"].astype(str).str.strip()
        chart_df["100점 환산"] = pd.to_numeric(chart_df["100점 환산"], errors="coerce").fillna(0)
        chart_df = chart_df.sort_values(["100점 환산", "행정동"], ascending=[False, True]).reset_index(drop=True)
        chart_df["구내순위"] = np.arange(1, len(chart_df) + 1)

        if len(chart_df) == 1:
            chart_df["구내색상점수"] = 100.0
        else:
            chart_df["구내색상점수"] = ((len(chart_df) - chart_df["구내순위"]) / (len(chart_df) - 1) * 100).round(1)

        pie_df = pd.DataFrame()
        if child_density_df is not None and not child_density_df.empty:
            pie_df = child_density_df[
                (child_density_df["시군구정규화"] == normalize_gu_name(st.session_state.selected_gu)) &
                ((child_density_df["시도정규화"] == normalize_sido_name(st.session_state.selected_sido)) | (child_density_df["시도정규화"] == "")) &
                (child_density_df["읍면동명"].isin(dongs_in_gu))
            ].copy()

            pie_df = chart_df[["행정동"]].merge(
                pie_df,
                left_on="행정동",
                right_on="읍면동명",
                how="left"
            )

            pie_df["아동밀집도(%)"] = pd.to_numeric(
                pie_df["아동밀집도(%)"], errors="coerce"
            ).fillna(0).round(2)

    

        col_left, col_right = st.columns([1, 1])

        with col_left:
            fig_bar = px.bar(
                chart_df,
                x="행정동",
                y="100점 환산",
                text="100점 환산",
                color="구내색상점수",
                color_continuous_scale=COLOR_SCALE,
                labels={
                    "행정동": "",
                    "100점 환산": "점수",
                    "구내색상점수": f"{region_label} 내 순위"
                },
                hover_data={"구내순위": True, "구내색상점수": False},
            )
            fig_bar.update_traces(texttemplate="%{text:.1f}", textposition="outside")
            fig_bar.update_layout(
                margin=dict(l=0, r=0, t=20, b=0),
                height=350,
                showlegend=False,
                coloraxis_showscale=False,
                xaxis_tickangle=-45,
            )
            st.plotly_chart(fig_bar, use_container_width=True)

        with col_right:
            st.markdown(f"#### {region_label} 동별 아동 밀집도")

            if pie_df.empty or pie_df["아동밀집도(%)"].sum() == 0:
                st.info("아동 밀집도 데이터가 없습니다.")
            else:
                pie_df_sorted = pie_df.sort_values("아동밀집도(%)", ascending=False).copy()

                pie_df_sorted["color_idx"] = range(len(pie_df_sorted))

                fig_rose = px.bar_polar(
                    pie_df_sorted,
                    r="아동밀집도(%)",
                    theta="행정동",
                    color="color_idx", 
                    color_continuous_scale=[
                        "#FFD700", 
                        "#FFE680",
                        "#FFF2B2",
                        "#FFF8D9",
                        "#FFFFFF"    
                    ],
                )

                fig_rose.update_traces(
                    opacity=0.9,
                    hovertemplate=(
                        "<b>%{theta}</b><br>"
                        "아동 밀집도: %{r:.2f}%<extra></extra>"
                    )
                )

                fig_rose.update_layout(
                    height=350,
                    margin=dict(l=20, r=20, t=20, b=90),
                    polar=dict(
                        radialaxis=dict(showticklabels=True, ticks=""),
                        angularaxis=dict(direction="clockwise")
                    ),
                    showlegend=False,
                    coloraxis_showscale=False
                )

                st.plotly_chart(fig_rose, use_container_width=True)

st.divider()
st.subheader(f"반경 {current_radius}m 내 시설 현황")

if st.session_state.result:
    nearby_df = get_nearby(
        st.session_state.result["lat"],
        st.session_state.result["lng"],
        facilities, radius=current_radius,
    )

    if nearby_df.empty:
        st.info("주변 시설 데이터가 없습니다.")
    else:
        summary_df = filter_summary_facilities(nearby_df)
        safety_df = summary_df[summary_df["category"] == "안전"].copy()
        cat_counts = {
            "교육/학군": int((summary_df["category"] == "교육").sum()),
            "놀이/친구": int((summary_df["category"] == "놀이").sum()),
            "안전/치안": int(len(safety_df)),
            "의료/복지": int((summary_df["category"] == "의료복지").sum()),
            "환경/생활": int((summary_df["category"] == "환경생활").sum()),
        }
        c1,c2,c3,c4,c5 = st.columns(5)
        c1.metric("교육/학군", f"{cat_counts['교육/학군']}개")
        c2.metric("놀이/친구", f"{cat_counts['놀이/친구']}개")
        c3.metric("안전/치안", f"{cat_counts['안전/치안']}개")
        c4.metric("의료/복지", f"{cat_counts['의료/복지']}개")
        c5.metric("환경/생활", f"{cat_counts['환경/생활']}개")
        st.caption("※ 안전/치안 시설 현황에서는 교통사고, CCTV, 안전벨을 제외했습니다.")

        rec_df = recommend_houses_near_point(
            st.session_state.result["lat"],
            st.session_state.result["lng"],
            st.session_state.result.get("dong", st.session_state.selected_dong),
            houses,
            st.session_state.weights,
            radius=current_radius,
            budget=st.session_state.budget,
        )

        graph_df = summary_df.copy()
        graph_df = graph_df.groupby(["category", "type"]).size().reset_index(name="개수")
        graph_df["category"] = graph_df["category"].map(to_display_category).fillna(graph_df["category"])

        fig_fac = px.bar(
            graph_df,
            x="category",
            y="개수",
            color="type",
            text="개수",
            category_orders={"category": ["교육/학군", "놀이/친구", "안전/치안", "의료/복지", "환경/생활"]},
            color_discrete_sequence=px.colors.qualitative.Pastel,
        )
        fig_fac.update_layout(
            margin=dict(l=0, r=0, t=40, b=0),
            height=350,
            barmode="stack",
            legend_title_text="type",
        )

        col_left, col_right = st.columns(2)

        with col_left:
            st.plotly_chart(fig_fac, use_container_width=True)

        with col_right:
            st.subheader("가격 vs C-LCI 점수")

            if rec_df.empty:
                st.info("Scatter Plot에 표시할 아파트 데이터가 없습니다.")
            else:
                scatter_df = rec_df.copy()

                price_col = None
                for c in ["평균금액", "가격", "거래금액", "매매가"]:
                    if c in scatter_df.columns:
                        price_col = c
                        break

                score_col = None
                for c in ["C-LCI점수", "C_LCI점수", "C-LCI 점수", "score", "점수"]:
                    if c in scatter_df.columns:
                        score_col = c
                        break

                name_col = None
                for c in ["아파트명", "단지명", "name"]:
                    if c in scatter_df.columns:
                        name_col = c
                        break

                if price_col is None or score_col is None:
                    st.warning("Scatter Plot에 필요한 가격/점수 컬럼을 찾지 못했습니다.")
                else:
                    scatter_df = scatter_df.copy()
                    scatter_df[price_col] = pd.to_numeric(scatter_df[price_col], errors="coerce")
                    scatter_df[score_col] = pd.to_numeric(scatter_df[score_col], errors="coerce")
                    scatter_df = scatter_df.dropna(subset=[price_col, score_col])

                    if scatter_df.empty:
                        st.info("Scatter Plot에 사용할 수 있는 유효한 데이터가 없습니다.")
                    else:
                        scatter_df["가격_표시"] = scatter_df[price_col].apply(format_kor_price)
                        scatter_df["점수_표시"] = scatter_df[score_col].round(1)
                        scatter_df["가격_억"] = scatter_df[price_col] / 100000000

                        fig_scatter = px.scatter(
                            scatter_df,
                            x="가격_억",
                            y=score_col,
                            hover_name=name_col if name_col else None,
                            custom_data=["가격_표시", "점수_표시"],
                        )

                        fig_scatter.update_traces(
                            hovertemplate=(
                                "<b>%{hovertext}</b><br>"
                                "가격: %{customdata[0]}<br>"
                                "C-LCI 점수: %{customdata[1]}<extra></extra>"
                            )
                        )

                        fig_scatter.add_vline(
                            x=scatter_df["가격_억"].mean(),
                            line_dash="dot"
                        )
                        fig_scatter.add_hline(
                            y=scatter_df[score_col].mean(),
                            line_dash="dot"
                        )
                        fig_scatter.update_layout(
                            margin=dict(l=0, r=0, t=40, b=0),
                            height=350,
                            xaxis_title="가격(억 원)",
                            yaxis_title="C-LCI 점수",
                        )                        
                        tick_vals = np.linspace(0, scatter_df["가격_억"].max(), 6)
                        fig_scatter.update_xaxes(
                            tickvals=tick_vals,
                            ticktext=[f"{v:.0f}억" for v in tick_vals]
                        )                        

                        st.plotly_chart(fig_scatter, use_container_width=True)
                        st.caption("좌상단: 가성비 우수 / 우상단: 비싸지만 좋음 / 우하단: 비싼데 아쉬움 / 좌하단: 저렴하지만 환경 아쉬움")

        tab1,tab2,tab3,tab4,tab5 = st.tabs(["교육/학군","놀이/친구","안전/치안","의료/복지","환경/생활"])

        def show_tab(tab, cat_val, label):
            with tab:
                facs = nearby_df[nearby_df["category"] == cat_val].copy()
                
                if cat_val == "안전":
                    facs = facs[
                        (facs["name"].astype(str).str.strip() != "교통사고") &
                        (facs["type"].astype(str).str.strip() != "교통사고")
                    ].copy()
                
                if facs.empty:
                    st.info(f"반경 {current_radius}m 내 {label} 시설 없음")
                else:
                    show_df = facs[["name", "type", "거리(m)"]].rename(
                        columns={"name": "시설명", "type": "종류"}
                    ).copy()

                    show_df["거리(m)"] = pd.to_numeric(show_df["거리(m)"], errors="coerce")

                    def highlight_harmful_row(row):
                        if cat_val == "환경생활" and (
                            str(row["시설명"]).strip() == "유흥업소"
                            or str(row["종류"]).strip() == "유흥업소"
                        ):
                            return ["background-color: #ffcccc"] * len(row)
                        return [""] * len(row)

                    st.dataframe(
                        show_df.style
                            .format({"거리(m)": "{:.1f}"})
                            .apply(highlight_harmful_row, axis=1),
                        use_container_width=True,
                        hide_index=True,
                    )

        show_tab(tab1, "교육",    "교육/학군")
        show_tab(tab2, "놀이",    "놀이/친구")
        show_tab(tab3, "안전",    "안전/치안")
        show_tab(tab4, "의료복지","의료/복지")
        show_tab(tab5, "환경생활","환경/생활")


        st.divider()
        st.subheader(f"선택 지점 반경 {current_radius}m 내 아파트 리스트")

        nearby_house_df = st.session_state.get("nearby_houses", pd.DataFrame())
        if nearby_house_df is None or nearby_house_df.empty:
            st.info("선택 지점 반경 내 아파트가 없습니다.")
        else:
            nearby_show = nearby_house_df.copy()
            nearby_show = nearby_show[[c for c in ["name", "도로명", "평균금액", "주택유형", "거래유형", "거리(m)"] if c in nearby_show.columns]].copy()
            nearby_show = nearby_show.rename(columns={"name": "아파트명"})

            if "평균금액" in nearby_show.columns:
                nearby_show["평균금액"] = pd.to_numeric(nearby_show["평균금액"], errors="coerce")

            if "거리(m)" in nearby_show.columns:
                nearby_show["거리(m)"] = pd.to_numeric(nearby_show["거리(m)"], errors="coerce")
                nearby_show = nearby_show.sort_values("거리(m)", ascending=True)

            styled_nearby_show = nearby_show.style.format({
                "평균금액": lambda x: format_price_kor(x) if pd.notna(x) else "가격미상",
                "거리(m)": lambda x: f"{x:.1f}" if pd.notna(x) else "-"
            })

            st.dataframe(
                styled_nearby_show,
                use_container_width=True,
                hide_index=True,
            )
else:
    st.info("주소 검색 또는 지도 클릭을 해주시길 바랍니다.")