# -*- coding: utf-8 -*-
# app.py
import streamlit as st
from pathlib import Path
from PIL import Image
import uuid, re, os, json, shutil, time
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

from runway_api import generate_video_from_image, extract_video_url
from video_utils import download_video, concat_videos, add_subtitle_to_video, fit_video_to_duration
# TTS 모듈 캐싱 방지 - 항상 최신 코드 로드
import importlib

import tts_core
import tts_module
importlib.reload(tts_core)
importlib.reload(tts_module)

# 2. 함수 위치에 맞춰 Import 분리
# (1) API 호출이 필요한 함수 -> tts_module에서 가져옴
from tts_module import (
    text_to_speech,
)

# (2) 영상/오디오 파일 처리 유틸리티 -> tts_core에서 가져옴
from tts_core import (
    add_audio_to_video,
    concat_videos_with_audio,
    get_audio_duration,
    concat_audio_files,
)

import re
import json
# OpenAI 클라이언트
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

import b_text_based

# --------------------------------
# Streamlit UI 설정
# --------------------------------
st.set_page_config(page_title="AI 숏츠 생성기", layout="wide")

# --------------------------------
# 📱 모바일 반응형 스타일 (768px 이하)
# --------------------------------
st.markdown(
    """
    <style>
    @media (max-width: 768px) {
        /* 기본 본문 padding 축소 — 좁은 화면에서 좌우 여백 줄이기 */
        .main .block-container {
            padding-left: 0.75rem !important;
            padding-right: 0.75rem !important;
            padding-top: 1rem !important;
            max-width: 100% !important;
        }
        /* 본문 글씨 키워서 가독성 확보 */
        html, body, [class*="css"] {
            font-size: 16px !important;
        }
        /* 헤더 크기 살짝 축소 (모바일에선 너무 큼) */
        h1 { font-size: 1.6rem !important; line-height: 1.25 !important; }
        h2 { font-size: 1.35rem !important; line-height: 1.25 !important; }
        h3 { font-size: 1.15rem !important; line-height: 1.3 !important; }
        h4 { font-size: 1.05rem !important; line-height: 1.3 !important; }
        /* 버튼 터치 영역 확대 */
        .stButton > button {
            min-height: 44px !important;
            font-size: 1rem !important;
            padding: 0.6rem 1rem !important;
        }
        /* 입력 위젯 터치 영역 확대 */
        .stTextInput input, .stTextArea textarea, .stSelectbox div[data-baseweb="select"] > div {
            font-size: 1rem !important;
            min-height: 40px !important;
        }
        /* data_editor / dataframe 가로 스크롤 활성화 */
        .stDataFrame, .stDataEditor {
            overflow-x: auto !important;
        }
        /* 이미지가 컬럼을 벗어나지 않도록 */
        .stImage img {
            max-width: 100% !important;
            height: auto !important;
        }
        /* 사이드바 폭 자동 조정 */
        section[data-testid="stSidebar"] {
            min-width: 240px !important;
        }
        /* 라디오·체크박스 항목 간격 살짝 넓힘 (터치 오탑 방지) */
        .stRadio > div { gap: 0.4rem !important; }
    }
    /* rerun 진행 중 이전 콘텐츠가 불투명하게 잔상으로 남는 현상을 숨김.
       — 사용자가 '다음' 버튼을 눌렀을 때 이전 단계 위젯이 페이드 아웃되지 않고
       즉시 사라지도록 처리. */
    [data-stale="true"] { opacity: 0 !important; pointer-events: none !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title(" 동화책 예고편 만들기 ")

# --------------------------------
# 사용자 식별 (워크숍 데이터 수집용)
# --------------------------------
from session_logger import (
    init_session, log_event, render_sidebar_panel,
    log_stage_entry, log_button_click,
)

if "user_name" not in st.session_state:
    st.session_state.user_name = ""
if "session_id" not in st.session_state:
    st.session_state.session_id = ""

if not st.session_state.user_name:
    _name = st.text_input("이름:", placeholder="예: 김민지", key="user_name_input")
    if st.button("✨ 시작하기", type="primary"):
        if _name and _name.strip():
            init_session(_name)
            st.rerun()
        else:
            st.error("이름을 입력해 주세요.")
    st.stop()

# 사이드바: 사용자 정보 + 세션 다운로드 (모든 단계에서 노출)
render_sidebar_panel()

# --------------------------------
# 기본 경로 설정
# --------------------------------
BASE_DIR = Path(__file__).resolve().parent
CHARACTER_DIR = BASE_DIR / "character"
TXT_ROOT = CHARACTER_DIR / "txt"  # 하위에 048/, 049/, 050/ 같은 월령 폴더가 있음


def resolve_txt_path(book_name: str) -> Path:
    """책 이름에서 월령(48개월/49개월/50개월)을 추출해 해당 txt 파일 경로를 반환."""
    m = re.search(r"(\d+)개월", book_name)
    age_folder = f"{int(m.group(1)):03d}" if m else "048"
    return TXT_ROOT / age_folder / f"{book_name}.txt"

# 사용자별 출력 폴더 (모든 결과물이 여기로 저장됨)
from session_logger import get_session_dir
SESSION_DIR = get_session_dir() or Path("outputs")

# --------------------------------
# 🗂 Session State
# --------------------------------
if "loaded_images" not in st.session_state:
    st.session_state.loaded_images = []
if "selected_pages" not in st.session_state:
    st.session_state.selected_pages = []
if "current_book" not in st.session_state:
    st.session_state.current_book = None

# --------------------------------
#  책 선택
# --------------------------------
# character 폴더에서 책 목록 가져오기 (txt/json 폴더 + 표지 책 제외)
book_folders = [f.name for f in CHARACTER_DIR.iterdir()
                if f.is_dir()
                and f.name not in ["txt", "json"]
                and "_표지_" not in f.name]
book_folders = sorted(book_folders)

if not book_folders:
    st.error("character 폴더에 책이 없습니다.")
    st.stop()

def _pretty_book_name(folder: str) -> str:
    """폴더명 '리딩토탈_48개월_내지_헨젤과그레텔_재쇄2_ISBN' → '48_헨젤과그레텔'.
    매칭 실패 시 원본 폴더명 그대로 반환."""
    m = re.search(r"_(\d+)개월_(?:내지|표지)_(.+?)_[^_]+_ISBN$", folder)
    if m:
        return f"{m.group(1)}_{m.group(2)}"
    return folder

log_stage_entry("book_select")
# Wizard step 2+에서는 책 선택 selectbox와 '삽화 로드 완료' 메시지를 숨김.
# selected_book은 session_state의 current_book에서 그대로 유지.
_hide_top_chrome = st.session_state.get("modeA_wizard_step", 1) > 1
if _hide_top_chrome:
    selected_book = st.session_state.get("current_book") or book_folders[0]
else:
    selected_book = st.selectbox(
        "책 선택:",
        book_folders,
        format_func=_pretty_book_name,
    )

# 책이 변경되면 이미지 목록 초기화 + 로깅
if st.session_state.current_book != selected_book:
    st.session_state.current_book = selected_book
    st.session_state.loaded_images = []
    st.session_state.selected_pages = []
    log_event("book_selected", {"book": selected_book})

# 이미지 폴더와 txt 파일 자동 설정
folder = CHARACTER_DIR / selected_book
txt_file = resolve_txt_path(selected_book)

if not folder.exists():
    st.error(f"이미지 폴더를 찾을 수 없습니다: {folder}")
    st.stop()

if not txt_file.exists():
    st.warning(f" txt 파일을 찾을 수 없습니다: {txt_file.name}")

# --------------------------------
# 🖼 삽화 로드 (썸네일로 메모리 절약)
# --------------------------------
THUMBNAIL_SIZE = (200, 200)

@st.cache_data(show_spinner=False)
def load_images(folder_path: str):
    folder = Path(folder_path)
    results = []
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() in {".png", ".jpg", ".jpeg"}:
            img = Image.open(p)
            img.thumbnail(THUMBNAIL_SIZE)  # Resize to thumbnail
            results.append((p.name, img.copy()))
            img.close()
    return results

if not st.session_state.loaded_images:
    st.session_state.loaded_images = load_images(str(folder))

images = st.session_state.loaded_images
if not _hide_top_chrome:
    st.success(f" {len(images)}개의 삽화 로드 완료")


#-----------------
# 0. 작업 방식 선택 — UI 노출 없이 '이미지 선택 기반 제작'으로 고정.
# (텍스트 분석 모드는 그대로 코드에 존재하지만 사용자에게 노출되지 않음)
#------------------
log_stage_entry("mode_select")
mode = "이미지 선택 기반 제작"

# 모드 변경 시 세션 상태 초기화 (필요시)
if "current_mode" not in st.session_state:
    st.session_state.current_mode = mode

if st.session_state.current_mode != mode:
    st.session_state.current_mode = mode
    log_event("mode_selected", {"mode": mode})
    # 모드 간 leak 방지: 한쪽 모드에서 만든 산출물이 다른 모드 화면에 끌려오지 않도록 리셋
    _MODE_STATE_KEYS = (
        # Mode A 산출물
        "step1_scripts", "step2_audio", "step3_final_video", "raw_texts", "proc_uid",
        "mode_a_characters",
        # Mode B 산출물
        "track_b_analysis", "track_b_characters", "track_b_segments", "track_b_step",
        "track_b_audio", "track_b_full_audio", "track_b_matches", "track_b_candidates",
        "track_b_video_results", "track_b_preview_video",
    )
    for _k in _MODE_STATE_KEYS:
        st.session_state.pop(_k, None)
    st.session_state.selected_pages = []
    st.rerun()

#-------------------------------
# A. 기존 이미지 선택 기반 제작
#-----------------------------
if mode == "이미지 선택 기반 제작":
    log_stage_entry("mode_a_entry", {"book": selected_book})

    # =========================================================
    # WIZARD: 단계별 페이지 분리. 현재는 인프라만 추가됨 — 다음 phase에서
    # 각 단계 콘텐츠를 if modeA_step == N으로 감쌈.
    # 1: 이미지 선택 / 2: 캐릭터 보이스 / 3: 장면 편집 / 4: BGM + 최종
    # =========================================================
    MODEA_STEPS = [
        "이미지 선택",
        "캐릭터 보이스",
        "장면 편집",
        "최종 합성",
    ]
    if "modeA_wizard_step" not in st.session_state:
        st.session_state.modeA_wizard_step = 1
    modeA_step = st.session_state.modeA_wizard_step

    def _render_modeA_step_indicator(current: int):
        """상단에 단계 진행도(✅ 완료 / 🟢 진행 중 / ⚪️ 대기) 표시.
        현재 단계 라벨은 밑줄 처리."""
        cols = st.columns(len(MODEA_STEPS))
        for i, label in enumerate(MODEA_STEPS, start=1):
            with cols[i - 1]:
                marker = "✅" if i < current else ("🟢" if i == current else "⚪️")
                if i == current:
                    label_html = f"<u>{label}</u>"
                else:
                    label_html = label
                st.markdown(
                    f"<div style='text-align:center'>{marker}<br>{label_html}</div>",
                    unsafe_allow_html=True,
                )

    def _render_modeA_nav(prev_ok: bool, next_ok: bool, next_label: str = "다음 →"):
        """하단에 [← 이전] [다음 →] 버튼. next_ok=False면 다음 버튼 비활성."""
        st.markdown("---")
        c_prev, c_spacer, c_next = st.columns([1, 2, 1])
        with c_prev:
            if prev_ok and st.button("← 이전", key=f"modeA_nav_prev_{st.session_state.modeA_wizard_step}"):
                st.session_state.modeA_wizard_step -= 1
                log_button_click("modeA_wizard_prev", {"to_step": st.session_state.modeA_wizard_step})
                st.rerun()
        with c_next:
            if next_ok and st.button(next_label, key=f"modeA_nav_next_{st.session_state.modeA_wizard_step}",
                                     type="primary"):
                st.session_state.modeA_wizard_step += 1
                log_button_click("modeA_wizard_next", {"to_step": st.session_state.modeA_wizard_step})
                st.rerun()

    _render_modeA_step_indicator(modeA_step)
    st.divider()

    # --------------------------------
    # TXT 매칭 함수 (삽화 선택과 대본 생성 양쪽에서 사용)
    # --------------------------------
    def extract_text_for_image(page_name: str, txt_path: Path):
        """
        이미지 이름에서 페이지 번호를 추출하고, txt 파일 내 해당 페이지의 텍스트를 반환.
        지원하는 파일명 패턴:
          - page_006.png  → 6
          - ..._#07.png   → 7  (리딩토탈 시리즈)
          - ..._007.png   → 7  (확장자 직전 숫자)
        """
        if not txt_path.exists():
            return ""

        # 페이지 번호 추출 (b_text_based.extract_page_num_from_filename과 동일 패턴)
        page_num = None
        for pat in (r"page_(\d+)", r"#(\d+)", r"(\d+)\.(?:png|jpg|jpeg|webp)$"):
            m = re.search(pat, page_name, re.IGNORECASE)
            if m:
                page_num = int(m.group(1))
                break
        if page_num is None:
            return ""

        txt_content = txt_path.read_text(encoding="utf-8")

        # --- Page N --- 형식에서 해당 페이지 텍스트 추출
        pattern = rf"--- Page {page_num} ---\n(.*?)(?=--- Page \d+ ---|$)"
        match = re.search(pattern, txt_content, re.DOTALL)
        if match:
            return match.group(1).strip()
        return ""

    # --------------------------------
    # 캐릭터 분석 기반 TTS (Mode B의 analyze_characters_and_speakers 결과를 활용)
    # --------------------------------
    # TTS 엔진은 Gemini 2.5 Pro로 고정 (워크숍에서 사용자 선택 옵션 제거).
    st.session_state.mode_a_tts_engine = "gemini-pro"
    MODE_A_TTS_ENGINE = "gemini-pro"

    def _generate_mode_a_audio_with_characters(scripts, characters, dialogue_map, output_dir, uid):
        """각 장면 텍스트를 따옴표 기준으로 분리, dialogue_map으로 화자/톤 식별,
        캐릭터별 voice_label에 매핑된 보이스로 합성 후 concat.
        매칭 안 되는 대사나 narration은 scene의 기본 speaker(narrator)로 fallback.
        dialogue_map 각 항목의 'tone' 필드가 있으면 그 대사 합성 시 style_prompt로 전달."""
        char_voice = {}
        for c in characters or []:
            cid = c.get("id")
            voice_label = c.get("voice_label", "🎙️ 나레이터")
            clova_id = b_text_based.VOICE_PRESETS.get(voice_label) or "njiyun"
            if cid:
                char_voice[cid] = clova_id

        # 사용자가 캐릭터 프로필에서 지정한 나레이터 보이스. _narrator_ 가상 캐릭터의
        # voice_label에서 추출. 없으면 기본 "narrator" 라벨로 폴백.
        narrator_voice = char_voice.get("_narrator_", "narrator")

        # quote -> (speaker_id, tone) 매핑
        quote_to_meta = {}
        for d in dialogue_map or []:
            q = (d.get("quote") or "").strip()
            sid = d.get("speaker_id")
            tone = (d.get("tone") or "").strip()
            if q:
                quote_to_meta[q] = (sid, tone)

        def _lookup_quote(quote):
            """quote에 맞는 (speaker_id, tone) 찾기. exact → substring 순."""
            meta = quote_to_meta.get(quote)
            if meta:
                return meta
            for q, m in quote_to_meta.items():
                if quote in q or q in quote:
                    return m
            return (None, "")

        def _tone_to_prompt(tone):
            if not tone:
                return ""
            return (
                f"Roleplay with a '{tone}' tone. "
                f"Speak the following Korean text naturally with matching emotion."
            )

        output_dir = Path(output_dir)
        audio_paths = []
        quote_pattern = r'[“"]([^“”"]*?)[”"]'

        for i, scene in enumerate(scripts):
            text = (scene.get("text") or "").strip()
            narr_spk = scene.get("speaker", "narrator")
            if not text or narr_spk == "none":
                audio_paths.append(None)
                continue

            # 세그먼트별 보이스 fallback 정책:
            #   - 따옴표 안 대사: dialogue_map에서 캐릭터 찾음 → 못 찾으면 _safe_narr_spk
            #   - 본문 나레이션은 항상 narrator_voice (캐릭터 프로필에서 지정한 보이스)로
            #     읽음. scene.speaker는 "narrator" 또는 "none" 두 값만 들어옴.
            _safe_narr_spk = narrator_voice if narr_spk != "none" else narrator_voice

            # 본문에서 extra_lines 위치를 찾아 chunk 단위로 split. extra chunk는
            # 사용자가 지정한 화자로 강제, 나머지 chunk는 기존 quote 매핑 로직 적용.
            # 같은 텍스트가 본문에 중복 등장하면 첫 등장만 매칭, 겹치는 extra는 무시.
            extras = scene.get("extra_lines") or []
            extra_hits = []
            for ex in extras:
                ex_text = (ex.get("text") or "").strip()
                if not ex_text:
                    continue
                pos = text.find(ex_text)
                if pos < 0:
                    continue
                extra_hits.append({
                    "start": pos,
                    "end": pos + len(ex_text),
                    "text": ex_text,
                    "speaker": (ex.get("speaker") or "narrator") or "narrator",
                    "tone": (ex.get("tone") or "").strip(),
                })
            extra_hits.sort(key=lambda h: h["start"])
            non_overlap_hits = []
            for h in extra_hits:
                if non_overlap_hits and h["start"] < non_overlap_hits[-1]["end"]:
                    continue
                non_overlap_hits.append(h)

            chunks = []
            cursor = 0
            for h in non_overlap_hits:
                if h["start"] > cursor:
                    chunks.append((text[cursor:h["start"]], False, None, ""))
                chunks.append((h["text"], True, h["speaker"], h.get("tone", "")))
                cursor = h["end"]
            if cursor < len(text):
                chunks.append((text[cursor:], False, None, ""))
            if not chunks:
                chunks.append((text, False, None, ""))

            # (text, speaker, style_prompt) 세그먼트 빌드 — chunk 단위
            segments = []
            for chunk_text, is_extra, forced_spk, forced_tone in chunks:
                if is_extra:
                    spk = forced_spk if forced_spk and forced_spk != "none" else _safe_narr_spk
                    # extra에서 "narrator" 라벨 선택했으면 캐릭터 프로필의 나레이터로 통일
                    if spk == "narrator":
                        spk = narrator_voice
                    else:
                        # 캐릭터 id를 골랐다면 char_voice로 매핑, 아니면 라벨 그대로
                        spk = char_voice.get(spk, spk)
                    seg_text = chunk_text.strip()
                    if seg_text:
                        segments.append((seg_text, spk, _tone_to_prompt(forced_tone)))
                    continue

                # 일반 chunk: 따옴표 단위로 다시 split
                last_end = 0
                for m in re.finditer(quote_pattern, chunk_text):
                    if m.start() > last_end:
                        narr = chunk_text[last_end:m.start()].strip()
                        if narr:
                            segments.append((narr, _safe_narr_spk, ""))
                    quote = m.group(1).strip()
                    if quote:
                        spk_id, tone = _lookup_quote(quote)
                        spk = char_voice.get(spk_id, _safe_narr_spk) if spk_id else _safe_narr_spk
                        segments.append((quote, spk, _tone_to_prompt(tone)))
                    last_end = m.end()
                if last_end < len(chunk_text):
                    tail = chunk_text[last_end:].strip()
                    if tail:
                        segments.append((tail, _safe_narr_spk, ""))

            if not segments:
                segments.append((text, _safe_narr_spk, ""))

            out_path = output_dir / f"clip_{i:02d}_{uid}.mp3"

            if len(segments) == 1:
                seg_text, seg_spk, seg_prompt = segments[0]
                ok = text_to_speech(
                    seg_text, str(out_path), speaker=seg_spk,
                    engine=MODE_A_TTS_ENGINE, style_prompt=seg_prompt,
                )
                audio_paths.append(str(out_path) if ok and out_path.exists() else None)
                continue

            temp_paths = []
            for j, (seg_text, seg_spk, seg_prompt) in enumerate(segments):
                # 세그먼트 간 throttle — Gemini quota burst 방지
                if j > 0:
                    time.sleep(0.1)
                tmp = output_dir / f"clip_{i:02d}_{uid}_seg{j:02d}.mp3"
                if text_to_speech(
                    seg_text, str(tmp), speaker=seg_spk,
                    engine=MODE_A_TTS_ENGINE, style_prompt=seg_prompt,
                ) and tmp.exists():
                    temp_paths.append(str(tmp))

            if not temp_paths:
                audio_paths.append(None)
                continue
            if len(temp_paths) == 1:
                shutil.move(temp_paths[0], str(out_path))
            else:
                concat_audio_files(temp_paths, str(out_path))
                for tp in temp_paths:
                    Path(tp).unlink(missing_ok=True)
            audio_paths.append(str(out_path) if out_path.exists() else None)

        return audio_paths

    # =========================================================
    # [SHARED MODE A SETUP] 모든 단계가 공유하는 헬퍼/상태/상수
    # (Wizard 도입 후 step 2 안에 있던 것들을 step 3·4도 쓸 수 있게 위로 끌어올림)
    # =========================================================
    BGM_MAPPING = {
        "리딩토탈_48개월_내지_아기토끼포포의가족_최종 2_ISBN": "19",
        "리딩토탈_48개월_표지_아기토끼포포의가족_QR교체_수정_ISBN": "19",
        "리딩토탈_48개월_내지_헨젤과그레텔_재쇄2_ISBN": "110",
        "리딩토탈_48개월_표지_헨젤과그레텔_재쇄1_ISBN": "110",
    }

    def get_bgm_folder_name(full_name):
        """책 이름으로 BGM 폴더명 찾기 (명시적 매핑 우선, 없으면 유사도 비교)"""
        if full_name in BGM_MAPPING:
            return BGM_MAPPING[full_name]
        from difflib import SequenceMatcher
        bgm_root = BASE_DIR / "BGM"
        if not bgm_root.exists():
            return full_name
        bgm_folders = [f.name for f in bgm_root.iterdir() if f.is_dir()]
        if not bgm_folders:
            return full_name
        normalized_input = full_name.replace(" ", "").replace("_", "")
        for folder in bgm_folders:
            normalized_folder = folder.replace(" ", "").replace("_", "")
            if normalized_folder in normalized_input or normalized_input in normalized_folder:
                return folder
        best_match = None
        best_ratio = 0.0
        for folder in bgm_folders:
            normalized_folder = folder.replace(" ", "").replace("_", "")
            ratio = SequenceMatcher(None, normalized_input, normalized_folder).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = folder
        if best_match and best_ratio >= 0.4:
            return best_match
        return full_name

    bgm_folder_name = get_bgm_folder_name(selected_book)
    BGM_DIR = BASE_DIR / "BGM" / bgm_folder_name

    def get_bgm_for_page(page_name: str, bgm_dir: Path):
        """페이지 이름에서 번호를 추출하여 해당하는 BGM 파일 찾기"""
        if not bgm_dir.exists():
            return None
        match = re.search(r"page_(\d+)", page_name)
        if not match:
            return None
        page_num = int(match.group(1))
        for bgm_file in bgm_dir.iterdir():
            if bgm_file.suffix.lower() not in ['.wav', '.mp3', '.m4a']:
                continue
            bgm_match = re.search(r'_(\d+)P', bgm_file.name)
            if bgm_match and int(bgm_match.group(1)) == page_num:
                return bgm_file
            bgm_match = re.search(r'Page\s*(\d+)', bgm_file.name)
            if bgm_match and int(bgm_match.group(1)) == page_num:
                return bgm_file
        return None

    # Session state 초기화 (모든 step이 공유)
    if "proc_uid" not in st.session_state:
        st.session_state.proc_uid = None
    if "step1_scripts" not in st.session_state:
        st.session_state.step1_scripts = None
    if "step2_audio" not in st.session_state:
        st.session_state.step2_audio = None
    if "step3_final_video" not in st.session_state:
        st.session_state.step3_final_video = None
    if "mode_a_preview_video" not in st.session_state:
        st.session_state.mode_a_preview_video = None
    if "modeA_scene_videos" not in st.session_state:
        st.session_state.modeA_scene_videos = None
    if "mode_a_characters" not in st.session_state:
        st.session_state.mode_a_characters = None

    # 나레이터 가상 캐릭터 (id 고정)
    NARRATOR_ID = "_narrator_"
    def _default_narrator_entry():
        return {
            "id": NARRATOR_ID,
            "name": "🎙️ 나레이터 (전체 나레이션)",
            "voice_type": "narrator",
            "voice_label": "🎙️ 나레이터",
        }

    # 분위기 프롬프트 / 기본값 (사용자가 step 2에서 수정)
    if "modeA_prompt" not in st.session_state:
        st.session_state.modeA_prompt = ""
    PROMPT = st.session_state.modeA_prompt
    DEFAULT_DURATION = 5
    use_bgm = False
    bgm_volume = 0.15

    # --------------------------------
    # [WIZARD STEP 1] 삽화 선택
    # --------------------------------
    if modeA_step == 1:
        st.subheader("사용할 삽화 선택")
        st.success("예고편 생성에 사용할 장면을 개수제한없이 골라주세요.")

        with st.form("select_form"):
            cols = st.columns(4)
            selected = list(st.session_state.selected_pages)

            def _pretty_image_label(file_name: str) -> str:
                """이미지 파일명에서 페이지 번호만 추출. '..._#07.png' → '07'."""
                m = re.search(r"_#(\d+)\.(?:png|jpg|jpeg)", file_name, re.IGNORECASE)
                if m:
                    return m.group(1)
                m = re.search(r"page_(\d+)", file_name, re.IGNORECASE)
                if m:
                    return m.group(1).zfill(2)
                m = re.search(r"_(\d+)\.(?:png|jpg|jpeg)$", file_name, re.IGNORECASE)
                if m:
                    return m.group(1).zfill(2)
                return file_name

            for i, (name, img) in enumerate(images):
                with cols[i % 4]:
                    st.image(img, use_container_width=True)
                    if st.checkbox(_pretty_image_label(name), name in selected, key=f"chk_{name}"):
                        if name not in selected:
                            selected.append(name)
                    else:
                        if name in selected:
                            selected.remove(name)

                    # 페이지 텍스트 미리보기 — 고정 높이 + 내부 스크롤로 그리드 정렬 유지
                    _page_text = extract_text_for_image(name, txt_file)
                    if _page_text:
                        _escaped = (
                            _page_text
                            .replace("&", "&amp;")
                            .replace("<", "&lt;")
                            .replace(">", "&gt;")
                            .replace("\n", "<br>")
                        )
                        st.markdown(
                            f'<div style="height: 110px; overflow-y: auto; padding: 8px 10px; '
                            f'background-color: rgba(250,250,250,0.5); border-radius: 6px; '
                            f'font-size: 0.85em; line-height: 1.45; color: #444; '
                            f'margin-bottom: 12px;">{_escaped}</div>',
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            '<div style="height: 110px; padding: 8px 10px; display: flex; '
                            'align-items: center; justify-content: center; '
                            'background-color: rgba(250,250,250,0.3); border-radius: 6px; '
                            'color: #999; font-style: italic; font-size: 0.85em; '
                            'margin-bottom: 12px;">(그림만 있는 페이지)</div>',
                            unsafe_allow_html=True,
                        )

            if st.form_submit_button(" 선택 확정", type="primary"):
                st.session_state.selected_pages = selected
                log_event("images_selected", {
                    "book": selected_book,
                    "count": len(selected),
                    "pages": selected,
                })

        _has_selection = bool(st.session_state.selected_pages)
        if not _has_selection:
            st.info("아직 선택한 삽화가 없습니다. 선택 확정 후 다음 단계로 이동할 수 있어요.")
        _render_modeA_nav(prev_ok=False, next_ok=_has_selection)
        st.stop()

    # 단계 2+에 들어왔는데 이미지가 비어있는 비정상 상태 가드
    if not st.session_state.selected_pages:
        st.warning("선택한 삽화가 없습니다. 1단계로 돌아가서 선택해 주세요.")
        if st.button("← 1단계로 돌아가기"):
            st.session_state.modeA_wizard_step = 1
            st.rerun()
        st.stop()

    # --------------------------------
    # [WIZARD STEP 2] 분위기·캐릭터 보이스 설정
    # (BGM helpers, session_state init, NARRATOR setup은 SHARED SETUP으로 이동됨)
    # --------------------------------
    if modeA_step == 2:
        st.divider()
        st.subheader("동화책 예고편 분위기 설정")

        PROMPT = st.text_input(
            "동화 예고편의 전체적인 분위기를 자세하게 지시해주세요.",
            value=st.session_state.modeA_prompt,
            key="modeA_prompt_input",
        )
        st.session_state.modeA_prompt = PROMPT

        # mode_a_characters가 None이면 narrator만 있는 dict로 초기화
        if st.session_state.mode_a_characters is None:
            st.session_state.mode_a_characters = {
                "characters": [_default_narrator_entry()],
                "dialogue_map": [],
            }
        else:
            # 분석 결과는 있는데 narrator가 빠져 있으면 첫 행에 삽입
            _chars_list = st.session_state.mode_a_characters.get("characters", []) or []
            if not any(c.get("id") == NARRATOR_ID for c in _chars_list):
                _chars_list.insert(0, _default_narrator_entry())
                st.session_state.mode_a_characters["characters"] = _chars_list
    
        # _has_chars = "narrator 외에 분석된 캐릭터가 있는가" — 자동 분석 트리거 판단용.
        _analyzed_chars = [
            c for c in st.session_state.mode_a_characters.get("characters", [])
            if c.get("id") != NARRATOR_ID
        ]
        _has_chars = bool(_analyzed_chars)
    
        # 목소리 모드 / TTS 엔진은 워크숍 단순화를 위해 고정.
        #   - voice_mode: "캐릭터별 보이스 사용" (분석 결과 활용)
        #   - tts engine: Gemini 2.5 Pro (google-genai 경유, 톤/말투 prompt 반영)
        st.session_state.mode_a_voice_mode_widget = "캐릭터별 보이스 사용 (분석 결과 활용)"
        _selected_chars_mode = True
    
        # 캐릭터 분석 결과가 없으면 자동 분석 트리거 (첫 진입 시 1회).
        _just_switched_to_chars = not _has_chars and not st.session_state.get("mode_a_char_analysis_attempted")
    
        def _merge_analysis_with_narrator(result, existing_narrator=None):
            """캐릭터 분석 결과에 narrator 항목을 첫 행에 보존."""
            chars = result.get("characters", []) or []
            # voice_label 정규화
            for c in chars:
                vt = c.get("voice_type", "narrator")
                c["voice_label"] = b_text_based.GPT_VOICE_TO_UI_LABEL.get(vt, "🎙️ 나레이터")
            # 기존 narrator 보이스 설정 보존
            narrator_entry = existing_narrator or _default_narrator_entry()
            chars = [narrator_entry] + [c for c in chars if c.get("id") != NARRATOR_ID]
            result["characters"] = chars
            return result
    
        # 자동 캐릭터 분석 트리거 (첫 진입 시 1회만)
        if _just_switched_to_chars and not _has_chars:
            st.session_state.mode_a_char_analysis_attempted = True
            full_text = txt_file.read_text(encoding="utf-8") if txt_file.exists() else ""
            if full_text:
                with st.spinner("등장인물 분석 중... (5~15초)"):
                    try:
                        # 분석 전 narrator 보이스 설정 보존
                        _existing_narrator = next(
                            (c for c in st.session_state.mode_a_characters.get("characters", [])
                             if c.get("id") == NARRATOR_ID),
                            None,
                        )
                        result = b_text_based.analyze_characters_and_speakers(client, full_text)
                        result = _merge_analysis_with_narrator(result, _existing_narrator)
                        st.session_state.mode_a_characters = result
                        log_event("modeA_char_analysis_done", {
                            "characters": result.get("characters", []),
                            "dialogue_count": len(result.get("dialogue_map", [])),
                            "trigger": "auto_on_entry",
                        })
                        st.success(f"✅ {len(result.get('characters', [])) - 1}명의 캐릭터를 찾았습니다.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"분석 실패: {e}")
            else:
                st.error("책 txt 파일을 읽을 수 없습니다.")

        # 캐릭터 프로필 — narrator는 항상 있으므로 무조건 표시
        if True:
            chars_data = st.session_state.mode_a_characters.get("characters", [])
            # 안전장치: voice_label이 새 VOICE_PRESETS에 없으면 기본값으로 복구
            for c in chars_data:
                if c.get("voice_label") not in b_text_based.VOICE_PRESETS:
                    c["voice_label"] = b_text_based.GPT_VOICE_TO_UI_LABEL.get(
                        c.get("voice_type", "narrator"), "🎙️ 나레이터"
                    )
    
            st.markdown("##### 캐릭터 목소리 지정")

            # 사용자에게 친화적인 ID 표시(_narrator_ → 해설자, char_01 → 등장인물1)를
            # 별도 컬럼으로 추가. 실제 id 필드는 코드 로직에서 그대로 사용.
            def _friendly_char_id(raw_id: str) -> str:
                if raw_id == NARRATOR_ID:
                    return "해설자"
                m = re.match(r"char_(\d+)", raw_id or "")
                if m:
                    return f"등장인물{int(m.group(1))}"
                return raw_id or ""

            for c in chars_data:
                c["display_id"] = _friendly_char_id(c.get("id", ""))

            edited_chars = st.data_editor(
                chars_data,
                column_order=["display_id", "name", "voice_label"],
                column_config={
                    "display_id": st.column_config.TextColumn("ID", disabled=True, width="small"),
                    "name": st.column_config.TextColumn("이름", width="medium"),
                    "voice_label": st.column_config.SelectboxColumn(
                        "🎙️ 지정 목소리",
                        options=list(b_text_based.VOICE_PRESETS.keys()),
                        width="medium",
                        required=True,
                        help="이 캐릭터의 대사를 읽을 보이스를 선택하세요.",
                    ),
                },
                num_rows="fixed",
                use_container_width=True,
                key="mode_a_char_editor",
            )
            st.session_state.mode_a_characters["characters"] = edited_chars
    
            # 🎧 보이스 미리듣기 — 보이스 종류별로 어떤 느낌인지 한 번에 들어볼 수 있도록.
            # UI 부담을 줄이려고 expander 안에 selectbox + 듣기 버튼 1쌍만 둠.
            with st.expander("목소리 들어보고 고르기", expanded=True):
                vc_col_pick, vc_col_btn = st.columns([3, 1])
                with vc_col_pick:
                    _voice_options = list(b_text_based.VOICE_PRESETS.keys())
                    _picked = st.selectbox(
                        "목소리 들어보기 선택",
                        options=_voice_options,
                        key="mode_a_voice_sample_pick",
                    )
                with vc_col_btn:
                    # selectbox 라벨 높이만큼 spacer를 넣어 버튼이 입력 박스와 같은 줄에
                    # 오도록 정렬. (Streamlit 기본 label height ≈ 1.85rem)
                    st.markdown(
                        '<div style="height:1.85rem"></div>',
                        unsafe_allow_html=True,
                    )
                    _play_clicked = st.button("🎧 듣기", key="mode_a_voice_sample_btn")
    
                if _play_clicked:
                    _clova_id = b_text_based.VOICE_PRESETS.get(_picked) or "njiyun"
                    log_event("modeA_voice_sample_play", {
                        "label": _picked,
                        "voice_id": _clova_id,
                    })
                    _sample_dir = SESSION_DIR / "voice_samples"
                    _sample_dir.mkdir(parents=True, exist_ok=True)
                    _sample_path = _sample_dir / f"sample_{_clova_id}_{MODE_A_TTS_ENGINE}.mp3"
                    if not _sample_path.exists():
                        _sample_text = "안녕하세요. 저는 이런 목소리로 책을 읽어요."
                        with st.spinner("샘플 합성 중..."):
                            _ok = text_to_speech(
                                _sample_text, str(_sample_path),
                                speaker=_clova_id, engine=MODE_A_TTS_ENGINE,
                                style_prompt="",
                            )
                        if not _ok or not _sample_path.exists():
                            st.error("샘플 생성 실패")
                            _sample_path = None
                    if _sample_path and _sample_path.exists():
                        st.session_state["mode_a_voice_sample_path"] = str(_sample_path)
                        st.session_state["mode_a_voice_sample_label"] = _picked
    
                _last_path = st.session_state.get("mode_a_voice_sample_path")
                _last_label = st.session_state.get("mode_a_voice_sample_label")
                if _last_path and os.path.exists(_last_path):
                    st.caption(f"▶️ {_last_label}")
                    st.audio(_last_path)
    
            # 대사별 톤 프롬프트 에디터 - 선택한 페이지의 대사만 노출
            all_dialogues = st.session_state.mode_a_characters.get("dialogue_map", []) or []
            for d in all_dialogues:
                d.setdefault("tone", "")
    
            # 캐릭터 id ↔ 표시명 매핑. 같은 이름의 캐릭터가 둘 이상이면 id를 덧붙여 구분.
            _name_count = {}
            for _c in edited_chars:
                _n = _c.get("name") or _c.get("id") or "?"
                _name_count[_n] = _name_count.get(_n, 0) + 1
    
            def _disp_name(c):
                n = c.get("name") or c.get("id") or "?"
                return f"{n} ({c.get('id', '?')})" if _name_count.get(n, 0) > 1 else n
    
            _char_id_to_disp = {c.get("id"): _disp_name(c) for c in edited_chars}
            _char_disp_to_id = {v: k for k, v in _char_id_to_disp.items()}
            NARRATOR_LABEL = "(narrator)"
            _speaker_options = list(_char_id_to_disp.values()) + [NARRATOR_LABEL]
    
            # 선택한 이미지에서 페이지 번호 추출
            _selected_page_nums = set()
            for _img_name in st.session_state.selected_pages:
                for _pat in (r"page_(\d+)", r"#(\d+)", r"(\d+)\.(?:png|jpg|jpeg|webp)$"):
                    _m = re.search(_pat, _img_name, re.IGNORECASE)
                    if _m:
                        _selected_page_nums.add(int(_m.group(1)))
                        break
    
            # 디스플레이용: quote 기준 dedup. 같은 대사가 여러 페이지에 걸쳐 있으면 한 행으로 합침.
            unique_view = []
            seen_quotes = set()
            for d in all_dialogues:
                if d.get("page_num") not in _selected_page_nums:
                    continue
                quote = (d.get("quote") or "").strip()
                if not quote or quote in seen_quotes:
                    continue
                seen_quotes.add(quote)
                # 이 quote가 선택된 페이지 중 어디에 등장하는지 모두 수집
                pages = sorted({
                    d2.get("page_num") for d2 in all_dialogues
                    if (d2.get("quote") or "").strip() == quote
                    and d2.get("page_num") in _selected_page_nums
                })
                # 기존 톤(같은 quote 항목 중 가장 먼저 채워진 비어있지 않은 값) 가져오기
                existing_tone = ""
                for d2 in all_dialogues:
                    if (d2.get("quote") or "").strip() == quote and (d2.get("tone") or "").strip():
                        existing_tone = d2["tone"]
                        break
                unique_view.append({
                    "pages_display": ", ".join(str(p) for p in pages),
                    "speaker_name": _char_id_to_disp.get(d.get("speaker_id"), NARRATOR_LABEL),
                    "quote": quote,
                    "tone": existing_tone,
                })
    
            if unique_view:
                st.markdown("##### 캐릭터가 말할 대사의 톤을 지시해주세요")
                # dict 리스트를 그대로 넘기면 data_editor가 행을 추적 못 해 가끔
                # 사용자 입력이 사라지는 버그가 있음. DataFrame으로 변환해서
                # 안정된 인덱스를 부여하면 입력값 보존됨.
                import pandas as _pd
                _editor_df = _pd.DataFrame(unique_view)
                edited_df = st.data_editor(
                    _editor_df,
                    column_order=["pages_display", "speaker_name", "quote", "tone"],
                    column_config={
                        "pages_display": st.column_config.TextColumn("Pages", disabled=True, width="small"),
                        "speaker_name": st.column_config.SelectboxColumn(
                            "화자",
                            options=_speaker_options,
                            width="small",
                            required=False,
                            help="한 장면에 여러 명의 대사가 섞여 있으면 여기서 각 대사의 화자를 골라 주세요. 자동 분석이 틀린 경우에도 수정 가능.",
                        ),
                        "quote": st.column_config.TextColumn("대사", disabled=True, width="large"),
                        "tone": st.column_config.TextColumn(
                            "목소리 말투 지시 칸",
                            width="medium",
                            help="예: 흥분된 목소리, 슬프게 흐느끼며, 무서운 분위기로",
                        ),
                    },
                    num_rows="fixed",
                    use_container_width=True,
                    hide_index=True,
                    key="mode_a_dialogue_editor",
                )
                # DataFrame → dict 리스트로 되돌려 기존 머지 로직 재사용.
                edited_view = edited_df.to_dict("records")
                # 편집한 화자/톤을 quote 기준으로 dialogue_map의 모든 동일 항목에 머지.
                # 화자: 표시명 → speaker_id로 역매핑. (narrator)는 빈 id로 저장해 _lookup_quote
                # 단계에서 narrator로 자연스럽게 폴백되게 함.
                _tone_by_quote = {}
                _spk_id_by_quote = {}
                for r in edited_view:
                    q = (r.get("quote") or "").strip()
                    if not q:
                        continue
                    _tone_by_quote[q] = (r.get("tone") or "").strip()
                    disp = (r.get("speaker_name") or "").strip()
                    if disp == NARRATOR_LABEL or not disp:
                        _spk_id_by_quote[q] = ""
                    elif disp in _char_disp_to_id:
                        _spk_id_by_quote[q] = _char_disp_to_id[disp]
                    # 매핑 안 되는 표시명은 dialogue_map의 기존 speaker_id 유지
    
                for d in all_dialogues:
                    q = (d.get("quote") or "").strip()
                    if q in _tone_by_quote:
                        d["tone"] = _tone_by_quote[q]
                    if q in _spk_id_by_quote:
                        d["speaker_id"] = _spk_id_by_quote[q]
                st.session_state.mode_a_characters["dialogue_map"] = all_dialogues
            elif not _selected_page_nums:
                st.caption("📝 위에서 페이지를 먼저 선택하면 그 페이지의 대사가 여기 나옵니다.")
            else:
                st.caption("💬 선택한 페이지에는 따옴표 안 대사가 식별되지 않았어요.")

        # Step 2 navigation
        _render_modeA_nav(prev_ok=True, next_ok=True)
        st.stop()

    # --------------------------------
    # [WIZARD STEP 3] 장면 편집 (텍스트 + 추가대사 + TTS + Runway)
    # --------------------------------
    if modeA_step == 3:
        log_stage_entry("mode_a_step1", {"book": selected_book})

        # =========================================================
        # [STEP 1] 페이지 원문 그대로 자막으로 사용 (Mode A 정책: 원본 텍스트)
        # =========================================================

        def _modeA_generate_step1_scripts():
            """선택된 페이지의 원문 텍스트를 그대로 자막으로 사용. 화자는 narrator 기본값,
            텍스트 없는 페이지는 speaker='none'으로 TTS 스킵. 펼침면 dedupe도 처리."""
            st.session_state.proc_uid = uuid.uuid4().hex[:8]
            log_event("modeA_step1_start", {
                "book": selected_book,
                "prompt": PROMPT,
                "default_duration": DEFAULT_DURATION,
                "use_bgm": use_bgm,
                "bgm_volume": bgm_volume if use_bgm else None,
            })

            subtitle_data = []
            page_texts = []
            prev_text = ""
            for name in st.session_state.selected_pages:
                text = extract_text_for_image(name, txt_file)
                page_texts.append((name, text))
                if not text:
                    subtitle_data.append({"text": "", "speaker": "none", "extra_lines": []})
                elif text == prev_text:
                    subtitle_data.append({"text": "", "speaker": "none", "_dedupe_of_prev": True, "extra_lines": []})
                else:
                    subtitle_data.append({"text": text, "speaker": "narrator", "extra_lines": []})
                    prev_text = text

            st.session_state.raw_texts = page_texts
            st.session_state.step1_scripts = subtitle_data

            try:
                with open(SESSION_DIR / f"modeA_step1_scripts_{st.session_state.proc_uid}.json", "w", encoding="utf-8") as _f:
                    import json as _json
                    _json.dump({
                        "book": selected_book,
                        "raw_texts": page_texts,
                        "initial_scripts": subtitle_data,
                    }, _f, ensure_ascii=False, indent=2)
            except Exception as _e:
                print(f"[log] step1 save failed: {_e}")

            log_event("modeA_step1_done", {
                "scenes": len(subtitle_data),
                "scripts": subtitle_data,
            })

            st.session_state.step2_audio = None

        # 단계 진입 시 step1_scripts가 비어있으면 자동으로 초안 생성 (사용자 클릭 불필요).
        if st.session_state.step1_scripts is None:
            log_button_click("mode_a_step1_auto", {
                "book": selected_book,
                "scene_count": len(st.session_state.selected_pages),
            })
            _modeA_generate_step1_scripts()
            st.rerun()

        # ---------------------------------------------------------
        # [STEP 1.5] 대본 검토 및 수정 UI (1단계 완료 시 표시)
        # ---------------------------------------------------------
        if st.session_state.step1_scripts is not None:
            st.success("선생님께서 동화책 수업을 하실 때 아동에게 질문하고, 상호작용하는 수업과정을 그대로 반영하시면 됩니다.")

            # 수정된 내용을 담을 리스트 (UI 렌더링용이 아니라 실제 데이터 저장용)
            # Streamlit은 위젯 값을 바로 세션에 반영하지 않으므로, form이나 콜백을 쓰거나
            # 아래처럼 화면에 뿌려진 widget의 값을 나중에 읽어와야 합니다.

            with st.expander("선택하신 삽화 페이지와 해당 페이지 동화 내용입니다. 해당 장면의 대사가 예고편에 소리요소로 반영됩니다", expanded=True):
                updated_scripts = []
                
                # 장면별 입력창 표시
                for i, item in enumerate(st.session_state.step1_scripts):
                    img_name = st.session_state.selected_pages[i]

                    st.markdown(f"**장면 {i+1}**")
                    # 펼침면 dedup 안내 (직전 장면과 동일한 텍스트면 자동으로 무음 처리됨)
                    if item.get("_dedupe_of_prev") and not item.get("text"):
                        st.caption("↳ 이전 장면과 동일한 텍스트, 자막·오디오 자동 생략됨 (이미지만 노출). 다른 텍스트로 바꾸려면 아래 입력창에 직접 적어주세요.")
    
                    # 추가 문장(extras) 드롭다운에서 재사용할 보이스 옵션 — 본문은 항상
                    # 캐릭터 프로필의 나레이터로 읽으므로 여기엔 selectbox가 필요 없음.
                    speakers_list = [
                        "narrator", "child_male", "child_female",
                        "adult_male", "adult_female",
                        "elder_male", "elder_female",
                        "young_male", "young_female",
                        "animal", "none",
                    ]
                    # 화자 ID를 화면에 표시할 때 사용할 한글 라벨. 데이터는 영문 ID 그대로 저장.
                    SPEAKER_LABELS_KR = {
                        "narrator": "해설자",
                        "child_male": "남자아이",
                        "child_female": "여자아이",
                        "adult_male": "남자 어른",
                        "adult_female": "여자 어른",
                        "elder_male": "할아버지",
                        "elder_female": "할머니",
                        "young_male": "청년 남자",
                        "young_female": "청년 여자",
                        "animal": "동물",
                        "none": "없음",
                    }
    
                    col_img, col_text = st.columns([1, 4])
    
                    with col_img:
                        img_obj = next((img for n, img in st.session_state.loaded_images if n == img_name), None)
                        if img_obj:
                            st.image(img_obj)
    
                    with col_text:
                        # 텍스트 길이에 맞게 height 동적 계산 — 스크롤 없이 한 화면에 다 보이도록.
                        # 한 줄 약 40자 가정 + 명시적 줄바꿈 합산. 최소 70, 최대 600.
                        _txt = item["text"] or ""
                        _line_count = max(1, _txt.count("\n") + 1 + len(_txt) // 40)
                        _ta_height = max(70, min(600, _line_count * 28 + 20))
                        new_text = st.text_area(
                            label="대사",
                            value=_txt,
                            key=f"script_text_{i}",
                            height=_ta_height,
                        )
    
                    # 🙋 추가 문장 (워크숍 인터랙션) — 본문 안에 적어둔 새 문장을 여기에 다시
                    # 적고 화자만 다르게 지정하면, TTS가 그 부분만 지정 화자로 읽고 나머지는
                    # 원래대로 처리. 자막은 본문 그대로 사용되므로 시각적 흐름은 안 끊김.
                    _existing_extras = item.get("extra_lines", []) or []
                    with st.expander(
                        f"새롭게 추가한 대사를 작성해주세요 — {len(_existing_extras)}개",
                        expanded=True,
                    ):
                        st.caption(
                            "본문에 새로 추가하신 문장을 추가문장 칸에 다시 적고 "
                            "해당 문장을 읽을 화자 목소리를 선택 및 말투를 지시해주세요."
                        )
    
                        for ei, ex in enumerate(_existing_extras):
                            # 1행: 텍스트 + 화자 + 삭제
                            ex_col_text, ex_col_spk, ex_col_del = st.columns([3, 1, 0.5])
                            with ex_col_text:
                                st.text_input(
                                    label=f"추가 문장 #{ei+1}",
                                    value=ex.get("text", ""),
                                    key=f"extra_text_{i}_{ei}",
                                    placeholder='예: 민준아, 어떻게 됐을까?',
                                    help="본문 textarea에 적어둔 문장과 똑같이 적어주세요. 첫 등장 위치에 매칭됩니다.",
                                )
                            with ex_col_spk:
                                _ex_spk = ex.get("speaker", "narrator")
                                _ex_spk_list = list(speakers_list)
                                if _ex_spk not in _ex_spk_list:
                                    _ex_spk_list.append(_ex_spk)
                                st.selectbox(
                                    label="화자",
                                    options=_ex_spk_list,
                                    index=_ex_spk_list.index(_ex_spk),
                                    format_func=lambda v: SPEAKER_LABELS_KR.get(v, v),
                                    key=f"extra_spk_{i}_{ei}",
                                )
                            with ex_col_del:
                                st.markdown("&nbsp;", unsafe_allow_html=True)
                                if st.button("🗑️", key=f"extra_del_{i}_{ei}", help="이 추가 문장 삭제"):
                                    _existing_extras.pop(ei)
                                    st.session_state.step1_scripts[i]["extra_lines"] = _existing_extras
                                    st.rerun()
    
                            # 2행: 톤/말투 지시 (한 줄 전체 사용)
                            st.text_input(
                                label="추가하신 문장을 읽을 목소리 톤을 지시해주세요",
                                value=ex.get("tone", ""),
                                key=f"extra_tone_{i}_{ei}",
                                placeholder="예: 흥분된 목소리, 속삭이듯, 매우 과장되게",
                                help="Gemini Pro 엔진에서 이 prompt가 음성 톤에 반영됩니다.",
                            )
                            # 3행: 미리듣기 버튼 — 톤 지시 칸 아래에 단독 배치.
                            if st.button("🎧 미리듣기", key=f"extra_prev_btn_{i}_{ei}"):
                                _pv_text = (st.session_state.get(f"extra_text_{i}_{ei}") or "").strip()
                                _pv_spk = st.session_state.get(f"extra_spk_{i}_{ei}") or "narrator"
                                _pv_tone = (st.session_state.get(f"extra_tone_{i}_{ei}") or "").strip()
                                if not _pv_text:
                                    st.warning("문장을 먼저 적어주세요.")
                                else:
                                    # 캐릭터 프로필에서 narrator 보이스 추출
                                    _cd = st.session_state.get("mode_a_characters") or {}
                                    _nar = next((c for c in _cd.get("characters", [])
                                                 if c.get("id") == "_narrator_"), None)
                                    _nar_label = _nar.get("voice_label") if _nar else None
                                    _nar_voice = b_text_based.VOICE_PRESETS.get(_nar_label) if _nar_label else None
                                    _resolved_spk = _nar_voice if (_pv_spk == "narrator" and _nar_voice) else _pv_spk
                                    # tone → style_prompt
                                    _style = (
                                        f"Roleplay with a '{_pv_tone}' tone. "
                                        f"Speak the following Korean text naturally with matching emotion."
                                    ) if _pv_tone else ""
                                    # 합성 + 캐시
                                    _prev_dir = SESSION_DIR / f"preview_extras_{st.session_state.proc_uid}"
                                    _prev_dir.mkdir(parents=True, exist_ok=True)
                                    _prev_path = _prev_dir / f"extra_{i:02d}_{ei:02d}.mp3"
                                    with st.spinner("미리듣기 합성 중..."):
                                        _ok = text_to_speech(
                                            _pv_text, str(_prev_path),
                                            speaker=_resolved_spk,
                                            engine=MODE_A_TTS_ENGINE,
                                            style_prompt=_style,
                                        )
                                    if _ok and _prev_path.exists():
                                        st.session_state[f"extra_audio_{i}_{ei}"] = str(_prev_path)
                                        _try_key = f"extra_prev_try_{i}_{ei}"
                                        st.session_state[_try_key] = st.session_state.get(_try_key, 0) + 1
                                        log_event("modeA_extra_preview", {
                                            "scene": i, "extra_idx": ei,
                                            "text": _pv_text,
                                            "speaker": _pv_spk,
                                            "tone": _pv_tone,
                                            "attempt": st.session_state[_try_key],
                                            "success": True,
                                        })
                                    else:
                                        st.error("미리듣기 합성 실패")
                                        log_event("modeA_extra_preview", {
                                            "scene": i, "extra_idx": ei,
                                            "text": _pv_text,
                                            "speaker": _pv_spk,
                                            "tone": _pv_tone,
                                            "success": False,
                                        })
    
                            # 미리듣기 결과 (있으면 표시)
                            _prev_audio = st.session_state.get(f"extra_audio_{i}_{ei}")
                            if _prev_audio and os.path.exists(_prev_audio):
                                st.audio(_prev_audio)
    
                            st.markdown("---")
    
                        if st.button("+ 추가 문장 등록", key=f"extra_add_{i}"):
                            _existing_extras.append({"text": "", "speaker": "narrator", "tone": ""})
                            st.session_state.step1_scripts[i]["extra_lines"] = _existing_extras
                            st.rerun()
    
                    # 🎧 이 장면 단독 TTS — 장면별로 반복 튜닝하며 즉시 들어볼 수 있게.
                    _scene_audio_info = None
                    if st.session_state.step2_audio and i < len(st.session_state.step2_audio):
                        _scene_audio_info = st.session_state.step2_audio[i]
    
                    _has_scene_audio = bool(_scene_audio_info and _scene_audio_info.get("path")
                                            and os.path.exists(_scene_audio_info["path"]))
                    _btn_label = (
                        "전체 목소리 다시 생성해서 확인해보기"
                        if _has_scene_audio
                        else "전체 목소리 생성해서 확인해보기"
                    )
    
                    if st.button(_btn_label, key=f"scene_tts_btn_{i}"):
                        _text = (st.session_state.get(f"script_text_{i}") or "").strip()
                        _orig_text = (item.get("text") or "").strip()
                        _spk = "narrator" if _text else (item.get("speaker") or "none")
                        _scene_extras_out = []
                        for ei in range(len(_existing_extras)):
                            _ex_text = (st.session_state.get(f"extra_text_{i}_{ei}") or "").strip()
                            _ex_spk = st.session_state.get(f"extra_spk_{i}_{ei}") or "narrator"
                            _ex_tone = (st.session_state.get(f"extra_tone_{i}_{ei}") or "").strip()
                            if _ex_text:
                                _scene_extras_out.append({"text": _ex_text, "speaker": _ex_spk, "tone": _ex_tone})
    
                        # 사용자 입력 스냅샷 — TTS 시도 시점에 무조건 기록 (성공/실패 무관)
                        _char_snapshot = [
                            {"id": c.get("id"), "name": c.get("name"),
                             "voice_label": c.get("voice_label")}
                            for c in (st.session_state.get("mode_a_characters") or {}).get("characters", []) or []
                        ]
                        _tts_try_key = f"scene_tts_try_{i}"
                        st.session_state[_tts_try_key] = st.session_state.get(_tts_try_key, 0) + 1
                        log_event("modeA_scene_tts_start", {
                            "scene": i,
                            "text_edited": _text,
                            "text_original": _orig_text,
                            "text_changed": _text != _orig_text,
                            "runway_prompt": st.session_state.get(f"script_rw_prompt_{i}", "").strip(),
                            "extras": _scene_extras_out,
                            "character_voices": _char_snapshot,
                            "attempt": st.session_state[_tts_try_key],
                        })
    
                        _scene_dict = {
                            "text": _text,
                            "speaker": _spk,
                            "runway_prompt": st.session_state.get(f"script_rw_prompt_{i}", "").strip(),
                            "_dedupe_of_prev": item.get("_dedupe_of_prev", False),
                            "extra_lines": _scene_extras_out,
                        }
                        # step1_scripts에도 즉시 반영 → Step 3가 최신 데이터 사용
                        st.session_state.step1_scripts[i] = _scene_dict
    
                        # step2_audio 길이 보정/초기화
                        if (st.session_state.step2_audio is None
                                or len(st.session_state.step2_audio) != len(st.session_state.step1_scripts)):
                            st.session_state.step2_audio = [
                                {"path": None, "duration": None}
                                for _ in range(len(st.session_state.step1_scripts))
                            ]
    
                        if not _text:
                            # 빈 텍스트(dedup·무음 장면)는 TTS 스킵
                            st.session_state.step2_audio[i] = {"path": None, "duration": None}
                            st.rerun()
                        else:
                            _cd = st.session_state.get("mode_a_characters") or {}
                            _chars = _cd.get("characters") or []
                            _dialogues = _cd.get("dialogue_map") or []
    
                            SESSION_DIR.mkdir(parents=True, exist_ok=True)
                            _uid = st.session_state.proc_uid or uuid.uuid4().hex[:8]
                            if not st.session_state.proc_uid:
                                st.session_state.proc_uid = _uid
    
                            with st.spinner(f"장면 {i+1} 음성 합성 중..."):
                                _audio_paths = _generate_mode_a_audio_with_characters(
                                    [_scene_dict], _chars, _dialogues, SESSION_DIR,
                                    f"{_uid}_scene{i:02d}",
                                )
                            if _audio_paths and _audio_paths[0]:
                                _dur = get_audio_duration(_audio_paths[0])
                                st.session_state.step2_audio[i] = {
                                    "path": _audio_paths[0], "duration": _dur,
                                }
                                log_event("modeA_scene_tts_done", {
                                    "scene": i, "duration": _dur,
                                    "extra_count": len(_scene_extras_out),
                                })
                                st.rerun()
                            else:
                                st.error(f"장면 {i+1} TTS 생성 실패")
    
                    if _has_scene_audio:
                        _dur = _scene_audio_info.get("duration") or 0
                        st.caption(f"⏱ {_dur:.1f}초")
                        st.audio(_scene_audio_info["path"])
    
                    # 캐릭터 움직임 설정 — TTS 아래. 그 장면 전용 Runway 프롬프트.
                    with st.expander("캐릭터 움직임 설정", expanded=True):
                        st.caption("해당 삽화를 영상화할 때 적용할 움직임을 지시해주세요.")
                        st.text_input(
                            label="이 장면 움직임 프롬프트",
                            label_visibility="collapsed",
                            value=item.get("runway_prompt", ""),
                            placeholder="예: 남자캐릭터가 활짝 웃으며 성큼성큼 앞으로 걸어나가며 주변을 두리번두리번 살핀다.",
                            key=f"script_rw_prompt_{i}",
                            help="입력하면 이 장면만 이 프롬프트로 영상을 생성합니다.",
                        )
    
                    # 🎬 이 장면 영상 — Runway 단발 호출로 결과 확인 후 마음에 안 들면 재생성.
                    _scene_vid_info = None
                    if (st.session_state.modeA_scene_videos
                            and i < len(st.session_state.modeA_scene_videos)):
                        _scene_vid_info = st.session_state.modeA_scene_videos[i]
                    _has_scene_vid = bool(
                        _scene_vid_info and _scene_vid_info.get("raw_path")
                        and os.path.exists(_scene_vid_info["raw_path"])
                    )
                    _rw_btn_label = (
                        "영상 다시 만들기"
                        if _has_scene_vid
                        else "영상화하기"
                    )
                    if st.button(_rw_btn_label, key=f"scene_runway_btn_{i}"):
                        _scene_rw_prompt = (st.session_state.get(f"script_rw_prompt_{i}") or "").strip()
                        _final_prompt = _scene_rw_prompt or PROMPT
    
                        # 시행 카운터로 같은 장면의 재시도 횟수 추적
                        _rw_try_key = f"scene_runway_try_{i}"
                        st.session_state[_rw_try_key] = st.session_state.get(_rw_try_key, 0) + 1
                        log_event("modeA_scene_runway_start", {
                            "scene": i,
                            "user_prompt": _scene_rw_prompt,
                            "fallback_to_global": not _scene_rw_prompt,
                            "final_prompt": _final_prompt,
                            "attempt": st.session_state[_rw_try_key],
                        })
    
                        # 길이는 TTS 결과 기준 (없으면 DEFAULT_DURATION)
                        _audio_info_for_dur = (
                            st.session_state.step2_audio[i]
                            if (st.session_state.step2_audio and i < len(st.session_state.step2_audio))
                            else None
                        )
                        _tts_dur = _audio_info_for_dur.get("duration") if _audio_info_for_dur else None
                        if _tts_dur is None:
                            _rw_dur = DEFAULT_DURATION
                        elif _tts_dur <= 5.0:
                            _rw_dur = 5
                        else:
                            _rw_dur = 10
    
                        _img_path = folder / img_name
                        _uid = st.session_state.proc_uid or uuid.uuid4().hex[:8]
                        if not st.session_state.proc_uid:
                            st.session_state.proc_uid = _uid
    
                        try:
                            with st.spinner(f"장면 {i+1} 영상 생성 중 (1~3분)..."):
                                _result = generate_video_from_image(str(_img_path), _final_prompt, _rw_dur)
                                _video_url = extract_video_url(_result)
                                SESSION_DIR.mkdir(parents=True, exist_ok=True)
                                _raw_path = SESSION_DIR / f"clip_{i:02d}_{_uid}_raw.mp4"
                                download_video(_video_url, _raw_path)
    
                            # 세션 캐시 업데이트 (길이 보정)
                            if (st.session_state.modeA_scene_videos is None
                                    or len(st.session_state.modeA_scene_videos) != len(st.session_state.step1_scripts)):
                                st.session_state.modeA_scene_videos = [None] * len(st.session_state.step1_scripts)
                            st.session_state.modeA_scene_videos[i] = {
                                "raw_path": str(_raw_path),
                                "prompt_used": _final_prompt,
                                "runway_dur": _rw_dur,
                            }
                            # 그 장면의 runway_prompt도 step1_scripts에 반영
                            st.session_state.step1_scripts[i]["runway_prompt"] = _scene_rw_prompt
                            log_event("modeA_scene_runway_done", {
                                "scene": i, "prompt": _final_prompt, "runway_dur": _rw_dur,
                            })
                            st.rerun()
                        except Exception as e:
                            st.error(f"영상 생성 실패: {e}")
    
                    if _has_scene_vid:
                        st.caption(f"🎬 프롬프트: {_scene_vid_info.get('prompt_used', '')[:80]}")
                        # 영상이 너무 크게 보이지 않도록 가운데 좁은 컬럼에 표시.
                        # 사용자는 플레이어 우하단의 전체화면 버튼으로 크게 볼 수 있음.
                        _svid_l, _svid_m, _svid_r = st.columns([1, 2, 1])
                        with _svid_m:
                            st.video(_scene_vid_info["raw_path"])

                    st.divider()

        # Step 3 navigation — TTS 음성이 하나라도 있으면 다음 단계로
        _has_any_audio = bool(st.session_state.step2_audio) and any(
            (a and a.get("path")) for a in (st.session_state.step2_audio or [])
        )
        if not _has_any_audio:
            st.success("전체 목소리 생성해서 확인해보기 버튼과 영상화하기 버튼을 클릭했는지 확인해주세요")
        _render_modeA_nav(prev_ok=True, next_ok=_has_any_audio)
        st.stop()

    # --------------------------------
    # [WIZARD STEP 4] BGM 설정 + 최종 영상 합성
    # --------------------------------
    if modeA_step == 4:
        log_stage_entry("mode_a_step4", {"book": selected_book})

    # ---------------------------------------------------------
    # [STEP 2] 오디오 검토 (장면별 TTS가 하나라도 생성된 경우)
    # ---------------------------------------------------------
        if st.session_state.step2_audio is not None:
            # 장면별로 이미 위에서 들을 수 있으므로 디폴트는 접힘 — 전체 한눈에 보고 싶을 때만 펼침.
            with st.expander("🎧 음성 한눈에 보기 (전체 장면)", expanded=False):
                for i, audio_info in enumerate(st.session_state.step2_audio):
                    path = audio_info["path"]
                    dur = audio_info["duration"]
                    script = st.session_state.step1_scripts[i]["text"]
    
                    # dur가 None일 경우 0.0으로 표시
                    dur_display = f"{dur:.1f}" if dur is not None else "0.0"
    
                    st.write(f"**장면 {i+1}** ({dur_display}초) : {script}")
    
                    if path and os.path.exists(path):
                        st.audio(path)
                    else:
                        st.caption("🔇 음성 없음 (생성 실패 또는 무음)")
    
            # =========================================================
            # [STEP 3] Runway 영상 생성 및 최종 병합
            # =========================================================
            st.divider()
            st.markdown("#### 🎵 배경음악(BGM) 설정")
    
            use_bgm = st.checkbox("배경음악 사용 (페이지별 자동 매칭)", value=False)
            bgm_volume = 0.15  # default, used if use_bgm is enabled later
            if use_bgm:
                if BGM_DIR.exists():
                    bgm_files = sorted([f.name for f in BGM_DIR.iterdir() if f.suffix.lower() in ['.wav', '.mp3', '.m4a']])
                    if bgm_files:
                        bgm_volume = st.slider("BGM 볼륨 (%):", 5, 50, 15) / 100.0
                        st.info(f"📂 BGM 폴더 발견: {len(bgm_files)}개 파일")
                        st.caption("각 페이지 번호에 맞는 BGM이 자동으로 선택됩니다.")
                    else:
                        st.warning(f"BGM 폴더에 오디오 파일이 없습니다: {BGM_DIR}")
                        use_bgm = False
                else:
                    st.warning(f"BGM 폴더가 없습니다: {BGM_DIR}")
                    use_bgm = False
    
            # =========================================================
            # [STEP 3] 최종 영상 합성 — 장면별로 만든 Runway·TTS·BGM을 한 영상으로 묶음
            # =========================================================
            st.divider()
            st.markdown("#### 3️⃣ 최종 영상 합성")
            log_stage_entry("mode_a_step3", {"book": selected_book})
    
            # 캐시된 장면별 Runway 개수 표시 → 사용자가 크레딧 차감 여부 미리 파악
            _cached_count = 0
            if st.session_state.modeA_scene_videos:
                for _v in st.session_state.modeA_scene_videos:
                    if _v and _v.get("raw_path") and os.path.exists(_v.get("raw_path") or ""):
                        _cached_count += 1
            _total_scenes = len(st.session_state.selected_pages)
            _missing = _total_scenes - _cached_count
    
            if _missing == 0 and _total_scenes > 0:
                st.success(f"✅ 모든 장면({_total_scenes}개) Runway 영상이 준비됨. 합성만 진행되어 크레딧 차감 없음.")
            elif _missing > 0:
                st.warning(
                    f"⚠️ {_total_scenes}개 장면 중 {_missing}개가 아직 Runway 미생성 상태예요. "
                    f"이 버튼을 누르면 미생성 장면만 새로 Runway 호출 (크레딧 차감)."
                )
    
            if st.button("🎬 최종 영상 합성", type="primary"):
                log_button_click("mode_a_step3_compose", {
                    "scene_count": len(st.session_state.selected_pages),
                    "cached_runway": _cached_count,
                    "missing_runway": _missing,
                    "use_bgm": use_bgm,
                })
                uid = st.session_state.proc_uid
                OUT = SESSION_DIR
                video_paths = []
                log_event("modeA_step3_start", {
                    "uid": uid,
                    "scene_count": len(st.session_state.selected_pages),
                    "prompt": PROMPT,
                })
                
                progress_bar = st.progress(0)
                status_text = st.empty()
                total = len(st.session_state.selected_pages)
                
                # 1. 영상 생성
                for i, name in enumerate(st.session_state.selected_pages):
                    status_text.text(f"[{i+1}/{total}] '{name}' 영상 생성 중...")
                    
                    img_path = folder / name
                    tts_dur = st.session_state.step2_audio[i]["duration"]
                    
                    # 길이 결정
                    if tts_dur is None:
                        runway_dur = DEFAULT_DURATION
                    elif tts_dur <= 5.0:
                        runway_dur = 5
                    else:
                        runway_dur = 10
                    
                    # 장면별 Runway 프롬프트 우선, 없으면 글로벌 PROMPT
                    _scene_rw_prompt = ""
                    try:
                        _scene_rw_prompt = (st.session_state.step1_scripts[i].get("runway_prompt") or "").strip()
                    except (IndexError, KeyError, AttributeError):
                        _scene_rw_prompt = ""
                    _runway_prompt = _scene_rw_prompt or PROMPT
    
                    # Step 1.5에서 장면별로 이미 Runway 돌렸으면 그 결과 재사용
                    # (사용자가 마음에 든 버전을 그대로 영상에 적용 — 크레딧 절약).
                    _cached_vid = None
                    if (st.session_state.modeA_scene_videos
                            and i < len(st.session_state.modeA_scene_videos)):
                        _cached_vid = st.session_state.modeA_scene_videos[i]
                    _cached_raw = (
                        _cached_vid.get("raw_path")
                        if _cached_vid and _cached_vid.get("raw_path")
                        else None
                    )
                    _use_cache = bool(_cached_raw and os.path.exists(_cached_raw))
    
                    try:
                        if _use_cache:
                            raw_path = Path(_cached_raw)
                            status_text.text(f"[{i+1}/{total}] '{name}' 캐시된 영상 재사용")
                        else:
                            result = generate_video_from_image(str(img_path), _runway_prompt, runway_dur)
                            video_url = extract_video_url(result)
                            raw_path = OUT / f"clip_{i:02d}_{uid}_raw.mp4"
                            download_video(video_url, raw_path)
    
                        # 영상 길이를 TTS 길이에 정확히 맞춤. TTS가 영상보다 짧으면 trim,
                        # 길면 마지막 프레임 freeze-frame으로 extend (그래야 음성이 안 잘림).
                        out_path = OUT / f"clip_{i:02d}_{uid}.mp4"
                        if tts_dur:
                            fit_video_to_duration(str(raw_path), tts_dur, str(out_path))
                        else:
                            shutil.copy(str(raw_path), str(out_path))
                        video_paths.append(out_path)
                        
                    except Exception as e:
                        st.error(f"영상 생성 실패 ({name}): {e}")
                        video_paths.append(None)
                    
                    progress_bar.progress((i + 1) / total)
                    
                # 2. 합성
                status_text.text("자막 및 오디오 합성 중...")
                final_clips = []
                
                for i, vid in enumerate(video_paths):
                    if vid is None: continue
    
                    sub = st.session_state.step1_scripts[i]["text"]
                    audio = st.session_state.step2_audio[i]["path"]
                    img_name = st.session_state.selected_pages[i]
    
                    # 자막
                    sub_out = str(vid).replace(".mp4", "_sub.mp4")
                    add_subtitle_to_video(str(vid), sub, sub_out, scene_index=i)
    
                    # 오디오 (BGM 포함 - 페이지별 자동 매칭)
                    final_out = sub_out.replace("_sub.mp4", "_audio.mp4")
                    if audio and os.path.exists(audio):
                        # 해당 페이지의 BGM 찾기
                        page_bgm = None
                        if use_bgm:
                            page_bgm = get_bgm_for_page(img_name, BGM_DIR)
    
                        if page_bgm and page_bgm.exists():
                            add_audio_to_video(sub_out, audio, final_out, bgm_path=str(page_bgm), bgm_volume=bgm_volume)
                            st.caption(f"🎵 Scene {i+1} ({img_name}): BGM '{page_bgm.name}' 적용")
                        else:
                            add_audio_to_video(sub_out, audio, final_out)
                            if use_bgm:
                                st.caption(f"⚠️ Scene {i+1} ({img_name}): 매칭되는 BGM 없음")
                    else:
                        shutil.copy(sub_out, final_out)
    
                    final_clips.append(final_out)
                    
                # 3. 최종 병합
                status_text.text("최종 파일 저장 중...")
                final_video = OUT / f"short_final_{uid}.mp4"
                concat_videos_with_audio(final_clips, str(final_video))
    
                progress_bar.progress(100)
                status_text.text("완료!")
    
                # 세션에 저장하여 리런 후에도 유지
                st.session_state.step3_final_video = str(final_video)
                log_event("modeA_step3_done", {
                    "final_video": str(final_video),
                    "scene_count": len(final_clips),
                })
                st.rerun()
    
        # ---------------------------------------------------------
        # [STEP 3.5] 최종 영상 표시 (리런 후에도 유지)
        # ---------------------------------------------------------
        if st.session_state.step3_final_video and os.path.exists(st.session_state.step3_final_video):
            st.success("🎉 모든 작업이 완료되었습니다!")
            # 영상을 너무 크지 않게 가운데 좁은 컬럼에 표시. 사용자는 플레이어 우하단의
            # 전체화면 버튼으로 크게 볼 수 있음.
            _vid_left, _vid_mid, _vid_right = st.columns([1, 2, 1])
            with _vid_mid:
                st.video(st.session_state.step3_final_video)

            with open(st.session_state.step3_final_video, "rb") as f:
                st.download_button(
                    " 최종 영상 다운로드",
                    f,
                    file_name=Path(st.session_state.step3_final_video).name
                )

        # Step 4 navigation — 마지막 단계라 next 없음
        _render_modeA_nav(prev_ok=True, next_ok=False)


#-------------------------------
# B. 텍스트 분석 기반 제작
#-----------------------------
elif mode == "텍스트 분석 기반 예고편 제작":
    b_text_based.run_text_analysis_mode(client, folder, txt_file)

    