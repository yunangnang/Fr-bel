# -*- coding: utf-8 -*-
"""
워크숍 세션 분석기.
사용자가 다운로드한 ZIP 또는 outputs/sessions/<id>/ 폴더의 events.jsonl을
파싱해서 단계별 소요 시간, API 호출 수, 토큰량, 비용 추정, 오류 요약 출력.

사용:
    python scripts/analyze_session.py outputs/sessions/허지웅_20260608_142315/
    python scripts/analyze_session.py 허지웅_20260608_142315.zip
    python scripts/analyze_session.py outputs/sessions/  # 모든 세션 일괄
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

# =========================================================
# 단가표 — 필요 시 직접 수정. 단가 변동 잦으면 환경변수로 빼도 됨.
# OpenAI는 토큰당, Runway는 초당, Clova는 글자수당.
# (2026-06 시점 추정치, 정확한 값은 각 콘솔에서 재확인)
# =========================================================
PRICING = {
    # OpenAI gpt-5.2: 토큰당 $0.000010 입력 / $0.000030 출력 (가정)
    "openai_chat_prompt_per_token":      0.000010,
    "openai_chat_completion_per_token":  0.000030,
    # OpenAI TTS gpt-4o-mini-tts: 문자당 $0.000015
    "openai_tts_per_char":               0.000015,
    # Gemini 2.5 TTS: 토큰당 $0.000002 추정
    "gemini_tts_per_token":              0.000002,
    # Clova TTS: 글자당 ₩0.072 ≈ $0.000055
    "clova_tts_per_char":                0.000055,
    # Runway Gen4 Turbo: 초당 $0.05
    "runway_per_second":                 0.05,
}


# =========================================================
# 입력 처리 — 폴더, zip, 폴더 내 다중 세션 모두 수용
# =========================================================
def iter_event_files(target: Path) -> Iterable[tuple[str, Path]]:
    """target에서 (session_id, events.jsonl 경로)들을 찾아 yield."""
    if target.is_file() and target.suffix == ".zip":
        # zip은 임시 풀어내지 않고 그대로 처리 (yield 시 추출)
        with zipfile.ZipFile(target) as zf:
            for name in zf.namelist():
                if name.endswith("events.jsonl"):
                    sid = Path(name).parent.name
                    tmp = target.parent / f"_extracted_{sid}_events.jsonl"
                    with zf.open(name) as src, open(tmp, "wb") as dst:
                        dst.write(src.read())
                    yield sid, tmp
        return

    if target.is_dir():
        # 단일 세션 폴더?
        if (target / "events.jsonl").exists():
            yield target.name, target / "events.jsonl"
            return
        # outputs/sessions/ 같은 상위 폴더: 하위 세션 전부
        for sub in sorted(target.iterdir()):
            if sub.is_dir() and (sub / "events.jsonl").exists():
                yield sub.name, sub / "events.jsonl"


def load_events(path: Path) -> list[dict]:
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


# =========================================================
# 분석
# =========================================================
def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def analyze(events: list[dict]) -> dict:
    if not events:
        return {}

    started = parse_ts(events[0]["ts"])
    ended = parse_ts(events[-1]["ts"])

    user_name = None
    stages: list[tuple[str, datetime]] = []
    button_clicks: list[tuple[str, datetime, dict]] = []

    # API 집계: (api_name) → 누적 통계
    api_stats: dict[str, dict] = defaultdict(lambda: {
        "calls": 0,
        "duration_ms_total": 0,
        "duration_ms_max": 0,
        "errors": 0,
        "error_messages": [],
        # OpenAI 계열
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        # Gemini
        "prompt_token_count": 0,
        "candidates_token_count": 0,
        # Char-based (TTS)
        "char_count": 0,
        # Runway
        "billed_seconds": 0,
    })

    # request → response 매칭용 임시 저장 (가장 가까운 같은 API의 request 사용)
    pending_requests: dict[str, dict] = {}

    for e in events:
        action = e.get("action", "")
        data = e.get("data") or {}
        ts = parse_ts(e["ts"])

        if action == "session_start":
            user_name = data.get("user_name")
        elif action == "stage_entered":
            stages.append((data.get("stage", "?"), ts))
        elif action == "button_click":
            button_clicks.append((data.get("button", "?"), ts, data))

        # API request/response 처리
        if action.endswith("_request"):
            api_name = action[:-len("_request")]
            pending_requests[api_name] = data
        elif action.endswith("_response"):
            api_name = action[:-len("_response")]
            stats = api_stats[api_name]
            stats["calls"] += 1
            dms = data.get("duration_ms") or 0
            stats["duration_ms_total"] += dms
            stats["duration_ms_max"] = max(stats["duration_ms_max"], dms)

            if not data.get("success", True):
                stats["errors"] += 1
                err = (data.get("error") or {}).get("message", "")
                if err and len(stats["error_messages"]) < 5:
                    stats["error_messages"].append(err[:200])

            usage = data.get("usage") or {}
            for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                     "prompt_token_count", "candidates_token_count"):
                if k in usage:
                    stats[k] += usage[k]

            # 문자수: request에 저장된 text.length 사용
            req = pending_requests.pop(api_name, {})
            req_payload = req.get("request") or {}
            for txt_key in ("text", "user_text", "prompt"):
                if isinstance(req_payload.get(txt_key), dict):
                    stats["char_count"] += req_payload[txt_key].get("length", 0)
                    break

            # Runway 청구 초
            result = data.get("result") or {}
            if "billed_duration_sec" in result:
                stats["billed_seconds"] += result["billed_duration_sec"]

    # 단계별 머문 시간 계산
    stage_durations: list[tuple[str, float]] = []
    for i, (name, t) in enumerate(stages):
        end_t = stages[i + 1][1] if i + 1 < len(stages) else ended
        stage_durations.append((name, (end_t - t).total_seconds()))

    return {
        "user_name": user_name,
        "started": started,
        "ended": ended,
        "duration_sec": (ended - started).total_seconds(),
        "stages": stage_durations,
        "button_clicks": button_clicks,
        "api_stats": dict(api_stats),
    }


# =========================================================
# 비용 추정
# =========================================================
def estimate_cost(api: str, stats: dict) -> float:
    if api == "openai_chat":
        return (stats["prompt_tokens"] * PRICING["openai_chat_prompt_per_token"]
                + stats["completion_tokens"] * PRICING["openai_chat_completion_per_token"])
    if api == "openai_tts":
        return stats["char_count"] * PRICING["openai_tts_per_char"]
    if api == "gemini_tts":
        # token_count가 비어있으면 char 기반 추정 (보수적으로 0.5 토큰/글자)
        toks = stats["prompt_token_count"] or int(stats["char_count"] * 0.5)
        return toks * PRICING["gemini_tts_per_token"]
    if api == "clova_tts":
        return stats["char_count"] * PRICING["clova_tts_per_char"]
    if api == "runway_gen4":
        return stats["billed_seconds"] * PRICING["runway_per_second"]
    return 0.0


# =========================================================
# 출력
# =========================================================
def fmt_dur(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def render(session_id: str, summary: dict) -> str:
    if not summary:
        return f"Session: {session_id}\n  (이벤트 없음)\n"

    out = [f"=== Session: {session_id} ==="]
    if summary.get("user_name"):
        out.append(f"User: {summary['user_name']}")
    out.append(f"Started: {summary['started'].isoformat(timespec='seconds')}")
    out.append(f"Ended:   {summary['ended'].isoformat(timespec='seconds')}")
    out.append(f"Total duration: {fmt_dur(summary['duration_sec'])}")
    out.append("")

    out.append("Stages:")
    for name, dur in summary["stages"]:
        out.append(f"  {name:25s} {fmt_dur(dur)}")
    out.append("")

    out.append("Button clicks:")
    for btn, ts, extra in summary["button_clicks"]:
        extra_str = ", ".join(f"{k}={v}" for k, v in extra.items() if k != "button")[:80]
        out.append(f"  [{ts.strftime('%H:%M:%S')}] {btn:30s} {extra_str}")
    out.append("")

    out.append("API usage:")
    total_cost = 0.0
    total_errors = 0
    for api, stats in summary["api_stats"].items():
        if stats["calls"] == 0:
            continue
        avg_ms = stats["duration_ms_total"] / stats["calls"]
        cost = estimate_cost(api, stats)
        total_cost += cost
        total_errors += stats["errors"]

        line = (f"  {api:15s} {stats['calls']:4d} calls, "
                f"avg {avg_ms/1000:5.1f}s, max {stats['duration_ms_max']/1000:5.1f}s, "
                f"errors {stats['errors']}")
        out.append(line)

        # 세부
        details = []
        if stats["total_tokens"]:
            details.append(f"tokens {stats['total_tokens']:,} "
                           f"(prompt {stats['prompt_tokens']:,} / "
                           f"completion {stats['completion_tokens']:,})")
        if stats["prompt_token_count"]:
            details.append(f"gemini tokens {stats['prompt_token_count']:,}")
        if stats["char_count"]:
            details.append(f"chars {stats['char_count']:,}")
        if stats["billed_seconds"]:
            details.append(f"billed {stats['billed_seconds']}s")
        details.append(f"est. ${cost:.4f}")
        out.append(f"      {' | '.join(details)}")

        for err in stats["error_messages"]:
            out.append(f"      ⚠ {err[:120]}")

    out.append("")
    out.append(f"Total estimated cost: ${total_cost:.4f}")
    out.append(f"Total API errors:     {total_errors}")
    out.append("")
    return "\n".join(out)


# =========================================================
# 엔트리
# =========================================================
def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="세션 events.jsonl 분석기")
    p.add_argument("target", type=Path,
                   help="세션 폴더, zip 파일, 또는 outputs/sessions/")
    p.add_argument("--json", action="store_true",
                   help="JSON으로 출력 (집계 스크립트용)")
    args = p.parse_args(argv)

    if not args.target.exists():
        print(f"경로 없음: {args.target}", file=sys.stderr)
        return 1

    found_any = False
    all_summaries = {}
    for sid, path in iter_event_files(args.target):
        found_any = True
        events = load_events(path)
        summary = analyze(events)
        all_summaries[sid] = summary
        if not args.json:
            print(render(sid, summary))

    if not found_any:
        print(f"events.jsonl을 찾지 못함: {args.target}", file=sys.stderr)
        return 1

    if args.json:
        # datetime은 isoformat 직렬화
        def _default(o):
            if isinstance(o, datetime):
                return o.isoformat()
            raise TypeError
        print(json.dumps(all_summaries, default=_default,
                         ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
