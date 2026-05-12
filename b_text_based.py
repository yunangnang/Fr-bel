# -*- coding: utf-8 -*-
# b_test_based.py
import streamlit as st
from pathlib import Path
from PIL import Image
import uuid, re, os, json, copy, shutil, time, traceback, hashlib
import base64
from moviepy.editor import AudioFileClip, concatenate_audioclips, VideoFileClip, concatenate_videoclips, vfx, ImageClip
from moviepy.video.fx.all import crop
from openai import OpenAI
from datetime import datetime


client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
from difflib import SequenceMatcher

# 외부 모듈 임포트 (app.py와 동일한 위치에 있다고 가정)
from runway_api import generate_video_from_image, extract_video_url
from video_utils import (
    download_video, 
    add_subtitle_to_video, 
    trim_video_to_duration  
)

# 1. API 통신/생성을 담당하는 함수는 module에서
from tts_module import generate_audio_for_subtitles

# 2. 파일 조작/유틸리티 함수는 core에서
from tts_core import (
    add_audio_to_video, 
    concat_videos_with_audio, 
    get_audio_duration
)


# --------------------------------
# 1. B모드 전용 헬퍼 함수들 모음
# --------------------------------

# --------------------------------
# BGM 관련 함수
# --------------------------------
def get_bgm_folder_name(full_name: str) -> str:
    """BGM 폴더 안의 하위 폴더명과 유사도 비교하여 가장 일치하는 폴더명 반환"""
    bgm_root = Path("BGM")
    if not bgm_root.exists():
        return full_name

    # BGM 폴더 내 하위 폴더 목록
    bgm_folders = [f.name for f in bgm_root.iterdir() if f.is_dir()]
    if not bgm_folders:
        return full_name

    # 비교용: 공백/언더스코어 제거한 입력값
    normalized_input = full_name.replace(" ", "").replace("_", "")

    # 1차: 부분 문자열 매칭 (폴더명이 입력에 포함되어 있는지)
    for folder in bgm_folders:
        normalized_folder = folder.replace(" ", "").replace("_", "")
        if normalized_folder in normalized_input or normalized_input in normalized_folder:
            return folder

    # 2차: SequenceMatcher 유사도 비교
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

def get_bgm_for_page(page_name: str, bgm_dir: Path):
    """페이지 이름에서 번호를 추출하여 해당하는 BGM 파일 찾기"""
    if not bgm_dir.exists():
        return None

    # 파일명에서 페이지 번호 추출 (예: "page_11" -> 11)
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
# B. 파일명에서 제목 추출 함수
# --------------------------------
def extract_title_from_filename(filename: str) -> str:
    """
    파일명 규칙: xxxx_xx개월_내지_(제목)_xx_xxxx.txt
    '내지_' 뒤에 오는 문자열을 제목으로 추출합니다.
    """
    # '내지_' 뒤에 오는 내용부터, 그 다음 '_'가 나오기 전까지 추출
    pattern = r"내지_(.*?)_"
    match = re.search(pattern, filename)
    
    if match:
        return match.group(1)
    
    # 패턴 매칭 실패 시 파일명 자체를 반환 (확장자 제외)
    return Path(filename).stem


# --------------------------------
# B. [Step 1 Helper] 동화 텍스트 분석 함수
# --------------------------------
def analyze_story_structure(full_text: str, known_title: str = ""):
    """
    GPT를 통해 동화의 전체 내용을 분석합니다. (파일명에서 추출한 제목 참고)
    """
    system_prompt = f"""
    당신은 동화 분석 및 예고편 기획 전문가입니다. 
    주어진 동화 텍스트를 정밀하게 분석하여 다음 항목을 JSON 형식으로 반환하세요.
    
    [분석 요구사항]
    1. title: 동화의 제목 (제공된 제목 '{known_title}'을 최우선으로 사용하되, 내용과 맞지 않으면 수정)
    2. summary: 전체 줄거리 (예고편 구성을 위해 사건의 인과관계가 드러나도록 **4~8문장** 분량으로 상세하게 요약)
    3. plot_structure: 이야기의 흐름을 '기(발단)-승(전개)-전(위기/절정)-결(결말)' 4단계로 나누어 분석.
       - **중요:** 텍스트에 있는 `--- Page N ---` 표시를 기준으로 각 단계가 시작되는 페이지와 끝나는 페이지를 정확히 명시할 것.
       - 각 단계는 단순 요약이 아니라, **주요 사건, 갈등의 심화, 감정의 변화**가 충분히 드러나도록 상세하게 서술할 것. **4~8문장** 분량으로 상세하게 요약. 원문을 바탕으로 원문과 비슷한 양상으로 서술할 것.
    4. moral: 이 동화가 주는 교훈이나 메시지
    5. key_scenes: 예고편에 넣으면 좋을만큼 시각적/청각적으로 흥미로운 핵심 장면 3~5개 추천
    
    [반환 형식 - JSON]
    {{
        "title": "...",
        "summary": "...",
        "plot_structure": {{
            "introduction": {{
                "summary": "내용 요약...",
                "start_page": 1,
                "end_page": 5
            }},
            "development": {{
                "summary": "내용 요약...",
                "start_page": 6,
                "end_page": 15
            }},
            "climax": {{
                "summary": "내용 요약...",
                "start_page": 16,
                "end_page": 22
            }},
            "resolution": {{
                "summary": "내용 요약...",
                "start_page": 23,
                "end_page": 30
            }}
        }},
        "moral": "...",
        "key_scenes": ["...", "..."]
    }}
    """
    
    response = client.chat.completions.create(
        model="gpt-5.2",  # 상세 분석을 위해 고성능 모델 권장
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"다음 동화 내용을 분석해주세요:\n\n{full_text}"}
        ],
        response_format={"type": "json_object"}
    )
    
    return json.loads(response.choices[0].message.content)

VOICE_TYPES = [
    "child_male",
    "child_female",
    "child_bright",
    "young_male",
    "young_female",
    "adult_male",
    "adult_male_deep",
    "adult_female",
    "elder_male",
    "elder_female",
    "cute_animal",
    "dog",
    "demon",
    "robot",
    "fairy",
    "narrator"
]

# --------------------------------
# [Step 1.5 Helper] 캐릭터 및 화자 분석 
# --------------------------------
def analyze_characters_and_speakers(client, full_text: str):
    """
    동화 텍스트를 분석하여 등장인물 프로필과 대사-화자-페이지 매핑 정보를 추출합니다.
    """
    # GPT에게 선택지로 줄 목소리 타입 목록
    voice_options = """
    - child_male (남자 아이)
    - child_female (여자 아이)
    - child_bright (명랑한 아이)
    - young_male (청년 남성)
    - young_female (청년 여성)
    - adult_male (성인 남성 - 일반)
    - adult_male_deep (성인 남성 - 굵고 낮은/위엄있는/호랑이/왕)
    - adult_female (성인 여성)
    - elder_male (할아버지/노인)
    - elder_female (할머니/노인)
    - cute_animal (귀여운 동물 - 고양이, 토끼 등)
    - dog (강아지/개)
    - demon (악마/괴물)
    - robot (로봇/기계)
    - fairy (요정/신비로움)
    - narrator (해설자)
    """
    system_prompt = """
    당신은 소설 및 동화 분석 전문가이자 성우 캐스팅 디렉터입니다.
    제공된 텍스트에는 `--- Page N ---` 형식으로 페이지 번호가 구분되어 있습니다.
    이를 바탕으로 다음 정보를 JSON으로 반환하세요.

    1. **characters (등장인물 프로필)**
       - 등장하는 모든 캐릭터의 이름, 성별, 연령대, 어조를 정리하고 분석해 **가장 어울리는 목소리 타입(voice_type)**을 지정하세요.
       - 동일 인물의 다른 호칭(aliases)을 묶어서 하나의 ID로 관리하세요.

    [반환 데이터 구조 (JSON)]
    1. **characters (등장인물 프로필)**
       - `id`: 캐릭터 고유 ID (예: char_01)
       - `name`: 이름
       - `gender`: Male / Female : 성별을 하나 선택해야하며 텍스트에서 명확하게 묘사되지 않은 경우에는 대화문, 서사적 양상으로 성별을 결정해야함.
       - `age_group`: Child / Young / Adult / Elder
       - `tone`: 성격이나 말투 묘사
       - `voice_type`: **아래 제공된 [Voice List] 중 캐릭터에게 가장 잘 어울리는 키워드 1개를 선택하며 **키워드를 변형하지 않음** (필수)**
    
    [Voice List (아래 voice_options 여기서만 선택할 것, 수정하거나 설명을 추가하지 않음)]
    {voice_options}

    2. **dialogue_map (대사 정밀 분석)**
       - 텍스트 내의 **모든 대화문("")**을 순서대로 추출하세요. `context`를 다음 규칙에 따라 작성하세요.
       - `speaker_id`: 누가 말했는지 식별하세요.
       - `page_num`: **해당 대사가 몇 페이지(Page N)에 있는지 정확한 정수(Int)로 적으세요.** (매우 중요)

    ⭐⭐ **`context` 작성 절대 규칙 (Context Writing Rules)** ⭐⭐
    1. **역할**: 이 자막은 대사가 오디오로 나올 때, **화면의 상황을 설명해주는 해설 자막**입니다.
    2. **내용**: 대사를 단순히 요약하거나 반복하지 마세요. (예: "~가 말했다" 금지)
       대신, **그 대사를 할 때 캐릭터가 취한 행동**이나, **그 대사를 유발한 시각적 상황**을 묘사하세요.
    3. **상호보완성**: 
       - 대사가 행동을 지시하면 -> `context`는 그 행동이 이루어지는 모습을 묘사.
       - 대사가 감탄/발견이면 -> `context`는 무엇을 보았는지 시각적으로 묘사.
    4. **문체**: 어린이 동화책의 **지문(Narration)**처럼 부드러운 '해요체' 또는 '합쇼체' 문장으로 쓰세요.

    [작성 예시]
    - (Case A: 행동 묘사)
      Quote: "콩쥐야, 내가 구멍을 막아줄 테니 물을 채우렴."
      Context: "두꺼비가 울퉁불퉁한 몸으로 독의 구멍 난 부분을 꽉 막아주었어요." (O)
      Context: "두꺼비가 콩쥐에게 구멍을 막아준다고 말했어요." (X - 단순 반복)

    - (Case B: 시각적 상황)
      Quote: "저기 환하게 빛나는 것이 무엇인지 알아보아라."
      Context: "수풀 속에 숨어있는 빛나는 각시를 발견하고 원님이 소리쳤어요." (O)
      Context: "원님이 저게 뭐냐고 물어봤어요." (X - 단순 요약)

    [반환 형식 - JSON]
    {
        "characters": [
            {
                "id": "char_01",
                "name": "흥부",
                "aliases": ["흥부", "동생"],
                "gender": "Male",
                "age_group": "Young",
                "tone": "공손하고 주눅 든 목소리",
                "visual": "허름한 옷차림",
                "voice_type": "young_male"
            }
        ],
        "dialogue_map": [
            {
                "quote": "형님, 쌀 좀 꾸어주세요.", 
                "speaker_id": "char_01", 
                "page_num": 8,
                "context": "흥부가 놀부 집을 찾아가 부탁했어요."
            },
            {
                "quote": "당장 나가지 못해!", 
                "speaker_id": "char_02", 
                "page_num": 9,
                "context": "놀부가 흥부를 밥주걱으로 때렸어요."
            }
        ]
    }
    """

    response = client.chat.completions.create(
        model="gpt-5.2",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": full_text}
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "character_and_dialogue_analysis",
                "schema": {
                    "type": "object",
                    "required": ["characters", "dialogue_map"],
                    "properties": {
                        "characters": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": [
                                    "id",
                                    "name",
                                    "aliases",
                                    "gender",
                                    "age_group",
                                    "tone",
                                    "voice_type"
                                ],
                                "properties": {
                                    "id": { "type": "string" },
                                    "name": { "type": "string" },
                                    "aliases": {
                                        "type": "array",
                                        "items": { "type": "string" }
                                    },
                                    "gender": {
                                        "type": "string",
                                        "enum": ["Male", "Female"]
                                    },
                                    "age_group": {
                                        "type": "string",
                                        "enum": ["Child","Young", "Adult", "Elder"]
                                    },
                                    "tone": { "type": "string" },
                                    "voice_type": {
                                        "type": "string",
                                        "enum": VOICE_TYPES
                                    }
                                }
                            }
                        },
                        "dialogue_map": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": [
                                    "quote",
                                    "speaker_id",
                                    "page_num",
                                    "context"
                                ],
                                "properties": {
                                    "quote": { "type": "string" },
                                    "speaker_id": { "type": "string" },
                                    "page_num": { "type": "integer" },
                                    "context": { "type": "string" }
                                }
                            }
                        }
                    }
                }
            }
        }
    )
    
    return json.loads(response.choices[0].message.content)

GPT_VOICE_TO_UI_LABEL = {
    # 아동
    "child_male": "👦 아동 남성 (하준)",
    "child_female": "👧 아동 여성 (다인)",
    "child_bright": "👧 아동 여성 (가람 - 밝음)",
    
    # 청년
    "young_male": "👱 청년 남성 (은우)",
    "young_female": "👩 청년 여성 (아라)",
    
    # 성인
    "adult_male": "👨 성인 남성 (민상 - 뉴스톤)",
    "adult_male_deep": "👨 성인 남성 (원탁 - 굵고 낮음)",
    "adult_female": "👩 성인 여성 (예진 - 차분함)",
    
    # 노인
    "elder_male": "👴 어르신 남성 (종현)",
    "elder_female": "👵 어르신 여성 (선희)",
    
    # 특수
    "cute_animal": "🐱 고양이/동물 (야옹이)",
    "dog": "🐶 강아지 (멍멍이)",
    "demon": "😈 악마/괴물 (마몬)",
    "robot": "🤖 로봇/기계 (원탁)",
    "fairy": "🧚 요정 (신우 - 중성적)",
    "narrator": "👩 성인 여성 (지윤 - 나레이션)"
}

# 3. VOICE_SAMPLE_TEXTS (각 음성의 성격이 드러나는 미리듣기 문장)
VOICE_SAMPLE_TEXTS = {
    "👦 아동 남성 (하준)": "엄마! 저 학교 다녀왔어요!",
    "👦 아동 남성 (재욱)": "와! 이거 진짜 재밌어요!",
    "👧 아동 여성 (다인)": "어, 이게 뭐지? 너무 신기해!",
    "👧 아동 여성 (가람 - 밝음)": "오늘은 정말 즐거운 하루였어요!",
    "👱 청년 남성 (은우)": "걱정하지 마. 내가 도와줄게.",
    "👱 청년 남성 (지훈)": "그래, 우리 함께 가보자!",
    "👩 청년 여성 (아라)": "정말요? 너무 좋아요!",
    "👩 청년 여성 (유진)": "이쪽으로 따라오세요.",
    "👨 성인 남성 (민상 - 뉴스톤)": "오늘의 주요 소식을 전해드립니다.",
    "👨 성인 남성 (준영 - 부드러움)": "괜찮아, 모든 게 다 잘 될 거야.",
    "👨 성인 남성 (원탁 - 굵고 낮음)": "이 숲은 위험하다. 조심해야 한다.",
    "👩 성인 여성 (예진 - 차분함)": "천천히 생각해보고 결정하렴.",
    "👩 성인 여성 (영미 - 따뜻함)": "우리 아가, 이리 와서 안아줄까?",
    "👩 성인 여성 (지윤 - 나레이션)": "옛날 옛적, 어느 마을에 한 아이가 살았어요.",
    "👴 어르신 남성 (종현)": "허허, 그래. 옛날에는 말이다.",
    "👵 어르신 여성 (선희)": "에구, 우리 강아지. 잘 다녀왔니?",
    "🐱 고양이/동물 (야옹이)": "야옹~ 배고프다옹!",
    "🐶 강아지 (멍멍이)": "멍멍! 같이 놀자!",
    "😈 악마/괴물 (마몬)": "크크크. 어디 한번 도망쳐 봐라!",
    "🤖 로봇/기계 (원탁)": "삐빅. 명령을 수행합니다.",
    "🧚 요정 (신우 - 중성적)": "두려워하지 마. 내가 함께 있을게.",
}


def generate_voice_preview(voice_label: str) -> "Optional[str]":
    """선택한 음성으로 샘플 문장을 Clova TTS로 생성. 캐시된 파일이 있으면 재사용."""
    from tts_module import text_to_speech

    clova_id = VOICE_PRESETS.get(voice_label)
    sample_text = VOICE_SAMPLE_TEXTS.get(voice_label)
    if not clova_id or not sample_text:
        return None

    sample_dir = Path("outputs/voice_samples")
    sample_dir.mkdir(parents=True, exist_ok=True)
    output_path = sample_dir / f"sample_{clova_id}.mp3"

    if output_path.exists() and output_path.stat().st_size > 0:
        return str(output_path)

    success = text_to_speech(
        text=sample_text,
        output_path=str(output_path),
        speaker=clova_id,
        engine="clova",
    )
    return str(output_path) if success and output_path.exists() else None


# 2. VOICE_PRESETS (UI 표시용 -> 실제 Clova ID)
# (이전 답변과 동일하게 유지)
VOICE_PRESETS = {
    "--- 자동/기본값 ---": None,
    "👦 아동 남성 (하준)": "nhajun",
    "👦 아동 남성 (재욱)": "njaewook",
    "👧 아동 여성 (다인)": "ndain",
    "👧 아동 여성 (가람 - 밝음)": "ngaram",
    "👱 청년 남성 (은우)": "neunwoo",
    "👱 청년 남성 (지훈)": "njihun",
    "👩 청년 여성 (아라)": "nara",
    "👩 청년 여성 (유진)": "nyujin",
    "👨 성인 남성 (민상 - 뉴스톤)": "nminsang",
    "👨 성인 남성 (준영 - 부드러움)": "njoonyoung",
    "👨 성인 남성 (원탁 - 굵고 낮음)": "nwontak",
    "👩 성인 여성 (예진 - 차분함)": "nyejin",
    "👩 성인 여성 (영미 - 따뜻함)": "nyoungmi",
    "👩 성인 여성 (지윤 - 나레이션)": "njiyun",
    "👴 어르신 남성 (종현)": "njonghyun",
    "👵 어르신 여성 (선희)": "nsunhee",
    "🐱 고양이/동물 (야옹이)": "nmeow",
    "🐶 강아지 (멍멍이)": "nwoof",
    "😈 악마/괴물 (마몬)": "nmammon",
    "🤖 로봇/기계 (원탁)": "nwontak",
    "🧚 요정 (신우 - 중성적)": "nsinu",
}
# --------------------------------
# [Step 2 Helper] 예고편 구간 확인
# --------------------------------

# --------------------------------
# [Step 2 Helper] 예고편 구간 추천 함수 (프롬프트 강화판)
# --------------------------------
def recommend_trailer_segments(full_text: str, analysis_data: dict):
    """
    GPT에게 전체 텍스트와 분석 데이터를 주고, 예고편으로 쓰기 좋은 3가지 구간을 추천받습니다.
    (조건: 초반 도입부 배제 + 중~후반부 위기/절정 직전 집중 + 결말 절대 배제)
    """
    
    # 1단계에서 분석한 기승전결 정보 활용
    structure = analysis_data.get("plot_structure", {})
    summary = analysis_data.get("summary", "")
    
    system_prompt = f"""
    당신은 관객의 애를 태우는 '악마의 예고편 편집자'입니다.
    전체 동화 텍스트 중에서 가장 긴박하고 호기심을 자극하는 **하이라이트 구간(300~600자)** 3가지를 찾으세요.

    [🎯 위치 선정 규칙 (매우 중요)]
    1. **초반부(Introduction) 배제**: "옛날 옛적에..."와 같은 평화로운 배경 설명이나 캐릭터 소개 부분은 쓰지 마세요. 지루합니다.
    2. **중~후반부 집중**: 사건이 이미 벌어지고, 갈등이 심화되는 **'전개(Development)'에서 '위기/절정(Climax)' 직전** 사이의 구간을 선택하세요.
    3. **이미 진행된 상황**: 주인공이 이미 모험을 떠났거나, 악당을 만났거나, 곤란한 상황에 빠져 있는 시점이어야 합니다.

    [⛔ 절대 금지 사항]
    1. **결말(Resolution) 포함 금지**: 모든 갈등이 해결되거나 행복한 결말이 나오는 부분은 절대 포함하지 마세요.
    2. **단순 나열 금지**: 사건의 인과관계 없이 장면만 나열하지 말고, 하나의 긴박한 흐름이 있는 덩어리 텍스트를 발췌하세요.

    [✂️ 편집 포인트: 절단신공]
    - 텍스트의 끝부분은 반드시 주인공이 **최대 위기에 처하거나, 충격적인 사실을 알게 되는 순간**에서 딱 끊어야 합니다.
    - 시청자가 "도대체 어떻게 되는 거야?"라고 소리치게 만드세요.

    [구간 추출(target_text) 규칙]
    -구간을 추출할 때(target_text), 원문에 있는 페이지 표시(예: --- Page 19 ---)를 절대 삭제하지 말고 포함해서 출력해줘. 원문 그대로 인용해야 해.

    [3가지 추천 옵션]
    - 옵션 1 (Main Stream): 이야기의 가장 큰 갈등이 폭발하기 직전 (가장 정석적인 예고편)
    - 옵션 2 (Character Crisis): 주인공이 가장 큰 시련이나 딜레마에 빠진 순간
    - 옵션 3 (Mystery/Horror): 도대체 무슨 일이 일어나는지 알 수 없는 기이하고 긴박한 상황

    [반환 형식 - JSON]
    {{
        "options": [
            {{
                "id": 1,
                "type": "Main Stream",
                "title": "(긴박함을 강조하는 제목)",
                "reason": "사건이 한창 진행된 중반부로, 주인공이 ~한 상황에 처해 있어 몰입도가 높고 다음 내용을 궁금하게 만듭니다.",
                "target_text": "\n--- Page 19 ---\n다음 날에도...\n(중략)\n--- Page 20 ---\n“당테스를 없애 버렸으면 좋겠어!”...\n..."
            }},
            ...
        ]
    }}
    """

    user_content = f"""
    [이야기 구조 정보]
    - 줄거리: {summary}
    - 기승전결: {structure}

    [전체 텍스트]
    {full_text}
    """

    response = client.chat.completions.create(
        model="gpt-5.2",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        response_format={"type": "json_object"}
    )
    
    return json.loads(response.choices[0].message.content)


# --------------------------------
# [Step 2~6 Helper] 스포일러 방지 (상한선 계산)
# --------------------------------
def find_spoiler_limit_page(target_text: str, page_map: dict = None):
    """
    target_text 내부에 포함된 '--- Page N ---' 형식을 파싱하여
    등장하는 페이지 번호 중 가장 큰 값을 스포일러 상한선으로 반환합니다.
    (명시된 페이지 태그를 우선 사용합니다.)
    """
    if not target_text:
        return 999

    # 1. 정규표현식으로 '--- Page 숫자 ---' 패턴 찾기
    #    예: "--- Page 30 ---", "---Page 31---" 등 공백 유동성 허용
    pattern = re.compile(r"---\s*Page\s*(\d+)\s*---", re.IGNORECASE)
    matches = pattern.findall(str(target_text))
    
    # 2. 매칭된 숫자가 없으면 정보가 없는 것으로 간주 -> 전체 허용(999)
    if not matches:
        return 999
    
    # 3. 추출된 숫자들을 정수로 변환하여 최댓값(가장 뒤쪽 페이지) 찾기
    try:
        page_nums = [int(m) for m in matches]
        max_page = max(page_nums)
        # 펼침면 처리 로직
        # 짝수 페이지(예: 30)에서 끝났다면, 같은 펼침면의 오른쪽(31)까지는 스포일러가 아님
        if max_page % 2 == 0:
            max_page += 1            
        return max_page
    except Exception:
        return 999
    
def trim_full_text_by_page(full_text: str, max_page: int) -> str:
    """
    full_text가 '--- Page N ---' 블록들로 구성되어 있다고 가정하고,
    max_page 이하 페이지만 남겨서 반환.
    """
    if max_page is None or max_page >= 999:
        return full_text

    # 패턴은 위와 동일하게 적용
    pattern = re.compile(r"---\s*Page\s*(\d+)\s*---", re.IGNORECASE)
    matches = list(pattern.finditer(full_text))
    
    if not matches:
        return full_text

    out_chunks = []
    for i, m in enumerate(matches):
        pg = int(m.group(1))
        
        # 현재 페이지 헤더의 시작 위치
        start = m.start()
        # 다음 페이지 헤더의 시작 위치 (없으면 텍스트 끝)
        end = matches[i+1].start() if i+1 < len(matches) else len(full_text)

        # 상한선 이하인 페이지만 추가
        if pg <= max_page:
            out_chunks.append(full_text[start:end])
        else:
            # 페이지 순서가 뒤죽박죽일 수도 있으므로 break 하지 않고 continue 하는 것이 안전할 수 있으나,
            # 보통 책 전체 텍스트는 순서대로이므로, 뒤쪽 내용을 자르기 위해 break 사용 가능.
            # (여기서는 안전하게 순차적이라 가정하고 break를 걸거나, 
            #  혹시 모를 뒤죽박죽 순서를 대비해 스포일러 페이지만 건너뛰려면 continue 사용)
            continue 

    return "".join(out_chunks).strip()

# --------------------------------
# [Step 3 Helper] 대화문 대본 스포일러 제거
# --------------------------------
def trim_dialogue_map_by_page(dialogue_map: list, max_page: int) -> list:
    if max_page is None or max_page >= 999:
        return dialogue_map
    # page_num이 문자열일 경우 대비하여 int 변환
    return [d for d in dialogue_map if int(d.get("page_num", 0)) <= max_page]

# --------------------------------
# [Step 3 Helper] 월령 추출 및 대본 스펙 계산 함수
# --------------------------------
def extract_age_from_filename(filename: str) -> int:
    """
    파일명에서 'xx개월'의 숫자만 추출합니다.
    예: 'EQ_048개월_내지_...' -> 48
    """
    pattern = r"(\d+)개월"
    match = re.search(pattern, filename)
    if match:
        return int(match.group(1))
    return 0  # 추출 실패 시 0 반환 (기본값 처리용)

def get_recommendation_by_age(months: int):
    """
    월령에 따른 추천 속도, 톤, 기본 길이 옵션을 반환합니다.
    """
    if 48 <= months <= 72:
        return {
            "group": "미취학 (4~6세)",
            "speed": "slow",
            "tone": "친절하고 쉬운 어투",
            "default_option": "Short"
        }
    elif 73 <= months <= 96:
        return {
            "group": "초등 저학년 (7~8세)",
            "speed": "normal",
            "tone": "생동감 있고 호기심을 자극하는 어투",
            "default_option": "Standard"
        }
    elif months >= 97:
        return {
            "group": "초등 고학년 (9세 이상)",
            "speed": "fast",
            "tone": "박진감 넘치고 트렌디한 어투",
            "default_option": "Standard"
        }
    else:
        return {
            "group": "연령 미상",
            "speed": "normal",
            "tone": "표준 동화 구연 어투",
            "default_option": "Standard"
        }
    

# --------------------------------
# [Step 3 Helper] 일반(Standard) 대본 작성 함수
# --------------------------------

def generate_script_with_specs(target_text: str, duration_opt: str, age_info: dict, full_text: str, char_info: dict):
    """
    전체 원문(full_text)으로 맥락을 잡고, 
    핵심 구간(target_text)의 '기발한 해결책 제안'까지만 보여주고 그 결과를 숨기는 예고편을 작성합니다.
    """
    
    # 1. 길이 옵션 설정
    if duration_opt == "Short":
        time_range = "30초 ~ 50초"; char_limit = "200 ~ 300자"
    elif duration_opt == "Long":
        time_range = "1분 30초 ~ 1분 50초"; char_limit = "600 ~ 750자"
    else: # Standard
        time_range = "1분 ~ 1분 20초"; char_limit = "400 ~ 530자"

    # 캐릭터 목록 문자열 생성 (프롬프트 참고용)
    char_list_str = ""
    if char_info and "characters" in char_info:
        for c in char_info["characters"]:
            char_list_str += f"- {c['name']} (ID: {c['id']}, 성별: {c['gender']}, 나이: {c['age_group']}, 목소리 타입 : {c['voice_type']})\n"

    system_prompt = f"""
    당신은 관객의 호기심을 자극하며 원작의 출처를 정확히 밝히며 분석된 캐릭터 화자를 배정하는 **영화 예고편 편집자**입니다.
    제공된 [전체 원문]에는 `--- Page N ---` 형식으로 페이지 번호가 적혀 있습니다.
     **기발한 아이디어가 나오는 순간**에서 딱 멈추는 대본을 작성하세요. 또한 예고편 대본을 작성할 때, **각 문장이 원문의 몇 페이지(Page N) 내용을 바탕으로 썼는지** 정확히 명시하세요.

    [🎯 타겟 정보]
    - 대상: {age_info['group']}
    - 목표 시간: {time_range} (글자수: {char_limit} 내외)
    - 말투: {age_info['tone']} (원문의 어투 유지)

    [👥 **화자 배정 규칙 (Step 1.5 데이터 반영)**]
    1. **캐릭터 목록**: 아래 분석된 캐릭터의 **ID**을 `speaker` 필드에 사용하세요.
       {char_list_str}
    2. **우선순위 (Mixed Sentence)**: 한 문장에 **지문(나레이션)과 대사("")가 섞여 있다면**, `speaker` 필드에는 반드시 **대사를 말하는 캐릭터의 ID**를 적으세요.
       - (예시) 텍스트: '형들은 "미안해"라고 말했어요.'
       - (X) speaker: "narrator" (틀림! 대사가 있으므로 캐릭터 우선)
       - (O) speaker: "char_01" (정답! 시스템이 자동으로 따옴표 안만 캐릭터 목소리로 변환합니다.)
    3. **Narrator**: 순수하게 상황 설명만 있는 문장일 때만 `narrator`를 쓰세요.
    4. **매칭 원칙**: 원문에 있는 대사를 인용할 경우, 해당 대사의 원래 화자의 **ID**을 정확히 쓰세요.

    [✍️ 문장 작성 필수 규칙 (Syntax Rules) - 매우 중요]
    1. **완전한 문장 사용**: "조용한 숲길..." 같은 명사형 종결이나 말줄임표로 문장을 끝내지 마세요.
       - (X) "숲속의 고슴도치… 쿵!"
       - (O) "숲속에 사는 고슴도치는 깜짝 놀랐어요."
    2. **육하원칙 필수 (Who/What/How)**: 문장만 보고도 상황이 이해되도록 주어와 목적어를 명확히 하세요. "그것을 보았어요" 대신 "철수는 피 묻은 칼을 보았습니다"처럼 **누가, 무엇을, 어떻게 했는지** 구체적으로 서술해야 합니다.
    3. **서술어 필수**: 모든 문장은 주어와 서술어(~다, ~요)를 갖춰야 합니다.
    4. **의성어/의태어 통합**: 의성어만 단독으로 쓰지 말고, 문장 안에 자연스럽게 녹여내세요.
       - (X) "와르르!"
       - (O) "도토리가 '와르르' 쏟아져 내렸지요!"
    5. **접속사 활용**: 문장과 문장 사이가 뚝뚝 끊기지 않도록 '그런데', '바로 그때', '하지만' 등으로 자연스럽게 이으세요.

    [⛔ 편집의 핵심 규칙 (절단신공)]
    1. **'제안'은 노출, '결과'는 삭제 (매우 중요)**: 
       - 핵심 구간에 등장하는 **기상천외한 해결책이나 아이디어**(예: "도토리로 옷을 만들자!")는 예고편의 하이라이트입니다. **반드시 포함하세요.**
       - 하지만, 그 아이디어를 **실행하는 과정(만드는 장면)이나 성공한 결과**는 절대 보여주지 마세요.
    2. **타이밍**: 누군가 "좋은 생각이 났어! ~를 해보자!"라고 외치는 순간이 엔딩 직전이어야 합니다.
    3. **팩트 준수**: 원문에 없는 내용은 지어내지 마세요.

    [✍️ 작성 가이드 (서사 구성)]
    1. **도입 (Intro)**: [전체 원문]을 활용해 주인공의 평화로운 일상이나 사건의 발단을 소개하세요.
    2. **위기 (Crisis)**: 주인공이 곤란한 상황에 빠져 "어떡하지?"라고 고민하는 과정을 [핵심 구간]에서 가져오세요.
    3. **반전 (The Hook)**: [핵심 구간]에 있는 **해결책 제안 대사**("도토리 옷을 만들자!")를 클라이막스로 배치하세요.
    4. **마무리 (Outro)**: 제안이 나오자마자 바로 내레이션으로 넘기세요. 
       - "과연 이 엉뚱한 방법이 통할까요?", "도토리 옷이라니, 정말 가능할까요?"

    [✍️ 작성 가이드 (출처 추적 Source Page)]
       - 대본의 한 줄을 쓸 때마다, 그 내용이 있는 **페이지 번호(숫자만)**를 찾으세요.
       - 여러 페이지에 걸친 내용이라면, 가장 핵심이 되는 페이지 하나를 고르세요.
       - 원문에 없는 내레이션(질문 등)은 직전 장면의 페이지를 따르거나, 표지(5페이지)로 설정하세요.
       - 단, **예고편의 마지막 씬이 관객에게 던지는 내레이션 질문(궁금증 유발)으로 끝나는 경우**, 그 마지막 자막의 `source_page`는 **0**으로 설정해도 됩니다.
        - 이 규칙은 **마지막 씬 1개에만 적용**됩니다.
       
    [🚫 **펼침면(Spread) 중복 방지 규칙 (매우 중요)**]
    1. **다양한 장면 사용**: 한 장소(펼침면)에서 너무 많은 대사가 나오면 영상이 지루해집니다.
    2. **펼침면 정의**: [짝수 페이지(2k)]와 [홀수 페이지(2k+1)]은 하나의 펼침면(같은 그림)입니다. (예: 20p와 21p는 같은 장면)
    3. **최대 허용 한도**: **하나의 펼침면에서 대사를 2개까지만 가져오세요.** (3개 이상 연속 금지)
       - (X) Scene 1(20p), Scene 2(21p), Scene 3(20p) -> **지루함!**
       - (O) Scene 1(20p), Scene 2(21p) -> Scene 3(다음 장으로 이동)
    4. 만약 대화가 길어진다면, 과감히 요약하거나 내레이션으로 처리하여 장면을 넘기세요.
          
    [🗣️ 화자 배정]
    - **narrator**: 상황 설명, 마지막 질문 던지기.
    - **캐릭터**: 원문의 따옴표("") 안 **직접 대사**만 사용. (아이디어를 제안하는 캐릭터의 대사는 필수 포함!)
    
    [반환 형식 - JSON]
    {{
        "subtitles": [
            {{"text": "옛날 어느 숲속에, 욕심쟁이 호랑이가 살고 있었어요.", "speaker": "narrator","source_page": 2}},
            {{"text": "어흥! 맛있는 떡 하나 주면 안 잡아먹지!", "speaker": "char_03","source_page": 6}},
            {{"text": "호랑이는 오누이를 향해 무섭게 달려들었답니다.", "speaker": "narrator","source_page": 11}},
            {{"text": "과연 오누이는 호랑이를 피해 도망칠 수 있을까요?", "speaker": "narrator","source_page": 15}},
             {{"text": "하지만 형들은 "미안해. 우리가 다시 그려 줄게." 하고 조심조심 말했어요.", "speaker": "char_04","source_page": 17}},
            {{"text": "어떻게요? 할머니가 한번 보여 주세요?", "speaker": "char_01","source_page": 27}},
            {{"text": "헨젤, 손가락을 내밀어 봐라. 얼마나 살이 쪘는지 보자.", "speaker": "char_02","source_page": 24}},
            {{"text": "형님, 쌀 조금만 꾸어주세요.", "speaker": "char_01", "source_page": 8}}
        ],
        "estimated_duration": "예상 시간",
        "comment": "원문의 어느 문장을 활용했는지 간략 설명"
    }}
    """
    
    # 원문 전체를 참고 자료로 제공
    user_content = f"""
    [참고 자료 1: 전체 원문 텍스트 (Context & Facts)]
    {full_text}

    [참고 자료 2: 예고편의 핵심 하이라이트 구간 (Focus)]
    {target_text}
    """

    response = client.chat.completions.create(
        model="gpt-5.2", # 긴 텍스트 처리를 위해 gpt-5.2 권장
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        response_format={"type": "json_object"}
    )
    
    return json.loads(response.choices[0].message.content)


# --------------------------------
# [Step 3 Helper] 대화문 위주(Conversation)에서 대화문에서의 의성어/효과음 제거 함수
# --------------------------------
def has_real_sentence_after_sfx(q: str) -> bool:
    # "짹짹, 저는 ..." / "쿵! 무슨 소리야?" 같은 케이스를 살림
    # 구두점 뒤에 한글/영문/숫자 같은 본문이 이어지면 True
    return re.search(r"[,\.\!\?\…]\s*[가-힣A-Za-z0-9]", q) is not None

def is_sfx_like(quote: str) -> bool:
    """
    의성어/효과음/동물 울음 같은 '대사 아닌' 짧은 문자열을 걸러내기 위한 휴리스틱.
    완벽하진 않지만 실무에서 꽤 잘 걸러짐.
    """
    if not quote:
        return True

    q = quote.strip()
    # (예외) 효과음이 앞에 붙었지만 뒤에 문장이 이어지면 대사로 취급
    if has_real_sentence_after_sfx(q):
        return False

    # 1) 너무 짧은 단독 소리(예: "톡", "짹짹", "쿵")
    #    (한글 1~6자 + 선택적 반복/구두점 정도)
    if len(q) <= 6:
        # 한글/자모/반복기호/느낌표/물음표/쉼표만으로 구성되면 효과음으로 간주
        if re.fullmatch(r"[가-힣ㄱ-ㅎㅏ-ㅣ,!.?~…·\-]+", q):
            return True

    # 2) '의성어 패턴'이 연속되는 경우 (예: "우르릉 쿵쿵쿵", "짹짹, 파드닥!")
    #    단어가 2개 이상이고, 각 단어가 짧고(<=6) 의미 있는 문장 부호/조사가 거의 없으면 sfx로 간주
    tokens = re.split(r"[\s,]+", q)
    tokens = [t for t in tokens if t]
    if len(tokens) >= 2:
        short_koreanish = 0
        for t in tokens:
            t2 = re.sub(r"[!.?~…·\-]", "", t)
            if 1 <= len(t2) <= 6 and re.fullmatch(r"[가-힣ㄱ-ㅎㅏ-ㅣ]+", t2):
                short_koreanish += 1
        if short_koreanish == len(tokens):
            return True

    # 3) 명백한 SFX 표기 (괄호/대괄호 안에 소리)
    #    예: "(쿵!)", "[효과음] 우르릉"
    if re.match(r"^[\(\[\{].*[\)\]\}]$", q):
        return True
    if re.search(r"(효과음|의성어|의태어|sound|sfx)", q, re.IGNORECASE):
        return True

    # 4) 문장으로 보기 어려운 경우(문장부호/조사/띄어쓰기 조합이 거의 없고 반복만 많음)
    #    예: "쿵쿵쿵쿵", "우르르르르"
    if re.fullmatch(r"[가-힣ㄱ-ㅎㅏ-ㅣ]+", q) and re.search(r"(.)\1\1", q):
        # 같은 문자가 3번 이상 반복되는 패턴이 있으면 효과음 가능성 높음
        return True

    return False

# --------------------------------
# [Step 3 Helper] 대화문 위주(Conversation) 대본 작성 함수 
# --------------------------------
def generate_conversation_oriented_script(target_text: str, duration_opt: str, age_info: dict, full_text: str, char_info: dict):
    """
    [Step 1.5 데이터 기반 Selector]
    GPT가 문장을 새로 쓰지 않고, Step 1.5에서 이미 분석된 `dialogue_map` 리스트에서
    예고편에 사용할 대사의 '인덱스(Index)'를 선택하여 조립합니다.
    
    Returns:
        Step 3와 동일한 JSON 구조 (subtitles, text, speaker, source_page)
    """
    
    # 1. 필수 데이터 검증
    if not char_info or "dialogue_map" not in char_info or not char_info["dialogue_map"]:
        # 데이터가 없을 경우 예외 처리보다는 빈 리스트 반환 혹은 에러 로깅
        return {"subtitles": [], "estimated_duration": duration_opt, "comment": "Step 1.5 데이터 없음"}

    spoiler_limit_page = None
    dialogue_list = char_info["dialogue_map"]
    characters = char_info.get("characters", [])
    # 1) 효과음/의성어 후보 제거
    raw_dialogue_list = char_info["dialogue_map"]
    dialogue_list = [d for d in raw_dialogue_list if not is_sfx_like(d.get("quote", ""))]
    # page_num이 말도 안 되게 큰 게 섞여 들어오는 상황 차단
    dialogue_list = [d for d in dialogue_list if int(d.get("page_num", 0)) > 0]

    # 화자 ID -> 목소리 타입 매핑 생성
    speaker_meta = {}
    for c in characters:
        # voice_type이 없으면 기본값 narrator
        speaker_meta[c['id']] = c.get("id", "narrator") 
        # 참고: 이름을 매핑하고 싶다면 c['name'] 사용 가능

    # 2. GPT에게 넘겨줄 대사 리스트 텍스트화
    # 포맷: [Index] 화자ID: 대사내용 (Page N) - 상황설명
    dialogue_context_str = ""
    for idx, item in enumerate(dialogue_list):
        s_id = item.get('speaker_id', 'unknown')
        # 매핑된 보이스 타입이나 이름 등을 표시해 줌 (GPT가 맥락 파악하기 좋게)
        s_role = speaker_meta.get(s_id, "unknown")
        
        dialogue_context_str += f"[{idx}] {s_id}({s_role}): \"{item['quote']}\" (Page {item['page_num']}) | 상황: {item['context']}\n"

    # 3. 길이 옵션 설정
    if duration_opt == "Short":
        select_guideline = "8개 ~ 12개의 대사를 선택하세요."
    elif duration_opt == "Long":
        select_guideline = "18개 ~ 24개의 대사를 선택하세요."
    else: # Standard
        select_guideline = "12개 ~ 16개의 대사를 선택하세요."

    # =========================================================
    # [System Prompt] 편집자(Editor) 모드
    # =========================================================
    system_prompt = f"""
    당신은 동화책 예고편 편집자입니다. 당신에게는 이미 분석된 [대사 리스트]가 있습니다.
    제공된 리스트에서 예고편의 기승전결(특히 위기나 기발한 제안 단계까지)을 가장 잘 보여주는 **대사들의 번호(Index)**를 순서대로 선택하세요.

    [작업 목표]
    1. **서사 구성**: 사건의 발단 -> 갈등 심화 -> 해결책 제안/절정 직전(절단신공).
    2. **티키타카**: 대화가 자연스럽게 이어지도록 연속된 인덱스를 묶어서 선택하는 것을 권장합니다.

    [제공된 데이터]
    - 핵심 요약(Target): {target_text}

    [반환 형식 - JSON]
    {{
        "selected_indices": [
            {{ "index": 0, "reason": "도입부" }},
            {{ "index": 1, "reason": "이어지는 대화" }},
            {{ "index": 5, "reason": "점프하여 위기 상황" }}
        ],
        "comment": "전반적인 편집 의도"
    }}

    [제약 사항]
    - 반드시 제공된 리스트의 **[숫자] Index**만 사용하세요.
    - "우르릉", "쿵쿵", "톡톡", "짹짹" 같은 **효과음/의성어/동물 울음**으로 보이는 대사는 선택하지 마세요.
    - {select_guideline}
    """
    
    user_content = f"""
    [대사 리스트 (Candidate Dialogues)]
    {dialogue_context_str}
    """

    try:
        response = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            response_format={"type": "json_object"},
            temperature=0.1 # 인덱스 선택의 정확성을 위해 낮춤
        )
        
        gpt_result = json.loads(response.choices[0].message.content)
        
        # 5. [Post-Processing] 선택된 인덱스를 Step 3와 동일한 형식으로 변환
        final_subtitles = []
        selected_indices = gpt_result.get("selected_indices", [])
        
        for item in selected_indices:
            idx = item["index"]
            
            # 인덱스 유효성 검사
            if 0 <= idx < len(dialogue_list):
                original_data = dialogue_list[idx]
                speaker_id = original_data.get("speaker_id")
                
                # Step 1.5 데이터 매핑
                # Step 3(generate_script_with_specs)의 반환 포맷(text, speaker, source_page)을 엄격히 준수
                scene_obj = {
                    "text": original_data["quote"],        # ★ Step 1.5의 원문 대사 그대로 사용 (매칭 키)
                    "speaker": speaker_meta.get(speaker_id, "narrator"), # Voice Type
                    "source_page": original_data["page_num"],
                    # 필요하다면 여기에 context를 미리 포함할 수도 있지만, 
                    # 요청하신 대로 'Step 3 반환 양식'을 유지하기 위해 최소화하거나,
                    # 나중에 편의를 위해 hidden field로 context를 넣어둘 수도 있습니다.
                    # 여기서는 Step 8에서 quote로 찾을 수 있도록 원본 텍스트 유지에 집중합니다.
                }
                final_subtitles.append(scene_obj)
        
        # 결과 반환 (Step 3와 동일한 키 구조)
        return {
            "subtitles": final_subtitles,
            "estimated_duration": duration_opt,
            "comment": gpt_result.get("comment", "")
        }

    except Exception as e:
        return {
            "subtitles": [],
            "estimated_duration": duration_opt,
            "comment": f"Error in Step 3: {str(e)}"
        }


# --------------------------------
# [Step 3 Helper] 종합(Comprehensive) 구성 (4-Step Trailer Formula Edition) 대본 작성 함수
# --------------------------------
def generate_comprehensive_script(target_text: str, duration_opt: str, age_info: dict, full_text: str, char_info: dict, analysis_data: dict):
    """
    전체 원문(full_text)과 핵심 구간(target_text)을 활용하여
    [훅 - 초압축 - 빌드업 - 절단]의 4단계 공식을 따르는 예고편 대본을 작성합니다.
    """
    
    # 1. 길이 옵션 설정
    if duration_opt == "Short":
        time_range = "30초 ~ 50초"; char_limit = "200 ~ 300자"
    elif duration_opt == "Long":
        time_range = "1분 30초 ~ 1분 50초"; char_limit = "600 ~ 750자"
    else: # Standard
        time_range = "1분 ~ 1분 20초"; char_limit = "400 ~ 530자"

    # 캐릭터 목록 문자열 생성
    char_list_str = ""
    if char_info and "characters" in char_info:
        for c in char_info["characters"]:
            char_list_str += f"- {c['name']} (ID: {c['id']}, 성별: {c['gender']}, 나이: {c['age_group']}, 목소리 타입 : {c['voice_type']})\n"

    system_prompt = f"""
    당신은 관객의 도파민을 자극하는 **숏폼/영화 예고편 전문 편집자**입니다.
    제공된 [전체 원문]과 [핵심 구간]을 바탕으로 **'4단계 실전 압축 공식'**을 완벽하게 적용한 대본을 작성하세요.
    각 문장이 원문의 몇 페이지(Page N) 내용을 바탕으로 썼는지 `source_page`에 정확히 명시하세요.

    [ 타겟 정보]
    - 대상: {age_info['group']}
    - 목표 시간: {time_range} (글자수: {char_limit} 내외)
    - 말투: {age_info['tone']} (원문의 어투 유지)

    [ **4단계 실전 압축 공식 (Strict Formula)**]
    아래 순서를 반드시 따르세요. 시간 순서대로 나열하지 말고, **가장 강렬한 장면을 먼저 배치**하는 편집 기술을 쓰세요.

    **1단계: 훅 (Hook) - "가장 센 한 마디"**
    - **목표:** 영상 시작 1초 만에 시청자의 귀를 사로잡기.
    - **방법:** [핵심 구간] 혹은 이야기 후반부의 **가장 자극적이고 결정적인 대사나 상황**을 맨 앞으로 가져오세요.
    - **주의:** 결말을 보여주는 게 아니라, 궁금증을 유발하는 충격적인 대사여야 합니다.
    - (예시) "공양미 삼백 석에 저를 인당수에 던져주세요."

    **2단계: 초압축 전개 (Fast-Forward) - "왜 그랬어?"**
    - **목표:** 1단계의 충격적인 상황이 오게 된 배경을 속도감 있게 요약.
    - **방법:** [전체 원문]의 앞부분(발단)을 활용하되, 구구절절한 설명은 생략합니다. 주인공의 불행이나 사건의 시작을 '~했고, ~했다' 식으로 빠르게 나열하세요.
    - (예시) "아버지를 위해 삯바느질도 하고 동냥도 다녔지만, 방법은 공양뿐이었다. 효녀 심청에게 남은 선택지는 없었다."

    **3단계: 서사 집중 (Build-up) - "조여오는 긴장"**
    - **목표:** 클라이맥스 직전까지 분위기 고조.
    - **방법:** [핵심 구간]의 내용을 사용하여 위기 상황, 위협, 혹은 돌이킬 수 없는 선택의 과정을 디테일하게 묘사하세요. 배경음악이 고조되는 느낌으로 긴박하게 작성하세요.
    - (예시) "거친 파도가 몰아치고 뱃사람들은 제물을 원했다. 치마를 뒤집어쓰고 뱃머리에 선 심청."

    **4단계: 절단신공 (The Cut) - "가장 궁금할 때 끊기"**
    - **목표:** 뒷내용이 궁금해서 미치게 만들기.
    - **방법:** 결정적인 행동(문 열기, 점프, 비명 등)이나 절체절명의 순간에서 **화면을 암전(Black out)**시키거나 질문을 던지며 끝내세요.
    - **절대 금지:** 위기의 결과나 해결책의 성공 여부를 보여주지 마세요.
    - (예시) "몸을 던지려던 그 순간, 물속에서 무언가를 보았다. 과연 그녀를 기다리는 것은?"

    [ **화자 배정 규칙**]
    1. **캐릭터 목록**: 아래 ID를 `speaker` 필드에 사용하세요.
       {char_list_str}
    2. **우선순위**: 지문(나레이션)과 대사("")가 섞여 있다면, 반드시 **대사 화자의 ID**를 적으세요.
    3. **Narrator**: 순수 상황 설명이나 예고편의 마지막 질문(멘트)일 때만 `narrator`를 사용하세요.

    [ 문장 및 편집 규칙]
    1. **Source Page**: 모든 문장은 근거가 되는 원문의 페이지 번호를 `source_page`에 적어야 합니다. (마지막 멘트만 0 허용)
    2. **펼침면 제한**: 같은 페이지(혹은 펼침면)에서 대사를 3개 이상 연속으로 가져오지 마세요. 지루함을 피하기 위해 장면을 빠르게 전환하세요.
    3. **육하원칙 필수 (Who/What/How)**: 문장만 보고도 상황이 이해되도록 주어와 목적어를 명확히 하세요. "그것을 보았어요" 대신 "철수는 피 묻은 칼을 보았습니다"처럼 **누가, 무엇을, 어떻게 했는지** 구체적으로 서술해야 합니다.
    4. **의성어 종결 금지**: "풍덩!", "쿵!" 같은 의성어로 문장을 끝내지 마세요. 반드시 "심청이 인당수에 풍덩 빠졌습니다", "문이 쿵 하고 닫혔어요"와 같이 **행동을 묘사하는 서술어**로 문장을 완결 지으세요.

    [반환 형식 - JSON]
    {{
        "subtitles": [
            {{"text": "(1단계) 떡 하나 주면 안 잡아먹지! 어흥!", "speaker": "char_03", "source_page": 12}},
            {{"text": "(2단계) 엄마는 고개 다섯 개를 넘어야 했지만, 호랑이는 떡도 목숨도 앗아가 버렸습니다.", "speaker": "narrator", "source_page": 4}},
            {{"text": "(3단계) 엄마 옷을 입은 호랑이가 문을 두드렸어요. '얘들아 엄마 왔다'", "speaker": "narrator", "source_page": 16}},
            {{"text": "문구멍으로 거친 손이 쑥 들어왔고, 오누이는 뒷문으로 도망쳐 나무 위로 올라갔습니다.", "speaker": "narrator", "source_page": 20}},
            {{"text": "(4단계) 썩은 동아줄일지도 모르는 상황! 과연 오누이의 운명은?", "speaker": "narrator", "source_page": 0}}
        ],
        "estimated_duration": "예상 시간",
        "comment": "4단계 공식이 어떻게 적용되었는지 간략 설명"
    }}
    """
    
    # 원문 전체를 참고 자료로 제공
    user_content = f"""
    [참고 자료 1: 전체 원문 텍스트 (Context & Facts - 요약 단계에서 활용)]
    {full_text}

    [참고 자료 2: 예고편의 핵심 하이라이트 구간 (Build-up 단계에서 집중 활용)]
    {target_text}
    """

    response = client.chat.completions.create(
        model="gpt-5.2", # 긴 텍스트 처리를 위해 gpt-5.2 권장
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        response_format={"type": "json_object"}
    )
    
    return json.loads(response.choices[0].message.content)
        
# --------------------------------
# [Step 3 Helper] 장면 번호(Scene No) 자동 할당 함수
# --------------------------------
def assign_scene_numbers(subtitles):
    """
    규칙:
    1. source_page가 0 (요약)이면: 무조건 새로운 장면 번호 부여 (빠른 컷 전환)
    2. source_page가 1 이상 (본문)이면: 
       - 직전 대사와 페이지가 같으면 -> 같은 장면 번호 유지 (그룹핑)
       - 페이지가 다르면 -> 새로운 장면 번호 부여
    """
    if not subtitles:
        return subtitles
        
    scene_counter = 1
    last_page = -1
    
    for i, item in enumerate(subtitles):
        try:
            current_page = int(item.get("source_page", 0))
        except:
            current_page = 0
            
        if i == 0:
            # 첫 번째 항목
            item["scene_no"] = scene_counter
            last_page = current_page
            continue

        # 로직 적용
        if current_page == 0:
            # 요약 파트는 무조건 컷을 나눔 (지루하지 않게)
            scene_counter += 1
            item["scene_no"] = scene_counter
            
        else:
            # 본문 파트
            if current_page == last_page:
                # 페이지가 같으면 같은 장면 번호 공유 (Merge 효과)
                item["scene_no"] = scene_counter
            else:
                # 페이지가 달라지면 새 장면
                scene_counter += 1
                item["scene_no"] = scene_counter
        
        last_page = current_page
        
    return subtitles


# --------------------------------
# [Step 3 Helper] 독립형 훅(Hook) 생성 함수
# --------------------------------
def generate_standalone_hooks(target_text: str, full_text: str, char_info: dict):
    """
    전체 내용과 핵심 구간을 분석하여, 예고편의 오프닝(또는 엔딩)으로 쓸 수 있는
    '가장 강렬한 한 방(Hook)' 후보 3가지를 생성합니다.
    """
    
    # 캐릭터 정보 문자열 변환
    char_list_str = ""
    if char_info and "characters" in char_info:
        for c in char_info["characters"]:
            char_list_str += f"- {c['name']} (ID: {c['id']}, 성별: {c['gender']}, 말투: {c['tone']})\n"

    system_prompt = f"""
    당신은 영화 예고편의 **'도입부(Hook)' 전문 카피라이터**입니다.
    이야기 전체에서 시청자의 시선을 단 3초 만에 사로잡을 수 있는 **가장 자극적이고 충격적인 장면(대사)** 3가지를 찾으세요.

    [작업 목표]
    - 예고편의 맨 앞(오프닝)이나 맨 뒤(절단신공)에 붙일 수 있는 짧고 강렬한 씬을 만듭니다.
    - 문맥 설명은 최소화하고, **대사 위주**나 **긴박한 상황 묘사**에 집중하세요.

    [후보 3가지 구성]
    1. **Option A (The Shock)**: 가장 충격적인 대사나 사건의 시작점.
    2. **Option B (The Question)**: 호기심을 유발하는 미스터리한 상황.
    3. **Option C (The Action)**: 긴박한 도주, 비명, 충돌 등 역동적인 순간.

    [👥 화자 배정]
    - 아래 캐릭터 ID를 사용하여 `speaker`를 지정하세요.
    {char_list_str}

    [반환 형식 - JSON]
    {{
        "hooks": [
            {{
                "id": "A",
                "type": "충격적 반전형",
                "content": [
                    {{"text": "공양미 삼백 석에 저를 인당수에 던져주세요.", "speaker": "char_01", "source_page": 20}},
                    {{"text": "뭐라고? 심청이 네가 제물이 되겠다고?", "speaker": "char_02", "source_page": 21}}
                ]
            }},
            {{
                "id": "B",
                "type": "미스터리형",
                "content": [ ... ]
            }},
            {{
                "id": "C",
                "type": "액션/위기형",
                "content": [ ... ]
            }}
        ]
    }}
    """
    
    user_content = f"""
    [핵심 하이라이트 구간]
    {target_text}

    [전체 원문 참고]
    {full_text}
    """

    response = client.chat.completions.create(
        model="gpt-5.2", 
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        response_format={"type": "json_object"}
    )
    
    return json.loads(response.choices[0].message.content)

# =========================================================
# [Step 3 Helper] 연속된 씬 압축 로직 (3개 이상 -> 2개)
# =========================================================
def compress_consecutive_scenes(subtitles):
    """
    같은 source_page를 가진 연속된 씬이 3개 이상일 경우,
    문장 단위로 쪼개어 글자 수 균형이 가장 잘 맞는 2개의 씬으로 압축합니다.
    """
    if not subtitles:
        return []

    compressed_subs = []
    
    # 1. 연속된 같은 페이지 그룹핑
    groups = []
    if not subtitles:
        return []
        
    current_group = [subtitles[0]]
    
    for i in range(1, len(subtitles)):
        curr = subtitles[i]
        prev = subtitles[i-1]
        
        # source_page가 있고 서로 같다면 그룹에 추가
        curr_page = curr.get("source_page")
        prev_page = prev.get("source_page")
        
        if curr_page is not None and curr_page == prev_page:
            current_group.append(curr)
        else:
            groups.append(current_group)
            current_group = [curr]
    groups.append(current_group)

    # 2. 그룹별 처리
    for group in groups:
        # 3개 미만이면 그대로 유지
        if len(group) < 3:
            compressed_subs.extend(group)
            continue
            
        # 3개 이상이면 2개로 압축 로직 시작
        # (1) 모든 텍스트를 문장 단위로 분해 (원래 화자 정보 보존)
        sentence_units = []
        for item in group:
            text = item.get("text", "")
            speaker = item.get("speaker", "narrator")
            # 문장 분리 (마침표, 물음표, 느낌표 뒤 공백 기준, 따옴표 내부는 보존 노력)
            # 간단하게 정규식으로 분리 후 빈 문자열 제거
            # (?<=[.?!])\s+ : 문장부호 뒤 공백 기준 분리
            sentences = re.split(r'(?<=[.?!])\s+', text)
            for s in sentences:
                clean_s = s.strip()
                if clean_s:
                    sentence_units.append({
                        "text": clean_s,
                        "origin_speaker": speaker,
                        "origin_item": item
                    })
        
        if not sentence_units: # 예외 처리
            compressed_subs.extend(group)
            continue

        # (2) 최적의 분할 지점 찾기 (글자 수 차이 최소화)
        total_len = sum(len(u["text"]) for u in sentence_units)
        best_split_idx = 1
        min_diff = total_len  # 초기값: 아주 큰 수
        
        # 최소 1문장씩은 가져가야 하므로 range(1, len)
        for i in range(1, len(sentence_units)):
            left_len = sum(len(u["text"]) for u in sentence_units[:i])
            right_len = total_len - left_len
            diff = abs(left_len - right_len)
            
            if diff < min_diff:
                min_diff = diff
                best_split_idx = i
                
        # (3) 2개의 씬으로 병합 생성
        # 왼쪽 파트 / 오른쪽 파트
        parts = [sentence_units[:best_split_idx], sentence_units[best_split_idx:]]
        
        for part in parts:
            if not part: continue
            
            # 텍스트 합치기
            merged_text = " ".join([u["text"] for u in part])
            
            # 화자 결정 로직:
            # 1. 파트 내에 'narrator'가 아닌 캐릭터가 있다면 그 캐릭터를 우선 사용
            # 2. 여러 캐릭터가 섞여 있다면, 첫 번째 등장한 캐릭터(또는 비중 큰 캐릭터) 사용
            # 3. 모두 narrator라면 narrator 사용
            
            final_speaker = "narrator"
            # 나레이터가 아닌 화자들을 수집
            char_speakers = [u["origin_speaker"] for u in part if "narrator" not in u["origin_speaker"].lower()]
            
            if char_speakers:
                # 캐릭터가 하나라도 있으면 그 캐릭터를 화자로 (첫번째 발견된 캐릭터 기준)
                final_speaker = char_speakers[0]
            else:
                # 캐릭터가 없으면 나레이터 유지
                final_speaker = "narrator"

            # 메타 데이터는 해당 파트의 첫 번째 원본 아이템의 것을 일부 승계
            base_item = part[0]["origin_item"]
            
            new_scene = {
                "speaker": final_speaker,
                "text": merged_text,
                "source_page": base_item.get("source_page"),
                "emotion": base_item.get("emotion", "neutral") # 감정은 첫 부분 따라감
            }
            compressed_subs.append(new_scene)

    return compressed_subs

# [step 3 Helper] 버전 파일 목록 가져오기 (내림차순 정렬)
def get_sorted_versions(base_dir, safe_name, mode):
    """
    내림차순 정렬된 버전 숫자 리스트와 파일 경로 맵을 반환
    Returns: 
        sorted_versions: [3, 2, 1] 
        version_map: {3: Path(..v3..), 2: Path(..v2..)}
    """
    pattern = re.compile(rf"script_{re.escape(safe_name)}_{re.escape(mode)}_v(\d+)\.json")
    version_map = {}
    
    if base_dir.exists():
        for f in base_dir.iterdir():
            match = pattern.match(f.name)
            if match:
                ver = int(match.group(1))
                version_map[ver] = f
                
    # 내림차순 정렬 (최신이 0번 인덱스에 오도록)
    sorted_versions = sorted(version_map.keys(), reverse=True)
    return sorted_versions, version_map

# =========================================================
# [step 4 Helper] (Page + Speaker) 기반 정밀 매칭 함수(화자 별 프롬프트 제작 용)
# =========================================================
def find_context_by_structure(target_text, target_speaker_raw, target_page, dialogue_map_data):
    """
    1. Page와 Speaker ID가 일치하는 후보군을 먼저 추립니다.
    2. 후보군 내에서 텍스트 유사도가 가장 높은 Context를 찾습니다.
    """
    from difflib import SequenceMatcher

    # 1. 화자 ID 추출 (예: "char_01 (흥부) young_male" -> "char_01")
    # 나레이터는 context가 없으므로 제외
    if not target_speaker_raw or "narrator" in target_speaker_raw.lower():
        return ""
    
    target_id = target_speaker_raw.split(" ")[0].strip() # "char_01"
    
    # 2. 후보군 필터링 (Page와 Speaker가 같은 것만!)
    candidates = []
    for d in dialogue_map_data:
        # Step 1.5 데이터의 page_num과 speaker_id
        src_page = int(d.get("page_num", -1))
        src_id = d.get("speaker_id", "")
        
        # 페이지 매칭 (대본의 page가 0이면 매칭 불가)
        if target_page > 0 and src_page == target_page:
            if src_id == target_id:
                candidates.append(d)
    
    # 3. 후보가 없으면? (페이지가 달라졌거나 화자가 바뀐 경우) -> 전체 검색으로 확장 (Fallback)
    if not candidates:
        # 페이지 정보가 틀렸을 수도 있으니, 화자 ID만 같은 것 중에서라도 찾음
        candidates = [d for d in dialogue_map_data if d.get("speaker_id") == target_id]

    if not candidates:
        return "" # 해당 화자의 데이터가 아예 없음

    # 4. 텍스트 유사도 비교 (후보군 내에서)
    best_context = ""
    highest_ratio = 0.0
    
    # 공백 제거 정규화
    def normalize(s):
        return re.sub(r"\s+", "", str(s)).strip()
    
    target_norm = normalize(target_text)

    for cand in candidates:
        origin_quote = cand.get("quote", "")
        origin_norm = normalize(origin_quote)
        
        # 완전 일치 (Lucky!)
        if target_norm == origin_norm:
            return cand.get("context", "")
        
        # 유사도 계산
        ratio = SequenceMatcher(None, target_norm, origin_norm).ratio()
        
        if ratio > highest_ratio:
            highest_ratio = ratio
            best_context = cand.get("context", "")
            
    # 유사도가 너무 낮으면(예: 0.3 미만) 엉뚱한 걸 가져올 수 있으니 커트라인 설정
    if highest_ratio < 0.3: 
        return ""

    return best_context

# --------------------------------
# [step 4 helper] 오디오 병합하기
# --------------------------------
def merge_audio_files(audio_paths: list, output_path: str):
    """
    여러 개의 오디오 파일 경로를 받아 하나로 합쳐서 저장합니다.
    """
    clips = []
    try:
        for p in audio_paths:
            if p and os.path.exists(p):
                clips.append(AudioFileClip(str(p)))
        
        if clips:
            final_clip = concatenate_audioclips(clips)
            final_clip.write_audiofile(output_path, logger=None) # logger=None으로 콘솔 출력 억제
            return True
        return False
    except Exception as e:
        print(f"오디오 병합 중 오류: {e}")
        return False
    finally:
        # 리소스 해제
        for clip in clips:
            clip.close()

# [step 4 Helper] 대본_음성 버전 폴더 파싱 함수 
def get_tts_versions_v2(base_dir):
    """
    폴더명이 'v{script_ver}_{audio_ver}' 형식을 기본으로 하되,
    뒤에 모델명(_clova 등)이 붙어도 앞의 숫자 2개를 기준으로 파싱합니다.
    
    Returns:
        sorted_list: [(s_ver, a_ver), ...] (내림차순 정렬)
        path_map: {(s_ver, a_ver): Path객체}
    """
    version_map = {}
    
    if base_dir.exists():
        for item in base_dir.iterdir():
            if item.is_dir() and item.name.startswith("v"):
                # 정규식 대신 split 사용
                # v1_1_clova -> ['v1', '1', 'clova']
                parts = item.name.split("_")
                
                # 최소 2덩어리(v대본, 음성) 이상이면 유효한 폴더로 간주
                if len(parts) >= 2:
                    try:
                        s_ver = int(parts[0].replace("v", "")) # v1 -> 1
                        a_ver = int(parts[1])                  # 1
                        
                        # manifest.json이 있어야 유효 데이터로 인정
                        if (item / "manifest.json").exists():
                            # 맵에 저장 (동일 버전이 여러 개면 나중 것이 덮어써지지만,
                            # 보통 버전 번호를 증가시키므로 큰 문제 없습니다)
                            version_map[(s_ver, a_ver)] = item
                            
                    except ValueError:
                        continue # 숫자가 아니면 패스 (예: v_temp 등)
    
    # 정렬 기준: 대본 버전(내림차순) -> 음성 버전(내림차순)
    sorted_versions = sorted(version_map.keys(), key=lambda x: (x[0], x[1]), reverse=True)
    return sorted_versions, version_map
# --------------------------------
# [Step 5 Helper] 이미지 필터링 및 텍스트 파싱
# --------------------------------
def load_filtered_images(folder_path: Path):
    """
    폴더 내의 PNG 파일을 이름순으로 정렬한 뒤,
    앞 4장(표지, 내지 등)과 뒤 3장(뒷표지 등)을 제외한 유효 이미지 리스트를 반환합니다.
    """
    # 1. 모든 PNG 파일 가져오기
    all_images = sorted(list(folder_path.glob("*.png")))
    
    # 2. 예외 처리: 이미지가 너무 적을 경우
    if len(all_images) <= 7:
        return all_images # 필터링 없이 반환 (오류 방지)
        
    # 3. 앞 4장, 뒤 3장 제외 (Slicing)
    # 0,1,2,3 제외 -> 4부터 시작
    # 뒤에서 3개 제외 -> -3까지
    valid_images = all_images[4:-3]
    
    return valid_images

def parse_book_text_by_page(txt_path: Path):
    """
    TXT 파일 전체를 읽어, 페이지 번호를 키(Key)로 하는 텍스트 딕셔너리를 반환합니다.
    Format: { 6: "6페이지 내용...", 7: "7페이지 내용..." }
    """
    if not txt_path.exists():
        return {}
        
    content = txt_path.read_text(encoding="utf-8")
    page_map = {}
    
    # 정규식: "--- Page 숫자 ---" 패턴 찾기
    # (?s)는 .이 줄바꿈도 포함하게 하는 플래그 (DOTALL)
    # (.*?)는 다음 "--- Page"가 나오기 전까지의 내용을 비탐욕적으로 캡처
    pattern = re.compile(r"--- Page (\d+) ---\n(.*?)(?=--- Page \d+ ---|$)", re.DOTALL)
    
    matches = pattern.findall(content)
    for pg_str, text in matches:
        try:
            pg_num = int(pg_str)
            page_map[pg_num] = text.strip()
        except:
            continue
            
    return page_map

def extract_page_num_from_filename(filename: str):
    """
    파일명에서 페이지 번호 추출.
    지원 패턴:
      - page_006.png  → 6
      - ..._#07.png   → 7  (리딩토탈 시리즈)
      - ..._007.png   → 7  (확장자 직전 숫자)
    """
    m = re.search(r"page_(\d+)", filename)
    if m:
        return int(m.group(1))
    m = re.search(r"#(\d+)", filename)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\.(?:png|jpg|jpeg|webp)$", filename, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None



# --------------------------------
# [Step 6 Helper] 펼침면 분석 및 매칭 로직
# --------------------------------
def analyze_spread_structure(candidates: list):
    """
    페이지들을 2장씩(펼침면) 묶어서 구조를 분석합니다.
    - 규칙: (짝수, 홀수)를 하나의 짝으로 봄 (예: 6-7, 8-9)
    - 반환: { page_num: {"pair": pair_num, "has_text": bool, "is_spread": bool} }
    """
    spread_map = {}

    # page_num이 정수인 항목만 통과 (None/missing 방어)
    candidates = [c for c in candidates if isinstance(c.get("page_num"), int)]

    # page_num 기준으로 정렬
    sorted_pages = sorted(candidates, key=lambda x: x["page_num"])
    
    # 딕셔너리로 변환 (빠른 조회를 위해)
    page_dict = {item["page_num"]: item for item in sorted_pages}
    
    # 그룹핑
    for i, item in enumerate(sorted_pages):
        pg = item["page_num"]
        raw_text = item["text"].strip()
        has_text = (item["text"] != "(텍스트 없음)" and len(raw_text) > 0)
        if i > 0:
            prev_item = sorted_pages[i-1]
            prev_text = prev_item["text"].strip()
            # 앞장과 텍스트가 같고, 텍스트가 존재하는 경우 -> 나는 '그림 전용'으로 취급
            if has_text and raw_text == prev_text:
                has_text = False # 강제 설정
        # 짝꿍 찾기 (짝수면 +1, 홀수면 -1)
        # 예: 6이면 7이 짝꿍, 7이면 6이 짝꿍
        if pg % 2 == 0: 
            pair_pg = pg + 1
        else:
            pair_pg = pg - 1
            
        pair_item = page_dict.get(pair_pg)
        
        spread_info = {
            "my_page": pg,
            "pair_page": pair_pg if pair_item else None,
            "has_text": has_text,
            "img_path": item["img_path"]
        }
        
        spread_map[pg] = spread_info
        
    return spread_map


# --------------------------------
# [Step 6 Helper] 저작권 페이지를 바탕으로 표지 찾기
# --------------------------------

def _norm(s: str) -> str:
    # 줄바꿈/공백/탭 제거 + 소문자화 (배치/줄바꿈 차이 흡수)
    return re.sub(r"\s+", "", (s or "")).lower()

def find_cover_page_num(text_map: dict, candidates: list) -> int:
    """
    저작권 문단이 있는 페이지를 찾고, 그 다음 존재하는 페이지를 표지로 반환.
    실패하면 candidates의 첫 페이지로 fallback.
    """
    if not candidates:
        return 5

    pages_sorted = sorted([c["page_num"] for c in candidates])
    # (1) 저작권 문단 판별용 키워드들 (줄바꿈/붙어쓰기 무관하게 검사)
    # 최소 세트: All rights reserved + Published in Singapore
    # 필요하면 키워드 추가 가능
    kw1 = _norm("All rights reserved")
    kw2 = _norm("Published in Singapore")
    kw3 = _norm("HS PARTNERS PTE LTD")  # 책마다 들어가면 더 강하게 판별

    copyright_pg = None
    for pg, txt in text_map.items():
        t = _norm(txt)
        # 조건은 너무 빡세지 않게: kw1과 kw2는 필수, kw3는 있으면 가산(선택)
        if (kw1 in t) and (kw2 in t):
            copyright_pg = pg
            # kw3까지 포함된 페이지를 더 우선하고 싶으면 여기서 break 대신 우선순위 처리 가능
            break

    # (2) "다음 페이지" 찾기: candidates에 실제로 존재하는 다음 페이지로 점프
    if copyright_pg is not None:
        for pg in pages_sorted:
            if pg > copyright_pg:
                return pg

    # (3) fallback
    return pages_sorted[0]

# [step 6.5 Helper] 프리뷰 버전 파싱 (v{script}_{audio}_{preview})
def get_preview_versions(base_dir):
    version_map = {}
    if base_dir.exists():
        for item in base_dir.iterdir():
            if item.is_dir():
                # 정규식: v(S)_(A)_(P)
                match = re.match(r"^v(\d+)_(\d+)_(\d+)$", item.name)
                if match:
                    s, a, p = map(int, match.groups())
                    if (item / "manifest.json").exists():
                        version_map[(s, a, p)] = item
    # 정렬: S->A->P 내림차순
    sorted_keys = sorted(version_map.keys(), key=lambda x: x, reverse=True)
    return sorted_keys, version_map


# [step 6.5, 8 Helper] 자막 색상 변경

def generate_dynamic_color_map(scripts):
    """
    대본을 분석하여 등장인물별로 고유한 밝은 색상을 배정합니다.
    Returns: { "화자이름": "#HexCode", ... }
    """
    # 1. 시인성이 좋은 밝은 색상 팔레트 (배경이 어두울 때 잘 보이는 색)
    # (노랑, 민트, 핑크, 하늘, 라임, 오렌지, 라벤더, 산호색 등)
    palette = [
        "#FFD700", # Gold
        "#00FFFF", # Cyan
        "#FF69B4", # HotPink
        "#ADFF2F", # GreenYellow
        "#FFA500", # Orange
        "#D8BFD8", # Thistle (연보라)
        "#F0E68C", # Khaki
        "#7FFFD4", # Aquamarine
        "#FF6347", # Tomato
        "#87CEFA", # LightSkyBlue
        "#EE82EE", # Violet
        "#98FB98", # PaleGreen
    ]
    
    # 2. 대본에서 고유 화자 추출
    unique_speakers = set()
    for s in scripts:
        spk = s.get("speaker", "narrator")
        unique_clips = str(spk).strip()
        unique_speakers.add(unique_clips)
        
    # 3. 색상 매핑 생성
    color_map = {}
    
    # 나레이터 계열은 무조건 흰색 고정
    narrator_keys = ["narrator", "narration", "나레이션", "해설"]
    
    palette_idx = 0
    
    # 정렬하여 할당 (매번 같은 사람이 같은 색을 받도록)
    for spk in sorted(list(unique_speakers)):
        # 나레이터 체크
        is_narrator = any(n in spk.lower() for n in narrator_keys)
        
        if is_narrator:
            color_map[spk] = "white"
        else:
            # 팔레트에서 색 꺼내기 (사람이 많으면 로테이션)
            assigned_color = palette[palette_idx % len(palette)]
            color_map[spk] = assigned_color
            palette_idx += 1
            
    return color_map


# [step 7 Helper] 3단 버전 폴더 파싱 (v{script}_{audio}_{video})
def get_video_versions_v3(base_dir):
    """
    폴더명이 'v{s}_{a}_{v}' 형식인 것을 찾아 파싱합니다.
    Returns:
        sorted_keys: [(s, a, v), ...] (내림차순)
        path_map: {(s, a, v): Path객체}
    """
    version_map = {}
    
    if base_dir.exists():
        for item in base_dir.iterdir():
            if item.is_dir():
                # 정규식: v(숫자)_(숫자)_(숫자)
                match = re.match(r"^v(\d+)_(\d+)_(\d+)$", item.name)
                if match:
                    s = int(match.group(1)) # 대본
                    a = int(match.group(2)) # 음성
                    v = int(match.group(3)) # 영상
                    
                    if (item / "manifest.json").exists():
                        version_map[(s, a, v)] = item
    
    # 정렬: S -> A -> V 순서로 내림차순
    sorted_keys = sorted(version_map.keys(), key=lambda x: (x[0], x[1], x[2]), reverse=True)
    return sorted_keys, version_map

# [step 8 Helper] 4단 버전 파싱 (v{S}_{A}_{V}_{F})
def get_final_versions(base_dir):
    """
    폴더명이 'v{s}_{a}_{v}_{f}' 형식인 것을 찾아 파싱
    """
    version_map = {}
    if base_dir.exists():
        for item in base_dir.iterdir():
            if item.is_dir():
                # 정규식: v(S)_(A)_(V)_(F)
                match = re.match(r"^v(\d+)_(\d+)_(\d+)_(\d+)$", item.name)
                if match:
                    s, a, v, f = map(int, match.groups())
                    if (item / "manifest.json").exists():
                        version_map[(s, a, v, f)] = item
    
    # 정렬: S->A->V->F 내림차순
    sorted_keys = sorted(version_map.keys(), key=lambda x: x, reverse=True)
    return sorted_keys, version_map

# --------------------------------
# 2. 메인 실행 함수 (Render Function)
# --------------------------------

def run_text_analysis_mode(client, folder, txt_file):
    """
    app.py에서 호출되는 메인 함수입니다.
    :param client: OpenAI Client 객체
    :param folder: 이미지 폴더 Path 객체
    :param txt_file: 텍스트 파일 Path 객체
    """
    if "script_results" not in st.session_state:
        st.session_state["script_results"] = {} 
    st.info("📜 전체 줄거리를 바탕으로 예고편 대본을 먼저 작성합니다.")

    # 0. 파일 확인 및 텍스트 로드
    if txt_file.exists():
        full_text = txt_file.read_text(encoding="utf-8")
        extracted_title = extract_title_from_filename(txt_file.name)
        with st.expander(f"📄 원본 텍스트 확인 (추출 제목: {extracted_title})"):
            st.text_area("Full Text", full_text, height=200)
    else:
        st.error("TXT 파일이 없습니다.")
        st.stop()

    st.divider()


    # 저장 경로 설정 (text/tts.mp3/mp4)

    story_dir_name = txt_file.stem
    TEXT_OUT = Path("outputs") / story_dir_name / "TEXT"
    TEXT_OUT.mkdir(parents=True, exist_ok=True)
    # 파일명 안전하게 변환 (확장자 제외한 이름 사용)
    safe_name = Path(txt_file.name).stem
    paths = {
        "analysis":    TEXT_OUT / f"analysis_{safe_name}.json",
        "characters": TEXT_OUT / f"characters_{safe_name}.json",
        "segments": TEXT_OUT / f"segments_{safe_name}.json",
        "script":     TEXT_OUT / f"script_{safe_name}.json",
    }

    # -----------------------------------------
    # [Step 1] 동화 내용 분석
    # -----------------------------------------
    st.subheader("Step 1. 동화 내용 분석 및 구조화")

    # 1. 초기 세션 상태 및 파일 경로 설정
    if "track_b_analysis" not in st.session_state:
        st.session_state.track_b_analysis = None

    analysis_file = paths["analysis"]

    # 2. 저장된 파일이 있다면 자동으로 불러오기 (세션이 비어있을 때만)
    if st.session_state.track_b_analysis is None and analysis_file.exists():
        try:
            with open(analysis_file, "r", encoding="utf-8") as f:
                st.session_state.track_b_analysis = json.load(f)
            st.toast("📂 저장된 분석 결과를 불러왔습니다.", icon="✅")
        except Exception as e:
            st.error(f"파일 로드 중 오류: {e}")

    # 3. 분석 버튼 상태 결정 
    if st.session_state.track_b_analysis is None:
        btn_label_1 = "🔍 AI 동화 분석 시작"
        btn_type_1 = "primary"
    else:
        btn_label_1 = "🔄 다시 분석하기 (덮어쓰기)"
        btn_type_1 = "secondary"

    # 4. 분석 실행 버튼
    if st.button(btn_label_1, type=btn_type_1):
        with st.spinner(f"GPT가 '{extracted_title}' 이야기를 분석 중입니다..."):
            try:
                # AI 분석 함수 호출 (force run)
                result = analyze_story_structure(full_text, known_title=extracted_title)
                
                # 파일 저장 및 세션 업데이트
                with open(analysis_file, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=4)
                
                st.session_state.track_b_analysis = result
                st.success("✅ 분석 및 저장 완료!")
                st.rerun()
            except Exception as e:
                st.error(f"분석 중 오류 발생: {e}")

    # 분석 결과 화면 표시
    if st.session_state.track_b_analysis:
        data = st.session_state.track_b_analysis
        col_t, col_m = st.columns([2, 1])
        with col_t: st.markdown(f"### 🏷️ {data.get('title', extracted_title)}")
        with col_m: st.info(f"**💡 교훈:** {data.get('moral', '없음')}")
        st.markdown("#### 📝 전체 줄거리 요약")
        st.write(data.get("summary", ""))
        
        st.markdown("#### 🌊 이야기 구조 (기승전결 & 페이지 범위)")
        structure = data.get("plot_structure", {})
        
        # 탭 구성
        tab1, tab2, tab3, tab4 = st.tabs(["1. 발단 (Intro)", "2. 전개 (Dev)", "3. 위기/절정 (Climax)", "4. 결말 (Res)"])
        
        # 헬퍼 함수: 구조 데이터 표시
        def display_phase_info(phase_data):
            if isinstance(phase_data, dict):
                # 새로운 구조 (Dict)
                s_page = phase_data.get("start_page", "?")
                e_page = phase_data.get("end_page", "?")
                st.markdown(f"**📖 Page Range:** `{s_page}p ~ {e_page}p`")
                st.write(phase_data.get("summary", "내용 없음"))
            else:
                # 구버전 호환 (String)
                st.write(phase_data)

        with tab1: 
            st.success("🌱 이야기의 시작")
            display_phase_info(structure.get("introduction", {}))
        with tab2: 
            st.info("☁️ 사건의 전개")
            display_phase_info(structure.get("development", {}))
        with tab3: 
            st.warning("⚡ 위기 및 절정")
            display_phase_info(structure.get("climax", {}))
        with tab4: 
            st.error("✨ 결말")
            display_phase_info(structure.get("resolution", {}))

        st.divider()
        col_msg, col_btn = st.columns([3, 1])
        with col_msg: st.caption("위 분석 내용이 맞다면 다음 단계로 넘어가세요.")
        with col_btn:
            if st.button("➡️ Step 1.5: 등장인물 및 화자 분석 "):
                st.session_state.track_b_step = 2
                st.rerun()

    # =========================================================
    # [Step 1.5] 등장인물 및 화자 분석 (Character & Speaker Analysis)
    # =========================================================
    # Step 1 분석이 완료되었거나, 사용자가 수동으로 1.5단계를 열었을 때
    if st.session_state.get("track_b_analysis") and st.session_state.get("track_b_step", 0) >= 1:
        st.divider()
        st.subheader("Step 1.5. 등장인물 및 화자 정밀 분석")
        st.info("🗣️ 등장인물의 성별, 나이, 말투를 정의하고 모든 대사의 주인을 찾습니다. (TTS 및 영상 일관성용)")

        # 1. 파일 경로 설정
        char_file = paths["characters"]

        # 2. 세션 초기화 & 자동 불러오기
        if "track_b_characters" not in st.session_state:
            if char_file.exists():
                try:
                    with open(char_file, "r", encoding="utf-8") as f:
                        st.session_state.track_b_characters = json.load(f)
                    st.toast("📂 저장된 캐릭터 분석 정보를 불러왔습니다.", icon="✅")
                except:
                    st.session_state.track_b_characters = None
            else:
                st.session_state.track_b_characters = None

        # 3. 분석 버튼 상태 결정
        if st.session_state.track_b_characters is None:
            btn_label_15 = "👥 캐릭터 및 화자 분석 시작"
            btn_type_15 = "primary"
        else:
            btn_label_15 = "🔄 다시 분석하기 (덮어쓰기)"
            btn_type_15 = "secondary"

        # 분석 실행 버튼 로직
        if st.button(btn_label_15, type=btn_type_15, key="btn_step15_character_analysis"):
            with st.spinner("등장인물을 식별하고, 어울리는 목소리(성우)를 매칭 중입니다..."):
                try:
                    # 1) GPT 분석 함수 호출 (프롬프트가 voice_type을 포함)
                    result_15 = analyze_characters_and_speakers(client, full_text)
                    
                    # 2) 분석 결과에 성우 자동 매칭 (Mapping)
                    # GPT가 준 'voice_type'을 UI용 'voice_label'로 변환하여 저장
                    for char in result_15.get("characters", []):
                        # GPT가 제안한 타입 (예: adult_male_deep)
                        gpt_type = char.get("voice_type", "narrator")
                        
                        # UI 표시용 라벨로 변환 (예: 👨 성인 남성 (원탁...))
                        # 매핑되지 않는 타입이 오면 기본값 설정
                        ui_label = GPT_VOICE_TO_UI_LABEL.get(gpt_type, "--- 자동/기본값 ---")
                        
                        # 데이터에 저장
                        char["voice_label"] = ui_label

                    # 3) 파일 저장
                    with open(char_file, "w", encoding="utf-8") as f:
                        json.dump(result_15, f, ensure_ascii=False, indent=4)
                    
                    st.session_state.track_b_characters = result_15
                    st.success("✅ 화자 분석 및 성우 캐스팅 완료!")
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"분석 중 오류 발생: {e}")

        # 4. 결과 확인 및 편집 (Data Editor)
        if st.session_state.track_b_characters:
            char_data = st.session_state.track_b_characters.get("characters", [])
            
            # [안전장치] 기존 파일에 'voice_label' 필드가 없을 경우 대비 (기본값 채우기)
            for char in char_data:
                if "voice_label" not in char:
                    char["voice_label"] = "--- 자동/기본값 ---"
        
            st.markdown("#### 🎭 등장인물 프로필 & 성우 설정")
            st.caption("AI가 추천한 목소리가 마음에 들지 않으면, 아래 **'지정 목소리'** 항목을 클릭하여 변경하세요.")

            # 데이터 에디터 
            edited_chars = st.data_editor(
                char_data,
                column_config={
                    "name": "이름 (대표)",
                    "gender": st.column_config.SelectboxColumn("성별", options=["Male", "Female", "Unknown"], width="small"),
                    "age_group": st.column_config.SelectboxColumn("연령대", options=["Child", "Young","Adult", "Elder"], width="small"),
                    
                    # ★ 목소리 선택 
                    "voice_label": st.column_config.SelectboxColumn(
                        "🎙️ 지정 목소리 (TTS)",
                        options=list(VOICE_PRESETS.keys()), # 상단에 정의한 프리셋 목록
                        width="medium",
                        required=True,
                        help="이 캐릭터의 대사를 읽을 성우를 선택하세요."
                    ),
                    
                    "tone": st.column_config.TextColumn("말투/성격", width="medium"),
                    "visual": st.column_config.TextColumn("외모 묘사", width="medium"),
                    "id": st.column_config.TextColumn("ID", disabled=True, width="small")
                },
                num_rows="dynamic",
                key="editor_chars_1_5"
            )

            # --------------------------------
            # 🔊 캐릭터별 음성 미리듣기
            # --------------------------------
            st.divider()
            st.markdown("#### 🔊 캐릭터 음성 미리듣기")
            st.caption("배정된 목소리가 어떤 느낌인지 확인해보세요. 위 표에서 '지정 목소리'를 바꾸고 다시 듣기를 누르면 변경된 음성이 재생됩니다.")

            if "voice_preview_cache" not in st.session_state:
                st.session_state.voice_preview_cache = {}

            for i, char in enumerate(edited_chars):
                char_name = char.get("name") or f"캐릭터 {i+1}"
                voice_label = char.get("voice_label", "--- 자동/기본값 ---")

                preview_col_info, preview_col_btn = st.columns([4, 1])
                with preview_col_info:
                    st.markdown(f"**{char_name}**  ·  _{voice_label}_")
                    sample = VOICE_SAMPLE_TEXTS.get(voice_label)
                    if sample:
                        st.caption(f"샘플 대사: \"{sample}\"")
                with preview_col_btn:
                    btn_key = f"voice_preview_btn_{i}"
                    if st.button("🔊 듣기", key=btn_key, use_container_width=True):
                        with st.spinner("음성 생성 중..."):
                            audio_path = generate_voice_preview(voice_label)
                            if audio_path:
                                st.session_state.voice_preview_cache[i] = {
                                    "path": audio_path,
                                    "label": voice_label,
                                }
                            else:
                                st.session_state.voice_preview_cache[i] = {
                                    "path": None,
                                    "label": voice_label,
                                    "error": True,
                                }

                cache_entry = st.session_state.voice_preview_cache.get(i)
                if cache_entry and cache_entry.get("label") == voice_label:
                    if cache_entry.get("path") and os.path.exists(cache_entry["path"]):
                        st.audio(cache_entry["path"])
                    elif cache_entry.get("error"):
                        st.warning("음성 생성에 실패했어요. Clova API 키 또는 음성 설정을 확인해주세요.")

                st.divider()

            st.markdown("#### 💬 대사 분석 결과 (화자 & 페이지)")
            st.caption("각 대사가 **누구의 말**이며 **몇 페이지**에 나오는지 확인하세요.")

            # 대사 리스트 확인 (기존 동일)
            dialogue_data = st.session_state.track_b_characters.get("dialogue_map", [])
            
            st.dataframe(
                dialogue_data, 
                column_config={
                    "quote": "대사 내용",
                    "speaker_id": "화자 ID",
                    "page_num": st.column_config.NumberColumn("페이지(Page)", format="%d"),
                    "context": "상황 설명"
                },
                use_container_width=True
            )

            # 저장 및 다음 단계 버튼
            col_c1, col_c2 = st.columns([3, 1])
            with col_c1:
                st.caption("캐릭터 정보를 확정하고 저장합니다.")
            with col_c2:
                if st.button("✅ 캐릭터 확정 (Step 2 이동)"):
                    # 캐릭터 정보 저장
                    st.session_state.track_b_characters["characters"] = edited_chars
                    with open(char_file, "w", encoding="utf-8") as f:
                        json.dump(st.session_state.track_b_characters, f, ensure_ascii=False, indent=4)
                    
                    st.session_state.track_b_step = 2
                    st.rerun()

    # =========================================================
    # [Step 2] 예고편 구간 선택 (Step 1이 완료되어야 표시)
    # =========================================================
    if st.session_state.get("track_b_step", 0) >= 2:
        st.divider()
        st.subheader("Step 2. 예고편 구간(Highlight) 선택")
        st.info("🎞️ 전체 이야기 중, 예고편으로 만들었을 때 가장 흥미진진한 부분을 선택하세요.")
        segments_file =  paths["segments"]
        
        # 세션 초기화 & 자동 불러오기
        if "track_b_segments" not in st.session_state:
            # 저장된 파일이 있는지 확인
            if segments_file.exists():
                try:
                    with open(segments_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        st.session_state.track_b_segments = data.get("options", [])
                    st.toast("📂 저장된 추천 구간을 불러왔습니다.", icon="✅")
                except:
                    st.session_state.track_b_segments = None
            else:
                st.session_state.track_b_segments = None

        if "selected_segment_index" not in st.session_state:
            st.session_state.selected_segment_index = 0 

        # 구간 추천 버튼 (상태에 따라 텍스트 변경)
        if st.session_state.track_b_segments is None:
            btn_label = "🎬 AI 예고편 구간 추천받기 (3가지 옵션)"
            btn_type = "primary"
        else:
            btn_label = "🔄 다른 구간 추천받기 (다시 분석)"
            btn_type = "secondary"

        if st.button(btn_label, type=btn_type):
            if not st.session_state.track_b_analysis:
                st.error("Step 1 분석을 먼저 완료해주세요.")
            else:
                with st.spinner("GPT가 텍스트를 훑으며 가장 쫄깃한 구간을 찾고 있습니다..."):
                    try:
                        # GPT 호출
                        segments_result = recommend_trailer_segments(
                            full_text, 
                            st.session_state.track_b_analysis
                        )
                        
                        # 결과를 JSON 파일로 저장
                        with open(segments_file, "w", encoding="utf-8") as f:
                            json.dump(segments_result, f, ensure_ascii=False, indent=4)

                        st.session_state.track_b_segments = segments_result.get("options", [])
                        st.success("3가지 예고편 후보를 찾았습니다!")
                        st.rerun() # UI 갱신을 위해 리런
                    except Exception as e:
                        st.error(f"구간 추천 중 오류 발생: {e}")

        # 추천 결과가 있으면 선택 UI 표시
        if st.session_state.track_b_segments:
            options = st.session_state.track_b_segments
            
            # 라디오 버튼을 위한 라벨 생성
            radio_options = [f"[{opt['type']}] {opt['title']}" for opt in options]
            
            selected_label = st.radio(
                "어떤 스타일의 예고편을 만드시겠습니까?",
                radio_options,
                index=st.session_state.selected_segment_index
            )
            
            # 선택된 옵션의 인덱스 찾기
            sel_idx = radio_options.index(selected_label)
            st.session_state.selected_segment_index = sel_idx 
            
            selected_data = options[sel_idx]

            # 선택된 구간 상세 정보 보여주기
            with st.container(border=True):
                st.markdown(f"### 👉 선택된 컨셉: {selected_data['title']}")
                st.caption(f"💡 추천 이유: {selected_data['reason']}")
                st.text_area(
                    "📜 예고편으로 쓰일 원문 구간 (이 내용을 바탕으로 대본이 작성됩니다)", 
                    selected_data['target_text'], 
                    height=200
                )

            st.divider()
            
            # 다음 단계(Step 3)로 이동 버튼
            col_msg_2, col_btn_2 = st.columns([3, 1])
            with col_msg_2:
                st.caption("이 구간으로 확정하고 대본을 작성하시겠습니까?")
            with col_btn_2:
                if st.button("✅ 구간 확정 (Step 3 대본작성)"):
                    st.session_state.track_b_step = 3
                    # 다음 단계에서 쓸 텍스트를 세션에 저장해둡니다.
                    st.session_state.target_trailer_text = selected_data['target_text']
                    st.toast("Step 3: 대본 작성 단계로 이동합니다.")
                    st.rerun()

    # =========================================================
    # [Step 3] 예고편 대본 작성 (모드별 개별 저장/관리 적용)
    # =========================================================
    if st.session_state.get("track_b_step", 0) >= 3:
        st.divider()
        st.subheader("Step 3. 맞춤형 예고편 대본 작성")
        st.info("🎙️ 주인공 소개부터 위기까지, 한 편의 영화 예고편처럼 이야기를 재구성합니다.")

        # 1. 월령 정보 및 기본 설정
        if "story_months" not in st.session_state:
            st.session_state.story_months = extract_age_from_filename(txt_file.name)
        
        months = st.session_state.story_months
        rec_info = get_recommendation_by_age(months)
        months_display = f"{months}개월" if months > 0 else "미상"

        # 캐릭터 정보 로드
        char_info = st.session_state.get("track_b_characters", {})
        
        # 화자 옵션 구성
        base_speakers = ["narrator"]
        character_options = []
        if char_info and "characters" in char_info:
            for c in char_info["characters"]:
                character_options.append(f"{c['id']} ({c['name']}) {c['voice_type']}")
        
        all_speaker_options = base_speakers + character_options + ["child_male", "child_female","child_bright", "young_female","young_male","adult_female","adult_male","adult_male_deep","elder_female","elder_male","cute_animal","dog","fairy"]

        # ------------------------------------------------------------------
        # [UI] 길이 및 스타일 선택
        # ------------------------------------------------------------------
        st.markdown(f"**대상 연령:** {months_display} ({rec_info['group']})")
        
        duration_options = ["Short (30~50초)", "Standard (1분~1분 20초)", "Long (1분 30초~1분 50초)"]
        default_dur_idx = 1
        if rec_info["default_option"] == "Short": default_dur_idx = 0
        elif rec_info["default_option"] == "Long": default_dur_idx = 2

        selected_duration = st.radio(
            "⏳ 영상 길이를 선택하세요:",
            duration_options,
            index=default_dur_idx,
            horizontal=True
        )
        duration_key = selected_duration.split()[0]

        st.write("🎭 **대본 구성 스타일 선택:**")
        style_options = [
            "1️⃣ 일반 버전 (밸런스형)", 
            "2️⃣ 대화문 위주 (캐릭터 연기 중심)", 
            "3️⃣ 종합 구성 모드 (요약 + 하이라이트)"
        ]
        
        style_selection = st.radio(
            "스타일을 선택하세요:",
            style_options,
            index=0,
            horizontal=True,
            label_visibility="collapsed",
            key="script_style_radio"
        )

        # 모드 식별자(Key) 결정
        if "대화문" in style_selection:
            script_style_mode = "Conversation"
            st.info("💡 **대화문 위주:** 캐릭터의 티키타카를 중심으로, 나레이션을 최소화하여 몰입감을 높입니다.")
        elif "종합 구성" in style_selection:
            script_style_mode = "Comprehensive"
            st.info("💡 **종합 구성 모드:** [초반 요약(30%)] + [핵심 사건 집중(70%)] 구조입니다.")
        else:
            script_style_mode = "Standard"
            st.caption("💡 **일반 버전:** 원문의 흐름을 적절히 요약하고 발췌하여 가장 안정적인 예고편을 만듭니다.")
        
        st.session_state["script_style_mode"] = script_style_mode

        # ------------------------------------------------------------------
        # [핵심 파트] 모드별 폴더 경로 정의 및 생성
        # ------------------------------------------------------------------
        safe_name = Path(txt_file.name).stem
        
        # 예: output/text/Standard/
        mode_dir = TEXT_OUT / script_style_mode
        
        # 폴더가 없으면 미리 생성 (parents=True: 상위폴더 없으면 생성, exist_ok=True: 이미 있어도 에러 안 냄)
        mode_dir.mkdir(parents=True, exist_ok=True)
        
        # ------------------------------------------------------------------
        # [핵심 로직] 해당 폴더 내에서 버전 스캔
        # ------------------------------------------------------------------
        # TEXT_OUT 대신 mode_dir를 넘겨줍니다.
        sorted_versions, version_map = get_sorted_versions(mode_dir, safe_name, script_style_mode)
        
        selected_version = None
        current_file_path = None
        
        # [UI] 버전 선택 (저장된 파일이 있을 경우)
        if sorted_versions:
            col_ver, col_space = st.columns([1, 2])
            with col_ver:
                selected_version = st.selectbox(
                    f"📂 '{script_style_mode}' 저장본 불러오기:",
                    sorted_versions,
                    format_func=lambda x: f"Ver. {x} (최신)" if x == sorted_versions[0] else f"Ver. {x}",
                    key=f"version_selector_{script_style_mode}"
                )
                # ★ 현재 선택된 대본 버전을 세션에 저장
                st.session_state.current_script_ver = selected_version
            
            # 선택된 파일 경로 (이미 mode_dir 안에 있는 파일임)
            current_file_path = version_map[selected_version]
            
            # 세션 로드 체크
            if st.session_state.get(f"loaded_path_{script_style_mode}") != current_file_path:
                try:
                    with open(current_file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        st.session_state.script_results[script_style_mode] = data
                        st.session_state[f"loaded_path_{script_style_mode}"] = current_file_path
                except Exception as e:
                    st.error(f"파일 로드 실패: {e}")
        
        current_data = st.session_state["script_results"].get(script_style_mode)


        
        # 다음 버전 번호 계산
        next_ver = (sorted_versions[0] + 1) if sorted_versions else 1

        # ------------------------------------------------------------------
        # [UI] 대본 생성 버튼
        # ------------------------------------------------------------------
        # 텍스트 준비
        raw_target_text = st.session_state.get("target_trailer_text", "")
        target_text = str(raw_target_text) if raw_target_text else ""
        
        if "full_text_cache" not in st.session_state:
            st.session_state.full_text_cache = txt_file.read_text(encoding="utf-8") if txt_file.exists() else ""
        full_text_content = st.session_state.full_text_cache

        btn_label = f"✍️ {script_style_mode} 대본 작성"
        # 데이터가 있으면 "새 버전 생성", 없으면 "대본 작성"
        if current_data:
            btn_label = f"✨ 대본 새로 생성하기 (Ver. {next_ver})"
            help_msg = "현재 내용을 유지한 채, 새로운 버전을 추가로 생성합니다."
        else:
            btn_label = f"✍️ {script_style_mode} 대본 작성 (Ver. 1)"
            help_msg = "첫 번째 대본을 생성합니다."

        if st.button(btn_label, type="primary", help=help_msg):
            if not target_text:
                st.error("Step 2에서 구간을 먼저 선택해주세요.")
            else:
                # 스포일러 방지 및 데이터 준비 (공통)
                page_map = st.session_state.get("page_map", {})
                spoiler_limit_page = find_spoiler_limit_page(target_text, page_map)
                safe_full_text = trim_full_text_by_page(full_text_content, spoiler_limit_page)
                safe_char_info = dict(char_info) if char_info else {}
                if safe_char_info and "dialogue_map" in safe_char_info:
                    safe_char_info["dialogue_map"] = trim_dialogue_map_by_page(safe_char_info["dialogue_map"], spoiler_limit_page)

                # AI 생성 시작
                with st.spinner(f"AI가 '{script_style_mode}' 스타일로 대본을 쓰고 있습니다..."):
                    try:
                        # 모드별 함수 호출
                        if script_style_mode == "Conversation":
                            result = generate_conversation_oriented_script(
                                target_text, duration_key, rec_info, safe_full_text, safe_char_info
                            )
                        elif script_style_mode == "Comprehensive":
                            analysis_data = st.session_state.get("track_b_analysis", {})
                            result = generate_comprehensive_script(
                                target_text, duration_key, rec_info, safe_full_text, safe_char_info, analysis_data
                            )
                        else: # Standard
                            result = generate_script_with_specs(
                                target_text, duration_key, rec_info, safe_full_text, safe_char_info
                            )

                        # ID -> 풀네임 변환 (공통 후처리)
                        if char_info and "characters" in char_info:
                            id_to_full = {c['id']: f"{c['id']} ({c['name']}) {c['voice_type']}" for c in char_info["characters"]}
                            for item in result.get("subtitles", []):
                                spk_id = item.get("speaker", "narrator")
                                if spk_id in id_to_full:
                                    item["speaker"] = id_to_full[spk_id]

                        # =========================================================
                        # [Step B] 대사 내 괄호 및 지문 제거 (독립 실행)
                        # =========================================================
                        # 자막 리스트를 별도로 순회하며 텍스트만 정제
                        for item in result.get("subtitles", []):
                            original_text = item.get("text", "")
                            
                            # 괄호가 포함된 경우에만 로직 수행
                            if "(" in original_text and ")" in original_text:
                                # 1. 괄호와 그 안의 내용 제거: (1단계), (지문) 등
                                clean_text = re.sub(r'\([^)]*\)', '', original_text)
                                
                                # 2. 공백 정리: 괄호 삭제로 생긴 다중 공백을 하나로 축소
                                clean_text = re.sub(r'\s+', ' ', clean_text).strip()
                                
                                # 3. 적용
                                item["text"] = clean_text
                        # =========================================================
                        # ★  씬 압축 로직 (같은 Page 3개 이상 -> 2개 압축) ★
                        # =========================================================
                        # Scene No 할당 전에 압축을 먼저 수행해야 합니다.
                        result["subtitles"] = compress_consecutive_scenes(result["subtitles"])

                        # ---------------------------------------------------------
                        # ★  장면 번호(Scene No) 자동 그룹핑 적용 ★
                        # ---------------------------------------------------------
                        result["subtitles"] = assign_scene_numbers(result["subtitles"])
                        # ---------------------------------------------------------

                        # [저장 로직] 새 버전 파일 생성 mode_dir 아래 저장
                        new_filename = f"script_{safe_name}_{script_style_mode}_v{next_ver}.json"
                        new_file_path = mode_dir / new_filename
                        
                        try:
                            with open(new_file_path, "w", encoding="utf-8") as f:
                                json.dump(result, f, ensure_ascii=False, indent=4)
                            
                            # 세션 갱신
                            st.session_state.script_results[script_style_mode] = result
                            st.session_state[f"loaded_path_{script_style_mode}"] = new_file_path
                            
                            st.toast(f"✅ Ver. {next_ver} 생성 완료!")
                            # Rerun하면 sorted_versions의 맨 앞에 새 버전이 오므로, 
                            # Selectbox가 자동으로 새 버전을 가리키게 됨.
                            st.rerun()
                            
                        except Exception as e:
                            st.error(f"저장 중 오류: {e}")
                        
                    except Exception as e:
                        st.error(f"생성 중 오류 발생: {e}")
                # 새로 만든 버전을 현재 버전으로 저장 
                st.session_state.current_script_ver = next_ver

        # ------------------------------------------------------------------
        # [UI] 결과 확인 및 에디터 (Data Editor)
        # ------------------------------------------------------------------
        if current_data:
            st.divider()
            ver_label = selected_version if selected_version else next_ver - 1
            st.markdown(f"### 📝 대본 에디터 (Ver. {ver_label})")
            st.caption(f"💡 AI 코멘트: {current_data.get('comment', '')}")

            subtitles = current_data.get("subtitles", [])
            
            # 화자 ID 매칭용
            id_to_option = {opt.split(" ")[0]: opt for opt in character_options}
            for item in subtitles:
                spk = item.get("speaker", "narrator")
                if spk in id_to_option:
                    item["speaker"] = id_to_option[spk]

            # 데이터 에디터 (모드별로 key를 다르게 주어 충돌 방지)
            edited_subtitles = st.data_editor(
                subtitles,
                column_config={
                    # 장면 번호 컬럼  (사용자가 직접 수정하여 합치거나 나누기 가능)
                    "scene_no": st.column_config.NumberColumn(
                        "장면# (Scene)", 
                        help="같은 번호를 가진 대사는 하나의 그림/영상 배경을 공유합니다.",
                        width="small",
                        step=1,
                        required=True
                    ),
                    "speaker": st.column_config.SelectboxColumn("화자", options=all_speaker_options, width="medium", required=True),
                    "text": st.column_config.TextColumn("대사 (Subtitle)", width="large", required=True),
                    "source_page": st.column_config.NumberColumn("Page", width="small")
                },
                num_rows="dynamic",
                use_container_width=True,
                key=f"editor_{script_style_mode}_v{ver_label}"  # Key 중요: 모드별 독립적 상태 유지
            )

            # 글자수 계산
            total_chars = sum(len(row["text"]) for row in edited_subtitles)
            est_time = total_chars / 6.6
            st.info(f"📊 글자 수: **{total_chars}자** (약 {est_time:.1f}초)")

            # ------------------------------------------------------------------
            # [Step 4 이동] 확정 버튼
            # ------------------------------------------------------------------
            col_msg, col_btn = st.columns([3, 1])
            with col_msg:
                st.caption(f"현재 보고 있는 '{script_style_mode}' 대본을 최종본으로 사용하여 다음 단계로 넘어갑니다.")
            with col_btn:
                if st.button("✅ 확정 및 Step 4 이동"):
                    # 1. 세션 데이터 업데이트 (편집본 반영)
                    current_data["subtitles"] = edited_subtitles
                    st.session_state.script_results[script_style_mode] = current_data
                    st.session_state.step1_scripts = edited_subtitles # 다음 단계용
                    
                    # 2. 현재 파일에 덮어쓰기 (새 버전 생성 X)
                    if current_file_path:
                        with open(current_file_path, "w", encoding="utf-8") as f:
                            json.dump(current_data, f, ensure_ascii=False, indent=4)
                    
                    st.session_state.track_b_step = 4
                    st.toast("대본이 확정되었습니다.")
                    st.rerun()
    # =========================================================
    # [Step 4] TTS 생성 및 전체 미리듣기
    # =========================================================
    if st.session_state.get("track_b_step", 0) >= 4:
        st.divider()
        st.subheader("Step 4. TTS 음성 생성 및 확인")
        
        # 1. 현재 대본 정보 확인
        # Step 3에서 넘어온 script_ver가 없으면 1로 가정 (혹은 에러 처리)
        current_script_ver = st.session_state.get("current_script_ver", 1)
        current_mode = st.session_state.get("script_style_mode", "Standard")
        
        st.info(f"🎧 현재 **대본 Ver. {current_script_ver} ({current_mode})**을 기반으로 작업을 진행합니다.")

        # 2. 경로 설정
        story_dir_name = txt_file.stem
        # outputs / 동화이름 / tts / 모드명
        TTS_MODE_BASE_DIR = Path("outputs") / story_dir_name / "tts" / current_mode
        TTS_MODE_BASE_DIR.mkdir(parents=True, exist_ok=True)

        # ------------------------------------------------------------------
        # [Logic] 기존 버전 스캔 (v{s}_{a}_{model} 대응)
        # ------------------------------------------------------------------
        # 폴더명을 분석하여 (script_ver, audio_ver) -> path 매핑
        # 뒤에 모델명이 붙어도 앞의 숫자만 파싱하여 버전을 관리함
        sorted_keys, path_map = get_tts_versions_v2(TTS_MODE_BASE_DIR)
        dir_list = [d for d in TTS_MODE_BASE_DIR.iterdir() if d.is_dir() and d.name.startswith("v")]
        
        version_map = {}  # key: (s_ver, a_ver), value: {path, model_name}
        
        for d in dir_list:
            try:
                # 예: v1_1_clova -> parts=["v1", "1", "clova"]
                # 예: v1_2       -> parts=["v1", "2"]
                parts = d.name.split("_")
                if len(parts) >= 2:
                    s_ver = int(parts[0].replace("v", "")) # v1 -> 1
                    a_ver = int(parts[1])                  # 1
                    
                    # 모델명 추출 (3번째 요소부터 끝까지, 혹은 없으면 "Unknown")
                    model_suffix = "_".join(parts[2:]) if len(parts) > 2 else ""
                    
                    # (s, a) 키가 중복될 일은 시스템상 없다고 가정 (있으면 덮어씀)
                    version_map[(s_ver, a_ver)] = {
                        "path": d,
                        "model": model_suffix,
                        "full_name": d.name
                    }
            except Exception:
                continue # 파싱 실패한 폴더는 무시

        # 키 정렬 (최신 버전이 위로 오도록 내림차순 정렬)
        sorted_keys = sorted(version_map.keys(), reverse=True)
        
        selected_key = None
        
        # [UI] 불러오기 Selectbox
        if sorted_keys:
            col_sel, col_sp = st.columns([1, 2])
            with col_sel:
                def format_func(key):
                    s, a = key
                    # version_map에서 정보 가져오기
                    info = version_map[key]
                    model_display = f" [{info['model']}]" if info['model'] else ""
                    
                    is_latest = (key == sorted_keys[0])
                    return f"📜v{s} ➔ 🎧v{a}{model_display}" + (" (최신)" if is_latest else "")

                selected_key = st.selectbox(
                    f"📂 '{current_mode}' TTS 기록 불러오기:",
                    sorted_keys,
                    format_func=format_func,
                    key=f"tts_sel_{current_mode}"
                )
                if selected_key:
                    s_ver, a_ver = selected_key
                    st.session_state.current_audio_ver = a_ver # 현재 오디오 버전이 어디인지 전달
            
                # 선택된 폴더 로드 로직
                # path_map 대신 version_map을 사용해야 합니다.
                if selected_key:
                    # version_map 구조: {'path': Path객체, 'model': 문자열, 'full_name': 문자열}
                    target_info = version_map[selected_key]
                    target_dir = target_info["path"]  # 여기서 경로 추출
                    
                    # 세션 갱신 (경로가 달라졌을 때만)
                    if st.session_state.get(f"loaded_tts_dir_{current_mode}") != str(target_dir):
                        try:
                            with open(target_dir / "manifest.json", "r", encoding="utf-8") as f:
                                data = json.load(f)
                            
                            st.session_state.track_b_audio = data.get("audio_data", [])
                            st.session_state.track_b_full_audio = data.get("full_audio_path", "")
                            st.session_state.step1_scripts = data.get("scripts", [])
                            
                            st.session_state[f"loaded_tts_dir_{current_mode}"] = str(target_dir)
                        except Exception as e:
                            st.error(f"불러오기 실패: {e}")

        # ------------------------------------------------------------------
        # [Logic] 다음 오디오 버전(Next Audio Ver) 계산
        # ------------------------------------------------------------------
        # 현재 대본 버전(s)과 일치하는 키들 중에서 오디오 버전(a)의 최댓값 찾기
        existing_audio_vers = [k[1] for k in sorted_keys if k[0] == current_script_ver]
        
        # 모델명과 상관없이 숫자만 보고 +1 (예: v1_1_clova가 있어도 다음은 v1_2_gpt가 됨)
        next_audio_ver = (max(existing_audio_vers) + 1) if existing_audio_vers else 1
        
        # 현재 표시 중인 데이터
        current_audio_data = st.session_state.get("track_b_audio", [])
        current_full_audio = st.session_state.get("track_b_full_audio", "")

        # ------------------------------------------------------------------
        # [UI] 생성 버튼
        # ------------------------------------------------------------------
        st.markdown("#### 🛠️ 음성 생성 설정")
    
        col_eng, col_opt = st.columns(2)
        
        with col_eng:
            # TTS 모델 선택
            tts_engine_choice = st.radio(
                "사용할 TTS 모델 선택",
                options=["Naver Clova", "GPT-4o Mini TTS", "Gemini 2.5 Flash TTS", "Gemini 2.5 Pro TTS"],
                index=0
            )
            
            # 실제 함수에 넘길 engine string 변환
            if tts_engine_choice == "Naver Clova":
                selected_engine = "clova"
            elif tts_engine_choice == "GPT-4o Mini TTS":
                selected_engine = "gpt" # 혹은 함수 내부 구현에 따라 'gpt-4o-mini' 등
            elif tts_engine_choice == "Gemini 2.5 Flash TTS":
                selected_engine = "gemini-flash"
            else: # Gemini 2.5 PRO TTS
                selected_engine = "gemini-pro"

        with col_opt:
            # 1. 화자 설정 UI (조건부 활성화)
            # Clova가 아닌 경우(GPT/Gemini)에만 변경 가능하도록 설정
            is_premium_engine = "Clova" not in tts_engine_choice
            
            st.write("🗣️ 화자 구성 설정")
            speaker_mode = st.selectbox(
                "목소리 구성", 
                ["다수 화자 (자동 배정)", "단일 화자 (Narrator Only)"],
                index=0, # 기본값: 다수 화자
                disabled=not is_premium_engine, # Clova면 선택 불가능하게 잠금
                help="GPT/Gemini 모델 사용 시 단일 화자(나레이션 전용) 모드를 선택할 수 있습니다."
            )
            
            # Clova 선택 시 안내 문구
            if not is_premium_engine:
                st.caption("🔒 Naver Clova는 스크립트의 화자 설정을 따릅니다.")
            # 2. 음성 속도 조절 슬라이더
            st.write("👶 대상 연령 및 속도 설정")    
            # 1. 대상 연령대 선택 (사전 설정)
            age_category = st.radio(
                "대상 연령대 선택",
                ["미취학 (48~72개월)", "초등 저학년 (73~96개월)", "초등 고학년 (96개월+)", "직접 설정"],
                horizontal=True,
                help="연령대에 맞춰 최적의 속도가 기본 제공됩니다."
            )

            # 2. 연령대별 기본값 매핑
            if age_category == "미취학 (48~72개월)":
                default_speed = 0.8  # 천천히 읽어줌
            elif age_category == "초등 저학년 (73~96개월)":
                default_speed = 1.0  # 표준 속도
            elif age_category == "초등 고학년 (96개월+)":
                default_speed = 1.2  # 조금 빠른 숏폼 스타일
            else:
                default_speed = 1.0

            # 3. 세밀한 조정을 위한 하단 바 (Slider)
            # 연령대를 선택하면 이 바의 위치가 자동으로 움직입니다.
            voice_speed = st.slider(
                "음성 속도 미세 조절",
                min_value=0.5,
                max_value=1.5,
                value=default_speed, # 위에서 정해진 값이 초기값으로 들어감
                step=0.1,
                help="연령대 선택 후에도 여기서 세밀하게 속도를 바꿀 수 있습니다."
            )
            
            st.caption(f"현재 설정된 속도: **{voice_speed}x**")

        st.write("") # 간격
        # ------------------------------------------------------------------
        # 생성 전 프롬프트 & 데이터 매핑 미리보기 (Preview)
        # ------------------------------------------------------------------
        st.markdown("#### 👁️ 생성 데이터 미리보기")
        with st.expander("🔍 화자 및 스타일 프롬프트 구성 확인하기 (클릭)", expanded=False):
            # 1. 데이터 준비
            preview_scripts = st.session_state.get("step1_scripts", [])
            char_info_p = st.session_state.get("track_b_characters", {})
            chars_data_p = char_info_p.get("characters", [])
            dial_map_p = char_info_p.get("dialogue_map", [])

            # 매핑 테이블 (Tone용)
            char_tone_map_p = {c['id']: c.get('tone', '') for c in chars_data_p}

            preview_data = []

            for idx, script in enumerate(preview_scripts):
                text = script["text"]
                raw_speaker = script["speaker"] # "char_01 (흥부)..."
                page_num = int(script.get("source_page", 0)) # 대본의 페이지 번호
                
                # ID 추출
                speaker_id = raw_speaker.split(" ")[0] if raw_speaker else "narrator"
                
                # API Input용 화자 결정
                final_api_speaker = speaker_id
                if speaker_mode == "단일 화자 (Narrator Only)":
                    final_api_speaker = "narrator (Override)"

                generated_prompt = ""
                
                if "narrator" in speaker_id.lower():
                    generated_prompt = "(기본 나레이션 톤) Calm and clear storytelling."
                else:
                    c_tone = char_tone_map_p.get(speaker_id, "Normal tone")
                    
                    # 구조적 검색 함수 호출 (화자, 페이지, 텍스트 모두 넘김)
                    c_ctx = find_context_by_structure(text, raw_speaker, page_num, dial_map_p)
                    
                    if c_ctx:
                        generated_prompt = f"Tone: {c_tone} / Situation: {c_ctx}"
                    else:
                        generated_prompt = f"Tone: {c_tone} (Context Not Found)"

                preview_data.append({
                    "No": idx + 1,
                    "화자 (API Input)": final_api_speaker,
                    "원본 ID": speaker_id,
                    "대사 내용 (Text)": text,
                    "생성된 프롬프트 (Style Prompt)": generated_prompt
                })

            # 2. DataFrame으로 표시
            if preview_data:
                st.dataframe(
                    preview_data,
                    column_config={
                        "No": st.column_config.NumberColumn(width="small"),
                        "화자 (API Input)": st.column_config.TextColumn(width="medium"),
                        "원본 ID": st.column_config.TextColumn(width="small"),
                        "대사 내용 (Text)": st.column_config.TextColumn(width="large"),
                        "생성된 프롬프트 (Style Prompt)": st.column_config.TextColumn(
                            "AI에게 전달될 지시문 (Prompt)", 
                            width="large",
                            help="이 내용이 Gemini/GPT에게 전달되어 연기 톤을 결정합니다."
                        ),
                    },
                    use_container_width=True,
                    hide_index=True
                )
            else:
                st.warning("표시할 대본 데이터가 없습니다.")

        st.write("") # 여백
        
        # ... (이 밑에 기존 'btn_label =' 및 'if st.button...' 코드가 이어집니다) ...
        # 버튼 라벨: "대본 v2 기반 음성 v3 생성"
        btn_label = f"🎙️ 음성 생성하기 (Script v{current_script_ver} ➔ Audio v{next_audio_ver})"
        
        if st.button(btn_label, type="primary"):
            final_scripts = st.session_state.get("step1_scripts", [])
            
            if not final_scripts:
                st.error("생성할 대본이 없습니다.")
            else:
                with st.spinner(f"음성 생성 중... (Model: {selected_engine})"):
                    try:
                        # 폴더명: v{대본}_{음성}_{모델명} 형태로 후처리 저장 (식별 용이)
                        # 모델명 파일시스템 안전하게 변환
                        safe_model_name = selected_engine.replace(" ", "_")
                        folder_name = f"v{current_script_ver}_{next_audio_ver}_{safe_model_name}"
                        
                        new_ver_dir = TTS_MODE_BASE_DIR / folder_name
                        segments_dir = new_ver_dir / "segments"
                        
                        new_ver_dir.mkdir(parents=True, exist_ok=True)
                        segments_dir.mkdir(parents=True, exist_ok=True)
                        
                        uid = uuid.uuid4().hex[:6]

                        # =============================================================
                        # 스타일 프롬프트(Style Prompt) 동적 생성 로직
                        # =============================================================
                        
                        # 1. 데이터 로드 (Step 1.5 결과물)
                        char_info = st.session_state.get("track_b_characters", {})
                        characters_data = char_info.get("characters", [])
                        dialogue_map_data = char_info.get("dialogue_map", [])

                        # 2. 검색용 매핑(Lookup) 테이블 생성
                        # (1) 캐릭터 ID -> Tone 매핑
                        # 예: {'char_01': '소심하고 겁이 많은 말투', ...}
                        char_tone_map = {c['id']: c.get('tone', '') for c in characters_data}
                        
                        # (2) 대사 내용 -> Context 매핑
                        # 대본 수정 과정에서 텍스트가 약간 바뀔 수 있으므로, 
                        # 완벽한 매칭이 안 될 수 있음을 감안해야 합니다. (여기서는 정확한 텍스트 매칭 시도)
                        dialogue_context_map = {d['quote'].strip(): d.get('context', '') for d in dialogue_map_data}

                        # 3. 스크립트 순회하며 프롬프트 리스트 생성
                        style_prompts_list = []
                        
                        # 화자 정보 보정을 위한 임시 리스트
                        processed_speakers = [] 

                        for script in final_scripts:
                            text = script["text"]
                            # speaker 필드에 이름이 섞여있을 수 있으므로 ID만 추출 (예: "char_01 (흥부)..." -> "char_01")
                            raw_speaker = script["speaker"]
                            speaker_id = raw_speaker.split(" ")[0] if raw_speaker else "narrator"
                            
                            # (A) 나레이터 처리
                            if "narrator" in speaker_id.lower() or speaker_id == "narrator":
                                if speaker_mode == "단일 화자 (Narrator Only)":
                                    # 단일 화자 모드면 나레이션도 상황에 따라 톤이 바뀌면 좋겠지만, 기본은 차분하게
                                    current_prompt = "차분하고 몰입감 있는 동화 구연조로 읽어주세요."
                                else:
                                    current_prompt = "차분하고 명확한 발음의 나레이션 톤."
                                processed_speakers.append("narrator") # 단일화자 모드 처리는 아래에서 덮어씌워짐

                            # (B) 캐릭터 처리
                            else:
                                # 1. 성격/말투(Tone) 가져오기
                                char_tone = char_tone_map.get(speaker_id, "일반적인 목소리")
                                
                                # 2. 상황(Context) 가져오기
                                # 텍스트 앞뒤 공백 제거 후 매칭 시도
                                script_context = find_context_by_structure(text, raw_speaker, page_num, dialogue_map_data)
                                
                                # 3. 프롬프트 조합 (Gemini/GPT용)
                                # 영어로 변환해서 넘기면 더 좋지만, 한글로도 최신 모델은 잘 이해합니다.
                                # 포맷: [Role/Tone] + [Context/Situation]
                                if script_context:
                                    # 예: "Roleplay with a '소심한 목소리' tone. The situation is '호랑이를 피해 도망침'. Speak..."
                                    current_prompt = (
                                        f"Roleplay with a '{char_tone}' tone. "
                                        f"The situation is '{script_context}'. "
                                        f"Speak the following Korean text with the appropriate emotion."
                                    )
                                else:
                                    current_prompt = (
                                        f"Roleplay with a '{char_tone}' tone. "
                                        f"Speak the following Korean text naturally."
                                    )
                                
                                processed_speakers.append(speaker_id)

                            style_prompts_list.append(current_prompt)
                        
                        # 1. 텍스트와 화자 리스트 추출 (기본값)
                        texts = [s["text"] for s in final_scripts]
                        original_speakers = [s["speaker"] for s in final_scripts] # 원본 보존
                        
                        #  단일 화자 모드일 경우, 모든 화자를 'narrator'로 강제 변경
                        if speaker_mode == "단일 화자 (Narrator Only)":
                            # 리스트 전체를 'narrator'로 채움
                            speakers = ["narrator"] * len(texts)
                            print(f"ℹ️ [Info] 단일 화자 모드 적용: 모든 화자를 narrator로 설정함.")
                        else:
                            # 다수 화자 모드라면 원본 그대로 사용
                            speakers = original_speakers

                        #  1. 배수(Float)를 Clova 기준 정수(Int)로 변환
                        # 공식: (1.0 - 배수) * 10 
                        # 예: 1.2배 -> -2 (빠름), 0.8배 -> 2 (느림)
                        clova_speed_int = int((1.0 - voice_speed) * 10)
                        
                        # 범위를 -5 ~ 5 로 안전하게 제한 (Clova API 허용범위 준수)
                        clova_speed_int = max(-5, min(5, clova_speed_int))
                        
                        # 여기서 engine 인자 전달
                        audio_paths = generate_audio_for_subtitles(
                            subtitles=texts,
                            output_dir=segments_dir,
                            uid=uid,
                            speakers=speakers,
                            engine=selected_engine,  # <--- 선택한 엔진 전달
                            parallel=True ,
                            global_speed=clova_speed_int,
                            style_prompts=style_prompts_list
                        )
                        
                        # 2. 데이터 구성
                        audio_data_list = []
                        valid_paths = []
                        for idx, path in enumerate(audio_paths):
                            script_item = final_scripts[idx]
                            if path and Path(path).exists():
                                dur = get_audio_duration(str(path))
                                audio_data_list.append({
                                    "text": script_item["text"],
                                    "speaker": script_item["speaker"],
                                    "path": str(path),
                                    "duration": dur,
                                    "scene_no": script_item.get("scene_no")
                                })
                                valid_paths.append(path)
                            else:
                                audio_data_list.append({
                                    "text": script_item["text"],
                                    "speaker": script_item["speaker"],
                                    "path": None,  # 경로는 없음
                                    "duration": 0,
                                    "scene_no": script_item.get("scene_no"),
                                    "status": "failed" # 실패했음을 표시
                                })
                                
                                # (선택) 디버깅을 위해 실패 로그 출력
                                print(f"⚠️ 오디오 생성 실패: {script_item['text'][:10]}...")

                        # 3. Full Audio 병합
                        full_audio_str = ""
                        if valid_paths:
                            full_path = new_ver_dir / f"full_{folder_name}.mp3"
                            if merge_audio_files(valid_paths, str(full_path)):
                                full_audio_str = str(full_path)
                        
                        # 4. Manifest 저장
                        manifest = {
                            "script_ver": current_script_ver,
                            "audio_ver": next_audio_ver,
                            "mode": current_mode,
                            "engine": selected_engine,
                            "created_at": str(datetime.now()),
                            "scripts": final_scripts,
                            "audio_data": audio_data_list,
                            "full_audio_path": full_audio_str
                        }
                        # [Debug] 어디에 set이 있는지 범인 찾기
                        def find_set_in_dict(d, path="root"):
                            if isinstance(d, set):
                                st.error(f"🚨 범인 발견! 경로: {path} / 값: {d}")
                            elif isinstance(d, dict):
                                for k, v in d.items():
                                    find_set_in_dict(v, f"{path}.{k}")
                            elif isinstance(d, list):
                                for i, v in enumerate(d):
                                    find_set_in_dict(v, f"{path}[{i}]")
                        
                        find_set_in_dict(manifest)
                        with open(new_ver_dir / "manifest.json", "w", encoding="utf-8") as f:
                            json.dump(manifest, f, ensure_ascii=False, indent=4)
                            
                        # 5. 세션 갱신 및 리런
                        st.session_state.track_b_audio = audio_data_list
                        st.session_state.track_b_full_audio = full_audio_str
                        st.session_state[f"loaded_tts_dir_{current_mode}"] = str(new_ver_dir)
                        st.session_state.current_audio_ver = next_audio_ver

                        st.success(f"✅ 생성 완료! (폴더: {folder_name})")
                        st.rerun()
                        
                    except Exception as e:
                        st.error(f"에러: {e}")

        # ------------------------------------------------------------------
        # [UI] 오디오 확인 및 플레이어
        # ------------------------------------------------------------------
        if current_audio_data:
            st.divider()
            
            # 변수명 에러 해결 로직
            # selected_key는 (대본버전, 음성버전) 튜플입니다.
            if selected_key:
                cur_s, cur_a = selected_key
            else:
                # 선택된 값이 없다면(방금 생성했거나 초기 로드 시), 
                # 현재 대본 버전과 (다음생성번호 - 1)을 현재 버전으로 간주
                cur_s = current_script_ver
                # next_audio_ver는 "다음에 생성할 번호"이므로, 현재 보는 건 1을 빼줍니다.
                # 단, 데이터가 있는데 next가 1이라면(기존 파일 로드 직후) 로직에 따라 조정 필요
                cur_a = (next_audio_ver - 1) if next_audio_ver > 1 else 1
            
            # 현재 로드된 데이터의 엔진 정보 확인 (표시용)
            current_engine_display = "Unknown"
            try:
                cur_dir = st.session_state.get(f"loaded_tts_dir_{current_mode}")
                if cur_dir:
                    with open(Path(cur_dir) / "manifest.json", 'r', encoding='utf-8') as f:
                        m = json.load(f)
                        current_engine_display = m.get('engine', 'clova')
            except:
                pass

            st.markdown(f"### 🎧 오디오 확인 (S{cur_s} / A{cur_a}) - {current_engine_display}")
            
            # 폴더 경로 표시 (디버깅용)
            current_path_display = st.session_state.get(f"loaded_tts_dir_{current_mode}", "New")
            # 전체 경로 대신 폴더명만 깔끔하게 표시
            display_short_path = Path(current_path_display).name if current_path_display != "New" else "New"
            st.caption(f"📁 저장 위치: .../{current_mode}/{display_short_path}")

            # 1. 전체 듣기 (Full Audio)
            if current_full_audio and os.path.exists(current_full_audio):
                # 전체 길이 계산
                total_dur = sum([d["duration"] for d in current_audio_data if d.get("duration")])
                st.audio(current_full_audio)
                st.caption(f"⏱️ 전체 길이: {total_dur:.1f}초")
            else:
                st.warning("⚠️ 전체 통합 오디오 파일이 없습니다.")

            # 2. 개별 듣기 (Segments)
            with st.expander(f"📂 문장별 상세 듣기 (S{cur_s}/A{cur_a})"):
                for i, item in enumerate(current_audio_data):
                    col_text, col_audio = st.columns([2, 1])
                    
                    with col_text:
                        spk = item.get('speaker', 'Unknown')
                        txt = item.get('text', '')
                        icon = "📖" if spk == "narrator" else "🗣️"
                        st.markdown(f"**#{i+1} {icon} [{spk}]**")
                        st.write(txt)
                    
                    with col_audio:
                        path = item.get("path")
                        if path and os.path.exists(path):
                            st.audio(path)
                            st.caption(f"{item.get('duration', 0):.1f}초")
                        else:
                            st.caption("음성 파일 없음")
                    st.divider()

            # -------------------------------------------------------------
            # [Step 4 완료] -> Step 6 이동
            # -------------------------------------------------------------
            col_msg, col_btn = st.columns([3, 1])
            with col_msg:
                st.caption(f"현재 선택된 **Script v{cur_s} / Audio v{cur_a}** 음성을 확정하고 이미지 매칭 단계로 이동합니다.")
            with col_btn:
                if st.button("✅ 음성 확정 (Step 6 이동)"):
                    # 1. 이미지 폴더 및 TXT 검증
                    if not folder.exists():
                        st.error("이미지 폴더 경로가 잘못되었습니다.")
                    elif not txt_file.exists():
                        st.error("TXT 파일이 없습니다.")
                    else:
                        with st.spinner("이미지 데이터 로드 중..."):
                            # A. 이미지 필터링
                            valid_imgs = load_filtered_images(folder)
                            # B. 텍스트 파싱
                            text_map = parse_book_text_by_page(txt_file)
                            
                            # C. Candidates 생성 (Step 6용 데이터)
                            candidates = []
                            for img_path in valid_imgs:
                                pg_num = extract_page_num_from_filename(img_path.name)
                                page_text = text_map.get(pg_num, "(텍스트 없음)")
                                candidates.append({
                                    "page_num": pg_num,
                                    "img_path": str(img_path),
                                    "img_name": img_path.name,
                                    "text": page_text
                                })
                            
                            # 세션에 중요 데이터 저장 (다음 단계를 위해)
                            st.session_state.track_b_candidates = candidates
                            st.session_state.step2_audio = current_audio_data # 확정된 오디오 리스트 넘김
                            
                            # 단계 이동
                            st.session_state.track_b_step = 6
                            st.toast(f"Audio v{cur_a} 확정! Step 6로 이동합니다.")
                            st.rerun()
    # =========================================================
    # [Step 6] 이미지 자동 매칭 (대본 출처 기반 + 스마트 분배)
    # =========================================================
    if st.session_state.get("track_b_step", 0) >= 6:        
        # [DEBUG] Source Page 값 확인 (필요시 주석 처리)
        if "step1_scripts" in st.session_state:
            with st.expander("🐛 [DEBUG] Step 3에서 넘어온 Source Page 값"):
                debug_data = [{
                    "Scene": i+1, 
                    "Source Page": item.get("source_page", "MISSING"),
                    "Type": type(item.get("source_page"))
                } for i, item in enumerate(st.session_state.step1_scripts)]
                st.dataframe(debug_data)

        st.info("⚡ Step 3의 페이지 정보를 바탕으로, 즉시 이미지를 배정합니다. (펼침면 및 중복 방지 규칙 자동 적용)")

        if "track_b_matches" not in st.session_state:
            st.session_state.track_b_matches = None

        if st.button("🧩 이미지 배정 실행 (알고리즘)", type="primary"):
            if not st.session_state.track_b_candidates:
                st.error("이미지 데이터가 없습니다.")
            elif not st.session_state.step1_scripts:
                st.error("대본 데이터가 없습니다.")
            else:
                with st.spinner("이미지 배정 규칙 적용 중..."):
                    
                    # -------------------------------------------------
                    # 0. 데이터 준비
                    # -------------------------------------------------
                    candidates = st.session_state.track_b_candidates
                    text_map = {item['page_num']: item['text'] for item in candidates}
                    spread_map = analyze_spread_structure(candidates)
                    
                    # [스포일러 상한선 계산]
                    target_text = st.session_state.get("target_trailer_text", "")
                    spoiler_limit_pg = find_spoiler_limit_page(target_text, text_map)
                    
                    # 안전장치
                    max_book_pg = max(text_map.keys()) if text_map else 0
                    if spoiler_limit_pg > max_book_pg: spoiler_limit_pg = max_book_pg
                    if spoiler_limit_pg % 2 == 0: spoiler_limit_pg += 1

                    # 표지 페이지 번호
                    #cover_page_num = candidates[0]['page_num'] if candidates else 5
                    cover_page_num = find_cover_page_num(text_map, candidates)

                    #  Step 6.5에서 쓰기 위해 세션에 저장
                    st.session_state['cover_page_num'] = cover_page_num

                    # -------------------------------------------------
                    # A. 대본에서 페이지 정보 가져오기 (Direct Access)
                    # -------------------------------------------------
                    scripts = st.session_state.step1_scripts
                    total_scenes = len(scripts)
                    
                    # -------------------------------------------------
                    # B. 스마트 배치 로직
                    # -------------------------------------------------
                    final_selection = []
                    
                    def get_spread_id(pg):
                        info = spread_map.get(pg)
                        if info and info['pair_page']:
                            return min(pg, info['pair_page'])
                        return pg

                    # 헬퍼: 안전하게 페이지 번호 가져오기
                    def get_safe_page(idx):
                        if 0 <= idx < total_scenes:
                            val = scripts[idx].get("source_page", 0)
                            try: return int(val)
                            except: return 0
                        return -1

                    for i in range(total_scenes):
                        # 1. Step 3의 값 그대로 가져오기
                        source_pg = get_safe_page(i)
                        scene_idx = i
                        note = "대본 출처"

                        # 유효성 검사 (0이거나 이미지 맵에 없으면 최소 페이지로 보정)
                        if source_pg != 0 and source_pg not in text_map:
                            if candidates:
                                source_pg = min(text_map.keys())
                                note = "유효하지 않은 페이지 보정"
                            else:
                                source_pg=cover_page_num

                        # -----------------------------------------------
                        # Rule 0. 마지막 장면(Outro)은 source_page==0일 때만 표지
                        # -----------------------------------------------
                        if i == total_scenes - 1 and source_pg == 0:
                            final_selection.append({
                                "scene_index": scene_idx,
                                "page": cover_page_num,
                                "original_pick": source_pg,
                                "note": "Rule: 마지막 장면(표지, source_page=0)"
                            })
                            continue

                        # -----------------------------------------------
                        # Logic: 연속 씬 vs 단독 씬 분기
                        # -----------------------------------------------
                        # 앞뒤 씬의 Source Page 가져오기
                        prev_pg_raw = get_safe_page(i-1)
                        next_pg_raw = get_safe_page(i+1)

                        curr_spread_id = get_spread_id(source_pg)
                        prev_spread_id = get_spread_id(prev_pg_raw) if i > 0 else -1
                        next_spread_id = get_spread_id(next_pg_raw) if i < total_scenes - 1 else -1

                        # 후보군 (Left, Right)
                        curr_info = spread_map.get(source_pg)
                        pair_pg = curr_info['pair_page'] if curr_info else None
                        
                        if pair_pg:
                            available_pages = sorted([source_pg, pair_pg])
                        else:
                            available_pages = [source_pg]
                        
                        final_pg = source_pg # 초기값

                        # Case 1: 연속 장면 시작 (Start) -> 왼쪽
                        if curr_spread_id == next_spread_id and curr_spread_id != prev_spread_id:
                            final_pg = available_pages[0] 
                            note += " + 연속 시작(좌)"

                        # Case 2: 연속 장면 이어짐 (Continue) -> 오른쪽
                        elif curr_spread_id == prev_spread_id:
                            if len(available_pages) > 1:
                                final_pg = available_pages[1]
                                note += " + 연속 이어짐(우)"
                            else:
                                final_pg = available_pages[0]
                                note += " (단일 페이지 유지)"

                        # Case 3: 단독 장면 -> 텍스트 없는 쪽 우선
                        else:
                            pair_info = spread_map.get(pair_pg) if pair_pg else None
                            if curr_info and curr_info['has_text'] and pair_info and not pair_info['has_text']:
                                final_pg = pair_pg
                                note += " (텍스트 없는 쪽 우선)"

                        # -----------------------------------------------
                        # Rule: 스포일러 상한선
                        # -----------------------------------------------
                        if final_pg > spoiler_limit_pg:
                            final_pg = spoiler_limit_pg
                            note = f"스포일러 제한(Limit P.{spoiler_limit_pg})"

                        # -----------------------------------------------
                        # Rule: 중복 방지
                        # -----------------------------------------------
                        if i > 0:
                            last_final_pg = final_selection[i-1]['page']
                            if final_pg == last_final_pg:
                                if pair_pg and pair_pg != final_pg:
                                    if pair_pg <= spoiler_limit_pg:
                                        final_pg = pair_pg
                                        note += " + 중복 회피"
                        
                        # 결과 저장
                        final_selection.append({
                            "scene_index": scene_idx,
                            "page": final_pg,
                            "original_pick": source_pg,
                            "note": note
                        })
                    
                    st.session_state.track_b_matches = final_selection
                    st.success(f"배정 완료! (스포일러 상한선: P.{spoiler_limit_pg})")

        # 3. 결과 확인 UI
        if st.session_state.track_b_matches:
            matches = st.session_state.track_b_matches
            candidates_map = {c['page_num']: c for c in st.session_state.track_b_candidates}
            scripts = st.session_state.step1_scripts
            
            selected_filenames = [] 

            for i, match in enumerate(matches):
                pg = match['page']
                scene_text = scripts[i]['text']
                orig_pg = match['original_pick']
                
                st.divider()
                st.markdown(f"#### 🎬 Scene {i+1} (Source: P.{orig_pg})")
                st.caption(f"📜 {scene_text}")
                
                c1, c2 = st.columns([1, 2])
                with c1:
                    img_data = candidates_map.get(pg)
                    if img_data:
                        st.image(img_data['img_path'], width=300)
                        selected_filenames.append(img_data['img_name'])
                    else:
                        st.warning(f"Page {pg} 이미지 없음")
                        # 예외처리: 첫 번째 이미지 사용
                        if candidates_map:
                            first_key = sorted(list(candidates_map.keys()))[0]
                            st.image(candidates_map[first_key]['img_path'], width=300)
                            selected_filenames.append(candidates_map[first_key]['img_name'])

                with c2:
                    st.info(f"**선택:** Page {pg} | {match['note']}")
                    
                    all_options = sorted(list(candidates_map.keys()))
                    
                    # selectbox index 안전 처리
                    try:
                        sel_idx = all_options.index(pg)
                    except ValueError:
                        sel_idx = 0

                    new_pg = st.selectbox(
                        "이미지 교체:", all_options, 
                        index=sel_idx,
                        key=f"manual_sel_{i}"
                    )
                    
                    if new_pg != pg:
                        st.session_state.track_b_matches[i]['page'] = new_pg
                        st.rerun()

            st.divider()
            
            col_msg, col_btn = st.columns([3, 1])
            with col_msg:
                st.caption(f"총 {len(selected_filenames)}개의 장면으로 영상을 생성합니다.")
            with col_btn:
                if st.button("최종 확정 (Step 7 영상 생성)"):
                    st.session_state.selected_pages = selected_filenames
                    st.session_state.track_b_step = 7
                    st.toast("이미지 확정! 영상 생성 단계로 넘어갑니다.")
                    st.rerun()

   
    # =========================================================
    # [Step 7] Runway 영상 생성 및 병합 (Visual & TTS)
    # =========================================================
    if st.session_state.get("track_b_step", 0) >= 7:
        # =========================================================
        # [Step 6.5] 정지 화상 프리뷰 (BGM: Title/nP 로직 적용)
        # =========================================================
        if st.session_state.get("track_b_step", 0) >= 7:
            st.divider()
            st.subheader("Step 6.5. (선택) 정지 화상 프리뷰 (Animatic)")
            st.info("🎞️ Runway 영상을 생성하기 전, [이미지 + TTS + 자막 + BGM]의 흐름을 미리 확인해보세요.")
            
            # ---------------------------------------------------------
            # 0. 의존성 데이터 및 경로 확인
            # ---------------------------------------------------------
            cur_script_ver = st.session_state.get("current_script_ver", 1)
            cur_audio_ver = st.session_state.get("current_audio_ver", 1)
            cur_mode = st.session_state.get("script_style_mode", "Standard")

            story_dir_name = txt_file.stem
            # outputs / 동화이름 / preview / 모드명
            PREVIEW_BASE_DIR = Path("outputs") / story_dir_name / "preview" / cur_mode
            PREVIEW_BASE_DIR.mkdir(parents=True, exist_ok=True)

            # BGM 경로 설정 (기존 로직 유지)
            path_candidate_1 = Path("BGM") / story_dir_name
            path_candidate_2 = Path("BGM") / get_bgm_folder_name(story_dir_name)

            if path_candidate_1.exists() and path_candidate_1.is_dir():
                BGM_DIR = path_candidate_1
            elif path_candidate_2.exists() and path_candidate_2.is_dir():
                BGM_DIR = path_candidate_2
            else:
                BGM_DIR = path_candidate_1
            

            # ---------------------------------------------------------
            # 1. 버전 탐색 및 불러오기 (Load)
            # ---------------------------------------------------------
            sorted_keys, path_map = get_preview_versions(PREVIEW_BASE_DIR)
            
            selected_key = None
            
            # [UI] 불러오기 Selectbox
            if sorted_keys:
                col_sel, col_sp = st.columns([1, 2])
                with col_sel:
                    def format_func(key):
                        s, a, p = key 
    
                        # 해당 버전의 모델 정보를 가져오기 위해 path_map(또는 version_map) 활용
                        model_hint = ""
                        try:
                            p_path = version_map[key] # 혹은 path_map[key] (변수명 확인 필요)
                            if (p_path / "manifest.json").exists():
                                with open(p_path / "manifest.json", "r", encoding="utf-8") as f:
                                    m_data = json.load(f)
                                    eng = m_data.get("engine", "clova")
                                    model_hint = f" [{eng}]"
                        except:
                            pass
                        
                        is_latest = (key == sorted_keys[0])
                        # 화면에 표시될 텍스트 형식 (P 버전도 포함)
                        return f"📜v{s} ➔ 🎧v{a} ➔ 🖼️v{p}{model_hint}" + (" (최신)" if is_latest else "")
                    selected_key = st.selectbox(
                        f"📂 '{current_mode}' 프리뷰(Preview) 기록 불러오기:", 
                        sorted_keys,
                        format_func=format_func,
                        key=f"prev_sel_{current_mode}"  # <--- 'tts_sel_' 대신 'prev_sel_'로 변경
                    )
                
                # 선택된 폴더 로드 로직
                target_dir = path_map[selected_key]
                
                # 세션 갱신 (경로가 바뀌었을 때만)
                if st.session_state.get(f"loaded_prev_dir_{cur_mode}") != str(target_dir):
                    try:
                        with open(target_dir / "manifest.json", "r", encoding="utf-8") as f:
                            data = json.load(f)
                        st.session_state.track_b_preview_video = data.get("final_path", "")
                        st.session_state[f"loaded_prev_dir_{cur_mode}"] = str(target_dir)
                    except Exception as e:
                        st.error(f"로드 실패: {e}")

            # 다음 프리뷰 버전 계산 (현재 S/A 조합 내에서)
            existing_p_vers = [k[2] for k in sorted_keys if k[0] == cur_script_ver and k[1] == cur_audio_ver]
            next_prev_ver = (max(existing_p_vers) + 1) if existing_p_vers else 1
                
            # ---------------------------------------------------------
            # 2. BGM 및 옵션 설정 UI 
            # ---------------------------------------------------------
            def find_bgm_file(keywords, bgm_dir_path):
                if not bgm_dir_path.exists(): return None
                # 폴더 내 모든 오디오 파일 검색
                for f in bgm_dir_path.iterdir():
                    if f.is_file() and f.suffix.lower() in ['.mp3', '.wav', '.m4a']:
                        # 전달받은 키워드(예: '2P', '2p') 중 하나라도 파일명에 있으면 성공
                        for kw in keywords:
                            if kw in f.name: 
                                return f
                return None
            
            # 세션 키 초기화
            if "track_b_preview_video" not in st.session_state:
                st.session_state.track_b_preview_video = None

            col_prev_info, col_prev_opt = st.columns([2, 2])
            
            with col_prev_info:
                st.caption("이미지, 오디오, 자막, BGM을 합쳐 미리보기를 생성합니다.")
            
            with col_prev_opt:
                use_bgm = st.checkbox("🎵 배경음악(BGM) 포함하기", value=False, key="step6_5_use_bgm")
                bgm_volume = 0.15
                
                if use_bgm:
                    if BGM_DIR.exists():
                        bgm_files = sorted(
                            [f for f in BGM_DIR.iterdir() if f.is_file() and f.suffix.lower() in ['.mp3', '.wav', '.m4a']],
                            key=lambda x: x.name
                        )
                        st.caption(f"📂 BGM 폴더: {len(bgm_files)}개 오디오 파일 감지")
                        bgm_volume = st.slider("BGM 볼륨", 5, 100, 15, key="step6_5_bgm_vol") / 100.0
                    else:
                        st.warning(f" BGM 폴더가 없습니다: {BGM_DIR}")
                        use_bgm = False

            # ---------------------------------------------------------
            # [UI] 자막 스타일 선택
            # ---------------------------------------------------------
            st.markdown("---")
            st.write("🎨 **자막 스타일 설정**")
            subtitle_mode = st.radio(
                "자막 색상 방식을 선택하세요:",
                ["🏳️ 기본 (흰색 통일)", "🌈 캐릭터별 자동 컬러링 (화자 구분)"],
                index=0,
                horizontal=True
            )
            # ---------------------------------------------------------
            # 3. 프리뷰 생성 로직
            # ---------------------------------------------------------
            btn_label = f"🎞️ 프리뷰 생성하기 (S{cur_script_ver}/A{cur_audio_ver} ➔ Preview v{next_prev_ver})"
            
            if st.button(btn_label, type="primary", use_container_width=True):
                # 1. 데이터 로드 (순서 중요)
                matches = st.session_state.get("track_b_matches", [])
                audios = st.session_state.get("track_b_audio", [])
                scripts = st.session_state.get("step1_scripts", [])
                candidates = st.session_state.get("track_b_candidates", [])
                candidates_map = {c['page_num']: c for c in candidates}
                
                # 2. 표지(Cover) 페이지 번호 확정
                # Step 6에서 저장된 값이 있으면 쓰고, 없으면 candidates 첫번째 값 추정
                if 'cover_page_num' in st.session_state:
                    cover_page_num = st.session_state['cover_page_num']
                elif candidates:
                    cover_page_num = candidates[0]['page_num']
                else:
                    cover_page_num = 1 # 기본값

                # Step 1.5 정보
                char_info = st.session_state.get("track_b_characters", {})
                dialogue_map = char_info.get("dialogue_map", []) if char_info else []
                current_mode = st.session_state.get("script_style_mode", "Standard")

                # [로직] 컬러링 모드일 경우에만 맵 생성
                speaker_color_map = {}
                if "컬러링" in subtitle_mode:
                    speaker_color_map = generate_dynamic_color_map(scripts)
                    st.caption(f"🎨 적용된 색상 팔레트: {len(speaker_color_map)}명의 화자 구분됨")

                if not matches or not audios:
                    st.error("데이터가 부족합니다. Step 4, 6을 확인해주세요.")
                else:
                    # 2. 폴더 생성: v{S}_{A}_{P}
                    folder_name = f"v{cur_script_ver}_{cur_audio_ver}_{next_prev_ver}"
                    NEW_VER_DIR = PREVIEW_BASE_DIR / folder_name
                    NEW_VER_DIR.mkdir(parents=True, exist_ok=True)

                    progress_bar = st.progress(0)
                    status_text = st.empty()
                    temp_clips = []

                    try:
                        status_text.write(f"🔄 프리뷰 생성 중... (표지: {cover_page_num}p 기준)")
                        
                        for i, match in enumerate(matches):
                            # A. 기본 데이터
                            pg = match['page']
                            img_info = candidates_map.get(pg)
                            if not img_info: continue
                            
                            img_path = str(img_info['img_path'])
                            audio_data = audios[i] if i < len(audios) else None
                            audio_path = audio_data['path'] if audio_data else None
                            audio_dur = audio_data['duration'] if (audio_data and audio_data['duration']) else 5.0

                            # B. 자막 및 색상 결정
                            script_item = scripts[i] if i < len(scripts) else {}
                            original_text = script_item.get("text", "")
                            current_speaker = str(script_item.get("speaker", "narrator")).strip()
                            
                            # ★ 색상 결정 로직
                            if "컬러링" in subtitle_mode:
                                # 맵에서 찾거나 없으면 흰색
                                text_color = speaker_color_map.get(current_speaker, "white")
                            else:
                                # 기본 모드
                                text_color = "white"
                            
                            # ★ 대본상의 실제 PDF 페이지 번호 (예: 5, 6, 7...)
                            source_page = script_item.get("source_page", 0) 
                            
                            subtitle_text = original_text 
                            if current_mode == "Conversation" and dialogue_map:
                                for d_item in dialogue_map:
                                    if (d_item.get("quote", "").strip() == original_text.strip()) and \
                                       (d_item.get("page_num") == source_page):
                                        subtitle_text = d_item.get("context")
                                        break
                            
                            # C. 경로 설정
                            base_clip_path = NEW_VER_DIR / f"preview_base_{i}.mp4"
                            sub_clip_path = NEW_VER_DIR / f"preview_sub_{i}.mp4"
                            final_clip_path = NEW_VER_DIR / f"preview_final_{i}.mp4"

                            # D. 영상 생성 (무음) - PIL로 이미지 전처리 후 클립 생성
                            from PIL import Image as PILImage
                            pil_img = PILImage.open(img_path).convert("RGB")
                            # 높이 1280 기준 리사이즈
                            ratio = 1280 / pil_img.height
                            pil_img = pil_img.resize((int(pil_img.width * ratio), 1280), PILImage.LANCZOS)
                            # 가운데 720px 크롭
                            left = (pil_img.width - 720) // 2
                            pil_img = pil_img.crop((left, 0, left + 720, 1280))
                            # numpy 배열로 변환하여 ImageClip 생성
                            import numpy as np
                            clip = ImageClip(np.array(pil_img), duration=audio_dur)
                            clip.fps = 24

                            clip.write_videofile(str(base_clip_path), codec="libx264", audio=False, preset="ultrafast", logger=None)
                            clip.close()

                            # E. 자막 입히기
                            # E. 자막 입히기 (font_color 인자 사용)
                            add_subtitle_to_video(
                                str(base_clip_path), 
                                subtitle_text, 
                                str(sub_clip_path), 
                                scene_index=i, 
                                font_color=text_color 
                            )

                            # F. 오디오 및 BGM 합성 (★ 사용자 요청 로직 적용)
                            if audio_path and os.path.exists(audio_path):
                                page_bgm = None
                                
                                if use_bgm and source_page >= 0:
                                    search_keywords = []
                                    
                                    # [Logic] 표지(Cover) 페이지인 경우 -> "title" 검색
                                    if source_page == cover_page_num:
                                        search_keywords = ["title", "Title", "TITLE"]
                                        log_msg = "Title(표지)"
                                        
                                    elif source_page == 0:
                                        search_keywords = ["title", "Title", "TITLE"]
                                        log_msg = "Title(표지)"

                                    # [Logic] 표지보다 뒤쪽 페이지인 경우 -> 상대 번호 + "P" 검색
                                    elif source_page > cover_page_num:
                                        # 예: source_page(6) - cover(5) + 1 = 2 --> "2p"
                                        story_seq = source_page - cover_page_num + 1
                                        search_keywords = [f"{story_seq}p", f"{story_seq}P", f"Page {story_seq}", f"Page{story_seq}"]
                                        log_msg = f"{story_seq}p"

                                    # BGM 파일 찾기
                                    if search_keywords:
                                        page_bgm = find_bgm_file(search_keywords, BGM_DIR)

                                if page_bgm and page_bgm.exists():
                                    add_audio_to_video(
                                        str(sub_clip_path), str(audio_path), str(final_clip_path),
                                        bgm_path=str(page_bgm), bgm_volume=bgm_volume
                                    )
                                    st.caption(f"🎵 Scene {i+1} (PDF {source_page}p): [{log_msg}] → '{page_bgm.name}' 적용")
                                else:
                                    add_audio_to_video(
                                        str(sub_clip_path), str(audio_path), str(final_clip_path)
                                    )
                                
                                temp_clips.append(str(final_clip_path))
                            else:
                                temp_clips.append(str(sub_clip_path))

                            progress_bar.progress((i + 1) / len(matches))
                        
                        # 병합
                        status_text.write(" 전체 영상 병합 중...")
                        final_preview_path = NEW_VER_DIR / "final_preview.mp4"
                        concat_videos_with_audio(temp_clips, str(final_preview_path))

                        # Manifest 저장
                        manifest = {
                            "script_ver": cur_script_ver,
                            "audio_ver": cur_audio_ver,
                            "preview_ver": next_prev_ver,
                            "created_at": str(datetime.now()),
                            "bgm_used": use_bgm,
                            "final_path": str(final_preview_path)
                        }
                        with open(NEW_VER_DIR / "manifest.json", "w", encoding="utf-8") as f:
                            json.dump(manifest, f, ensure_ascii=False, indent=4)
                        
                        st.session_state.track_b_preview_video = str(final_preview_path)
                        st.session_state[f"loaded_prev_dir_{cur_mode}"] = str(NEW_VER_DIR)

                        status_text.empty()
                        st.success(" 프리뷰 생성 완료!")
                      #  st.video(str(final_preview_path))

                    except Exception as e:
                        st.error(f"오류 발생: {e}")
                        st.text(traceback.format_exc())

        # 프리뷰 비디오 플레이어 표시
        if st.session_state.get("track_b_preview_video") and os.path.exists(st.session_state.track_b_preview_video):
            st.divider()
            
            # 버전 라벨
            if selected_key:
                s, a, p = selected_key
                ver_label = f"S{s}/A{a}/P{p}"
            else:
                ver_label = f"S{cur_script_ver}/A{cur_audio_ver}/P{next_prev_ver-1}"
            
            st.markdown(f"#### 📺 프리뷰 확인 ({ver_label})")
            st.video(st.session_state.track_b_preview_video)
            st.info("👆 위 영상은 '이미지+TTS+자막' 확인용입니다. 실제 영상(Runway) 생성은 아래 'Step 7'에서 진행하세요.")

        # =========================================================
        # 추후 코드 완성 시 삭제 예정
        # =========================================================
        st.divider()
        st.subheader("Step 7. Runway 영상 생성 및 풀버전 병합")
        st.info("🎥 AI 영상 생성 후, [무음 풀버전]과 [TTS 포함 풀버전]을 자동으로 제작합니다.")
        
        # 0. 데이터 준비
        if "track_b_video_results" not in st.session_state:
            st.session_state.track_b_video_results = [] 
        
        # 병합된 파일 경로 저장용 세션
        if "track_b_full_visual" not in st.session_state:
            st.session_state.track_b_full_visual = None
        if "track_b_full_tts" not in st.session_state:
            st.session_state.track_b_full_tts = None

        # [버전 관리] 현재 작업 중인 대본/음성 버전 확인
        cur_script_ver = st.session_state.get("current_script_ver", 1)
        cur_audio_ver = st.session_state.get("current_audio_ver", 1)
        cur_mode = st.session_state.get("script_style_mode", "Standard")

        # 데이터 검증
        if not st.session_state.track_b_matches:
            st.error("Step 6에서 이미지 매칭을 완료해주세요.")
        elif not st.session_state.track_b_audio:
            st.error("Step 4에서 TTS 생성을 완료해주세요.")
        else:
            # 기본 경로 설정: outputs/동화이름/video/모드명
            story_dir_name = txt_file.stem
            VIDEO_BASE_DIR = Path("outputs") / story_dir_name / "video" / cur_mode
            VIDEO_BASE_DIR.mkdir(parents=True, exist_ok=True)

            # ------------------------------------------------------------------
            # [로드 로직] 기존 버전 탐색 및 불러오기
            # ------------------------------------------------------------------
            sorted_keys, path_map = get_video_versions_v3(VIDEO_BASE_DIR)
            
            selected_key = None
            current_ver_dir = None # 현재 로드된 폴더 경로
            
            if sorted_keys:
                col_sel, col_space = st.columns([1, 2])
                with col_sel:
                    def format_func(k):
                        s, a, v = k
                        is_match = (s == cur_script_ver and a == cur_audio_ver)
                        prefix = "✅ " if is_match else ""
                        is_latest = (k == sorted_keys[0])
                        return f"{prefix}S{s}/A{a} ➔ Video v{v}" + (" (최신)" if is_latest else "")

                    selected_key = st.selectbox(
                        f"📂 '{cur_mode}' 영상 기록 불러오기:",
                        sorted_keys,
                        format_func=format_func,
                        key=f"vid_sel_{cur_mode}"
                    )
                
                # 선택된 폴더 로드
                current_ver_dir = path_map[selected_key]
                
                # 세션 로드 (경로가 바뀌었을 때만)
                if st.session_state.get(f"loaded_vid_dir_{cur_mode}") != str(current_ver_dir):
                    try:
                        with open(current_ver_dir / "manifest.json", "r", encoding="utf-8") as f:
                            data = json.load(f)
                        
                        st.session_state.track_b_video_results = data.get("clips", [])
                        st.session_state.track_b_full_visual = data.get("full_visual_path", "")
                        st.session_state[f"loaded_vid_dir_{cur_mode}"] = str(current_ver_dir)
                    except Exception as e:
                        st.error(f"로드 실패: {e}")
            
            # 다음 영상 버전 계산 (현재 S/A 조합 기준)
            existing_v_vers = [k[2] for k in sorted_keys if k[0] == cur_script_ver and k[1] == cur_audio_ver]
            next_video_ver = (max(existing_v_vers) + 1) if existing_v_vers else 1
            
            # 1. 설정 UI
            with st.container(border=True):
                col_p, col_d = st.columns([3, 1])
                with col_p:
                    global_prompt = st.text_input(
                        "🎨 모션 프롬프트 (영상 스타일/움직임):", 
                        value="Characters and elements within a children's book illustration move, as if coming to life, with gentle cinematic movement, without adding anything new.",
                        help="모든 장면에 공통으로 적용될 움직임 지시어입니다."
                    )
                with col_d:
                    default_dur_input = st.number_input("기본 길이(초):", min_value=5, max_value=5, value=5)

            # 2. 미리보기
            with st.expander(" 생성 대기열 확인", expanded=False):
                matches = st.session_state.track_b_matches
                audios = st.session_state.track_b_audio
                candidates_map = {c['page_num']: c for c in st.session_state.track_b_candidates}
                
                preview_data = []
                for i, match in enumerate(matches):
                    pg = match['page']
                    audio_dur = audios[i]['duration'] if i < len(audios) and audios[i] else None
                    gen_seconds = (5 if audio_dur <= 6.0 else 10) if audio_dur else default_dur_input
                    preview_data.append({
                        "Scene": i+1, "Page": pg, 
                        "TTS Len": f"{audio_dur:.1f}s" if audio_dur else "N/A",
                        "Gen Len": f"{gen_seconds}s"
                    })
                st.dataframe(preview_data)

            # 3. 실행 버튼
            has_results = len(st.session_state.track_b_video_results) > 0
            # 버튼 라벨에 버전 정보 표시
            btn_label = f" 영상 생성 (S{cur_script_ver}/A{cur_audio_ver} ➔ Video v{next_video_ver})"
            
            if st.button(btn_label, type="primary"):
                # UID 생성
                uid = uuid.uuid4().hex[:6]
                
                # [버전 관리] 폴더 구조 생성: v{S}_{A}_{V}
                folder_name = f"v{cur_script_ver}_{cur_audio_ver}_{next_video_ver}"
                
                # BASE_OUT을 버전별 폴더로 설정
                BASE_OUT = VIDEO_BASE_DIR / folder_name
                
                RAW_DIR = BASE_OUT / "raw"
                TRIMMED_DIR = BASE_OUT / "trimmed"
                # FULL_DIR, FULL_TTS_DIR 등은 이 폴더 구조에 맞게 통합
                
                BASE_OUT.mkdir(parents=True, exist_ok=True)
                RAW_DIR.mkdir(parents=True, exist_ok=True)
                TRIMMED_DIR.mkdir(parents=True, exist_ok=True)
                
                generated_data_list = []
                progress_bar = st.progress(0)
                status_text = st.empty()
                total_scenes = len(matches)

                try:
                    # =================================================
                    # [Part A] 개별 영상 생성 및 트리밍
                    # =================================================
                    for i, match in enumerate(matches):
                        pg = match['page']
                        img_info = candidates_map.get(pg)
                        if not img_info: continue
                        img_path = str(img_info['img_path'])
                        
                        audio_dur = audios[i]['duration'] if i < len(audios) and audios[i] else None
                        
                        if audio_dur is None:
                            runway_dur = default_dur_input; target_trim_dur = default_dur_input
                        else:
                            runway_dur = 5 if audio_dur <= 6.0 else 10
                            target_trim_dur = audio_dur

                        status_text.write(f" Scene {i+1}/{total_scenes}: Runway 생성({runway_dur}s) 및 자르기({target_trim_dur:.1f}s)...")

                        # Runway 호출
                        result_json = generate_video_from_image(img_path, global_prompt, runway_dur)
                        video_url = extract_video_url(result_json)

                        if not video_url:
                            st.error(f"Scene {i+1} 생성 실패")
                            continue

                        # 저장 (RAW_DIR는 버전별 폴더)
                        raw_filename = f"scene_{i+1:02d}_{uid}_raw.mp4"
                        raw_path = RAW_DIR / raw_filename
                        download_video(video_url, raw_path)

                        # 트리밍 (TRIMMED_DIR는 버전별 폴더)
                        final_filename = f"scene_{i+1:02d}_{uid}_trimmed.mp4"
                        final_path = TRIMMED_DIR / final_filename

                        # 3. 길이 조절
                        final_filename = f"scene_{i+1:02d}_{uid}_trimmed.mp4"
                        final_path = TRIMMED_DIR / final_filename

                        # MoviePy로 로드
                        clip = VideoFileClip(str(raw_path))
                        
                        if target_trim_dur <= runway_dur:
                            # Case A: 영상이 오디오보다 김 (예: 영상 5초 > 오디오 1.6초)
                            # -> 그냥 오디오 길이에 맞춰서 뒤를 잘라버림 (Trim)
                            final_clip = clip.subclip(0, target_trim_dur)
                            st.caption(f"✂️ Scene {i+1}: 자르기 (Trim) ({runway_dur}s → {target_trim_dur:.1f}s)")
                        
                        else:
                            # Case B: 영상이 오디오보다 짧음 (예: 영상 5초 < 오디오 6.0초)
                            # -> 영상을 오디오 길이만큼 느리게 늘림 (Stretch / Slow Motion)
                            # vfx.speedx를 쓰면 final_duration에 맞춰 속도를 자동 조절해줍니다.
                            final_clip = clip.fx(vfx.speedx, final_duration=target_trim_dur)
                            st.caption(f"🐢 Scene {i+1}: 늘리기 (Slow) ({runway_dur}s → {target_trim_dur:.1f}s)")
                        
                        # 처리된 영상 저장
                        final_clip.write_videofile(
                            str(final_path), 
                            fps=24, 
                            codec="libx264", 
                            audio=False, # 오디오 없는 상태로 저장
                            logger=None  # 로그 숨김
                        )
                        clip.close()
                        final_clip.close()
                        
                        generated_data_list.append({"raw": str(raw_path), "trimmed": str(final_path)})
                        progress_bar.progress((i + 1) / total_scenes)

                    st.session_state.track_b_video_results = generated_data_list


                    # =================================================
                    # [Part B] 전체 영상 병합 (Visual Only)
                    # =================================================
                    if generated_data_list:
                        status_text.write("🔗 전체 영상(무음) 병합 중...")
                        
                        # Visual Only (무음 병합)
                        clips_vis = [VideoFileClip(d['trimmed']) for d in generated_data_list]
                        final_clip_vis = concatenate_videoclips(clips_vis, method="compose")
                        
                        # Full 파일도 버전 폴더 안에 저장
                        full_vis_path = BASE_OUT / f"full_visual_{uid}.mp4"
                        final_clip_vis.write_videofile(
                            str(full_vis_path), fps=24, codec="libx264", audio=False, logger=None
                        )
                        full_str = str(full_vis_path)
                        
                        for c in clips_vis: c.close()

                    # [저장] Manifest 파일 생성 (버전 관리 핵심)
                    manifest = {
                        "script_ver": cur_script_ver,
                        "audio_ver": cur_audio_ver,
                        "video_ver": next_video_ver,
                        "created_at": str(datetime.now()),
                        "prompt": global_prompt,
                        "full_visual_path": full_str,
                        "clips": generated_data_list
                    }
                    with open(BASE_OUT / "manifest.json", "w", encoding="utf-8") as f:
                        json.dump(manifest, f, ensure_ascii=False, indent=4)

                    # 세션 갱신
                    st.session_state.track_b_video_results = generated_data_list
                    st.session_state.track_b_full_visual = full_str
                    st.session_state[f"loaded_vid_dir_{cur_mode}"] = str(BASE_OUT)

                    st.success(f" 영상 생성 완료! (버전: S{cur_script_ver}/A{cur_audio_ver}/V{next_video_ver})")
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"작업 중 오류 발생: {e}")

            # 4. 결과 확인 및 개별 재생성
            if st.session_state.track_b_video_results:
                
                # [상단] 무음 병합 결과물 확인
                if st.session_state.track_b_full_visual and os.path.exists(st.session_state.track_b_full_visual):
                    st.divider()
                    
                    # 현재 보여주는 버전 정보 표시
                    ver_info = selected_key if selected_key else (cur_script_ver, cur_audio_ver, next_video_ver - 1)
                    st.markdown(f"#### 전체 병합 영상 (S{ver_info[0]}/A{ver_info[1]}/V{ver_info[2]})")
                    
                    st.video(st.session_state.track_b_full_visual)
                    with open(st.session_state.track_b_full_visual, "rb") as f:
                        st.download_button(" 무음 영상 다운로드", f, file_name="full_visual.mp4", mime="video/mp4")

                # [하단] 개별 씬 확인 및 수정 (In-Place Regeneration)
                with st.expander(" 개별 씬(Scene) 상세 확인 및 수정", expanded=True):
                    cols = st.columns(3)
                    
                    # 재생성 시 필요한 현재 로드된 폴더 경로
                    current_loaded_dir = Path(st.session_state.get(f"loaded_vid_dir_{cur_mode}", ""))
                    
                    for i, vid_data in enumerate(st.session_state.track_b_video_results):
                        # 데이터가 유효하지 않으면 스킵
                        if not vid_data.get('raw'): continue

                        with cols[i % 3]:
                            st.markdown(f"**Scene {i+1}**")
                            
                            tab_trim, tab_raw = st.tabs(["✂️ Trim", "🎞️ Raw"])
                            with tab_trim:
                                if os.path.exists(vid_data['trimmed']): st.video(vid_data['trimmed'])
                            with tab_raw:
                                if os.path.exists(vid_data['raw']): st.video(vid_data['raw'])
                            
                            # =========================================================
                            # [개별 재생성 버튼 로직]
                            # =========================================================
                            if st.button(f"🔄 Scene {i+1} 다시 생성", key=f"regen_btn_{i}"):
                                try:
                                    if not current_loaded_dir.exists():
                                        st.error("저장 경로를 찾을 수 없습니다. 다시 로드해주세요.")
                                    else:
                                        status_text_regen = st.empty()
                                        status_text_regen.info(f"⏳ Scene {i+1} 다시 생성 중...")
                                        
                                        # 1. 정보 준비
                                        match = matches[i]
                                        img_info = candidates_map.get(match['page'])
                                        img_path = str(img_info['img_path'])
                                        
                                        audio_dur = audios[i]['duration'] if i < len(audios) and audios[i] else None
                                        
                                        if audio_dur is None:
                                            runway_dur = default_dur_input; target_trim_dur = default_dur_input
                                        else:
                                            runway_dur = 5 if audio_dur <= 6.0 else 10
                                            target_trim_dur = audio_dur
                                        
                                        # 2. Runway 재생성
                                        result_json = generate_video_from_image(img_path, global_prompt, runway_dur)
                                        video_url = extract_video_url(result_json)
                                        
                                        if video_url:
                                            # 3. 파일 덮어쓰기 (현재 버전 폴더 내의 파일)
                                            # vid_data['raw'] 경로가 절대 경로여야 함.
                                            raw_p = Path(vid_data['raw'])
                                            trim_p = Path(vid_data['trimmed'])
                                            
                                            download_video(video_url, raw_p)
                                            
                                            # 4. 다시 트리밍
                                            clip = VideoFileClip(str(raw_p))
                                            if target_trim_dur <= runway_dur:
                                                final_clip = clip.subclip(0, target_trim_dur)
                                            else:
                                                final_clip = clip.fx(vfx.speedx, final_duration=target_trim_dur)
                                            
                                            final_clip.write_videofile(
                                                str(trim_p), fps=24, codec="libx264", audio=False, logger=None
                                            )
                                            clip.close(); final_clip.close()
                                            
                                            # 5. 전체 병합 다시 (현재 폴더의 모든 클립으로)
                                            status_text_regen.info(" 전체 영상 갱신 중...")
                                            
                                            # 현재 세션 데이터 기준으로 병합
                                            all_clips_paths = [d['trimmed'] for d in st.session_state.track_b_video_results]
                                            clips_vis = [VideoFileClip(p) for p in all_clips_paths]
                                            final_clip_vis = concatenate_videoclips(clips_vis, method="compose")
                                            
                                            full_vis_path = current_loaded_dir / f"full_visual_updated_{uuid.uuid4().hex[:4]}.mp4"
                                            final_clip_vis.write_videofile(
                                                str(full_vis_path), fps=24, codec="libx264", audio=False, logger=None
                                            )
                                            for c in clips_vis: c.close()
                                            
                                            # 세션 및 Manifest 업데이트
                                            st.session_state.track_b_full_visual = str(full_vis_path)
                                            
                                            # Manifest 파일 갱신 (Full Path가 바뀌었으므로)
                                            manifest_path = current_loaded_dir / "manifest.json"
                                            if manifest_path.exists():
                                                with open(manifest_path, "r", encoding="utf-8") as f:
                                                    m_data = json.load(f)
                                                m_data['full_visual_path'] = str(full_vis_path)
                                                with open(manifest_path, "w", encoding="utf-8") as f:
                                                    json.dump(m_data, f, ensure_ascii=False, indent=4)
                                            
                                            st.success("재생성 및 병합 완료!")
                                            st.rerun()
                                        else:
                                            st.error("Runway 생성 실패")
                                        
                                except Exception as e:
                                    st.error(f"개별 재생성 중 오류: {e}")

                # 5. Step 8 이동 (기존 코드 유지)
                st.divider()
                col_m, col_b = st.columns([3, 1])
                with col_m:
                    st.caption("영상 소스가 준비되었습니다. 오디오 합성 및 자막 작업(Step 8)으로 이동하시겠습니까?")
                with col_b:
                    if st.button(" Step 8 이동 (최종작업)"):
                        story_dir_name = txt_file.stem
                        st.session_state.track_b_output_dirs = {
                            "root": str(Path("outputs") / story_dir_name),
                            "full": str(st.session_state.track_b_full_visual)
                        }
                        st.session_state.track_b_step = 8
                        st.session_state.current_script_ver = cur_script_ver
                        st.session_state.current_audio_ver  = cur_audio_ver
                        st.session_state.current_video_ver  = next_video_ver
                        st.toast("Step 8: 자막 및 최종 편집 단계로 이동합니다.")
                        st.rerun()

    # =========================================================
    # [Step 8] 최종 완성: 오디오 합성 및 자막 생성
    # =========================================================
    if st.session_state.get("track_b_step", 0) >= 8:
        st.divider()
        st.subheader("Step 8. 최종 영상 완성 (오디오 + 자막)")
        st.info(" 개별 영상에 TTS 음성과 자막을 입힌 뒤, 최종 결과물로 합칩니다.")

        # 0. 필요한 데이터 확인
        matches = st.session_state.get("track_b_matches", [])
        audios = st.session_state.get("track_b_audio", [])
        video_results = st.session_state.get("track_b_video_results", [])
        scripts = st.session_state.get("step1_scripts", [])

        # Step 1.5 데이터 가져오기 (매칭용)
        char_info = st.session_state.get("track_b_characters", {})
        dialogue_map = char_info.get("dialogue_map", []) if char_info else []
        
        # 현재 모드 확인 (Step 3에서 설정한 session_state 값)
        # 만약 값이 없으면 기본값 'Standard'로 가정
        current_mode = st.session_state.get("script_style_mode", "Standard")

        if not video_results or not audios or not scripts:
            st.error("이전 단계의 데이터(영상, 오디오, 대본)가 부족합니다. Step 7을 먼저 완료해주세요.")
        else:
            # ------------------------------------------------------------------
            # [버전 추적] 현재 작업 중인 S/A/V 버전 파악
            # ------------------------------------------------------------------
            # Step 7에서 로드된 영상 폴더명(v1_1_1)에서 버전을 역추적합니다.
            cur_s = st.session_state.get("current_script_ver")
            cur_a = st.session_state.get("current_audio_ver")
            cur_v = st.session_state.get("current_video_ver")

            if not (cur_s and cur_a and cur_v):
                # fallback: 기존처럼 loaded_vid_dir 파싱
                loaded_vid_dir = st.session_state.get(f"loaded_vid_dir_{current_mode}", "")
                try:
                    dir_name = Path(loaded_vid_dir).name
                    m = re.match(r"^v(\d+)_(\d+)_(\d+)$", dir_name)
                    if m:
                        cur_s, cur_a, cur_v = map(int, m.groups())
                    else:
                        raise ValueError
                except:
                    cur_s = st.session_state.get("current_script_ver", 1)
                    cur_a = st.session_state.get("current_audio_ver", 1)
                    cur_v = 1

            # 기본 경로: outputs/동화명/final/모드명
            story_dir_name = txt_file.stem
            FINAL_BASE_DIR = Path("outputs") / story_dir_name / "final" / current_mode
            FINAL_BASE_DIR.mkdir(parents=True, exist_ok=True)

            # ------------------------------------------------------------------
            # [로드 로직] 기존 Final 버전 탐색
            # ------------------------------------------------------------------
            sorted_keys, path_map = get_final_versions(FINAL_BASE_DIR)
            
            selected_key = None
            
            if sorted_keys:
                col_sel, col_space = st.columns([1, 2])
                with col_sel:
                    def format_func(k):
                        s, a, v, f = k
                        is_match = (s == cur_s and a == cur_a and v == cur_v)
                        prefix = "✅ " if is_match else ""
                        is_latest = (k == sorted_keys[0])
                        return f"{prefix}S{s}/A{a}/V{v} ➔ Final v{f}" + (" (최신)" if is_latest else "")

                    selected_key = st.selectbox(
                        f" '{current_mode}' 최종본 기록 불러오기:",
                        sorted_keys,
                        format_func=format_func,
                        key=f"final_sel_{current_mode}"
                    )
                
                # 선택된 폴더 로드
                target_dir = path_map[selected_key]
                if st.session_state.get(f"loaded_final_dir_{current_mode}") != str(target_dir):
                    try:
                        with open(target_dir / "manifest.json", "r", encoding="utf-8") as f:
                            data = json.load(f)
                        st.session_state.track_b_final_movie = data.get("final_movie_path", "")
                        st.session_state[f"loaded_final_dir_{current_mode}"] = str(target_dir)
                    except Exception as e:
                        st.error(f"로드 실패: {e}")

            # 다음 Final 버전 계산 (현재 S/A/V 조합 내에서)
            # 예: v1_1_1_1, v1_1_1_2가 있으면 next는 3
            existing_finals = [k[3] for k in sorted_keys if k[0]==cur_s and k[1]==cur_a and k[2]==cur_v]
            next_final_ver = (max(existing_finals) + 1) if existing_finals else 1


            # =======================================
            # 🎵 BGM 설정 섹션
            # =======================================
            st.markdown("---")
            st.markdown("#### 🎵 배경음악(BGM) 설정")

            # ---------------------------------------------------------
            # [BGM 경로 설정] OR 로직 적용 (우선순위 체크)
            # ---------------------------------------------------------
            story_dir_name = txt_file.stem

            # 후보 1: 파일명 그대로 사용 (Step 6.5 방식)
            path_candidate_1 = Path("BGM") / story_dir_name

            # 후보 2: 변환 함수 사용 (Step 8 방식)
            # (주의: get_bgm_folder_name 함수가 코드 상단에 정의되어 있어야 합니다)
            path_candidate_2 = Path("BGM") / get_bgm_folder_name(story_dir_name)

            # [로직] 1번이 있으면 1번, 없으면 2번, 둘 다 없으면 1번을 기본값으로
            if path_candidate_1.exists() and path_candidate_1.is_dir():
                BGM_DIR = path_candidate_1
                st.caption(f"BGM 경로 확인됨 (Type A): {BGM_DIR}")
            elif path_candidate_2.exists() and path_candidate_2.is_dir():
                BGM_DIR = path_candidate_2
                st.caption(f"BGM 경로 확인됨 (Type B): {BGM_DIR}")
            else:
                BGM_DIR = path_candidate_1  # 폴더가 없을 경우 기본값

            use_bgm = st.checkbox("배경음악 사용 (페이지별 자동 매칭)", value=False, key="step8_use_bgm")
            bgm_volume = 0.15

            if use_bgm:
                if BGM_DIR.exists():
                    bgm_files = sorted([f.name for f in BGM_DIR.iterdir() if f.suffix.lower() in ['.wav', '.mp3', '.m4a']])
                    if bgm_files:
                        bgm_volume = st.slider("BGM 볼륨 (%):", 5, 100, 15, key="step8_bgm_volume") / 100.0
                        st.info(f" BGM 폴더 발견: {len(bgm_files)}개 파일")
                        st.caption("각 페이지 번호에 맞는 BGM이 자동으로 선택됩니다.")
                    else:
                        st.warning(f"BGM 폴더에 오디오 파일이 없습니다: {BGM_DIR}")
                        use_bgm = False
                else:
                    st.warning(f"BGM 폴더가 없습니다: {BGM_DIR}")
                    use_bgm = False

            st.markdown("---")
            
            # ---------------------------------------------------------
            # [UI] 자막 스타일 선택
            # ---------------------------------------------------------
            st.write(" **자막 스타일 설정**")
            subtitle_mode_final = st.radio(
                "최종 영상 자막 색상:",
                ["🏳️ 기본 (흰색 통일)", "🌈 캐릭터별 자동 컬러링"],
                index=0,
                horizontal=True,
                key="step8_sub_mode"
            )

            st.markdown("---")

            # 2. 실행 버튼 (버전 정보 포함)
            btn_label = f" 최종 영상 생성 (S{cur_s}/A{cur_a}/V{cur_v} ➔ Final v{next_final_ver})"
            
            if st.button(btn_label, type="primary"):
                if "proc_uid" not in st.session_state:
                    st.session_state.proc_uid = uuid.uuid4().hex[:8]
                uid = st.session_state.proc_uid
                
                # ---------------------------------------------------------
                # [핵심] 버전별 폴더 생성 및 경로 지정
                # ---------------------------------------------------------
                folder_name = f"v{cur_s}_{cur_a}_{cur_v}_{next_final_ver}"
                NEW_VER_DIR = FINAL_BASE_DIR / folder_name
                
                # 내부 구조: clips(자막합성본), final(최종본)
                SUB_DIR = NEW_VER_DIR / "clips"
                
                NEW_VER_DIR.mkdir(parents=True, exist_ok=True)
                SUB_DIR.mkdir(parents=True, exist_ok=True)
                
                progress_bar = st.progress(0)
                status_text = st.empty()
                
                final_clips_paths = []
                total_cnt = len(video_results)

                # [중요] 표지 페이지 번호 가져오기 (Step 6 저장값)
                if 'cover_page_num' in st.session_state:
                    cover_page_num = st.session_state['cover_page_num']
                elif candidates:
                    cover_page_num = candidates[0]['page_num']
                else:
                    cover_page_num = 1

                speaker_color_map = {}
                if "컬러링" in subtitle_mode_final:
                    speaker_color_map = generate_dynamic_color_map(scripts)
                try:
                    for i, vid_data in enumerate(video_results):
                        # A. 데이터 준비
                        video_path = vid_data.get("trimmed")
                        if not video_path or not os.path.exists(video_path):
                            st.error(f"Scene {i+1} 영상 파일이 없습니다.")
                            continue

                        # 오디오 경로
                        audio_path = audios[i].get("path") if i < len(audios) else None
                        
                        
                        # 기본 대사 (Step 3 결과)
                        script_item = scripts[i] if i < len(scripts) else {}
                        original_text = script_item.get("text", "")      # 대사(Quote)
                        source_page = script_item.get("source_page", 0)  # 페이지 번호
                        current_speaker = str(script_item.get("speaker", "narrator")).strip()
                        
                        # ★ 색상 결정 로직
                        if "컬러링" in subtitle_mode_final:
                            text_color = speaker_color_map.get(current_speaker, "white")
                        else:
                            text_color = "white"
                        
                        subtitle_text_to_burn = original_text
                        
                        # -----------------------------------------------------------
                        # [핵심 로직 변경] 자막 텍스트 결정 (Context 매칭)
                        # -----------------------------------------------------------
                        subtitle_text_to_burn = original_text # 기본값: 원래 대사 그대로 사용
                        
                        """ # 모드가 'Conversation'이고, 매칭할 1.5 데이터가 있다면 실행
                        if current_mode == "Conversation" and dialogue_map:
                            found_context = None
                            
                            # dialogue_map에서 Quote와 Page가 일치하는 Context 찾기
                            for d_item in dialogue_map:
                                # 대사가 정확히 일치하거나 포함되어 있고, 페이지도 같다면 매칭
                                # (strip()으로 공백 제거 후 비교)
                                if (d_item.get("quote", "").strip() == original_text.strip()) and \
                                   (d_item.get("page_num") == source_page):
                                    found_context = d_item.get("context")
                                    break
                            
                            if found_context:
                                # 매칭 성공 시: 자막을 Context(상황 설명)로 교체
                                # 예: 대사는 "안돼!"지만 자막은 "(놀부가 밥주걱을 휘두르며)" 로 나옴
                                subtitle_text_to_burn = f"{found_context}" 
                                # 원한다면 화자 이름도 같이 넣을 수 있음: f"{script_item.get('speaker')}: {found_context}"
                            else:
                                # 매칭 실패 시: 그냥 대사 출력 (혹은 "상황 설명 없음" 등)
                                pass  """
                                
                        # -----------------------------------------------------------

                        # 출력 파일명 정의
                        output_clip_path = SUB_DIR / f"scene_{i+1:02d}_{uid}_complete.mp4"
                        
                        # 1. 자막 입히기                        
                        temp_sub_path = str(output_clip_path).replace(".mp4", "_temp_sub.mp4")

                        status_text.write(f" Scene {i+1}/{total_cnt}: 자막 & 오디오 합성 중...")

                        add_subtitle_to_video(
                            video_path,           # 1. 입력 영상
                            subtitle_text_to_burn,# 2. 결정된 자막 텍스트 (Context 혹은 대사)
                            temp_sub_path,        # 3. 출력 경로
                            scene_index=i,
                            font_color=text_color 
                        )

                        # 2. 오디오 입히기 (BGM 포함) - [Step 6.5 로직 적용]
                        audio_ok = False

                        # 자막 영상 생성 확인
                        if not os.path.exists(temp_sub_path):
                            st.warning(f"⚠️ Scene {i+1}: 자막 영상 생성 실패 (temp_sub 없음)")
                        elif audio_path and os.path.exists(audio_path):
                            page_bgm = None

                            # BGM 검색 로직
                            if use_bgm:
                                search_keywords = []
                                log_msg = ""

                                # Case 1: 표지 (Title)
                                if source_page == 0 or source_page == cover_page_num:
                                    search_keywords = ["title", "Title", "TITLE"]
                                    log_msg = "Title"

                                # Case 2: 본문 (nP)
                                elif source_page > cover_page_num:
                                    story_seq = source_page - cover_page_num + 1
                                    search_keywords = [f"{story_seq}p", f"{story_seq}P", f"Page {story_seq}", f"Page{story_seq}"]
                                    log_msg = f"{story_seq}p"

                                if search_keywords:
                                    page_bgm = find_bgm_file(search_keywords, BGM_DIR)

                            # BGM이 있으면 TTS+BGM 시도
                            if page_bgm and page_bgm.exists():
                                audio_ok = add_audio_to_video(
                                    temp_sub_path,
                                    str(audio_path),
                                    str(output_clip_path),
                                    bgm_path=str(page_bgm),
                                    bgm_volume=bgm_volume
                                )
                                if audio_ok is True:
                                    st.caption(f"🎵 Scene {i+1}: BGM '{page_bgm.name}' ({log_msg}) 적용")

                            # BGM이 없거나 BGM+TTS 실패 → TTS만 적용
                            if audio_ok is not True:
                                audio_ok = add_audio_to_video(
                                    temp_sub_path,
                                    str(audio_path),
                                    str(output_clip_path)
                                )
                                if audio_ok is True:
                                    if use_bgm:
                                        st.caption(f"Scene {i+1}: BGM 없음, TTS만 적용 (Source: {source_page}p)")
                                else:
                                    st.warning(f"⚠️ Scene {i+1}: TTS 합성 실패 - {audio_ok}")
                        else:
                            st.caption(f"Scene {i+1}: TTS 파일 없음 (audio_path={audio_path})")

                        # 오디오 합성 실패 시 → 자막만 입힌 영상 사용
                        if not audio_ok:
                            if os.path.exists(temp_sub_path):
                                shutil.copy2(temp_sub_path, str(output_clip_path))

                        # 임시 파일 정리
                        if os.path.exists(temp_sub_path) and temp_sub_path != str(output_clip_path):
                            os.remove(temp_sub_path)

                        # 클립 파일 추가
                        if os.path.exists(str(output_clip_path)) and os.path.getsize(str(output_clip_path)) > 0:
                            final_clips_paths.append(str(output_clip_path))
                        else:
                            st.warning(f"⚠️ Scene {i+1} 클립 생성 실패 - 건너뜁니다.")
                        progress_bar.progress((i + 1) / total_cnt)

                    # 3. 전체 이어붙이기
                    if final_clips_paths:
                        status_text.write(" 최종 합체 중...")
                        # 최종 파일도 NEW_VER_DIR 안에 저장
                        final_movie_path = NEW_VER_DIR / f"final_movie_{uid}.mp4"

                        concat_result = concat_videos_with_audio(final_clips_paths, str(final_movie_path))

                        if concat_result is True and os.path.exists(str(final_movie_path)):
                            # [Manifest] 저장
                            manifest = {
                                "version_info": {"s": cur_s, "a": cur_a, "v": cur_v, "f": next_final_ver},
                                "created_at": str(datetime.now()),
                                "bgm_used": use_bgm,
                                "bgm_volume": bgm_volume,
                                "final_movie_path": str(final_movie_path),
                                "clips": final_clips_paths
                            }
                            with open(NEW_VER_DIR / "manifest.json", "w", encoding="utf-8") as f:
                                json.dump(manifest, f, ensure_ascii=False, indent=4)

                            # 세션 갱신
                            st.session_state.track_b_final_movie = str(final_movie_path)
                            st.session_state[f"loaded_final_dir_{current_mode}"] = str(NEW_VER_DIR)

                            st.success(f" 최종 영상 완성! (Ver. {next_final_ver})")
                            status_text.empty()

                            # 즉시 영상 표시 (버튼 블록 내에서)
                            st.video(str(final_movie_path))
                            with open(str(final_movie_path), "rb") as dl_f:
                                st.download_button(
                                    label="💾 최종 영상 다운로드 (.mp4)",
                                    data=dl_f,
                                    file_name=f"{txt_file.stem}_final_v{next_final_ver}.mp4",
                                    mime="video/mp4",
                                    key="step8_immediate_download"
                                )
                        else:
                            error_detail = concat_result if isinstance(concat_result, str) else "알 수 없는 오류"
                            st.error(f"최종 영상 합치기에 실패했습니다: {error_detail}")
                            status_text.empty()

                except Exception as e:
                    st.error(f"작업 중 오류 발생: {e}")

            # 3. 결과 확인 및 다운로드
            if st.session_state.get("track_b_final_movie") and os.path.exists(st.session_state.track_b_final_movie):
                st.divider()
                # 버전 라벨 표시
                if selected_key:
                    s, a, v, f = selected_key
                    ver_display = f"S{s}/A{a}/V{v}/F{f}"
                else:
                    ver_display = f"S{cur_s}/A{cur_a}/V{cur_v}/F{next_final_ver-1}"

                st.markdown(f"### 🍿 최종 완성본 미리보기 ({ver_display})")

                st.session_state.current_final_ver = next_final_ver

                final_path = st.session_state.track_b_final_movie
                st.video(final_path)
                
                col_d1, col_d2 = st.columns(2)
                with col_d1:
                    with open(final_path, "rb") as f:
                        st.download_button(
                            label="💾 최종 영상 다운로드 (.mp4)",
                            data=f,
                            file_name=f"{txt_file.stem}_final_v{ver_display}.mp4",
                            mime="video/mp4",
                            type="primary"
                        )
                with col_d2:
                    if st.button("🔄 처음부터 다시 만들기 (새로고침)"):
                        st.session_state.clear()
                        st.rerun()