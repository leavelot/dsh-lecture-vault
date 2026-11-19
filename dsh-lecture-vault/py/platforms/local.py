"""platforms/local.py — 本地文件与课件目录。

学习通、网盘、手动下载的课都从这里进：不抓取、不刷课时、不碰任何平台限制。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from common import Config, LectureError, log

MEDIA_SUFFIXES = {".mp4", ".mkv", ".flv", ".mov", ".webm", ".m4a", ".mp3", ".wav", ".aac", ".flac", ".ts"}


def ffprobe_duration(config: Config, path: Path) -> tuple[float, dict[str, Any]]:
    ffprobe = Path(config.raw["ffmpegPath"]).with_name("ffprobe.exe")
    if not ffprobe.exists():
        raise LectureError(f"找不到 ffprobe（应在 {ffprobe}）", code="NO_FFPROBE")
    code, out = _run([
        str(ffprobe), "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name", "-of", "json", str(path),
    ])
    if code != 0:
        raise LectureError(f"ffprobe 读不了 {path.name}：{out.strip()[:200]}", code="FFPROBE_FAILED")
    import json as _json

    payload = _json.loads(out or "{}")
    duration = float((payload.get("format") or {}).get("duration") or 0.0)
    streams = payload.get("streams") or []
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if duration <= 0:
        raise LectureError(f"{path.name} 的时长是 0，可能下载不完整或不是媒体文件", code="BAD_MEDIA")
    return duration, {"audio_codec": (audio or {}).get("codec_name", ""), "streams": len(streams)}


def probe_file(config: Config, raw_path: str) -> dict[str, Any]:
    path = Path(raw_path).expanduser()
    if not path.exists():
        raise LectureError(
            f"找不到本地文件：{path}",
            next_steps=["确认路径是绝对路径；学习通的视频/课件要先自己下载到本机"],
            code="NO_FILE",
        )
    if path.is_dir():
        media = sorted(item for item in path.iterdir() if item.suffix.lower() in MEDIA_SUFFIXES)
        if not media:
            raise LectureError(
                f"{path} 里没有媒体文件（支持 {', '.join(sorted(MEDIA_SUFFIXES))}）",
                next_steps=["如果这是课件目录，用 lecture_courseware 解析，不要用 probe"],
                code="NO_MEDIA",
            )
        parts = []
        total = 0.0
        for index, item in enumerate(media, start=1):
            duration, _meta = ffprobe_duration(config, item)
            total += duration
            parts.append({
                "index": index,
                "title": item.stem,
                "duration_sec": int(round(duration)),
                "cid": "",
                "url": str(item),
            })
        title = path.name
    else:
        duration, _meta = ffprobe_duration(config, path)
        total = duration
        parts = [{
            "index": 1,
            "title": path.stem,
            "duration_sec": int(round(duration)),
            "cid": "",
            "url": str(path),
        }]
        title = path.stem

    import time as _time

    log(f"[local] {title}：{len(parts)} 讲，总时长 {int(total // 60)} 分钟")
    return {
        "platform": "local",
        "input": str(path),
        "title": title,
        "uploader": "",
        "published": _time.strftime("%Y-%m-%d"),
        "duration_sec": int(round(total)),
        "parts": parts,
        "subtitles": {},
        "grabbed_at": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _run(cmd: list[str], timeout: float = 30.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return -1, "not found"
    except subprocess.TimeoutExpired:
        return -2, "timeout"
