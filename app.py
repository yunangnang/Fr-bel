# -*- coding: utf-8 -*-
# app.py
import streamlit as st
from pathlib import Path
from PIL import Image
import uuid, re, os, json, shutil
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

from runway_api import generate_video_from_image, extract_video_url
from video_utils import download_video, concat_videos, add_subtitle_to_video, trim_video_to_duration
# TTS 모듈 캐싱 방지 - 항상 최신 코드 로드
import importlib

import tts_core
import tts_module
importlib.reload(tts_core)
importlib.reload(tts_module)

# 2. 함수 위치에 맞춰 Import 분리
# (1) API 호출이 필요한 함수 -> tts_module에서 가져옴
from tts_module import (
    generate_audio_for_subtitles, 
    text_to_speech  # 필요하다면 추가
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
st.title(" 동화책 예고편 만들기 ")

# --------------------------------
# 사용자 식별 (워크숍 데이터 수집용)
# --------------------------------
from session_logger import init_session, log_event, render_sidebar_panel

if "user_name" not in st.session_state:
    st.session_state.user_name = ""
if "session_id" not in st.session_state:
    st.session_state.session_id = ""

if not st.session_state.user_name:
    st.info("👋 시작 전 이름을 입력해 주세요. 워크숍 데이터가 이 이름으로 저장됩니다.")
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

selected_book = st.selectbox("책 선택:", book_folders)

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
st.success(f" {len(images)}개의 삽화 로드 완료")


#-----------------
# 0. 작업 방식 선택
#------------------
st.divider()
st.subheader("0.작업 방식 선택")

mode = st.radio(
    "어떤 방식으로 영상을 만드시겠습니까?",
    ["이미지 선택 기반 제작", "텍스트 분석 기반 예고편 제작"],
    captions=["내가 고른 삽화에 맞춰 대본을 씁니다.", "전체 내용을 요약해 예고편을 짜고, 어울리는 그림을 AI가 추천합니다."]
)

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
    st.info("🖼️ 마음에 드는 삽화를 먼저 고르면, AI가 이야기를 이어줍니다.")
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
    MODE_A_TTS_ENGINE = "gpt"  # Clova 키 복구 후 "clova"로 변경

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

            # (text, speaker, style_prompt) 세그먼트 빌드
            segments = []
            last_end = 0
            for m in re.finditer(quote_pattern, text):
                if m.start() > last_end:
                    narr = text[last_end:m.start()].strip()
                    if narr:
                        segments.append((narr, narr_spk, ""))
                quote = m.group(1).strip()
                if quote:
                    spk_id, tone = _lookup_quote(quote)
                    spk = char_voice.get(spk_id, narr_spk) if spk_id else narr_spk
                    segments.append((quote, spk, _tone_to_prompt(tone)))
                last_end = m.end()
            if last_end < len(text):
                tail = text[last_end:].strip()
                if tail:
                    segments.append((tail, narr_spk, ""))
            if not segments:
                segments.append((text, narr_spk, ""))

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

    # --------------------------------
    # ① 삽화 선택 (텍스트와 함께)
    # --------------------------------
    st.subheader("① 사용할 삽화 선택")
    st.caption("각 페이지의 그림과 텍스트를 함께 보고 영상에 쓸 장면을 고르세요.")

    with st.form("select_form"):
        cols = st.columns(4)
        selected = list(st.session_state.selected_pages)

        for i, (name, img) in enumerate(images):
            with cols[i % 4]:
                st.image(img, use_container_width=True)
                if st.checkbox(name, name in selected, key=f"chk_{name}"):
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

    # 선택된 이미지 미리보기
    if not st.session_state.selected_pages:
        st.info("아직 선택한 삽화가 없습니다.")
        st.stop()

    # --------------------------------
    # ② 영상 옵션 설정
    # --------------------------------
    st.divider()
    st.subheader("② Runway 프롬프트 & 길이")

    PROMPT = st.text_input(" 스타일 프롬프트:", "gentle cinematic movement, children's book illustration")
    DEFAULT_DURATION = st.slider("🖼 자막 없는 장면 기본 길이(초):", 3, 5, 5)
    st.info("💡 자막 있는 장면은 TTS 음성 길이에 맞춰 자동 조절됩니다.")

    # BGM 기본값 (실제 위젯은 Step 3 위에서 렌더됨)
    use_bgm = False
    bgm_volume = 0.15

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

        # 1차: 부분 문자열 매칭
        for folder in bgm_folders:
            normalized_folder = folder.replace(" ", "").replace("_", "")
            if normalized_folder in normalized_input or normalized_input in normalized_folder:
                return folder

        # 2차: 유사도 비교
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

        # page_006.png -> 6
        match = re.search(r"page_(\d+)", page_name)
        if not match:
            return None
        page_num = int(match.group(1))

        # BGM 폴더에서 해당 페이지 번호의 파일 찾기
        for bgm_file in bgm_dir.iterdir():
            if bgm_file.suffix.lower() not in ['.wav', '.mp3', '.m4a']:
                continue
            # 패턴1: _숫자P (예: _11P수정.wav)
            bgm_match = re.search(r'_(\d+)P', bgm_file.name)
            if bgm_match and int(bgm_match.group(1)) == page_num:
                return bgm_file
            # 패턴2: Page 숫자 (예: _Page 11.m4a)
            bgm_match = re.search(r'Page\s*(\d+)', bgm_file.name)
            if bgm_match and int(bgm_match.group(1)) == page_num:
                return bgm_file

        return None

    # --------------------------------
    # 🗂 Session State (3단계 데이터 저장용)
    # --------------------------------
    # 단계별 데이터를 저장할 공간을 초기화합니다.
    if "proc_uid" not in st.session_state:
        st.session_state.proc_uid = None      # 전체 프로세스 공유 ID
    if "step1_scripts" not in st.session_state:
        st.session_state.step1_scripts = None # 대본 데이터 [{"text":..., "speaker":...}]
    if "step2_audio" not in st.session_state:
        st.session_state.step2_audio = None   # 오디오 경로 및 길이 [{"path":..., "duration":...}]
    if "step3_final_video" not in st.session_state:
        st.session_state.step3_final_video = None  # 최종 영상 경로
    if "mode_a_characters" not in st.session_state:
        st.session_state.mode_a_characters = None  # {"characters":[...], "dialogue_map":[...]}

    _has_chars = bool((st.session_state.mode_a_characters or {}).get("characters"))

    # --------------------------------
    # 🎙️ TTS 보이스 모드 (큰 결정: 어떤 결과를 원하는지)
    # --------------------------------
    st.divider()
    st.markdown("#### 🎙️ TTS 보이스 모드")
    _voice_mode_opts = [
        "캐릭터별 보이스 사용 (분석 결과 활용)",
        "단일 narrator로 모두 읽기",
    ]
    # 위젯 key가 없으면 기본값으로 초기화. index= 인자는 일부러 빼서 충돌 방지.
    if st.session_state.get("mode_a_voice_mode_widget") not in _voice_mode_opts:
        st.session_state.mode_a_voice_mode_widget = _voice_mode_opts[1]

    st.radio(
        "어떤 방식으로 TTS를 합성할까요?",
        _voice_mode_opts,
        horizontal=True,
        key="mode_a_voice_mode_widget",
    )

    _selected_chars_mode = st.session_state.mode_a_voice_mode_widget.startswith("캐릭터별")
    if _selected_chars_mode and not _has_chars:
        st.warning("⚠️ 아래 🎭 캐릭터 분석을 실행해야 적용됩니다. 분석을 안 하면 자동으로 단일 narrator로 합성됩니다.")
    elif _selected_chars_mode and _has_chars:
        st.caption("✅ 분석된 캐릭터 정보로 따옴표 안 대사를 각자 다른 보이스로 합성합니다.")
    else:
        st.caption("🔇 모든 텍스트를 단일 narrator 보이스로 합성합니다. (캐릭터 분석 결과를 무시)")

    # --------------------------------
    # 🎭 캐릭터 분석 (선택)
    # --------------------------------
    st.divider()
    st.markdown("#### 🎭 캐릭터 분석 (선택)")
    st.caption(
        "실행하면 책 속 등장인물을 AI가 분석해, 따옴표 안 대사를 캐릭터별 다른 보이스로 합성합니다. "
        "실행 안 하면 모든 텍스트를 단일 narrator로 읽습니다."
    )

    btn_label = "🔄 캐릭터 다시 분석" if _has_chars else "🎭 캐릭터 분석 실행"
    btn_type = "secondary" if _has_chars else "primary"

    if st.button(btn_label, key="mode_a_char_analysis_btn", type=btn_type):
        full_text = txt_file.read_text(encoding="utf-8") if txt_file.exists() else ""
        if not full_text:
            st.error("책 txt 파일을 읽을 수 없습니다.")
        else:
            with st.spinner("등장인물 분석 중... (5~15초)"):
                try:
                    result = b_text_based.analyze_characters_and_speakers(client, full_text)
                    for c in result.get("characters", []):
                        vt = c.get("voice_type", "narrator")
                        c["voice_label"] = b_text_based.GPT_VOICE_TO_UI_LABEL.get(vt, "🎙️ 나레이터")
                    st.session_state.mode_a_characters = result
                    log_event("modeA_char_analysis_done", {
                        "characters": result.get("characters", []),
                        "dialogue_count": len(result.get("dialogue_map", [])),
                    })
                    st.success(f"✅ {len(result.get('characters', []))}명의 캐릭터를 찾았습니다.")
                    st.rerun()
                except Exception as e:
                    st.error(f"분석 실패: {e}")

    if _has_chars:
        chars_data = st.session_state.mode_a_characters.get("characters", [])
        # 안전장치: voice_label이 새 VOICE_PRESETS에 없으면 기본값으로 복구
        for c in chars_data:
            if c.get("voice_label") not in b_text_based.VOICE_PRESETS:
                c["voice_label"] = b_text_based.GPT_VOICE_TO_UI_LABEL.get(
                    c.get("voice_type", "narrator"), "🎙️ 나레이터"
                )

        st.markdown("##### 🎙️ 캐릭터 프로필 (목소리 지정)")
        edited_chars = st.data_editor(
            chars_data,
            column_order=["id", "name", "voice_label"],
            column_config={
                "id": st.column_config.TextColumn("ID", disabled=True, width="small"),
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

        # 대사별 톤 프롬프트 에디터 - 선택한 페이지의 대사만 노출
        all_dialogues = st.session_state.mode_a_characters.get("dialogue_map", []) or []
        for d in all_dialogues:
            d.setdefault("tone", "")

        # 선택한 이미지에서 페이지 번호 추출
        _name_by_id = {c.get("id"): c.get("name", "") for c in edited_chars}
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
                "speaker_name": _name_by_id.get(d.get("speaker_id"), d.get("speaker_id", "")),
                "quote": quote,
                "tone": existing_tone,
            })

        if unique_view:
            st.markdown("##### 💬 대사별 톤/말투 지시 (선택)")
            edited_view = st.data_editor(
                unique_view,
                column_order=["pages_display", "speaker_name", "quote", "tone"],
                column_config={
                    "pages_display": st.column_config.TextColumn("Pages", disabled=True, width="small"),
                    "speaker_name": st.column_config.TextColumn("화자", disabled=True, width="small"),
                    "quote": st.column_config.TextColumn("대사", disabled=True, width="large"),
                    "tone": st.column_config.TextColumn(
                        "톤/말투 지시 (선택)",
                        width="medium",
                        help="예: 흥분된 목소리, 슬프게 흐느끼며, 무서운 분위기로",
                    ),
                },
                num_rows="fixed",
                use_container_width=True,
                hide_index=True,
                key="mode_a_dialogue_editor",
            )
            # 편집한 톤을 quote 기준으로 dialogue_map의 모든 동일 항목에 머지
            _tone_by_quote = {
                (r.get("quote") or "").strip(): (r.get("tone") or "").strip()
                for r in edited_view
            }
            for d in all_dialogues:
                q = (d.get("quote") or "").strip()
                if q in _tone_by_quote:
                    d["tone"] = _tone_by_quote[q]
            st.session_state.mode_a_characters["dialogue_map"] = all_dialogues
        elif not _selected_page_nums:
            st.caption("📝 위에서 페이지를 먼저 선택하면 그 페이지의 대사가 여기 나옵니다.")
        else:
            st.caption("💬 선택한 페이지에는 따옴표 안 대사가 식별되지 않았어요.")

    # --------------------------------
    # ③ 실행 파트 (3단계 프로세스)
    # --------------------------------
    st.divider()
    st.subheader("③ 생성 프로세스")

    # =========================================================
    # [STEP 1] 페이지 원문 그대로 자막으로 사용 (Mode A 정책: 원본 텍스트)
    # =========================================================
    st.markdown("#### 1️⃣ 자막 불러오기 및 수정")

    if st.button("1단계: 페이지 원문으로 자막 만들기", type="primary"):
        st.session_state.proc_uid = uuid.uuid4().hex[:8]
        log_event("modeA_step1_start", {
            "book": selected_book,
            "prompt": PROMPT,
            "default_duration": DEFAULT_DURATION,
            "use_bgm": use_bgm,
            "bgm_volume": bgm_volume if use_bgm else None,
        })

        # 선택된 페이지의 원문 텍스트를 그대로 자막으로 사용. 화자는 narrator 기본값,
        # 텍스트 없는 페이지는 speaker="none"으로 TTS 스킵.
        # 펼침면 dedupe: 직전 장면과 텍스트가 같으면 자막+오디오 모두 비움 (이미지만 노출).
        subtitle_data = []
        page_texts = []
        prev_text = ""
        for name in st.session_state.selected_pages:
            text = extract_text_for_image(name, txt_file)
            page_texts.append((name, text))
            if not text:
                subtitle_data.append({"text": "", "speaker": "none"})
            elif text == prev_text:
                # 직전 장면과 동일 → dedup. 이미지만 보여주고 자막/오디오는 생략.
                subtitle_data.append({"text": "", "speaker": "none", "_dedupe_of_prev": True})
            else:
                subtitle_data.append({"text": text, "speaker": "narrator"})
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
        st.rerun()

    # ---------------------------------------------------------
    # [STEP 1.5] 대본 검토 및 수정 UI (1단계 완료 시 표시)
    # ---------------------------------------------------------
    if st.session_state.step1_scripts is not None:
        st.success("✅ 대본 초안이 생성되었습니다. 내용을 수정하고 2단계로 넘어가세요.")
        
        # 수정된 내용을 담을 리스트 (UI 렌더링용이 아니라 실제 데이터 저장용)
        # Streamlit은 위젯 값을 바로 세션에 반영하지 않으므로, form이나 콜백을 쓰거나
        # 아래처럼 화면에 뿌려진 widget의 값을 나중에 읽어와야 합니다.
        
        with st.expander("📝 대본 수정하기 (여기를 펼쳐서 내용을 확인하세요)", expanded=True):
            updated_scripts = []
            
            # 장면별 입력창 표시
            for i, item in enumerate(st.session_state.step1_scripts):
                img_name = st.session_state.selected_pages[i]
                
                st.markdown(f"**장면 {i+1}: {img_name}**")
                # 펼침면 dedup 안내 (직전 장면과 동일한 텍스트면 자동으로 무음 처리됨)
                if item.get("_dedupe_of_prev") and not item.get("text"):
                    st.caption("↳ 이전 장면과 동일한 텍스트, 자막·오디오 자동 생략됨 (이미지만 노출). 다른 텍스트로 바꾸려면 아래 입력창에 직접 적어주세요.")
                col_img, col_text, col_spk = st.columns([1, 3, 1])
                
                with col_img:
                    # 썸네일 표시
                    img_obj = next((img for n, img in st.session_state.loaded_images if n == img_name), None)
                    if img_obj: st.image(img_obj)
                
                with col_text:
                    # 텍스트 수정 (key를 지정하여 값을 유지)
                    new_text = st.text_area(
                        label="대사 (Subtitle)",
                        value=item["text"],
                        key=f"script_text_{i}",
                        height=70
                    )
                
                with col_spk:
                    # 화자 수정
                    speakers_list = ["narrator", "child_male", "child_female", "adult_male", "adult_female", "elder_male", "elder_female", "young_male", "young_female", "animal", "none"]

                    # 기존 화자가 목록에 없으면 추가
                    current_spk = item["speaker"]
                    if current_spk not in speakers_list:
                        speakers_list.append(current_spk)

                    new_speaker = st.selectbox(
                        label="화자 (Speaker)",
                        options=speakers_list,
                        index=speakers_list.index(current_spk),
                        key=f"script_spk_{i}"
                    )

                with st.expander("🎬 캐릭터 움직임 설정", expanded=False):
                    st.text_input(
                        label="이 장면에만 적용할 프롬프트",
                        value=item.get("runway_prompt", ""),
                        placeholder="비우면 ② 글로벌 프롬프트 사용. 예: child runs through dark forest, scared",
                        key=f"script_rw_prompt_{i}",
                        help="입력하면 이 장면만 이 프롬프트로 Runway 영상을 생성합니다.",
                    )

                st.divider()

        # =========================================================
        # [STEP 2] TTS 음성 생성
        # =========================================================
        st.markdown("#### 2️⃣ TTS 음성 생성 및 미리듣기")
        
        if st.button("2단계: 수정된 대본으로 TTS 생성", type="primary"):
            OUT = SESSION_DIR; OUT.mkdir(parents=True, exist_ok=True)
            uid = st.session_state.proc_uid

            # 수정 전 대본 (diff 분석용)
            gpt_initial = list(st.session_state.step1_scripts)

            # UI 입력값(수정된 값)을 읽어서 리스트 재구성
            final_scripts = []
            for i in range(len(st.session_state.step1_scripts)):
                _prev = st.session_state.step1_scripts[i]
                final_scripts.append({
                    "text": st.session_state[f"script_text_{i}"],
                    "speaker": st.session_state[f"script_spk_{i}"],
                    "runway_prompt": st.session_state.get(f"script_rw_prompt_{i}", "").strip(),
                    # dedup 플래그는 보존(이후 텍스트가 비어있고 플래그가 있으면 그대로 무음)
                    "_dedupe_of_prev": _prev.get("_dedupe_of_prev", False),
                })

            # 수정된 대본 업데이트
            st.session_state.step1_scripts = final_scripts

            log_event("modeA_step2_start", {
                "gpt_initial_scripts": gpt_initial,
                "edited_scripts": final_scripts,
            })

            st.info("🎙️ TTS 음성을 생성하고 길이를 측정합니다...")

            # 보이스 모드(라디오)에 따라 합성 방식 결정
            _char_data = st.session_state.get("mode_a_characters") or {}
            _chars = _char_data.get("characters") or []
            _dialogues = _char_data.get("dialogue_map") or []
            _voice_mode = st.session_state.get("mode_a_voice_mode_widget", "")
            _use_chars = bool(_chars) and _voice_mode.startswith("캐릭터별")

            if _use_chars:
                audio_paths = _generate_mode_a_audio_with_characters(
                    final_scripts, _chars, _dialogues, OUT, uid
                )
            else:
                texts = [s["text"] for s in final_scripts]
                speakers = [s["speaker"] for s in final_scripts]
                audio_paths = generate_audio_for_subtitles(
                    texts, OUT, uid, speakers=speakers, engine=MODE_A_TTS_ENGINE
                )
            
            # 길이 측정
            audio_data = []
            for path in audio_paths:
                if path and Path(path).exists():
                    dur = get_audio_duration(str(path))
                    audio_data.append({"path": path, "duration": dur})
                else:
                    audio_data.append({"path": None, "duration": None})
                    
            st.session_state.step2_audio = audio_data
            log_event("modeA_step2_done", {
                "durations": [d["duration"] for d in audio_data],
                "files": [Path(d["path"]).name if d["path"] else None for d in audio_data],
            })
            st.rerun()

    # ---------------------------------------------------------
    # [STEP 2.5] 오디오 검토 UI (수정된 코드)
    # ---------------------------------------------------------
    if st.session_state.step2_audio is not None:
        st.success(" 음성 생성이 완료되었습니다. 들어보고 이상 없으면 영상을 생성하세요.")
        
        with st.expander("🎧 음성 미리듣기", expanded=True):
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

        st.divider()
        st.markdown("#### 3️⃣ Runway 영상 생성 (최종)")
        st.warning(" 이 버튼을 누르면 Runway 크레딧이 차감됩니다!")

        if st.button("3단계: Runway 영상 생성 및 합치기", type="primary"):
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

                # Runway 호출
                try:
                    result = generate_video_from_image(str(img_path), _runway_prompt, runway_dur)
                    video_url = extract_video_url(result)
                    
                    raw_path = OUT / f"clip_{i:02d}_{uid}_raw.mp4"
                    download_video(video_url, raw_path)
                    
                    # 자르기
                    out_path = OUT / f"clip_{i:02d}_{uid}.mp4"
                    if tts_dur and tts_dur < runway_dur:
                        trim_video_to_duration(str(raw_path), tts_dur, str(out_path))
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
        st.video(st.session_state.step3_final_video)

        with open(st.session_state.step3_final_video, "rb") as f:
            st.download_button(
                " 최종 영상 다운로드",
                f,
                file_name=Path(st.session_state.step3_final_video).name
            )


#-------------------------------
# B. 텍스트 분석 기반 제작
#-----------------------------
elif mode == "텍스트 분석 기반 예고편 제작":
    b_text_based.run_text_analysis_mode(client, folder, txt_file)

    