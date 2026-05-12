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
    get_audio_duration
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
TXT_DIR = CHARACTER_DIR / "txt" / "048"

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
# character 폴더에서 책 목록 가져오기 (txt, json 폴더 제외)
book_folders = [f.name for f in CHARACTER_DIR.iterdir()
                if f.is_dir() and f.name not in ["txt", "json"]]
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
txt_file = TXT_DIR / f"{selected_book}.txt"

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
    ["(기존) 이미지 선택 기반 제작", "(신규) 텍스트 분석 기반 예고편 제작"],
    captions=["내가 고른 삽화에 맞춰 대본을 씁니다.", "전체 내용을 요약해 예고편을 짜고, 어울리는 그림을 AI가 추천합니다."]
)

# 모드 변경 시 세션 상태 초기화 (필요시)
if "current_mode" not in st.session_state:
    st.session_state.current_mode = mode

if st.session_state.current_mode != mode:
    st.session_state.current_mode = mode
    log_event("mode_selected", {"mode": mode})
    # 여기에 모드 변경 시 초기화할 변수들 리셋 (예: loaded_images 등)
    st.session_state.selected_pages = []
    st.rerun()

#-------------------------------
# A. 기존 이미지 선택 기반 제작
#-----------------------------
if mode == "(기존) 이미지 선택 기반 제작":
    st.info("🖼️ 마음에 드는 삽화를 먼저 고르면, AI가 이야기를 이어줍니다.")
    # --------------------------------
    # ① 삽화 선택
    # --------------------------------
    st.subheader("① 사용할 삽화 선택")

    with st.form("select_form"):
        cols = st.columns(6)
        selected = list(st.session_state.selected_pages)

        for i, (name, img) in enumerate(images):
            with cols[i % 6]:
                st.image(img, use_container_width=True)
                if st.checkbox(name, name in selected, key=f"chk_{name}"):
                    if name not in selected:
                        selected.append(name)
                else:
                    if name in selected:
                        selected.remove(name)

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

    # --------------------------------
    # 🎵 배경음악(BGM) 설정
    # --------------------------------
    st.divider()
    st.subheader("🎵 배경음악(BGM) 설정")

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

    use_bgm = st.checkbox("배경음악 사용 (페이지별 자동 매칭)", value=False)
    bgm_volume = 0.15

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

    # --------------------------------
    # TXT 매칭 함수
    # --------------------------------
    def extract_text_for_image(page_name: str, txt_path: Path):
        """
        이미지 이름(예: page_006.png)에서 페이지 번호를 추출하고,
        txt 파일 내에서 해당 페이지(--- Page 6 ---)의 텍스트를 반환
        """
        if not txt_path.exists():
            return ""

        m = re.search(r"page_(\d+)", page_name)
        if not m:
            return ""
        page_num = int(m.group(1))  # "006" -> 6

        txt_content = txt_path.read_text(encoding="utf-8")

        # --- Page N --- 형식에서 해당 페이지 텍스트 추출
        pattern = rf"--- Page {page_num} ---\n(.*?)(?=--- Page \d+ ---|$)"
        match = re.search(pattern, txt_content, re.DOTALL)
        if match:
            return match.group(1).strip()
        return ""


    # --------------------------------
    # OpenAI로 예고편 자막 + 화자 생성
    # --------------------------------
    def generate_trailer_subtitles_with_speakers(page_texts: list[tuple[str, str]], duration_per_clip: int):
        """
        각 페이지의 텍스트를 받아서 예고편 스타일 자막 + 화자 생성
        GPT가 문맥을 이해하고 적절한 화자를 배정함

        Args:
            page_texts: [(page_name, text), ...]
            duration_per_clip: 각 클립의 길이(초)

        Returns:
            list of {"text": str, "speaker": str}
        """
        # 페이지별 내용 정리
        content_info = "\n".join([
            f"[장면 {i+1} - {name}]: {text[:200]}..."
            if text and len(text) > 200
            else f"[장면 {i+1} - {name}]: {text}" if text
            else f"[장면 {i+1} - {name}]: (그림만 있는 페이지 - 자막 불필요)"
            for i, (name, text) in enumerate(page_texts)
        ])

        num_scenes = len(page_texts)

        prompt = f"""당신은 동화책 예고편 전문 작가입니다. 시청자가 "이 책 보고 싶다!"고 느끼게 만드세요.

    ## 장면 정보 ({num_scenes}개)
    {content_info}

    ---

    ## 예고편 자막 작성 규칙

    ### 1. 기본 규칙
    - 정확히 **{num_scenes}개** 자막 (장면당 1개, 순서 유지!)
    - **그림만 있는 페이지** → text: "", speaker: "none"
    - 자막 길이: **35~60자** (풍성하고 생동감 있게!)
    - 말투: 동화책 읽어주는 느낌 (~했어요, ~이었답니다, ~였지요)

    ### 2.  절대 금지 - 결말 스포일러!
    -  "삼국 통일을 이뤘습니다" → 결말 노출!
    -  "행복하게 살았답니다" → 해피엔딩 스포일러!
    -  "문제가 해결되었어요" → 결과 공개!

    ### 3.  마지막 자막은 반드시 궁금증 유발!
    - "과연 원이는 왕건을 만날 수 있을까요?"
    - "운명의 그 날, 무슨 일이 벌어질까요?"
    - "위기에 빠진 주인공! 어떻게 될까요?"

    ### 4. 자막 스타일 예시
    나쁜 예: "무량수전에서 왕건을 만났어요." (너무 밋밋)
    좋은 예: "천년고찰 무량수전, 그곳에서 운명적인 만남이 기다리고 있었어요!"

    나쁜 예: "원이는 계단을 올랐습니다." (단순 설명)
    좋은 예: "가파른 백팔 계단을 오르는 원이, 숨이 턱까지 차올랐지요!"

    ---

    ## 화자(speaker) 배정 - 핵심!

    ### 핵심 원칙: 나레이션 vs 직접 대사

    **narrator (내레이터)** - 다음 모든 경우:
    - 상황 설명: "원이는 계단을 올랐어요."
    - 캐릭터 행동 묘사: "설렘으로 가득찬 원이의 여정!"
    - 배경 설명: "옛날 옛적에...", "어느 날 아침..."
    - 감정 묘사: "원이는 가슴이 두근두근 뛰었어요."

    **캐릭터 음성** - 오직 직접 대사(따옴표 안)만!
    - child_male: 소년이 직접 말할 때 → "내가 할게요!"
    - adult_male: 성인 남성이 직접 말할 때 → "가자, 원아!"
    - elder_female: 할머니가 직접 말할 때 → "잘 다녀오렴"

    ### 사용 가능한 화자 목록:
    - **narrator**: 모든 나레이션/설명 (기본값!)
    - **child_male**: 소년의 직접 대사 ("...")
    - **child_female**: 소녀의 직접 대사
    - **adult_male**: 성인 남성의 직접 대사
    - **adult_female**: 성인 여성의 직접 대사
    - **elder_female**: 할머니의 직접 대사
    - **elder_male**: 할아버지의 직접 대사
    - **young_female**: 젊은 여성의 직접 대사
    - **young_male**: 젊은 남성의 직접 대사
    - **animal**: 동물의 직접 대사
    - **fairy**: 요정/마법사의 직접 대사
    - **none**: 빈 자막 (그림만 있는 페이지)

    ### 예시 (중요!):
    - "원이는 숨을 헐떡이며 계단을 올랐어요." → **narrator** (상황 설명)
    - "봉황산 숲길을 뛰어가는 원이!" → **narrator** (행동 묘사)
    - "설렘으로 가득찬 원이의 여정이 시작되었어요." → **narrator** (나레이션)
    - 원이가 "저도 같이 가도 될까요?"라고 말했어요 → **child_male** (직접 대사)
    - "과연 어떻게 될까요?" → **narrator** (궁금증 유발)

    ###  흔한 실수:
    - "원이는 설레는 마음을 감출 수 없었어요." → narrator (O) / child_male (X)
    - "가파른 계단을 오르는 원이!" → narrator (O) / child_male (X)

    ---

    ## JSON 응답 형식
    {{
    "subtitles": [
        {{"text": "천년고찰 무량수전으로 향하는 길, 설렘으로 가득찬 원이의 여정이 시작되었어요!", "speaker": "narrator"}},
        {{"text": "가파른 백팔 계단을 오르는 원이, 숨이 턱까지 차올랐지요!", "speaker": "narrator"}},
        {{"text": "그때, 눈앞에 웅장한 무량수전이 나타났어요!", "speaker": "narrator"}},
        {{"text": "그리고 그곳에서 운명처럼 만난 한 사람... 바로 왕건이었어요.", "speaker": "narrator"}},
        {{"text": "과연 원이와 왕건의 만남은 어떤 이야기로 이어질까요?", "speaker": "narrator"}}
    ]
    }}
    """

        response = client.chat.completions.create(
            model="gpt-5.2",  # GPT-5.2 for advanced context understanding (2025.12)
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )

        result = json.loads(response.choices[0].message.content)
        subtitles = result.get("subtitles", [])

        # 자막 개수가 장면 수와 다르면 조정
        while len(subtitles) < num_scenes:
            subtitles.append({"text": "", "speaker": "none"})
        if len(subtitles) > num_scenes:
            subtitles = subtitles[:num_scenes]

        # 유효한 화자 목록 (검증용)
        VALID_SPEAKERS = {
            "narrator", "child", "child_male", "child_female",
            "elder_female", "elder_male", "adult_female", "adult_male",
            "young_female", "young_male", "animal", "fairy", "none"
        }

        # 각 항목이 dict인지 확인하고 정규화 + 검증
        normalized = []
        for item in subtitles:
            if isinstance(item, dict):
                text = item.get("text", "")
                speaker = item.get("speaker", "narrator")

                # 유효성 검증 - 잘못된 화자는 narrator로 폴백
                if speaker not in VALID_SPEAKERS:
                    print(f"⚠️ 예상치 못한 화자 '{speaker}' → narrator로 변경")
                    speaker = "narrator"

                normalized.append({"text": text, "speaker": speaker})
            elif isinstance(item, str):
                # 이전 형식 호환 (문자열만 있는 경우)
                normalized.append({
                    "text": item,
                    "speaker": "narrator" if item else "none"
                })
            else:
                normalized.append({"text": "", "speaker": "none"})

        return normalized


    # 하위 호환용 래퍼 (기존 코드 지원)
    def generate_trailer_subtitles(page_texts: list[tuple[str, str]], duration_per_clip: int):
        """기존 함수 호환 - 텍스트만 반환"""
        results = generate_trailer_subtitles_with_speakers(page_texts, duration_per_clip)
        return [item["text"] for item in results]


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

    # --------------------------------
    # ③ 실행 파트 (3단계 프로세스)
    # --------------------------------
    st.divider()
    st.subheader("③ 생성 프로세스")

    # =========================================================
    # [STEP 1] 대본 초안 생성 (GPT)
    # =========================================================
    st.markdown("#### 1️⃣ 대본(Script) 생성 및 수정")

    if st.button("1단계: AI 대본 초안 생성", type="primary"):
        # 1. 고유 ID 생성
        st.session_state.proc_uid = uuid.uuid4().hex[:8]
        log_event("modeA_step1_start", {
            "book": selected_book,
            "prompt": PROMPT,
            "default_duration": DEFAULT_DURATION,
            "use_bgm": use_bgm,
            "bgm_volume": bgm_volume if use_bgm else None,
        })

        st.info("📜 원본 텍스트를 분석하여 대본과 화자를 설정합니다...")

        # 1-1. 텍스트 추출
        page_texts = []
        for name in st.session_state.selected_pages:
            text = extract_text_for_image(name, txt_file)
            page_texts.append((name, text))

        # 세션에 원본 텍스트 저장 (나중에 참고용)
        st.session_state.raw_texts = page_texts

        # 1-2. OpenAI 대본 생성
        subtitle_data = generate_trailer_subtitles_with_speakers(page_texts, DEFAULT_DURATION)

        # 결과 저장
        st.session_state.step1_scripts = subtitle_data

        # 사용자별 세션 폴더에 GPT 초안 그대로 기록
        try:
            with open(SESSION_DIR / f"modeA_step1_scripts_{st.session_state.proc_uid}.json", "w", encoding="utf-8") as _f:
                import json as _json
                _json.dump({
                    "book": selected_book,
                    "raw_texts": page_texts,
                    "gpt_initial_scripts": subtitle_data,
                }, _f, ensure_ascii=False, indent=2)
        except Exception as _e:
            print(f"[log] step1 save failed: {_e}")

        log_event("modeA_step1_done", {
            "scenes": len(subtitle_data),
            "scripts": subtitle_data,
        })

        # 2, 3단계 데이터 초기화 (새로 생성했으므로)
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
                final_scripts.append({
                    "text": st.session_state[f"script_text_{i}"],
                    "speaker": st.session_state[f"script_spk_{i}"]
                })

            # 수정된 대본 업데이트
            st.session_state.step1_scripts = final_scripts

            log_event("modeA_step2_start", {
                "gpt_initial_scripts": gpt_initial,
                "edited_scripts": final_scripts,
            })

            st.info("🎙️ TTS 음성을 생성하고 길이를 측정합니다...")
            
            # TTS 생성
            texts = [s["text"] for s in final_scripts]
            speakers = [s["speaker"] for s in final_scripts]
            
            audio_paths = generate_audio_for_subtitles(texts, OUT, uid, speakers=speakers)
            
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
                
                # Runway 호출
                try:
                    result = generate_video_from_image(str(img_path), PROMPT, runway_dur)
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
elif mode == "(신규) 텍스트 분석 기반 예고편 제작":
    b_text_based.run_text_analysis_mode(client, folder, txt_file)

    