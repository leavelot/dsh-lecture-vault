"""probe.py — 把输入解析成统一中间产物 course.json（唯一事实源）。

各平台都写进同一份结构，下游工具不再重新探测平台：

    {platform, input, title, uploader, published, duration_sec,
     parts:[{index,title,duration_sec,cid,url}], subtitles:{}, grabbed_at}
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from common import Config, LectureError, log, write_json
from platforms import detect_kind, extract_bvid

SAFE_RE = re.compile(r'[<>:"/\\|?*\r\n\t]+')


def safe_name(text: str, fallback: str = "course") -> str:
    cleaned = SAFE_RE.sub("_", (text or "").strip())
    cleaned = cleaned.strip(" ._")
    return cleaned[:80] or fallback


def course_cache_path(config: Config, course: dict[str, Any]) -> Path:
    return config.cache_dir / safe_name(str(course.get("title") or course.get("input") or "course")) / "course.json"


def run(config: Config, raw_input: str, kind: str = "auto") -> dict[str, Any]:
    text = (raw_input or "").strip()
    if not text:
        raise LectureError("input 不能为空", next_steps=["给 BV 号 / 视频链接 / m3u8 / 本地文件路径"], code="BAD_INPUT")

    resolved = detect_kind(text) if kind in (None, "", "auto") else kind
    warnings: list[str] = []

    if resolved == "bili":
        bvid = extract_bvid(text)
        if not bvid:
            raise LectureError(f"认不出 BV 号：{text}", code="BAD_BVID")
        from platforms import bili  # noqa: PLC0415

        course = bili.probe(config, bvid)
    elif resolved == "file":
        from platforms import local  # noqa: PLC0415

        course = local.probe_file(config, text)
    elif resolved == "youtube":
        raise LectureError(
            "YouTube 通道在 P2 实现；本机直连 youtube.com 超时",
            next_steps=[
                "先在插件配置里设 youtube.proxy（如 http://127.0.0.1:7890），再跑 lecture_doctor 确认可达",
                "或者自己下好视频/音频，用本地文件路径走 file 通道",
            ],
            code="NOT_IMPLEMENTED",
        )
    elif resolved == "hls":
        raise LectureError(
            "HLS（钉钉回放）通道在 P2 实现",
            next_steps=["先把该回放用录屏/下载工具落成本地文件，走 file 通道"],
            code="NOT_IMPLEMENTED",
        )
    else:
        raise LectureError(f"不支持的输入类型：{resolved}", code="BAD_KIND")

    path = write_json(course_cache_path(config, course), course)
    log(f"[probe] course.json -> {path}")

    table = _parts_table(course)
    total_min = int(round(float(course.get("duration_sec") or 0) / 60))
    summary = (
        f"{course.get('title')} · {course.get('platform')} · {course.get('uploader') or '作者未知'} · "
        f"{len(course.get('parts') or [])} 讲 / 共 {total_min} 分钟"
    )
    return {
        "ok": True,
        "summary": summary,
        "items": [table],
        "course": course,
        "warnings": warnings,
        "paths": {"courseJson": str(path)},
        "nextSteps": [
            "把上面的分P 表给用户看，停下来问读哪几讲（不要替用户决定）",
            f"确认后用 lecture_ingest 传 courseJsonPath：{path}",
        ],
    }


def _parts_table(course: dict[str, Any]) -> str:
    parts = course.get("parts") or []
    lines = ["| 序号 | 标题 | 时长 |", "|---|---|---|"]
    for part in parts:
        minutes = int(round(int(part.get("duration_sec") or 0) / 60))
        lines.append(f"| {part.get('index')} | {part.get('title')} | {minutes} 分钟 |")
    return "\n".join(lines)
