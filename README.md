# AI Shorts Builder

그림책 삽화를 기반으로 AI 숏폼 영상(숏츠/릴스)을 자동 생성하는 Streamlit 애플리케이션입니다.

삽화 이미지 → AI 영상 생성 → 자막 오버레이 → TTS 음성 합성 → 최종 숏츠 영상 파이프라인을 제공합니다.

## 주요 기능

- **Runway Gen4 영상 생성** — 삽화 이미지를 입력하면 AI가 움직이는 영상으로 변환
- **다중 TTS 엔진 지원** — Clova, OpenAI, Google Cloud TTS, Edge TTS, Gemini 중 선택
- **자동 자막 생성** — GPT가 삽화 텍스트를 분석하여 나레이션/대사 자막 자동 생성
- **화자별 음성 배정** — 등장인물별로 다른 TTS 음성 자동 배정
- **BGM 합성** — 페이지별 배경음악 자동 매칭 및 합성
- **두 가지 모드** — 이미지 선택 모드(A) / 텍스트 분석 기반 모드(B)

## 프로젝트 구조

```
shorts_builder11/
├── app.py                   # 메인 앱 (이미지 선택 모드)
├── app_test_separation.py   # 메인 앱 (BGM 통합 버전)
├── b_text_based.py          # 텍스트 분석 기반 모드 (B모드)
├── tts_module.py            # TTS API 인터페이스
├── tts_core.py              # TTS 공통 로직 / 영상·오디오 처리
├── video_utils.py           # 비디오 다운로드, 자막, 트리밍
├── runway_api.py            # Runway Gen4 영상 생성 API
├── malgun.ttf               # 자막용 한글 폰트
├── character                # 책별 삽화 이미지 
├── character/ txt           # 책별 삽화 이미지 및 텍스트
├── BGM/                     # 페이지별 배경음악
├── outputs/                 # 생성된 영상 출력 (자동 생성)
├── requirements.txt
└── .env                     # API 키 설정
```


**ffmpeg**
- Windows: `choco install ffmpeg` 또는 [공식 사이트](https://ffmpeg.org/download.html)에서 다운로드
- macOS: `brew install ffmpeg`
- Linux: `sudo apt install ffmpeg`

### 3. 환경변수 설정

`.env` 파일을 생성하고 아래 API 키를 입력하세요:

```env
RUNWAYML_API_SECRET=your_runway_api_key
OPENAI_API_KEY=your_openai_api_key
CLOVA_CLIENT_ID=your_clova_client_id
CLOVA_CLIENT_SECRET=your_clova_client_secret
GEMINI_API_KEY=your_gemini_api_key
```

Google Cloud TTS를 사용하려면 서비스 계정 키 파일(`tts-gemini-env.json`)도 프로젝트 루트에 배치하세요.

### 4. 리소스 준비

- `character/` 폴더에 책별 삽화 이미지 폴더 배치
- `character/txt/048/` 폴더에 책별 텍스트 파일(`.txt`) 배치
- (선택) `BGM/` 폴더에 페이지별 배경음악 파일 배치

## 실행

```bash
streamlit run app.py
```

또는 BGM 통합 버전:

```bash
streamlit run app_test_separation.py
```

## 사용한 API

| API | 용도 |
|-----|------|
| [Runway Gen4](https://runwayml.com/) | 이미지 → 영상 생성 |
| [OpenAI GPT](https://openai.com/) | 자막 생성, 텍스트 분석, TTS |
| [Naver Clova](https://clova.ai/) | 한국어 TTS |
| [Google Cloud TTS](https://cloud.google.com/text-to-speech) | TTS |
| [Google Gemini](https://ai.google.dev/) | TTS (선택) |
| [Edge TTS](https://github.com/rany2/edge-tts) | TTS (선택, 무료) |
