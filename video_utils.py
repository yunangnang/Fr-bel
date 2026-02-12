# -*- coding: utf-8 -*-
import requests
from pathlib import Path
import gc
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from moviepy.editor import VideoFileClip, concatenate_videoclips, ImageClip, CompositeVideoClip

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
#  텍스트 파싱 및 색상별 그리기 로직
# -------------------------
def draw_colored_text_multiline(draw, text, font, max_width, start_xy, default_color="white", highlight_color="yellow"):
    """
    텍스트를 따옴표 기준으로 분리하여, 대사 부분만 highlight_color로 그립니다.
    자동 줄바꿈 기능을 포함합니다.
    """
    x, y = start_xy
    initial_x = x
    line_spacing = font.getsize("가")[1] + 10  # 줄간격
    stroke_width = 3
    stroke_fill = (0, 0, 0, 255)

    # 1. 텍스트를 [ (일반, 흰색), (대사, 색상), (일반, 흰색) ... ] 형태로 파싱
    # 정규식: 큰따옴표(", “)로 둘러싸인 부분을 캡처
    # 예: A "B" C -> ['A ', '"B"', ' C']
    parts = re.split(r'(“[^”]*”|"[^"]*")', text)
    
    # 단어 단위로 쪼개서 처리 (줄바꿈 계산을 위해)
    tokens = []
    for part in parts:
        if not part: continue
        
        # 대사인지 판별 (따옴표로 시작하고 끝나는지)
        is_dialogue = (part.startswith('“') and part.endswith('”')) or \
                      (part.startswith('"') and part.endswith('"'))
        
        color = highlight_color if is_dialogue else default_color
        
        # 띄어쓰기 단위로 분리하되, 대사는 통째로 유지하거나 필요시 분리
        # 여기서는 단순화를 위해 어절 단위로 분리
        words = part.split(' ')
        for i, word in enumerate(words):
            # 분리하면서 사라진 공백 복원 (마지막 단어 제외)
            suffix = " " if i < len(words) - 1 else ""
            tokens.append({"text": word + suffix, "color": color})

    # 2. 그리기 루프 (줄바꿈 처리)
    current_line_width = 0
    
    for token in tokens:
        word = token["text"]
        color = token["color"]
        
        word_w, word_h = font.getsize(word)
        
        # 줄바꿈 체크
        if current_line_width + word_w > max_width:
            # 다음 줄로 이동
            x = initial_x
            y += line_spacing
            current_line_width = 0
        
        # 외곽선 그리기
        for dx, dy in [(-1, -1), (-1, 1), (1, -1), (1, 1)]:
            draw.text((x + dx*stroke_width, y + dy*stroke_width), word, font=font, fill=stroke_fill)
            
        # 본문 그리기
        draw.text((x, y), word, font=font, fill=color)
        
        # 커서 이동
        x += word_w
        current_line_width += word_w
        
    return y + line_spacing # 전체 높이 반환
import re

import os

# -------------------------
#텍스트 파싱 및 색상별 그리기 로직
# -------------------------
def draw_colored_text_multiline(draw, text, font, max_width, start_xy, default_color="white", highlight_color="yellow"):
    # 1. 초기 설정
    canvas_width = draw.im.size[0] # 전체 이미지 너비
    initial_x, y = start_xy
    
    bbox = font.getbbox("가")
    text_height = bbox[3] - bbox[1] 
    line_spacing = text_height * 1.5
    stroke_width = 3
    stroke_fill = (0, 0, 0, 255)

    # 2. 텍스트 파싱 (기존 동일)
    parts = re.split(r'(“[^”]*”|"[^"]*")', text)
    tokens = []
    for part in parts:
        if not part: continue
        is_dialogue = (part.startswith('“') and part.endswith('”')) or \
                      (part.startswith('"') and part.endswith('"'))
        color = highlight_color if is_dialogue else default_color
        words = part.split(' ')
        for i, word in enumerate(words):
            suffix = " " if i < len(words) - 1 else ""
            tokens.append({"text": word + suffix, "color": color})

    # 3. 단어들을 줄(Line) 단위로 묶기
    lines = []
    current_line = []
    current_line_width = 0

    for token in tokens:
        word_w = font.getlength(token["text"])
        
        if current_line_width + word_w > max_width:
            lines.append((current_line, current_line_width))
            current_line = [token]
            current_line_width = word_w
        else:
            current_line.append(token)
            current_line_width += word_w
            
    if current_line:
        lines.append((current_line, current_line_width))

    # 4.  각 줄을 중앙 정렬하여 그리기
    for line_tokens, line_width in lines:
        # 이 줄의 시작 X 좌표 계산 (중앙 정렬)
        line_start_x = (canvas_width - line_width) // 2
        current_x = line_start_x
        
        for token in line_tokens:
            draw.text(
                (current_x, y), 
                token["text"], 
                font=font, 
                fill=token["color"],
                stroke_width=stroke_width, 
                stroke_fill=stroke_fill
            )
            current_x += font.getlength(token["text"])
        
        y += line_spacing # 다음 줄로 이동
        
    return y
# -------------------------
# PIL로 자막 이미지 생성 (부분 컬러링 적용)
# -------------------------
def create_subtitle_image(text, width, font_size=36, scene_index=0, font_color="white"):

    if not os.path.exists(FONT_PATH):
         # 폰트가 없으면 기본 폰트 사용 (한글 깨질 수 있음)
        try:
            font = ImageFont.truetype("arial.ttf", font_size)
        except:
            font = ImageFont.load_default()
    else:
        font = ImageFont.truetype(FONT_PATH, font_size)
        
    max_width = int(width * 0.9)
    
    # 1. 높이 계산용 임시 캔버스
    dummy_img = Image.new("RGBA", (width, 500), (0,0,0,0))
    dummy_draw = ImageDraw.Draw(dummy_img)
    
    calculated_height = draw_colored_text_multiline(
        dummy_draw, text, font, max_width, (0, 0), 
        default_color="white", 
        highlight_color=font_color
    )
    
    # 2. 실제 이미지 생성
    # 계산된 높이에 여유를 조금 둡니다.
    img_height = int(calculated_height + font_size)
    img = Image.new("RGBA", (width, img_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # 3. 그리기 (가로 중앙 정렬을 위한 시작점 계산)
    start_x = (width - max_width) // 2
    start_y = 10
    
    draw_colored_text_multiline(
        draw, text, font, max_width, (start_x, start_y), 
        default_color="white", 
        highlight_color=font_color
    )

    return np.array(img)

# -------------------------
# 자막 오버레이 (PIL 사용)
# -------------------------
def add_subtitle_to_video(input_video, text, output_path, scene_index=0, font_color="white"):
    clip = VideoFileClip(input_video)
    subtitle_clip = None
    final = None

    try:
        if not text or text.strip() == "":
            clip.write_videofile(output_path, codec="libx264", fps=30, audio=False)
            return

        # PIL로 자막 이미지 생성 (장면 인덱스로 색상 결정 -> font_color 전달로 변경)
        subtitle_img = create_subtitle_image(
            text, 
            clip.w, 
            scene_index=scene_index, 
            font_color=font_color # [추가됨] 색상 전달
        )

        # ImageClip으로 변환
        subtitle_clip = (ImageClip(subtitle_img)
                         .set_duration(clip.duration)
                         .set_position(("center", clip.h - subtitle_img.shape[0] - 30)))

        final = CompositeVideoClip([clip, subtitle_clip])
        final.write_videofile(output_path, codec="libx264", fps=30, audio=False)
    finally:
        # Memory cleanup - close all clips
        if final:
            final.close()
        if subtitle_clip:
            subtitle_clip.close()
        clip.close()
        gc.collect()  # Force garbage collection


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
