# -*- coding: utf-8 -*-
# tts_core.py
# TTS 공통 로직 및 유틸리티 (캐릭터 분석, 텍스트 처리, 파일 조작)

import re
import shutil
import hashlib
from pathlib import Path
from typing import List, Dict, Optional
from collections import OrderedDict


# ==========================================
# 1. 상수 및 정규식 데이터
# ==========================================

# 캐릭터로 오인될 수 있는 단어 (제외 목록) - 오탐 방지
EXCLUDE_FROM_SPEAKER = {
    # 감정 표현 (명사형)
    "화", "화가", "슬픔", "기쁨", "분노", "두려움", "놀람",
    "행복", "불안", "공포", "흥분", "절망", "희망",
    # 부사/수식어
    "갑자기", "조용히", "빠르게", "천천히", "크게", "작게",
    "조금", "아주", "매우", "너무", "정말", "진짜",
    # 신체 부위
    "손", "발", "눈", "귀", "입", "코", "머리", "얼굴",
    # 일반 명사
    "소리", "말", "목소리", "이야기", "대답", "질문",
    # 시간/장소
    "오늘", "내일", "어제", "여기", "저기", "거기",
}

# ============================================================
# 캐릭터 이름 정규화 (같은 캐릭터 = 같은 목소리)
# ============================================================
CHARACTER_ALIASES = {
    # 가족 호칭 동의어
    "어머니": "엄마", "어뮈": "엄마", "마마": "엄마", "모친": "엄마",
    "엄만": "엄마",
    "아버지": "아빠", "아부지": "아빠", "부친": "아빠",
    "아빤": "아빠",
    "할머님": "할머니", "외할머니": "할머니", "친할머니": "할머니",
    "할아버님": "할아버지", "외할아버지": "할아버지", "친할아버지": "할아버지",
    "오라버니": "오빠", "형아": "형",
    "누님": "누나", "언냐": "언니",
    # 높임/낮춤
    "아이": "아이", "아": "아이", "아가": "아기", "애기": "아기",
    "임금": "왕", "임금님": "왕", "폐하": "왕",
    "왕비": "여왕", "왕비님": "여왕",
    # 동물 캐릭터
    "토끼님": "토끼", "토끼야": "토끼",
    "곰님": "곰", "곰아": "곰",
    "여우님": "여우", "여우야": "여우",
}

# 한국어 조사 패턴 (제거 대상)
KOREAN_PARTICLES = (
    r'('
    r'이|가|께서|에서|'  # 주격
    r'을|를|'  # 목적격
    r'은|는|'  # 보조사
    r'의|'  # 관형격
    r'에게|한테|더러|에게서|한테서|로부터|'  # 여격
    r'로|으로|'  # 도구
    r'와|과|하고|랑|이랑|'  # 공동
    r'에서|부터|'  # 출처
    r'도|만|까지|마저|조차|밖에|'  # 보조사
    r'야|아|여|이여|시여|님|씨'  # 호격
    r')$'
)

# ============================================================
# 화자 매핑 딕셔너리 (공식 API 확인된 음성만 사용)
# ============================================================
VOICE_ALIASES = {
    # 기본/아동/청년/성인
    "narrator": "njiyun",
    "child_male": "nhajun",
    "child_female": "ndain",
    "young_male": "neunwoo",          # 은우 (젊은 남성)
    "young_female": "nara",
    "adult_male": "nminsang",
    "adult_female": "nyejin",
    "elder_male": "njonghyun",        # 종현 (깊은 남성 목소리)
    "elder_female": "nsunhee",        # 선희 (차분한 여성)

    # 세부 프리셋
    "young_male_1": "neunwoo",        # 은우
    "young_male_2": "njihun",         # 지훈
    "young_male_3": "nian",           # 이안
    "young_male_energetic": "njooahn",

    "young_female_1": "nara",
    "young_female_2": "nara_call",
    "young_female_3": "nyejin",
    "young_female_4": "nsujin",

    "adult_male_1": "nminsang",
    "adult_male_2": "njoonyoung",     # 준영
    "adult_male_3": "ndonghyun",      # 동현
    "adult_male_deep": "nwontak",

    "adult_female_1": "nyejin",
    "adult_female_2": "nminjeong",    # 민정
    "adult_female_3": "nsujin",
    "adult_female_warm": "nyoungmi",  # 영미

    "narrator_male_1": "njoonyoung",
    "narrator_male_2": "njonghyun",
    "narrator_male_deep": "njonghyun",

    "narrator_female_1": "njiyun",
    "narrator_female_2": "nara",
    "narrator_female_calm": "nyejin",

    "cute_animal": "nmeow",
    "dog": "nwoof",
    "robot": "nwontak",
    "fairy": "nsinu",
    "child_bright": "ngaram",         # 가람 (아동여, 밝은 톤)
    "demon": "nmammon",               # 악마 마몬

    # PRO 성우군
    "pro_female_1": "vara",
    "pro_female_2": "vmikyung",
    "pro_female_3": "vdain",
    "pro_female_4": "vyuna",
    "pro_female_5": "vgoeun",
    "pro_male_1": "vdaeseong",

    # 기본 호환
    "energetic": "njooahn",
    "elder": "njonghyun",
    "default": "njiyun",

    # 어르신 = 노인 (동의어 매핑)
    "어르신": "njonghyun",
    "어르신_남": "njonghyun",
    "어르신_여": "nsunhee",
    "노인": "njonghyun",
    "노인_남": "njonghyun",
    "노인_여": "nsunhee",
    "할아버지": "njonghyun",
    "할머니": "nsunhee",
}

# ============================================================
# 음성 풀 (1:N 매핑) - 같은 타입에 여러 음성 후보
# 공식 API 문서 기반 (2025.01 확인)
# ============================================================
VOICE_POOLS = {
    # 아동 (공식: 아동 카테고리)
    "child_male": ["nhajun", "nwoof", "njaewook"],  # 하준, 멍멍이, 재욱 (어린이/청소년)
    "child_female": ["ndain", "ngaram", "nmeow", "nminseo", "nihyun", "njiwon"],  # 다인, 가람, 야옹이, 민서, 이현, 지원 (어린이/청소년)
    "child_bright": ["ndain", "ngaram"],

    # 청년/젊은 남성
    "young_male": ["neunwoo", "njihun", "nian", "njooahn", "nkyuwon", "nraewon"],
    # 청년/젊은 여성
    "young_female": ["nara", "nara_call", "nsujin", "nyuna", "nyujin", "ntiffany"],

    # 성인 남성
    "adult_male": ["nminsang", "njoonyoung", "ndonghyun", "nseonghoon", "nseungpyo"],
    "adult_male_deep": ["nwontak", "njonghyun", "nyoungil"],  # 낮고 깊은 목소리

    # 성인 여성
    "adult_female": ["nyejin", "njiyun", "nminjeong", "nyounghwa", "nyoungmi", "ngoeun"],

    # 노인/어르신 (공식 노인 카테고리 없음 - 성인 중 낮은 톤 사용)
    "elder_male": ["njonghyun", "nyoungil", "nwontak"],       # 깊은 남성 목소리
    "elder_female": ["nsunhee"],     # 선희 (할머니 고정)
    # 동의어 매핑
    "어르신": ["njonghyun", "nsunhee"],
    "노인": ["njonghyun", "nsunhee"],
    "할아버지": ["njonghyun", "nyoungil", "nwontak"],
    "할머니": ["nsunhee"],  # 선희 고정

    # 나레이터
    "narrator": ["njiyun", "njoonyoung", "nara"],
    "narrator_male": ["njoonyoung", "njonghyun", "nsinu"],
    "narrator_female": ["njiyun", "nara", "nyejin"],

    # 특수/캐릭터
    "cute_animal": ["nmeow", "ndain", "ngaram"],  # 야옹이, 다인, 가람
    "dog": ["nwoof"],                              # 멍멍이
    "demon": ["nmammon"],                          # 악마 마몬
    "witch": ["nsabina"],                          # 마녀 사비나 (미확인)
    "robot": ["nwontak"],
    "fairy": ["nsinu", "nara", "napple"],          # 신우, 아라, 늘봄
}


# 키워드 기반 캐릭터 → 음성 매핑 (체계적 분류)
KEYWORD_TO_VOICE = {
    # ========== 아이/어린이 ==========
    "아기": "child_female",
    "아이": "child_male",
    "꼬마": "child_male",
    "어린": "child_female",
    "소년": "young_male",
    "소녀": "young_female",

    # ========== 가족 관계 ==========
    "엄마": "adult_female",
    "아빠": "adult_male",
    "할머니": "elder_female",
    "할아버지": "elder_male",
    "오빠": "young_male",
    "형": "young_male",
    "언니": "young_female",
    "누나": "young_female",
    "동생": "child_male",
    "삼촌": "adult_male",
    "이모": "adult_female",
    "고모": "adult_female",

    # ========== 연령/사회적 역할 ==========
    "청년": "young_male",
    "아가씨": "young_female",
    "노인": "elder_male",
    "현자": "elder_male",
    "장로": "elder_male",
    "어르신": "elder_male",

    # ========== 직업 - 교육/의료 ==========
    "선생님": "adult_female",
    "교수": "adult_male",
    "의사": "adult_male",
    "간호사": "adult_female",
    "약사": "adult_female",

    # ========== 직업 - 공무원/법조 ==========
    "경찰": "adult_male",
    "판사": "adult_male_deep",
    "변호사": "adult_male",
    "검사": "adult_male",
    "군인": "adult_male",
    "소방관": "adult_male",
    "공무원": "adult_male",

    # ========== 직업 - 서비스/상업 ==========
    "사장님": "adult_male_deep",
    "기사님": "adult_male",
    "아저씨": "adult_male",
    "아줌마": "adult_female",
    "요리사": "adult_male",
    "상인": "adult_male",
    "점원": "young_female",

    # ========== 직업 - 종교 ==========
    "신부": "adult_male",
    "목사": "adult_male",
    "수녀": "adult_female",
    "스님": "elder_male",

    # ========== 직업 - 기타 ==========
    "농부": "adult_male",
    "어부": "adult_male",
    "사냥꾼": "adult_male",
    "대장장이": "adult_male_deep",
    "광대": "young_male",

    # ========== 왕족/귀족 ==========
    "왕": "adult_male_deep",
    "여왕": "adult_female",
    "왕비": "adult_female",
    "공주": "young_female_1",
    "왕자": "young_male_1",
    "황제": "adult_male_deep",
    "황후": "adult_female",
    "장군": "adult_male_deep",
    "기사": "young_male",
    "영주": "adult_male_deep",
    "귀족": "adult_male",
    "시녀": "young_female",

    # ========== 동물 - 포유류 (귀여운) ==========
    "토끼": "cute_animal",
    "고양이": "cute_animal",
    "강아지": "dog",
    "다람쥐": "cute_animal",
    "햄스터": "cute_animal",
    "양": "cute_animal",
    "팬더": "cute_animal",

    # ========== 동물 - 포유류 (큰/위협적) ==========
    "곰": "adult_male_deep",
    "여우": "young_female_3",
    "늑대": "adult_male_deep",
    "사자": "adult_male_deep",
    "호랑이": "adult_male_deep",
    "코끼리": "adult_male_deep",
    "하마": "adult_male_deep",
    "소": "adult_male_deep",

    # ========== 동물 - 포유류 (기타) ==========
    "원숭이": "child_male",
    "쥐": "child_male",
    "사슴": "young_female",
    "돼지": "adult_male",
    "말": "young_male",
    "염소": "adult_male",
    "당나귀": "adult_male",
    "기린": "young_male",

    # ========== 동물 - 조류 ==========
    "새": "child_female",
    "참새": "child_female",
    "비둘기": "child_male",
    "독수리": "adult_male_deep",
    "까마귀": "adult_male",
    "까치": "child_female",
    "부엉이": "elder_male",
    "올빼미": "elder_male",
    "앵무새": "child_female",
    "오리": "child_male",
    "백조": "young_female",
    "학": "elder_male",
    "닭": "adult_female",
    "수탉": "adult_male",

    # ========== 동물 - 파충류/양서류 ==========
    "뱀": "adult_male",
    "용": "adult_male_deep",
    "드래곤": "adult_male_deep",
    "거북이": "elder_male",
    "악어": "adult_male_deep",
    "도마뱀": "child_male",
    "개구리": "child_male",
    "두꺼비": "adult_male",

    # ========== 동물 - 곤충/해양 ==========
    "꿀벌": "child_female",
    "나비": "child_female",
    "개미": "child_male",
    "물고기": "child_male",
    "상어": "adult_male_deep",
    "고래": "adult_male_deep",
    "돌고래": "young_female",
    "문어": "adult_male",
    "게": "adult_male",

    # ========== 판타지/신화 ==========
    "요정": "fairy",
    "마녀": "elder_female",
    "마법사": "elder_male",
    "로봇": "robot",
    "천사": "young_female",
    "악마": "adult_male_deep",
    "유령": "adult_female",
    "귀신": "adult_female",
    "괴물": "adult_male_deep",
    "거인": "adult_male_deep",
    "난쟁이": "child_male",
    "요괴": "adult_male",
    "도깨비": "adult_male",
    "신": "adult_male_deep",
    "여신": "adult_female",
    "정령": "fairy",
    "인어": "young_female",
    "유니콘": "young_female",
    "피닉스": "adult_male_deep",
    "트롤": "adult_male_deep",
    "고블린": "child_male",
    "엘프": "young_female",
    "오크": "adult_male_deep",
    "해골": "adult_male",
    "좀비": "adult_male",
    "뱀파이어": "adult_male",
    "늑대인간": "adult_male_deep",

    # ========== 대명사 ==========
    "그": "adult_male",
    "그녀": "adult_female",
    "그들": "adult_male",
    "누군가": "adult_male",
    "아무도": "adult_male",
}

# 키워드 longest-first 정렬 (긴 키워드 우선 매칭 - "할아버지" > "할")
_KEYWORD_TO_VOICE_SORTED = sorted(
    KEYWORD_TO_VOICE.items(),
    key=lambda x: len(x[0]),
    reverse=True
)

# 대화 태그 패턴 (일반화: 이름+조사+동사+따옴표 구조)
# 따옴표 앞에 "이름+조사+동사" 패턴이 있으면 화자 추출
DIALOGUE_TAG_PATTERN = re.compile(
    r'([가-힣]{1,10})(?:이|가|은|는|께서|도)\s*'
    r'(?:[가-힣]{1,8}(?:을|를|에게|한테)?\s*)?'  # 선택적 목적어 (주문을, 말을 등)
    r'[가-힣]{1,12}(?:었|았|였|웠|했|하셨|셨|렸|으며|며|면서)?'
    r'(?:다|요|죠)?[.!?,]?\s*'
    r'(?=["\'\'""])',  # 따옴표가 뒤따라야 매칭 (lookahead)
    re.UNICODE
)

# 대사 뒤에 오는 화자 패턴 ("안녕!" 소녀가 인사했다.)
# 따옴표 lookahead 없이 검색
SPEAKER_AFTER_DIALOGUE_PATTERN = re.compile(
    r'([가-힣]{1,10})(?:이|가|은|는|께서|도)\s*'
    r'(?:[가-힣]{1,8}(?:을|를|에게|한테)?\s*)?'
    r'[가-힣]{1,12}(?:었|았|였|웠|했|하셨|셨|렸|으며|며|면서)?'
    r'(?:다|요|죠)?',
    re.UNICODE
)

# 호칭 패턴 (대사 내 부름)
VOCATIVE_PATTERN = re.compile(
    r'^["\']?\s*([가-힣]+)(?:야|아|님|씨)?[,!]',
    re.UNICODE
)


# ==========================================
# 2. 유틸리티 클래스 (Cache, Manager)
# ==========================================


class TTSCache:
    """LRU 캐시 - 최대 크기 제한으로 메모리 누수 방지"""
    def __init__(self, max_size: int = 100):
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._max_size = max_size

    def get(self, key: str) -> Optional[str]:
        if key in self._cache:
            self._cache.move_to_end(key)  # LRU 갱신
            return self._cache[key]
        return None

    def set(self, key: str, value: str):
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        # 크기 제한 초과 시 가장 오래된 항목 제거
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def __contains__(self, key: str) -> bool:
        return key in self._cache

_TTS_CACHE = TTSCache(max_size=100)  # 최대 100개 (약 5MB)


class SessionVoiceManager:
    """
    세션(책) 단위 캐릭터별 음성 일관성 관리

    - 캐릭터가 처음 등장할 때 VOICE_POOLS에서 랜덤 선택
    - 이후 같은 캐릭터는 같은 음성 유지
    - deterministic: hash 기반으로 동일 입력 = 동일 결과
    """

    def __init__(self, session_id: str = None):
        self.session_id = session_id or "default"
        self._character_to_voice: Dict[str, str] = {}  # 캐릭터명 -> Clova ID
        self._type_to_voice: Dict[str, str] = {}       # voice_type -> Clova ID
        self._used_voices: set = set()

    def get_clova_voice_id(self, voice_type: str, character_name: str = None) -> str:
        """
        voice_type에 대한 실제 Clova 음성 ID 반환

        Args:
            voice_type: "child_male", "adult_female" 등
            character_name: 캐릭터 이름 (같은 캐릭터 = 같은 음성)

        Returns:
            Clova 음성 ID (예: "nhajun", "nyejin")
        """
        # 1. 캐릭터명으로 이미 배정된 음성이 있으면 반환
        if character_name and character_name in self._character_to_voice:
            return self._character_to_voice[character_name]

        # 2. voice_type으로 이미 선택된 음성이 있으면 반환 (캐릭터명 없는 경우)
        if not character_name and voice_type in self._type_to_voice:
            return self._type_to_voice[voice_type]

        # 3. 새로 선택
        pool = VOICE_POOLS.get(voice_type, [])

        if not pool:
            # 풀이 없으면 VOICE_ALIASES에서 직접 가져오기
            return VOICE_ALIASES.get(voice_type, VOICE_ALIASES.get("default", "njiyun"))

        # =========================================================
        # get_best_voice의 중복 방지 로직
        # =========================================================
        selected = None
        
        # A. 시도 1: 해시 기반으로 선택하되, 사용되지 않은 목소리인지 확인
        seed_str = f"{self.session_id}_{character_name or voice_type}"
        hash_val = hash(seed_str)
        
        # pool 순서를 섞어서(offset) 탐색 (해시값 기준 시작점)
        start_idx = hash_val % len(pool)
        
        # pool을 순회하며 '아직 안 쓴 목소리' 찾기
        for i in range(len(pool)):
            idx = (start_idx + i) % len(pool)
            candidate = pool[idx]
            
            if candidate not in self._used_voices:
                selected = candidate
                break
        
        # B. 시도 2: 만약 pool에 있는 모든 목소리가 이미 다 쓰였다면?
        # 어쩔 수 없이 해시 기준으로 중복 허용 (그냥 원래대로 선택)
        if selected is None:
            selected = pool[start_idx]

        # =========================================================

        # 4. 배정 결과 저장 (캐싱)
        if character_name:
            self._character_to_voice[character_name] = selected
            # 로그 출력 (디버깅용)
            print(f"  🎤 [Voice Assign] {character_name} ({voice_type}) -> {selected}")
        else:
            self._type_to_voice[voice_type] = selected

        self._used_voices.add(selected)
        return selected

    def reset(self):
        """세션 초기화 (새 책 시작 시)"""
        self._character_to_voice.clear()
        self._type_to_voice.clear()
        self._used_voices.clear()

    # =========================================================
    # [추가된 부분] 방법 A 적용: Set을 List로 변환하여 반환
    # =========================================================
    def get_state_dict(self) -> Dict:
        """
        JSON 직렬화를 위해 내부 상태를 내보내는 함수
        set 타입인 _used_voices를 list로 변환해서 반환합니다.
        """
        return {
            "session_id": self.session_id,
            "character_to_voice": self._character_to_voice,
            "type_to_voice": self._type_to_voice,
            "used_voices": list(self._used_voices)  # <--- 핵심: set을 list로 변환!
        }

    def get_assignments(self) -> Dict[str, str]:
        """현재 캐릭터-음성 배정 현황 반환"""
        return dict(self._character_to_voice)
    


# 전역 세션 매니저 (기본값)
_SESSION_VOICE_MANAGER: SessionVoiceManager = None


def get_session_voice_manager(session_id: str = None) -> SessionVoiceManager:
    """세션 음성 매니저 가져오기/생성"""
    global _SESSION_VOICE_MANAGER
    if _SESSION_VOICE_MANAGER is None or (session_id and _SESSION_VOICE_MANAGER.session_id != session_id):
        _SESSION_VOICE_MANAGER = SessionVoiceManager(session_id)
    return _SESSION_VOICE_MANAGER


# ==========================================
# 3. 텍스트 및 오디오 처리 함수
# ==========================================

# ============================================================
# 캐릭터 정규화 및 화자 추론 함수들
# ============================================================

def normalize_character(name: str) -> str:
    """캐릭터 이름 정규화 - 조사 제거 + 동의어 통일"""
    if not name:
        return name

    clean = name.strip()
    clean = re.sub(KOREAN_PARTICLES, '', clean)
    clean = CHARACTER_ALIASES.get(clean, clean)

    return clean


def normalize_text(text: str) -> str:
    """텍스트 정규화 - 따옴표/공백 정리 (원본: improved_clova_dubbing.py)"""
    if not text:
        return text
    return (
        text.replace(""", '"').replace(""", '"')
            .replace("'", "'").replace("'", "'")
            .replace("\u00A0", " ")
            .strip()
    )


def split_text_safely(text: str, limit: int = 2000) -> List[str]:
    """
    API 한도(2,000자) 내로 텍스트 안전 분할 (원본: improved_clova_dubbing.py)

    Args:
        text: 분할할 텍스트
        limit: 최대 글자 수 (기본 2000)

    Returns:
        분할된 텍스트 리스트
    """
    text = normalize_text(text)
    if len(text) <= limit:
        return [text]

    parts, buf = [], []
    size = 0

    # 문장 경계 기준 분할
    tokens = re.split(r'([.!?])', text)
    for i in range(0, len(tokens), 2):
        sent = tokens[i] + (tokens[i+1] if i+1 < len(tokens) else "")
        if size + len(sent) > limit and buf:
            parts.append("".join(buf).strip())
            buf, size = [sent], len(sent)
        else:
            buf.append(sent)
            size += len(sent)
    if buf:
        parts.append("".join(buf).strip())

    # 최후 안전망: 너무 긴 문장 강제 분할
    out = []
    for p in parts:
        if len(p) <= limit:
            out.append(p)
        else:
            for k in range(0, len(p), limit):
                out.append(p[k:k+limit])

    return out

def get_voice_for_character(character: str) -> str:
    """캐릭터 이름에서 적절한 음성 alias 반환"""
    if not character:
        return "narrator"

    # 0. 제외 키워드 체크 (오탐 방지) - 원본으로 체크
    if character in EXCLUDE_FROM_SPEAKER:
        return "narrator"

    # 1. 원본으로 먼저 키워드 매칭 시도 (정규화 전)
    #    "호랑이", "원숭이" 등 조사로 오인식되는 글자가 포함된 단어 처리
    for keyword, voice in _KEYWORD_TO_VOICE_SORTED:
        if keyword in character:
            return voice

    # 2. 정규화 후 다시 시도
    normalized = normalize_character(character)

    if normalized in EXCLUDE_FROM_SPEAKER:
        return "narrator"

    # 3. 최소 길이 체크 (1글자이고 위에서 매칭 안됐으면 오탐 가능성)
    if len(normalized) < 2:
        return "narrator"

    # 4. 정규화된 이름으로 키워드 매칭
    for keyword, voice in _KEYWORD_TO_VOICE_SORTED:
        if keyword in normalized:
            return voice

    # 5. VOICE_ALIASES에서 직접 찾기
    if normalized in VOICE_ALIASES:
        return normalized

    # 6. 기본값
    return "narrator"


def infer_speaker_from_context(
    dialogue: str,
    prev_text: str = "",
    next_text: str = "",
    known_characters: List[str] = None,
    prev_speaker: str = None
) -> str:
    """
    대화 태그 없는 대사에서 화자 추론

    Args:
        dialogue: 대사 텍스트
        prev_text: 이전 문장
        next_text: 다음 문장
        known_characters: 등장인물 목록
        prev_speaker: 직전 대사의 화자

    Returns:
        추론된 화자명 또는 "narrator"
    """
    known_characters = known_characters or []

    # 1. 이전/다음 문장에서 대화 태그 찾기
    found_excluded = False

    # prev_text: 역순으로 탐색 (대사에 가장 가까운 speaker 우선)
    if prev_text:
        matches = list(DIALOGUE_TAG_PATTERN.finditer(prev_text))
        for match in reversed(matches):  # 역순 - 가장 가까운 것 먼저
            speaker_candidate = match.group(1)
            if speaker_candidate not in EXCLUDE_FROM_SPEAKER:
                return speaker_candidate
            else:
                found_excluded = True

    # next_text: 정순으로 탐색 (대사 뒤에 오는 speaker)
    # SPEAKER_AFTER_DIALOGUE_PATTERN 사용 (따옴표 lookahead 없음)
    if next_text:
        for match in SPEAKER_AFTER_DIALOGUE_PATTERN.finditer(next_text):
            speaker_candidate = match.group(1)
            if speaker_candidate not in EXCLUDE_FROM_SPEAKER:
                return speaker_candidate
            else:
                found_excluded = True

    # 1-1. 모든 매칭이 제외 대상이면 문장 앞의 주어 찾기
    # "소녀가 화가 나서" -> "화"는 제외됨 -> "소녀" 찾기
    if found_excluded and prev_text:
        subject_pattern = re.compile(r'([가-힣]{2,10})(?:이|가|은|는|께서)', re.UNICODE)
        subject_match = subject_pattern.search(prev_text)
        if subject_match:
            subject = subject_match.group(1)
            if subject not in EXCLUDE_FROM_SPEAKER:
                return subject

    # 2. 호칭 분석 - 부르는 사람은 화자가 아님
    vocative_match = VOCATIVE_PATTERN.search(dialogue)
    if vocative_match:
        called_person = normalize_character(vocative_match.group(1))
        for char in known_characters:
            if normalize_character(char) != called_person:
                return char  # 원본 반환 - get_voice_for_character에서 매칭

    # 3. 교대 패턴
    if prev_speaker and len(known_characters) >= 2:
        normalized_prev = normalize_character(prev_speaker)
        for char in known_characters:
            if normalize_character(char) != normalized_prev:
                return char  # 원본 반환

    # 4. 추론 실패 → 첫 번째 캐릭터 또는 narrator
    if known_characters:
        return known_characters[0]  # 원본 반환

    return "narrator"


def parse_dialogue_with_speaker(
    text: str,
    known_characters: List[str] = None
) -> List[Dict]:
    """
    텍스트를 대사 단위로 분리하고 화자 추론

    Args:
        text: 전체 텍스트
        known_characters: 등장인물 목록

    Returns:
        [{"type": "narration"|"dialogue", "text": str, "speaker": str}, ...]
    """
    known_characters = known_characters or []
    segments = []

    # 따옴표로 대사 분리 (한글 따옴표 + 홑따옴표 포함)
    # 지원 따옴표: " " " ' ' '
#    dialogue_pattern = re.compile(r'["""\u2018\u2019\u0027]([^"""\u2018\u2019\u0027]+)["""\u2018\u2019\u0027]')
    dialogue_pattern = re.compile(r'["\u201c\u201d\u2018\u2019\u0027]([^"\u201c\u201d\u2018\u2019\u0027]+)["\u201c\u201d\u2018\u2019\u0027]')

    last_end = 0
    prev_speaker = None
    matches = list(dialogue_pattern.finditer(text))

    for i, match in enumerate(matches):
        # 대사 전 나레이션
        if match.start() > last_end:
            narration = text[last_end:match.start()].strip()
            if narration:
                segments.append({
                    "type": "narration",
                    "text": narration,
                    "speaker": "narrator"
                })

        # 대사 처리
        dialogue = match.group(1)
        prev_text = text[max(0, match.start()-100):match.start()+1]  # 따옴표 포함
        next_text = text[match.end():min(len(text), match.end()+100)]

        speaker = infer_speaker_from_context(
            dialogue,
            prev_text=prev_text,
            next_text=next_text,
            known_characters=known_characters,
            prev_speaker=prev_speaker
        )

        segments.append({
            "type": "dialogue",
            "text": dialogue,
            "speaker": speaker
        })

        prev_speaker = speaker
        last_end = match.end()

    # 마지막 나레이션
    if last_end < len(text):
        narration = text[last_end:].strip()
        if narration:
            segments.append({
                "type": "narration",
                "text": narration,
                "speaker": "narrator"
            })

    # 세그먼트가 없으면 전체를 나레이션으로
    if not segments:
        segments.append({
            "type": "narration",
            "text": text,
            "speaker": "narrator"
        })

    return segments


# ============================================================
# 캐릭터별 다중 화자 TTS 생성
# ============================================================

def concat_audio_files(audio_paths: List[str], output_path: str) -> bool:
    """
    여러 오디오 파일을 하나로 합침

    Args:
        audio_paths: 오디오 파일 경로 리스트
        output_path: 출력 파일 경로

    Returns:
        성공 여부
    """
    try:
        from moviepy.editor import AudioFileClip, concatenate_audioclips

        clips = [AudioFileClip(p) for p in audio_paths if Path(p).exists()]
        if not clips:
            return False

        final = concatenate_audioclips(clips)
        final.write_audiofile(output_path)

        for clip in clips:
            clip.close()
        return True

    except Exception as e:
        print(f"  ❌ 오디오 합치기 실패: {e}")
        return False



def add_audio_to_video(video_path: str, audio_path: str, output_path: str, bgm_path: str = None, bgm_volume: float = 0.15) -> bool:
    """
    영상에 음성 파일 합성 (BGM 지원)

    Args:
        video_path: 원본 영상 경로
        audio_path: 음성 파일 경로 (.mp3)
        output_path: 출력 영상 경로
        bgm_path: BGM 파일 경로 (선택)
        bgm_volume: BGM 볼륨 (0.0 ~ 1.0, 기본 0.15)

    Returns:
        성공 여부
    """
    from moviepy.editor import VideoFileClip, AudioFileClip, CompositeAudioClip, concatenate_audioclips

    video = None
    audio = None
    bgm = None
    final = None
    final_audio = None

    try:
        video = VideoFileClip(video_path)
        audio = AudioFileClip(audio_path)

        # 영상 길이에 맞게 오디오 조정
        if audio.duration > video.duration:
            audio = audio.subclip(0, video.duration)

        # BGM 처리
        if bgm_path and Path(bgm_path).exists():
            bgm = AudioFileClip(bgm_path)
            bgm = bgm.volumex(bgm_volume)

            # BGM이 영상보다 짧으면 반복
            if bgm.duration < video.duration:
                num_loops = int(video.duration / bgm.duration) + 1
                bgm_loops = [bgm] * num_loops
                bgm = concatenate_audioclips(bgm_loops).subclip(0, video.duration)
            else:
                bgm = bgm.subclip(0, video.duration)

            # TTS + BGM 합성
            final_audio = CompositeAudioClip([audio, bgm])
            final = video.set_audio(final_audio)
        else:
            final = video.set_audio(audio)

        final.write_videofile(output_path, codec="libx264", fps=30, audio_codec="aac", logger=None)
        return True

    except Exception as e:
        print(f"  ❌ 영상+음성 합성 실패: {e}")
        return False

    finally:
        # 리소스 정리
        if final_audio:
            try: final_audio.close()
            except: pass
        if final:
            try: final.close()
            except: pass
        if bgm:
            try: bgm.close()
            except: pass
        if audio:
            try: audio.close()
            except: pass
        if video:
            try: video.close()
            except: pass

def get_audio_duration(audio_path: str) -> float:
    """
    MP3 파일 길이 측정

    Args:
        audio_path: MP3 파일 경로

    Returns:
        오디오 길이 (초), 실패 시 0.0
    """
    try:
        from moviepy.editor import AudioFileClip
        clip = AudioFileClip(audio_path)
        duration = clip.duration
        clip.close()
        return duration
    except Exception as e:
        print(f"  ⚠️ 오디오 길이 측정 실패: {e}")
        return 0.0
    
def concat_videos_with_audio(video_paths: list, output_path: str) -> bool:
    """
    여러 영상(음성 포함)을 하나로 합치기

    Args:
        video_paths: 영상 파일 경로 리스트
        output_path: 출력 영상 경로

    Returns:
        성공 여부
    """
    try:
        from moviepy.editor import VideoFileClip, concatenate_videoclips

        clips = [VideoFileClip(str(p)) for p in video_paths]
        final = concatenate_videoclips(clips, method="compose")
        final.write_videofile(str(output_path), codec="libx264", fps=30, audio_codec="aac")

        for clip in clips:
            clip.close()
        return True

    except Exception as e:
        print(f"  ❌ 영상 합치기 실패: {e}")
        return False


def reset_session_voice_manager():
    """세션 음성 매니저 초기화"""
    global _SESSION_VOICE_MANAGER
    if _SESSION_VOICE_MANAGER:
        _SESSION_VOICE_MANAGER.reset()



# 하위 호환용 (기존 SPEAKERS)
SPEAKERS = {
    "narrator": "njiyun",
    "narrator_warm": "nyejin",
    "child_girl": "ndain",
    "child_boy": "nhajun",
    "young_female": "nara",
    "default": "njiyun"
}

# 한국어 TTS 읽기 속도 (글자/초, speed=0 기준)
CHARS_PER_SEC = 4.5

def extract_characters_from_texts(texts: List[str]) -> List[str]:
    """
    전체 텍스트에서 등장인물 자동 추출 (빈도순 정렬)

    Args:
        texts: 전체 스토리 텍스트 리스트

    Returns:
        등장인물 리스트 (빈도순, 주인공이 앞)
    """
    from collections import Counter

    all_text = " ".join(texts)
    characters = Counter()

    # 1. 대화 태그에서 캐릭터 추출 ("엄마가 말했다", "토끼는 대답했다")
    for match in DIALOGUE_TAG_PATTERN.finditer(all_text):
        char = normalize_character(match.group(1))
        if char and len(char) >= 2:
            characters[char] += 3  # 대화 태그는 가중치 높음

    # 2. 호칭에서 캐릭터 추출 ("엄마야!", "토끼야~")
    vocative_pattern = re.compile(r'([가-힣]{2,4})(?:야|아|님|씨)[,!~\s]', re.UNICODE)
    for match in vocative_pattern.finditer(all_text):
        char = normalize_character(match.group(1))
        if char and len(char) >= 2:
            characters[char] += 2

    # 3. KEYWORD_TO_VOICE 키워드 매칭
    for keyword in KEYWORD_TO_VOICE.keys():
        count = all_text.count(keyword)
        if count > 0:
            char = normalize_character(keyword)
            characters[char] += count

    # 4. CHARACTER_ALIASES 역방향 매칭
    for alias, normalized in CHARACTER_ALIASES.items():
        count = all_text.count(alias)
        if count > 0:
            characters[normalized] += count

    # 5. 따옴표 대사에서 부르는 이름 추출
    quote_pattern = re.compile(r'["""\']([^"""\']+)["""\']')
    for match in quote_pattern.finditer(all_text):
        dialogue = match.group(1)
        # 대사 안에서 호칭 찾기
        inner_vocative = re.findall(r'([가-힣]{2,4})(?:야|아)[,!]', dialogue)
        for name in inner_vocative:
            char = normalize_character(name)
            if char and len(char) >= 2:
                characters[char] += 1

    # narrator 제외
    characters.pop('narrator', None)
    characters.pop('나레이터', None)

    # 빈도순 정렬 (상위 = 주인공 추정)
    sorted_chars = [char for char, count in characters.most_common(10)]

    return sorted_chars


def assign_voices_for_characters(
    characters: List[str],
    protagonist: str = None
) -> Dict[str, str]:
    """
    캐릭터별 음성 고정 배정 (세션 레벨)

    Args:
        characters: 등장인물 리스트 (빈도순)
        protagonist: 주인공 이름 (없으면 첫 번째 캐릭터)

    Returns:
        {캐릭터명: 음성alias} 딕셔너리
    """
    voice_assignments = {}
    used_voices = set()

    # 1. narrator 고정
    voice_assignments['narrator'] = 'narrator'
    used_voices.add('narrator')

    # 2. 주인공 결정 (없으면 첫 번째 캐릭터)
    if not protagonist and characters:
        protagonist = characters[0]

    # 음성 우선순위 (좋은 음성부터)
    voice_priority = {
        'child': ['child_female', 'child_male', 'child_bright'],
        'young': ['young_female_1', 'young_male_1', 'young_female_3', 'young_male_energetic'],
        'adult': ['adult_female', 'adult_male', 'adult_female_warm', 'adult_male_deep'],
        'elder': ['elder_female', 'elder_male'],
        'animal': ['cute_animal', 'dog', 'fairy'],
    }

    def get_best_voice(char: str, used: set) -> str:
        """캐릭터에 맞는 최적 음성 선택 (중복 방지)"""
        base_voice = get_voice_for_character(char)

        # 이미 사용 중이면 같은 카테고리에서 대체 음성 찾기
        if base_voice not in used:
            return base_voice

        # 카테고리 판별
        category = None
        if 'child' in base_voice:
            category = 'child'
        elif 'young' in base_voice:
            category = 'young'
        elif 'adult' in base_voice or 'elder' in base_voice:
            category = 'adult' if 'adult' in base_voice else 'elder'
        elif base_voice in ['cute_animal', 'dog', 'fairy']:
            category = 'animal'

        # 같은 카테고리에서 미사용 음성 찾기
        if category and category in voice_priority:
            for alt_voice in voice_priority[category]:
                if alt_voice not in used:
                    return alt_voice

        # 전체에서 미사용 음성 찾기
        for alias in VOICE_ALIASES.keys():
            if alias not in used and alias != 'default':
                return alias

        # 최후 수단: 기본 음성 반환 (중복 허용)
        return base_voice

    # 3. 주인공 우선 배정
    if protagonist:
        voice = get_best_voice(protagonist, used_voices)
        normalized = normalize_character(protagonist)
        voice_assignments[protagonist] = voice
        voice_assignments[normalized] = voice
        used_voices.add(voice)
        print(f"  [PROTAGONIST] {protagonist} -> {voice}")

    # 4. 나머지 캐릭터 배정
    for char in characters:
        if char == protagonist:
            continue

        normalized = normalize_character(char)

        # 이미 배정되었는지 확인 (동의어 처리)
        if char in voice_assignments or normalized in voice_assignments:
            continue

        voice = get_best_voice(char, used_voices)
        voice_assignments[char] = voice
        voice_assignments[normalized] = voice
        used_voices.add(voice)
        print(f"  [CHARACTER] {char} -> {voice}")

    return voice_assignments

def calculate_speed_for_duration(text: str, target_duration: float) -> int:
    """
    텍스트를 목표 시간에 맞추기 위한 speed 값 계산

    Args:
        text: 읽을 텍스트
        target_duration: 목표 시간 (초)

    Returns:
        speed 값 (-5 ~ 5)
    """
    if not text or target_duration <= 0:
        return 0

    # 예상 읽기 시간 (speed=0 기준)
    char_count = len(text.replace(" ", ""))
    estimated_duration = char_count / CHARS_PER_SEC

    # 속도 비율 계산
    if estimated_duration <= target_duration:
        # 음성이 충분히 짧음 - 약간 느리게
        ratio = estimated_duration / target_duration
        if ratio > 0.8:
            return 0  # 거의 맞음
        elif ratio > 0.6:
            return -1  # 조금 느리게
        else:
            return -2  # 더 느리게
    else:
        # 음성이 김 - 빠르게
        ratio = target_duration / estimated_duration
        if ratio > 0.8:
            return 1  # 조금 빠르게
        elif ratio > 0.6:
            return 2  # 빠르게
        elif ratio > 0.5:
            return 3  # 많이 빠르게
        elif ratio > 0.4:
            return 4  # 매우 빠르게
        else:
            return 5  # 최대 속도


