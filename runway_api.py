# -*- coding: utf-8 -*-
# runway_api.py
from runwayml import RunwayML, TaskFailedError
from dotenv import load_dotenv
from pathlib import Path
from PIL import Image
import io, base64
import os
from session_logger import log_api_call, summarize_text

# .env 로드 (RUNWAYML_API_SECRET 필요)
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

client = RunwayML() 


def image_file_to_data_uri(image_path: str, max_size=1280, quality=85) -> str:
    """이미지를 Base64로 변환 (Runway 업로드용)"""
    img = Image.open(image_path).convert("RGB")
    img.thumbnail((max_size, max_size))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    encoded = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{encoded}"


def generate_video_from_image(image_path: str, prompt_text: str, duration=5, ratio="720:1280"):
    """Runway Gen4 Turbo 영상 생성"""
    prompt_image = image_file_to_data_uri(image_path)

    try:
        with log_api_call("runway_gen4", "gen4_turbo", {
            "image": Path(image_path).name,
            "prompt": summarize_text(prompt_text),
            "duration": duration,
            "ratio": ratio,
        }) as _ctx:
            task = (
                client.image_to_video.create(
                    model="gen4_turbo",
                    prompt_image=prompt_image,
                    prompt_text=prompt_text,
                    duration=duration,
                    ratio=ratio,
                ).wait_for_task_output()
            )
            # Runway는 토큰 X. 요청 duration(초)로 크레딧 추정.
            _ctx["result_summary"] = {
                "billed_duration_sec": duration,
                "ratio": ratio,
            }
        return task

    except TaskFailedError as e:
        raise RuntimeError(f"Runway 작업 실패: {e.task_details}")


def extract_video_url(result):
    """Runway 응답 구조가 다양할 때 안전하게 URL 추출"""
    output = result.output

    if isinstance(output, str):
        return output
    if isinstance(output, dict) and "url" in output:
        return output["url"]
    if isinstance(output, list) and len(output) > 0:
        first = output[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict) and "url" in first:
            return first["url"]

    raise RuntimeError(f" Runway 응답에서 URL 추출 실패: {output}")
