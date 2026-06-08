# -*- coding: utf-8 -*-
"""
워크숍 세션 로깅 유틸리티.
사용자별로 격리된 폴더에 작업 데이터를 저장하고 ZIP으로 묶어 다운로드 가능하게 한다.
"""
import hashlib
import io
import json
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import streamlit as st

SESSIONS_ROOT = Path("outputs/sessions")


def init_session(user_name: str) -> str:
    """이름을 받아 세션 ID 생성 및 폴더 초기화."""
    safe_name = (
        user_name.strip()
        .replace("/", "_")
        .replace("\\", "_")
        .replace(" ", "_")
    ) or "anonymous"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_id = f"{safe_name}_{timestamp}"

    st.session_state.user_name = user_name.strip()
    st.session_state.session_id = session_id

    sdir = SESSIONS_ROOT / session_id
    sdir.mkdir(parents=True, exist_ok=True)

    log_event("session_start", {"user_name": user_name.strip()})
    return session_id


def get_session_dir() -> Optional[Path]:
    """현재 세션 폴더. 이름 입력 전이면 None."""
    sid = st.session_state.get("session_id")
    if not sid:
        return None
    d = SESSIONS_ROOT / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_event(action: str, data: Optional[dict] = None) -> None:
    """세션 events.jsonl에 한 줄 추가. 사용자 식별 전엔 무시."""
    sdir = get_session_dir()
    if not sdir:
        return
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "data": data or {},
    }
    log_file = sdir / "events.jsonl"
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        # 로깅 실패가 앱 동작을 막아선 안 됨
        print(f"[session_logger] log_event failed: {e}")


def _summarize_text(text: str, max_chars: int = 200) -> dict:
    """프롬프트·payload를 jsonl에 부담 없이 넣기 위한 요약."""
    if text is None:
        return {"length": 0, "preview": "", "sha8": ""}
    s = str(text)
    digest = hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()[:8]
    return {
        "length": len(s),
        "preview": s[:max_chars],
        "sha8": digest,
    }


def log_stage_entry(stage_name: str, extra: Optional[dict] = None) -> None:
    """단계 첫 진입 1회만 기록 (Streamlit 리런에도 idempotent)."""
    if not st.session_state.get("session_id"):
        return
    flag_key = f"_stage_entered_{stage_name}"
    if st.session_state.get(flag_key):
        return
    st.session_state[flag_key] = True
    payload = {"stage": stage_name}
    if extra:
        payload.update(extra)
    log_event("stage_entered", payload)


def log_button_click(button_id: str, extra: Optional[dict] = None) -> None:
    """주요 버튼 클릭 통일 이벤트."""
    payload = {"button": button_id}
    if extra:
        payload.update(extra)
    log_event("button_click", payload)


def _extract_usage(response: Any) -> Optional[dict]:
    """OpenAI / Gemini 응답에서 토큰 사용량 안전 추출. 실패하면 None."""
    if response is None:
        return None
    # OpenAI: response.usage.prompt_tokens / completion_tokens / total_tokens
    usage = getattr(response, "usage", None)
    if usage is not None:
        out = {}
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            v = getattr(usage, k, None)
            if v is not None:
                out[k] = v
        # Gemini google-genai: usage_metadata.prompt_token_count / candidates_token_count
        for k in ("input_tokens", "output_tokens", "cached_tokens"):
            v = getattr(usage, k, None)
            if v is not None:
                out[k] = v
        if out:
            return out
    meta = getattr(response, "usage_metadata", None)
    if meta is not None:
        out = {}
        for k in (
            "prompt_token_count", "candidates_token_count",
            "total_token_count", "cached_content_token_count",
        ):
            v = getattr(meta, k, None)
            if v is not None:
                out[k] = v
        if out:
            return out
    return None


@contextmanager
def log_api_call(
    api_name: str,
    endpoint: str,
    request_summary: Optional[dict] = None,
):
    """API 호출을 감싸 시작·종료·duration·usage·error를 자동 기록.

    사용 예:
        with log_api_call("openai_chat", "gpt-4o-mini",
                          {"prompt": _summarize_text(prompt)}) as ctx:
            resp = client.chat.completions.create(...)
            ctx["response_obj"] = resp  # usage 자동 추출
            ctx["result_summary"] = {"choices": len(resp.choices)}

    원본 예외는 로그 후 재발생되어 호출자가 평소처럼 처리할 수 있게 함.
    """
    started_perf = time.perf_counter()
    started_iso = datetime.now().isoformat(timespec="seconds")
    req_payload = {"endpoint": endpoint, "started_at": started_iso}
    if request_summary:
        req_payload["request"] = request_summary
    log_event(f"{api_name}_request", req_payload)

    ctx: dict = {}
    error_info = None
    try:
        yield ctx
    except Exception as e:
        error_info = {
            "type": type(e).__name__,
            "message": str(e)[:500],
        }
        raise
    finally:
        duration_ms = int((time.perf_counter() - started_perf) * 1000)
        resp_payload = {
            "endpoint": endpoint,
            "duration_ms": duration_ms,
            "success": error_info is None,
        }
        if error_info:
            resp_payload["error"] = error_info
        # response_obj가 있으면 usage 자동 추출
        usage = _extract_usage(ctx.get("response_obj"))
        if usage:
            resp_payload["usage"] = usage
        # 호출자가 직접 추가하고 싶은 데이터
        if ctx.get("result_summary"):
            resp_payload["result"] = ctx["result_summary"]
        log_event(f"{api_name}_response", resp_payload)


def make_session_zip() -> bytes:
    """세션 폴더 전체를 ZIP bytes로 직렬화."""
    sdir = get_session_dir()
    if not sdir or not sdir.exists():
        return b""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sdir.rglob("*"):
            if path.is_file():
                arcname = path.relative_to(sdir.parent)
                zf.write(path, arcname)
    return buf.getvalue()


def render_sidebar_panel() -> None:
    """모든 페이지에서 공통으로 보이는 사이드바 패널 (사용자명 + 다운로드)."""
    if not st.session_state.get("user_name"):
        return
    st.sidebar.markdown(f"### 👤 {st.session_state.user_name}")
    st.sidebar.caption(f"세션 ID: `{st.session_state.session_id}`")

    zip_bytes = make_session_zip()
    if zip_bytes:
        st.sidebar.download_button(
            "📦 내 세션 다운로드",
            data=zip_bytes,
            file_name=f"{st.session_state.session_id}.zip",
            mime="application/zip",
            use_container_width=True,
        )
        st.sidebar.caption(
            "지금까지의 모든 작업이 담겨 있어요.\n"
            "**워크숍 끝나면 꼭 받아 주세요.**"
        )
    else:
        st.sidebar.caption("아직 저장된 작업이 없습니다.")
