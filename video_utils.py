# -*- coding: utf-8 -*-
import os
import re
import gc
import unicodedata
import requests
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from moviepy.editor import VideoFileClip, concatenate_videoclips, ImageClip, CompositeVideoClip

# ─── decorator 라이브러리 호환성 패치 ───
# moviepy 1.0.3 + decorator 라이브러리 조합에서 write_videofile의 fps 파라미터가
# 유실되는 버그 수정. ffmpeg_write_video 레벨에서 fps=None일 때 clip.fps로 폴백.
import moviepy.video.VideoClip as _vc_module
_orig_ffmpeg_write_video = _vc_module.ffmpeg_write_video

def _patched_ffmpeg_write_video(clip, filename, fps, codec='libx264', bitrate=None,
                                 preset="medium", withmask=False, write_logfile=False,
                                 audiofile=None, verbose=True, threads=None,
                                 ffmpeg_params=None, logger='bar'):
    if fps is None:
        fps = getattr(clip, 'fps', None) or 24
    return _orig_ffmpeg_write_video(clip, filename, fps, codec, bitrate, preset,
                                     withmask, write_logfile, audiofile, verbose,
                                     threads, ffmpeg_params, logger)

_vc_module.ffmpeg_write_video = _patched_ffmpeg_write_video
# ─── 패치 끝 ───

#  같은 폴더에 있는 폰트 자동 연결
FONT_PATH = str(Path(__file__).resolve().parent / "malgun.ttf")

# 금도끼 은도끼 스타일 - 외곽선 텍스트 색상 (장면마다 순환)
TEXT_COLORS = [
    (255, 255, 255),  # 흰색
    (144, 238, 144),  # 연두색
    (255, 255, 150),  # 연노랑
    (255, 200, 200),  # 연분홍
    (200, 230, 255),  # 연하늘
]

# -------------------------
# 영상 다운로드
# -------------------------
def download_video(url: str, out_path: Path):
    r = requests.get(url)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(r.content)


# -------------------------
# 영상 이어붙이기
# -------------------------
def concat_videos(video_paths, out_path):
    clips = [VideoFileClip(str(p)) for p in video_paths]
    try:
        final = concatenate_videoclips(clips, method="compose")
        final.write_videofile(str(out_path), codec="libx264", fps=30, audio=False)
        final.close()
    finally:
        for c in clips:
            c.close()
        gc.collect()  # Force garbage collection


# -------------------------
# 텍스트 정규화 (PDF에서 추출한 텍스트의 보이지 않는 문자 정리)
# -------------------------
# PIL의 draw.text는 토큰 안에 \n/\r/\t 같은 화이트스페이스가 있으면 자체적으로
# 줄바꿈/탭 처리를 시도해 wrap 로직과 어긋난다. 또 PDF/OCR 결과에는 NFD로 분해된
# 한글 jamo, zero-width 조이너(U+200B/200C/200D), NBSP(U+00A0) 등이 섞여서
# 어떤 글자는 자리만 잡고 글리프가 그려지지 않는 현상이 발생할 수 있다.
# 자막 렌더 직전에 한 번 정규화해 이 문제를 모두 잡는다.
_INVISIBLE_RE = re.compile(r'[​-‍﻿]')

def _sanitize_subtitle_text(text):
    if not text:
        return ""
    # 분해된 한글(NFD) → 합쳐진 음절(NFC). 호환 문자도 통일.
    text = unicodedata.normalize("NFC", text)
    # zero-width 류 제거
    text = _INVISIBLE_RE.sub("", text)
    # 모든 화이트스페이스(\n, \r, \t, NBSP 포함)를 단일 스페이스로 통일
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# -------------------------
# 텍스트 파싱 + 자동 줄바꿈 wrap 시뮬레이션
# -------------------------
def _tokenize_for_subtitle(text, default_color, highlight_color):
    """따옴표 단위로 파싱해 (text, color) 토큰 리스트로 반환."""
    text = _sanitize_subtitle_text(text)
    parts = re.split(r'(“[^”]*”|"[^"]*")', text)
    tokens = []
    for part in parts:
        if not part:
            continue
        is_dialogue = (part.startswith('“') and part.endswith('”')) or \
                      (part.startswith('"') and part.endswith('"'))
        color = highlight_color if is_dialogue else default_color
        words = part.split(' ')
        for i, word in enumerate(words):
            suffix = " " if i < len(words) - 1 else ""
            chunk = word + suffix
            if chunk:
                tokens.append({"text": chunk, "color": color})
    return tokens


def _wrap_tokens(tokens, font, max_width):
    """토큰을 줄별로 묶어 [(line_tokens, line_width), ...] 반환."""
    lines = []
    current_line = []
    current_line_width = 0.0
    for token in tokens:
        word_w = font.getlength(token["text"])
        if current_line and current_line_width + word_w > max_width:
            lines.append((current_line, current_line_width))
            current_line = [token]
            current_line_width = word_w
        else:
            current_line.append(token)
            current_line_width += word_w
    if current_line:
        lines.append((current_line, current_line_width))
    return lines


# -------------------------
# 색상 분리 + 가로 중앙 정렬 + 줄별 반투명 배경 그리기
# -------------------------
def draw_colored_text_multiline(draw, text, font, max_width, start_xy,
                                 default_color="white", highlight_color="yellow",
                                 line_height=None, draw_bg=True):
    """
    텍스트를 따옴표로 분리해 대사만 highlight_color로 칠하고, canvas 가로
    기준 중앙 정렬로 자동 줄바꿈해 그린다. draw_bg=True면 줄마다 반투명
    검정 배경을 깔아 페이지 그림 위에서도 가독성을 확보한다.

    return: 마지막 줄 아랫변의 y 좌표.
    """
    canvas_width = draw.im.size[0]
    _, y = start_xy

    ascent, descent = font.getmetrics()
    if line_height is None:
        line_height = int((ascent + descent) * 1.35)
    stroke_width = 3
    stroke_fill = (0, 0, 0, 255)

    tokens = _tokenize_for_subtitle(text, default_color, highlight_color)
    lines = _wrap_tokens(tokens, font, max_width)

    for line_tokens, line_width in lines:
        line_start_x = (canvas_width - line_width) / 2

        if draw_bg:
            pad_x = max(int(line_height * 0.4), 14)
            pad_y_top = max(int((line_height - (ascent + descent)) / 2), 2)
            bg_left = max(int(line_start_x - pad_x), 0)
            bg_right = min(int(line_start_x + line_width + pad_x), canvas_width)
            bg_top = int(y - pad_y_top)
            bg_bottom = int(y + line_height - pad_y_top)
            draw.rectangle([bg_left, bg_top, bg_right, bg_bottom],
                           fill=(0, 0, 0, 150))

        current_x = line_start_x
        for token in line_tokens:
            draw.text(
                (current_x, y),
                token["text"],
                font=font,
                fill=token["color"],
                stroke_width=stroke_width,
                stroke_fill=stroke_fill,
            )
            current_x += font.getlength(token["text"])

        y += line_height

    return y


def _load_font(font_size):
    if os.path.exists(FONT_PATH):
        return ImageFont.truetype(FONT_PATH, font_size)
    try:
        return ImageFont.truetype("arial.ttf", font_size)
    except Exception:
        return ImageFont.load_default()


def _measure_subtitle(text, width, font_size):
    """주어진 폰트 크기로 자막 렌더 시 총 높이를 미리 계산."""
    font = _load_font(font_size)
    ascent, descent = font.getmetrics()
    line_height = int((ascent + descent) * 1.35)
    max_w = int(width * 0.9)

    tokens = _tokenize_for_subtitle(text, "white", "white")
    lines = _wrap_tokens(tokens, font, max_w)
    line_count = max(len(lines), 1)
    total_height = line_count * line_height + int(line_height * 0.4)  # 위·아래 여유
    return font, line_height, total_height


# -------------------------
# 자막 이미지 생성 (자동 폰트 축소 + 줄별 반투명 배경)
# -------------------------
def create_subtitle_image(text, width, max_height=None, font_size=36,
                          scene_index=0, font_color="white"):
    """
    width: 자막 이미지 가로 (비디오 가로와 동일)
    max_height: 자막이 차지할 수 있는 최대 세로(px). 넘으면 폰트 자동 축소.
    font_size: 시작 폰트 크기.
    """
    if max_height is None:
        max_height = 10_000  # 사실상 무제한

    font, line_height, total_height = _measure_subtitle(text, width, font_size)
    fs = font_size
    while total_height > max_height and fs > 18:
        fs -= 2
        font, line_height, total_height = _measure_subtitle(text, width, fs)

    img_height = max(int(total_height), line_height + 16)
    img = Image.new("RGBA", (width, img_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad_top = max(int(line_height * 0.2), 8)

    draw_colored_text_multiline(
        draw, text, font, int(width * 0.9), (0, pad_top),
        default_color="white",
        highlight_color=font_color,
        line_height=line_height,
        draw_bg=True,
    )

    return np.array(img)


# -------------------------
# 자막 오버레이 (하단 고정 anchor + 화면 하단 35% 안에 가둠)
# -------------------------
def add_subtitle_to_video(input_video, text, output_path, scene_index=0, font_color="white"):
    clip = VideoFileClip(input_video)
    subtitle_clip = None
    final = None

    try:
        if not text or text.strip() == "":
            clip.write_videofile(output_path, codec="libx264", fps=30, audio=False)
            return

        # 자막 영역을 화면 하단 35% 안에 가둔다. 텍스트가 길면 폰트가 자동 축소돼
        # 화면 위쪽으로 침범하지 않음.
        sub_area_max_h = int(clip.h * 0.35)
        bottom_margin = max(int(clip.h * 0.04), 24)

        subtitle_img = create_subtitle_image(
            text,
            clip.w,
            max_height=sub_area_max_h,
            scene_index=scene_index,
            font_color=font_color,
        )
        sub_h = subtitle_img.shape[0]

        # 하단 고정 anchor — 자막 길이가 달라져도 bottom baseline은 동일.
        y_pos = max(clip.h - sub_h - bottom_margin, 0)

        subtitle_clip = (ImageClip(subtitle_img)
                         .set_duration(clip.duration)
                         .set_position(("center", y_pos)))

        final = CompositeVideoClip([clip, subtitle_clip])
        final.write_videofile(output_path, codec="libx264", fps=30, audio=False)
    finally:
        if final:
            final.close()
        if subtitle_clip:
            subtitle_clip.close()
        clip.close()
        gc.collect()


# -------------------------
# 영상 길이 조절 (TTS 길이에 맞춤)
# -------------------------
def trim_video_to_duration(video_path: str, target_duration: float, output_path: str):
    """
    영상을 목표 길이로 자르기

    Args:
        video_path: 원본 영상 경로
        target_duration: 목표 길이 (초)
        output_path: 출력 영상 경로
    """
    clip = VideoFileClip(video_path)
    trimmed = None

    try:
        # 안전장치: 영상이 목표보다 짧으면 원본 길이 사용
        actual_duration = min(target_duration, clip.duration)

        trimmed = clip.subclip(0, actual_duration)
        trimmed.write_videofile(output_path, codec="libx264", fps=30, audio=False)
    finally:
        if trimmed:
            trimmed.close()
        clip.close()
        gc.collect()  # Force garbage collection


# -------------------------
# 영상 길이를 목표에 정확히 맞춤 (짧으면 trim, 길면 extend)
# -------------------------
def fit_video_to_duration(video_path: str, target_duration: float, output_path: str,
                          extend_mode: str = "loop"):
    """
    Runway 영상은 5초·10초 등 정해진 길이로만 나오는데 TTS가 더 길면 음성이 잘림.
    이 함수는 영상을 목표 길이에 정확히 맞춰서, 짧으면 잘라내고 길면 extend_mode에
    따라 채워 넣는다. 그래서 다음 장면으로 넘어가는 시점이 TTS 끝과 일치.

    extend_mode:
      - "loop"     : Runway 영상을 처음부터 반복 (이음새에서 점프 발생 가능)
      - "freeze"   : 마지막 프레임을 정지 화면으로 늘림
      - "pingpong" : 앞→역재생→앞 반복으로 이음새 매끄럽게
    """
    import math
    from moviepy.editor import ImageClip, concatenate_videoclips
    from moviepy.video.fx.all import time_mirror

    clip = VideoFileClip(video_path)
    final = None
    tail = None
    try:
        eps = 0.05  # 짧은 차이는 그냥 무시
        if target_duration <= clip.duration + eps:
            actual = min(target_duration, clip.duration)
            final = clip.subclip(0, actual)
        elif extend_mode == "freeze":
            # 마지막 프레임을 ImageClip으로 만들어 부족한 시간만큼 이어붙임
            extra = target_duration - clip.duration
            last_t = max(0.0, clip.duration - 0.04)
            tail = ImageClip(clip.get_frame(last_t), duration=extra).set_fps(clip.fps or 30)
            final = concatenate_videoclips([clip, tail], method="compose")
        else:
            # loop / pingpong: 단위 클립을 반복해 채운 후 정확한 길이로 컷
            if extend_mode == "pingpong":
                # 앞 + 역재생 한 묶음을 단위로 사용 → 이음새 매끄러움
                unit = concatenate_videoclips(
                    [clip, clip.fx(time_mirror)], method="compose"
                )
            else:  # loop
                unit = clip
            n_loops = math.ceil(target_duration / unit.duration)
            pieces = [unit.subclip(0, unit.duration) for _ in range(n_loops)]
            looped = concatenate_videoclips(pieces, method="compose")
            final = looped.subclip(0, target_duration)

        final.write_videofile(output_path, codec="libx264", fps=30, audio=False)
    finally:
        if final:
            final.close()
        if tail:
            tail.close()
        clip.close()
        gc.collect()
