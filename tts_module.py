# -*- coding: utf-8 -*-
# tts_module.py
# TTS 메인 인터페이스 (API 호출 및 워크플로우 관리)

import re
import os
import time
import shutil
import requests
import hashlib
import asyncio
from typing import List, Dict, Optional
from pathlib import Path
from dotenv import load_dotenv

# 공통 로직 모듈 임포트
import tts_core
from tts_core import add_audio_to_video, concat_videos_with_audio, get_audio_duration

# 클로바 API 설정 (환경변수 우선, 폴백으로 기본값)
import os
from dotenv import load_dotenv
load_dotenv()

# ==========================================
# 1. API 설정 (Clova / OpenAI / GEMINI 등)
# ==========================================
CLOVA_CLIENT_ID = os.getenv("CLOVA_CLIENT_ID", "ozpoytlz95")
CLOVA_CLIENT_SECRET = os.getenv("CLOVA_CLIENT_SECRET", "34c2g67KpznehFvEOzamxoqrrfSsQP5tzey1dwi2")
CLOVA_ENDPOINT = "https://naveropenapi.apigw.ntruss.com/tts-premium/v1/tts"

# [OpenAI 설정]
try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False
    print("OpenAI 라이브러리가 없습니다. (pip install openai)")

OPENAI_TTS_MODEL = "gpt-4o-mini-tts"

from google.cloud import texttospeech
from google.oauth2 import service_account

# 서비스 계정 키 파일 이름 (tts_module.py와 같은 폴더)
SERVICE_ACCOUNT_FILE = "tts-gemini-env.json"

import wave  # WAV 파일 저장용
# Google GenAI SDK 설정 preview 버전 사용
try:
    from google import genai
    from google.genai import types
    HAS_GOOGLE_GENAI = True
except ImportError:
    HAS_GOOGLE_GENAI = False
    print("⚠️ google-genai 라이브러리가 없습니다. (pip install google-genai)")

# [핵심] Gemini 모델 매핑 (엔진 키 -> 실제 모델 ID)
GEMINI_MODELS = {
    "default": "gemini-2.5-flash-tts",  # 기본값
    "flash": "gemini-2.5-flash-tts",    # engine="gemini-flash"
    "pro": "gemini-2.5-pro-tts",        # engine="gemini-pro"
}

# [엔진별 글자수 제한]
LIMITS = {
    "clova": 2000,
    "gpt": 2000,
    "gemini": 2000
}

# [GPT 화자 매핑] (#자동 배정 추후 상세 수정 필요함)
GPT_VOICE_MAP = {
    "narrator": "marin",       # 나레이션 최적
    "narrator_male": "cedar",
    "young_female": "coral",   # 밝은 톤
    "adult_female": "sage",    # 차분한 톤
    "child_female": "ballad",  # 감성적 (아이 대용)
    "child_male": "ash",       # (대안)
    "young_male": "verse",     # 드라마틱
    "adult_male": "ash",       # 강한 톤
    "elder_female": "shimmer",
    "elder_male": "onyx",      # 저음
    "default": "alloy"
}

#  Gemini 화자 매핑 (가이드 기반 별자리 이름)
GEMINI_VOICE_MAP = {
    "narrator": "Puck",       # 남성 (내레이션용)
    "narrator_male": "Puck",
    "young_female": "Aoede",  # 여성 (밝음)
    "adult_female": "Kore",   # 여성 (차분함)
    "child_female": "Aoede",  # (대안)
    "child_male": "Puck",     # (대안)
    "young_male": "Fenrir",   # 남성 (활기참)
    "adult_male": "Charon",   # 남성 (굵음)
    "elder_female": "Leda",   # 여성
    "elder_male": "Charon",   # 남성
    "default": "Puck"
}

# Edge TTS 폴백 지원 (무료, 다중 목소리)
try:
    import edge_tts
    import asyncio
    HAS_EDGE_TTS = True
except ImportError:
    HAS_EDGE_TTS = False


# Edge TTS 한국어 목소리 매핑 (Clova 실패 시 폴백용)
EDGE_TTS_VOICES = {
    'child_male': 'ko-KR-InJoonNeural',
    'child_female': 'ko-KR-SunHiNeural',
    'young_male': 'ko-KR-InJoonNeural',
    'young_female': 'ko-KR-SunHiNeural',
    'adult_male': 'ko-KR-BongJinNeural',
    'adult_female': 'ko-KR-YuJinNeural',
    'elder_male': 'ko-KR-GookMinNeural',
    'elder_female': 'ko-KR-SoonBokNeural',
    'narrator': 'ko-KR-SunHiNeural',
    'default': 'ko-KR-SunHiNeural',
}

# 감정 지원 화자 (공식 문서 기준)
EMOTION_SUPPORTED = {
    "nara": {"anger_supported": False},
    "vara": {"anger_supported": True},
    "vmikyung": {"anger_supported": True},
    "vdain": {"anger_supported": True},
    "vyuna": {"anger_supported": True},
    "vgoeun": {"anger_supported": True},
    "vdaeseong": {"anger_supported": True},
}

# PRO 화자의 emotion-strength 지원
EMOTION_STRENGTH_SUPPORTED = {"vara", "vmikyung", "vdain", "vyuna"}


# ==========================================
# 2. 내부 API 호출 함수 (Drivers)
# ==========================================


def get_edge_voice_type(speaker: str) -> str:
    """Clova 화자 → Edge TTS 목소리 타입 매핑"""
    speaker_lower = speaker.lower()

    # 1. Clova 화자 ID 직접 매핑 (VOICE_ALIASES 역매핑)
    CLOVA_TO_EDGE = {
        # 아동
        'nhajun': 'child_male', 'ndain': 'child_female', 'nmammon': 'child_female',
        # 청년 여성
        'nara': 'young_female', 'nara_call': 'young_female', 'vyuna': 'young_female',
        'vara': 'young_female', 'vmikyung': 'young_female', 'vdain': 'young_female',
        'vgoeun': 'young_female', 'nsujin': 'young_female', 'nsinu': 'young_female',
        # 청년 남성
        'nwoof': 'young_male', 'noyj': 'young_male', 'nyejun': 'young_male',
        'njooahn': 'young_male', 'vdaeseong': 'adult_male',
        # 성인 여성
        'nyejin': 'adult_female', 'nmiyeon': 'adult_female', 'nheeyeon': 'adult_female',
        'ngaram': 'adult_female',
        # 성인 남성
        'nminsang': 'adult_male', 'nminho': 'elder_male', 'nwontak': 'adult_male',
        'nkwangsu': 'adult_male', 'njonghyun': 'adult_male', 'njoonyoung': 'adult_male',
        # 나레이터
        'njiyun': 'narrator',
        # 특수
        'nmeow': 'child_female',
    }

    # Clova 화자 ID 직접 매칭
    if speaker_lower in CLOVA_TO_EDGE:
        return CLOVA_TO_EDGE[speaker_lower]

    # 2. 키워드 기반 매핑 (기존 로직)
    if any(k in speaker_lower for k in ['child', '아이', '꼬마', '어린']):
        if any(k in speaker_lower for k in ['female', '여', '소녀']):
            return 'child_female'
        return 'child_male'
    elif any(k in speaker_lower for k in ['elder', '할머니', '할아버지', '노인']):
        if any(k in speaker_lower for k in ['female', '여', '할머니']):
            return 'elder_female'
        return 'elder_male'
    elif any(k in speaker_lower for k in ['adult', '어른', '아저씨', '아줌마']):
        if any(k in speaker_lower for k in ['female', '여', '아줌마', '엄마']):
            return 'adult_female'
        return 'adult_male'
    elif any(k in speaker_lower for k in ['narrator', '나레이터']):
        return 'narrator'
    elif any(k in speaker_lower for k in ['female', '여']):
        return 'young_female'
    elif any(k in speaker_lower for k in ['male', '남']):
        return 'young_male'

    return 'default'

def edge_tts_fallback(text: str, output_path: str, voice_type: str = 'default') -> bool:
    """
    Edge TTS 폴백 (Clova 실패 시) - 무료, 다중 목소리

    Args:
        text: 변환할 텍스트
        output_path: 출력 파일 경로
        voice_type: 목소리 타입 (EDGE_TTS_VOICES 키)

    Returns:
        성공 여부
    """
    if not HAS_EDGE_TTS:
        print("    [WARN] Edge TTS not installed (pip install edge-tts)")
        return False

    try:
        edge_voice = EDGE_TTS_VOICES.get(voice_type, EDGE_TTS_VOICES['default'])

        async def _generate():
            communicate = edge_tts.Communicate(text, edge_voice)
            await communicate.save(output_path)

        asyncio.run(_generate())
        print(f"    [OK] Edge TTS fallback: {output_path} ({edge_voice})")
        return True
    except Exception as e:
        print(f"    [ERR] Edge TTS failed: {e}")
        return False


# ==========================================
# Gemini 생성 함수 
# ==========================================
def _generate_with_gemini(
    text: str, 
    output_path: str, 
    speaker: str, 
    speed: int, 
    style_prompt: str, 
    model_name: str
) -> bool:
    """
    Google Cloud Text-to-Speech (Service Account) 방식을 사용하여 오디오 생성
    """
    # 1. 서비스 계정 인증
    try:
        current_dir = Path(__file__).parent
        key_path = current_dir / SERVICE_ACCOUNT_FILE
        
        if not key_path.exists():
            print(f"    [ERR] 키 파일을 찾을 수 없습니다: {key_path}")
            return False

        cred = service_account.Credentials.from_service_account_file(key_path)
        client = texttospeech.TextToSpeechClient(credentials=cred)

    except Exception as e:
        print(f"    [ERR] Google Client 인증 실패: {e}")
        return False

    # 재시도 로직 설정
    max_retries = 3
    base_wait_time = 2

    for attempt in range(max_retries):
        try:
            # 2. 화자 매핑
            voice_name = GEMINI_VOICE_MAP.get(speaker, "Puck")
            
            # -------------------------------------------------------------
            #  Flash 모델은 프롬프트 완전 제거 프롬프트를 그대로 읽는 오류가 발생
            # -------------------------------------------------------------
            
            final_input_text = text
            
            # 모델명에 'flash'가 들어있으면 프롬프트를 아예 무시합니다.
            if "flash" in model_name.lower():
                # Flash: 그냥 텍스트만 넘김 (가장 안전)
                final_input_text = text
            else:
                # Pro: 프롬프트가 있으면 적용
                if style_prompt:
                    # "Act out..." 같은 명령조 제거하고 자연스럽게 결합
                    safe_prompt = style_prompt.replace("Act out this line", "").replace("Speak", "").strip()
                    # Pro 모델용 포맷 (헤더 없이 자연스럽게 줄바꿈으로만 구분)
                    final_input_text = f"{safe_prompt}\n\n{text}"
                else:
                    final_input_text = text

            # 3. 입력 객체 생성
            synthesis_input = texttospeech.SynthesisInput(
                text=final_input_text
            )

            # 4. 목소리 및 모델 설정
            voice = texttospeech.VoiceSelectionParams(
                language_code="ko-KR",
                name=voice_name,
                model_name=model_name,
            )

            # 5. 오디오 설정 (속도 조절)
            gemini_rate = 1.0 - (speed * 0.1)
            gemini_rate = max(0.25, min(4.0, gemini_rate))

            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                speaking_rate=gemini_rate
            )

            # 6. 합성 요청
            response = client.synthesize_speech(
                input=synthesis_input,
                voice=voice,
                audio_config=audio_config,
            )

            # 7. 파일 저장
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as out:
                out.write(response.audio_content)
                
            return True

        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "ResourceExhausted" in error_msg or "Quota" in error_msg:
                wait_time = base_wait_time * (1** attempt)
                print(f"     [429 Quota] {wait_time}초 대기 후 재시도 ({attempt+1}/{max_retries})...")
                time.sleep(wait_time)
            else:
                print(f"     Gemini TTS Error ({model_name}): {e}")
                return False
    
    print(f"     재시도 횟수 초과로 실패")
    return False

# GPT 생성 함수
def _generate_with_gpt(text: str, output_path: str, speaker: str, speed: float, instructions: str) -> bool:
    if not HAS_OPENAI:
        print("    [ERR] OpenAI 라이브러리 미설치")
        return False
        
    # 화자 매핑
    voice_id = GPT_VOICE_MAP.get(speaker, GPT_VOICE_MAP["default"])
    
    # 속도 변환 (Clova -5~5 -> GPT 0.25~4.0)
    gpt_speed = 1.0 - (speed * 0.1)
    gpt_speed = max(0.5, min(2.0, gpt_speed))

    try:
        client = OpenAI()
        with client.audio.speech.with_streaming_response.create(
            model=OPENAI_TTS_MODEL,
            voice=voice_id,
            input=text,
            speed=gpt_speed,
            instructions=instructions # [핵심] 감정 프롬프트
        ) as response:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            response.stream_to_file(output_path)
        return True
    except Exception as e:
        print(f"     GPT API Error: {e}")
        return False

# Clova 생성 함수 (이름 변경 없음, 내부 로직만 분리 가능하나 그대로 유지)
def _generate_with_clova(text, output_path, speaker, speed, pitch, volume, emotion, emotion_strength):
    headers = {
        "X-NCP-APIGW-API-KEY-ID": CLOVA_CLIENT_ID,
        "X-NCP-APIGW-API-KEY": CLOVA_CLIENT_SECRET,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    payload = {
        "speaker": speaker, "text": text, "speed": str(speed),
        "pitch": str(pitch), "volume": str(volume), "format": "mp3"
    }
    if emotion and speaker in EMOTION_SUPPORTED:
        if emotion == "angry" and not EMOTION_SUPPORTED[speaker].get("anger_supported"): emotion = "neutral"
        payload["emotion"] = emotion
        if emotion_strength: payload["emotion-strength"] = str(min(max(emotion_strength, 0), 2))

    for attempt in range(3):
        try:
            resp = requests.post(CLOVA_ENDPOINT, headers=headers, data=payload, timeout=30)
            if resp.status_code == 200:
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, "wb") as f: f.write(resp.content)
                return True
            elif resp.status_code >= 500: time.sleep(1)
            else: break
        except: time.sleep(1)
    return False

def _text_to_speech_single(
    text: str,
    output_path: str,
    speaker: str = "narrator",
    speed: int = 0,
    pitch: int = 0,
    volume: int = 0,
    emotion: Optional[str] = None,
    emotion_strength: Optional[int] = None,
    use_edge_fallback: bool = True,
    use_cache: bool = True,
    engine: str = "clova",      #  엔진 선택
    style_prompt: str = ""      #  gpt, gemini 용 프롬프트
) -> bool:
    """
    단일 텍스트 청크를 TTS 변환 (내부 함수)

    Args:
        emotion: 감정 (neutral, happy, sad, angry) - 지원 화자만
        emotion_strength: 감정 강도 (0-2) - PRO 화자만
        use_cache: 캐시 사용 여부
    """
    # 1. 화자 ID 결정 (Clova일 때만 변환)
    clova_speaker_id = speaker
    if engine == "clova":
        if not speaker.startswith(('n', 'v', 'd', 'm', 'c', 's')):
            clova_speaker_id = tts_core.VOICE_ALIASES.get(speaker, "njiyun")

    # 2. 캐시 키 생성 (엔진과 프롬프트 포함)
    cache_key = hashlib.md5(
        f"{text}|{speaker}|{engine}|{speed}|{pitch}|{style_prompt}".encode()
    ).hexdigest()

    if use_cache:
        cached_path = tts_core._TTS_CACHE.get(cache_key)
        if cached_path and Path(cached_path).exists():
            shutil.copy(cached_path, output_path)
            print(f"    [CACHE HIT] {output_path}")
            return True

    # 3. 엔진별 호출 분기
    success = False
    engine_lower = engine.lower()
    if "gpt" in engine_lower:
        success = _generate_with_gpt(text, output_path, speaker, speed, style_prompt)
        
    elif "gemini" in engine_lower:
        # [핵심] gemini-pro / gemini-flash 구분 로직
        if "pro" in engine_lower:
            target_model = GEMINI_MODELS["pro"]
        elif "flash" in engine_lower:
            target_model = GEMINI_MODELS["flash"]
        else:
            target_model = GEMINI_MODELS["default"] # 그냥 "gemini"로 들어온 경우
            
        success = _generate_with_gemini(text, output_path, speaker, speed, style_prompt, model_name=target_model)
        
    else:
        # Clova (Default)
        success = _generate_with_clova(text, output_path, clova_speaker_id, speed, pitch, volume, emotion, emotion_strength)
        
        # Clova 실패 시 Edge TTS 폴백
        if not success and use_edge_fallback:
            print(f"    Edge TTS 폴백 시도...")
            success = edge_tts_fallback(text, output_path, get_edge_voice_type(speaker))

    # 4. 결과 캐싱
    if success and use_cache:
        tts_core._TTS_CACHE.set(cache_key, output_path)

    return success



def generate_audio_with_character_voices(
    subtitle: str,
    output_dir: Path,
    uid: str,
    scene_idx: int,
    known_characters: List[str] = None,
    voice_assignments: Dict[str, str] = None
) -> Path:
    """
    자막을 세그먼트로 분리하여 캐릭터별 다른 목소리로 TTS 생성

    Args:
        subtitle: 자막 텍스트
        output_dir: 출력 폴더
        uid: 고유 ID
        scene_idx: 장면 인덱스
        known_characters: 등장인물 목록
        voice_assignments: 캐릭터별 음성 매핑 딕셔너리 (고정용)

    Returns:
        생성된 오디오 파일 경로 (또는 None)
    """
    if not subtitle or not subtitle.strip():
        return None

    known_characters = known_characters or []
    voice_assignments = voice_assignments or {}

    # 자막을 세그먼트로 분리
    segments = tts_core.parse_dialogue_with_speaker(subtitle, known_characters)

    print(f"   장면 {scene_idx+1}: {len(segments)}개 세그먼트로 분리")

    def get_voice_with_assignments(speaker: str) -> str:
        """voice_assignments 우선, 없으면 자동 추론"""
        normalized = tts_core.normalize_character(speaker)
        # 1. voice_assignments에서 찾기
        if speaker in voice_assignments:
            return voice_assignments[speaker]
        if normalized in voice_assignments:
            return voice_assignments[normalized]
        # 2. 자동 추론
        return tts_core.get_voice_for_character(speaker)

    # 세그먼트가 1개면 단일 TTS
    if len(segments) == 1:
        seg = segments[0]
        voice_alias = get_voice_with_assignments(seg["speaker"])

        output_path = output_dir / f"tts_{scene_idx:02d}_{uid}.mp3"

        if text_to_speech(seg["text"], str(output_path), speaker=voice_alias):
            print(f"     {seg['speaker']} → {voice_alias}")
            return output_path
        return None

    # 여러 세그먼트 → 각각 TTS 후 합성
    temp_paths = []
    for i, seg in enumerate(segments):
        voice_alias = get_voice_with_assignments(seg["speaker"])

        temp_path = output_dir / f"tts_{scene_idx:02d}_{uid}_seg{i:02d}.mp3"

        if text_to_speech(seg["text"], str(temp_path), speaker=voice_alias):
            temp_paths.append(str(temp_path))
            print(f"    [{seg['type']}] {seg['speaker']} → {voice_alias}")
        else:
            print(f"    [{seg['type']}] {seg['speaker']} TTS 실패")

    if not temp_paths:
        return None

    # 하나의 파일로 합성
    output_path = output_dir / f"tts_{scene_idx:02d}_{uid}.mp3"

    if len(temp_paths) == 1:
        # 하나만 성공하면 그냥 이동
        shutil.move(temp_paths[0], str(output_path))
    else:
        # 여러 개 합성
        if tts_core.concat_audio_files(temp_paths, str(output_path)):
            # 임시 파일 정리
            for tp in temp_paths:
                try:
                    Path(tp).unlink()
                except OSError:
                    pass  # 파일 삭제 실패 무시
        else:
            # 합성 실패 시 첫 번째 파일만 사용
            shutil.move(temp_paths[0], str(output_path))

    return output_path if output_path.exists() else None


def generate_audio_for_subtitles_with_characters(
    subtitles: list,
    output_dir: Path,
    uid: str,
    known_characters: List[str] = None,
    voice_assignments: Dict[str, str] = None
) -> list:
    """
    자막 리스트를 캐릭터별 목소리로 음성 변환 (같은 캐릭터 = 같은 목소리 보장)

    Args:
        subtitles: 자막 텍스트 리스트
        output_dir: 출력 폴더
        uid: 고유 ID
        known_characters: 등장인물 목록
        voice_assignments: 캐릭터별 음성 고정 매핑 (세션 레벨)

    Returns:
        생성된 음성 파일 경로 리스트
    """
    audio_paths = []
    known_characters = known_characters or []
    voice_assignments = voice_assignments or {}

    # voice_assignments 로그 출력
    if voice_assignments:
        print(f"  🎤 고정된 음성 매핑: {len(voice_assignments)}개 캐릭터")

    for i, text in enumerate(subtitles):
        if not text or not text.strip():
            audio_paths.append(None)
            print(f"  🖼 장면 {i+1} 자막 없음 (스킵)")
            continue

        audio_path = generate_audio_with_character_voices(
            text, output_dir, uid, i, known_characters,
            voice_assignments=voice_assignments  # 고정된 매핑 전달!
        )

        if audio_path:
            audio_paths.append(audio_path)
            print(f"   장면 {i+1} 음성 생성 완료")
        else:
            audio_paths.append(None)
            print(f"   장면 {i+1} 음성 생성 실패")

    return audio_paths



# ==========================================
# 3. 메인 인터페이스 (Wrapper)
# ==========================================

# ============================================================
# 스피커 문자열 파싱 및 화자 배정 로직 개선
# ============================================================

def parse_speaker_info(speaker_str: str):
    """
    Step 3의 복합 스피커 문자열을 파싱합니다.
    입력 예: "char_01 (흥부) adult_male" 또는 "narrator"
    반환: (voice_type, character_name)
    """
    if not speaker_str:
        return "narrator", None
    
    # 1. 괄호가 있는 포맷: "ID (이름) 타입"
    # 예: char_01 (흥부) adult_male -> 이름: 흥부, 타입: adult_male
    match = re.search(r'\((.*?)\)\s*([a-zA-Z0-9_]+)', speaker_str)
    if match:
        char_name = match.group(1) # 흥부
        voice_type = match.group(2) # adult_male
        return voice_type, char_name
    
    # 2. 괄호는 없지만 공백으로 구분된 경우 (예비)
    parts = speaker_str.split()
    if len(parts) >= 2:
        # 마지막 부분이 보통 voice_type
        voice_type = parts[-1]
        char_name = "_".join(parts[:-1]) # 나머지를 이름으로 간주
        return voice_type, char_name

    # 3. 단일 문자열 (예: "narrator", "child_male")
    return speaker_str, None

def text_to_speech(
    text: str,
    output_path: str,
    speaker: str = "narrator",
    speed: int = 0,
    pitch: int = 0,
    volume: int = 0,
    use_edge_fallback: bool = True,
    character_name: str = None,
    session_id: str = None,
    engine: str = "clova",      #  엔진 선택 (기본값 clova)
    style_prompt: str = ""      # gpt, gemini 용 프롬프트
) -> bool:
    """
    텍스트를 클로바 TTS로 음성 변환 (Edge TTS 폴백 지원)

    Args:
        text: 변환할 텍스트 (2000자 초과 시 자동 분할)
        output_path: 저장할 파일 경로 (.mp3)
        speaker: 화자 키 (narrator, child_male 등) 또는 Clova ID
        speed: 속도 (-5 ~ 5, 기본 0)
        pitch: 피치 (-5 ~ 5, 기본 0)
        volume: 볼륨 (-5 ~ 5, 기본 0)
        use_edge_fallback: Clova 실패 시 Edge TTS 사용 여부
        character_name: 캐릭터 이름 (세션 내 음성 일관성 유지용)
        session_id: 세션 ID (책/프로젝트 단위)

    Returns:
        성공 여부 (True/False)
    """
    if not text or not text.strip():
        return False

    # 1. 텍스트 정규화 (따옴표, 공백 정리)
    text = tts_core.normalize_text(text)
    if not text:
        return False

    # Clova일 때만 SessionManager 사용
    if engine == "clova":
        if speaker in tts_core.VOICE_POOLS or speaker in tts_core.VOICE_ALIASES:
            session_mgr = tts_core.get_session_voice_manager(session_id)
            speaker = session_mgr.get_clova_voice_id(speaker, character_name)

    # 엔진별 제한에 맞춰 텍스트 분할
    limit = LIMITS.get(engine, 2000)
    text_chunks = tts_core.split_text_safely(text, limit=limit)

    if len(text_chunks) == 1:
        return _text_to_speech_single(
            text_chunks[0], output_path, speaker,
            speed, pitch, volume, use_edge_fallback=use_edge_fallback,
            engine=engine, style_prompt=style_prompt
        )

    # 다중 청크 처리
    temp_files = []
    output_dir = Path(output_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)

    for i, chunk in enumerate(text_chunks):
        temp_path = str(output_dir / f"_chunk_{i:02d}.mp3")
        if _text_to_speech_single(
            chunk, temp_path, speaker, speed, pitch, volume,
            use_edge_fallback=use_edge_fallback,
            engine=engine, style_prompt=style_prompt
        ):
            temp_files.append(temp_path)
        else:
            print(f"   청크 {i+1} 실패")

    if temp_files:
        try:
            tts_core.concat_audio_files(temp_files, output_path)
            for tf in temp_files: Path(tf).unlink(missing_ok=True)
            return True
        except: return False

    return False


# ==========================================
# ==========================================



def generate_audio_for_subtitles(
    subtitles: list,
    output_dir: Path,
    uid: str,
    speaker: str = "narrator",
    speakers: List[str] = None,
    parallel: bool = True,
    max_workers: int = 5,
    split_narration: bool = True,
    engine: str = "clova",
    global_speed: int = 0,
    style_prompts: List[str] = None
) -> list:
    """
    자막 리스트를 음성 파일들로 변환 (병렬 처리 지원)

    Args:
        subtitles: 자막 텍스트 리스트
        output_dir: 출력 폴더
        uid: 고유 ID
        speaker: 단일 화자 키 (모든 자막에 동일 적용)
        speakers: 자막별 화자 리스트 (GPT가 배정한 화자) - speaker보다 우선
        parallel: 병렬 처리 여부 (기본 True)
        max_workers: 최대 동시 작업 수 (API rate limit 고려)
        split_narration: 나래이션/대사 분리 여부 (기본 True)
            - True: 나래이션은 narrator, 대사만 GPT 화자 적용
            - False: 기존 동작 (전체에 GPT 화자 적용)

    Returns:
        생성된 음성 파일 경로 리스트
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    audio_paths = [None] * len(subtitles)
    # Gemini 엔진 감지 및 스로틀링(Throttling) 설정
    is_gemini = "gemini" in engine.lower()
    
    # 1. Gemini 엔진 여부 확인
    is_gemini = "gemini" in engine.lower()

    # 2. 작업자(Worker) 수 및 병렬 설정 조정
    if is_gemini:
        # [Gemini] 429 에러 방지를 위해 동시 실행 수를 3개로 제한
        # (기존에 5개였으면 3개로 줄임, parallel은 유지하되 속도 조절)
        run_max_workers = 3 
        print(f"ℹ️ [Gemini] 안정성을 위해 병렬 작업 수를 {run_max_workers}개로 제한합니다.")
    else:
        # [Clova/GPT] 기존 설정 그대로 (보통 5개) -> 딜레이 없이 빠르게 처리
        run_max_workers = max_workers

    # speakers 리스트가 있으면 사용, 없으면 단일 speaker로 채움
    if speakers is None:
        speakers = [speaker] * len(subtitles)
    elif len(speakers) < len(subtitles):
        # 부족하면 기본값으로 채움
        speakers = speakers + [speaker] * (len(subtitles) - len(speakers))

    #  전체 스피커 리스트에서 '등장인물 이름'을 미리 추출하여 known_characters 구축
    # 이를 통해 나레이션 속 대화문의 주인을 더 잘 찾게 만듭니다.
    collected_characters = set()
    for s in speakers:
        _, name = parse_speaker_info(s)
        if name:
            collected_characters.add(name)
    known_char_list = list(collected_characters)

    # GPT speaker type → VOICE_ALIASES key 매핑 (확장)
    SPEAKER_TYPE_MAP = {
        "narrator": "narrator",
        "child": "child_male",  # 기본값 (GPT가 성별 미지정 시)
        "child_male": "child_male",
        "child_female": "child_female",
        "elder_female": "elder_female",
        "elder_male": "elder_male",
        "adult_female": "adult_female",
        "adult_male": "adult_male",
        "young_female": "young_female",
        "young_male": "young_male",
        "animal": "cute_animal",
        "fairy": "fairy",
        "demon": "elder_female",
        "none": None,  # 음성 생성 안 함
    }
    # style_prompts가 없으면 빈 리스트로 초기화 (에러 방지)
    if style_prompts is None:
        style_prompts = [""] * len(subtitles)
        

    def process_subtitle_with_split(i: int, text: str, raw_spk_str: str, specific_prompt: str):
        """
        나래이션/대사 분리 + 개선된 화자 매핑 적용
        (GPT/Gemini 사용 시 Edge TTS로 빠지는 것 방지)
        """
        # Gemini일 경우 요청 시작 전 잠시 대기 (Rate Limiting)
        if is_gemini:
            time.sleep(0.2)  
        if not text or not text.strip():
            return i, None, "skip", raw_spk_str

        # =========================================================================
        # [1] 화자 정보 파싱 로직
        # =========================================================================
        
        final_voice_key = "narrator"  # 기본값 초기화
        main_char_name = None
        found_valid_type = False

        # 1. 괄호 안에 있는 이름 추출 (예: '마녀 할머니')
        name_match = re.search(r'\((.*?)\)', raw_spk_str)
        if name_match:
            main_char_name = name_match.group(1).strip()
        
        # 2. 보이스 타입 추출 (뒤에서부터 검색)
        parts = raw_spk_str.split()
        for part in reversed(parts):
            clean_part = part.strip("(),")
            if clean_part in SPEAKER_TYPE_MAP:
                final_voice_key = SPEAKER_TYPE_MAP[clean_part]
                found_valid_type = True
                break
        
        # 3. 1차 시도 실패 시 기존 파싱 함수 시도
        if not found_valid_type:
            p_type, p_name = parse_speaker_info(raw_spk_str)
            if p_type in SPEAKER_TYPE_MAP:
                final_voice_key = SPEAKER_TYPE_MAP[p_type]
                found_valid_type = True
            
            if not main_char_name and p_name:
                main_char_name = p_name

        # 4. 이름 강제 설정 (Priority 1 발동용)
        if not main_char_name and found_valid_type and final_voice_key != "narrator":
            main_char_name = raw_spk_str

        # =========================================================================
        # [2]  엔진별 Strict Mode 적용 
        # =========================================================================
        
        # 현재 엔진이 프리미엄 엔진(GPT, Gemini)인지 확인
        is_premium_engine = any(k in engine.lower() for k in ["gpt", "gemini"])

        if is_premium_engine:
            # GPT나 Gemini인데 유효한 화자 타입(child_male 등)을 못 찾았다면?
            # 절대 Edge TTS로 넘기지 말고, 무조건 'narrator'(기본 GPT 목소리)로 고정!
            if not found_valid_type:
                final_voice_key = "narrator"
        
        # (참고: Clova는 위 조건에 걸리지 않으므로, 매핑 실패 시 기존처럼 넘어가서
        #  내부 로직에 따라 Edge TTS나 다른 대안을 사용할 수 있게 둠)

        # 5. 최종 키 할당
        scene_voice_key = final_voice_key

        if scene_voice_key is None:
            return i, None, "skip", raw_spk_str

        # =========================================================================
        # 외부에서 넘어온 specific_prompt가 있으면 그걸 최우선으로 사용
        if specific_prompt:
            style_prompt = specific_prompt
        else:
            # 스타일 프롬프트 설정
            style_prompt = ""
            # [신규] Gemini용 프롬프트 로직 (필요시 확장 가능)
            if "gemini" in engine.lower():
                if "child" in scene_voice_key: style_prompt = "Speak like a young child."
                elif "anger" in scene_voice_key: style_prompt = "Speak with an angry tone."
            # GPT용 프롬프트
            elif "gpt" in engine.lower():
                if "child" in scene_voice_key: style_prompt = "Speak like a cute child."
                elif "anger" in scene_voice_key: style_prompt = "Speak angrily."
            
        # 텍스트 분리
        segments = tts_core.parse_dialogue_with_speaker(text, known_characters=known_char_list)

        # ----------------------------------------------------------------
        # [Helper] 세그먼트별 화자 결정 로직
        # ----------------------------------------------------------------
        def resolve_voice(segment):
            seg_type = segment["type"]
            inferred_speaker = segment.get("speaker") 

            # A. 나레이션
            if seg_type == "narration":
                if len(segments) == 1 and scene_voice_key != "narrator":
                    return scene_voice_key, main_char_name
                return "narrator", None

            # B. 대화문
            # (Priority 1) 사용자 UI 설정 (이름이 있으면 무조건 이걸로)
            if main_char_name and scene_voice_key:
                return scene_voice_key, main_char_name
            
            # (Priority 2) 문맥 추론
            if inferred_speaker and inferred_speaker != "narrator":
                inferred_voice_type = tts_core.get_voice_for_character(inferred_speaker)
                # ⭐️ 중요: 프리미엄 엔진일 때 추론 결과가 엉뚱하면 narrator로 방어
                if is_premium_engine and inferred_voice_type not in SPEAKER_TYPE_MAP.values():
                    return "narrator", inferred_speaker
                
                return inferred_voice_type, inferred_speaker
            
            # (Priority 3) 기본값 (이미 위에서 방어됨)
            return scene_voice_key, None
        # ----------------------------------------------------------------

        # (A) 단일 세그먼트 처리
        if len(segments) == 1:
            seg = segments[0]
            voice_key, char_name = resolve_voice(seg)
            
            audio_path = output_dir / f"tts_{i:02d}_{uid}.mp3"
            success = text_to_speech(
                seg["text"], str(audio_path), speaker=voice_key, speed=global_speed,
                character_name=char_name, session_id=uid, engine=engine, style_prompt=style_prompt
            )
            return i, audio_path if success else None, "ok" if success else "fail", f"{raw_spk_str}"

        # (B) 다중 세그먼트 처리
        temp_paths = []
        segment_info = []

        for seg_idx, seg in enumerate(segments):
            #  세그먼트 사이에도 딜레이 필요 (한 문장이 3개로 쪼개질 때 폭주 방지)
            if is_gemini and seg_idx > 0:
                time.sleep(0.1)
            voice_key, char_name = resolve_voice(seg)
            temp_path = output_dir / f"tts_{i:02d}_{uid}_seg{seg_idx:02d}.mp3"

            if text_to_speech(
                seg["text"], str(temp_path), speaker=voice_key, speed=global_speed,
                character_name=char_name, session_id=uid, engine=engine, style_prompt=style_prompt
            ):
                temp_paths.append(str(temp_path))
                segment_info.append(f"{seg['type']}")
            else:
                print(f"    ⚠️ [Segment Fail] Scene {i}-{seg_idx}: {seg['text'][:10]}...")

        if not temp_paths:
            return i, None, "fail", raw_spk_str

        audio_path = output_dir / f"tts_{i:02d}_{uid}.mp3"

        if len(temp_paths) == 1:
            shutil.move(temp_paths[0], str(audio_path))
        else:
            if tts_core.concat_audio_files(temp_paths, str(audio_path)):
                for tp in temp_paths:
                    try: Path(tp).unlink()
                    except: pass
            else:
                shutil.move(temp_paths[0], str(audio_path))

        return i, audio_path if audio_path.exists() else None, "ok", "+".join(segment_info)

    # 분리 모드 선택
    process_func = process_subtitle_with_split

    if parallel and len(subtitles) > 1:
        # 병렬 처리 (5-10배 속도 향상)
        with ThreadPoolExecutor(max_workers=run_max_workers) as executor:
            futures = {
                executor.submit(process_subtitle_with_split, i, subtitles[i], speakers[i], style_prompts[i]): i
                for i in range(len(subtitles))
            }

            for future in as_completed(futures):
                idx, result, status, spk_info = future.result()
                audio_paths[idx] = result
                if status == "skip":
                    print(f"  [SKIP] Scene {idx+1} - no subtitle")
                elif status == "ok":
                    print(f"  [OK] Scene {idx+1} [{spk_info}] audio generated")
                else:
                    print(f"  [FAIL] Scene {idx+1} [{spk_info}] audio failed")
    else:
        # 순차 처리 (단일 자막 또는 parallel=False)
        for i, text in enumerate(subtitles):
            idx, result, status, spk_info = process_subtitle_with_split(i, subtitles[i], speakers[i], style_prompts[i])
            audio_paths[idx] = result
            if status == "skip":
                print(f"  [SKIP] Scene {idx+1} - no subtitle")
            elif status == "ok":
                print(f"  [OK] Scene {idx+1} [{spk_info}] audio generated")
            else:
                print(f"  [FAIL] Scene {idx+1} [{spk_info}] audio failed")

    return audio_paths

