# -*- coding: utf-8 -*-
"""
워크숍 세션 로깅 유틸리티.
사용자별로 격리된 폴더에 작업 데이터를 저장하고 ZIP으로 묶어 다운로드 가능하게 한다.
"""
import io
import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

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
