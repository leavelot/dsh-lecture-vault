"""ingest.py — 取文字：原生字幕优先，没有才下音频转写。

两层（写进代码，不靠模型记）：
  1. `subtitles`：B站 `x/player/v2` 的字幕轨。**空数组 = 真的没有**，不要再折腾 cookie。
  2. `audio` + `transcribe`：playurl 拿音频直链 → 串行下载 → ffprobe 校验 → faster-whisper。

长任务（转写）由宿主转后台：这里只负责按 `--job-id` 写进度文件。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from common import Config, JobStatus, LectureError, log, write_json

SUBTITLE_PREFERENCE = ("zh-CN", "zh-Hans", "zh", "zh-Hant", "zh-TW")


def run(config: Config, course_json: str, items: list[int] | None, modes: list[str] | None,
        dry_run: bool, job: JobStatus) -> dict[str, Any]:
    course_path = Path(course_json)
    if not course_path.exists():
        raise LectureError(
            f"找不到 course.json：{course_path}",
            next_steps=["先跑 lecture_probe 拿 courseJsonPath"],
            code="NO_COURSE_JSON",
        )
    course = json.loads(course_path.read_text(encoding="utf-8"))
    platform = str(course.get("platform") or "")
    parts = course.get("parts") or []
    if not parts:
        raise LectureError("course.json 里没有分P", code="NO_PARTS")

    wanted = _select_parts(parts, items)
    modes = [mode for mode in (modes or ["subtitles", "transcribe"]) if mode]
    session_dir = config.sessions_dir / _safe(course.get("title") or "course")
    subs_dir = session_dir / "subtitles"
    audio_dir = session_dir / "audio"
    text_dir = session_dir / "transcripts"

    if dry_run:
        plan = []
        for part in wanted:
            plan.append({
                "part": part["index"],
                "title": part["title"],
                "modes": modes,
                "sessionDir": str(session_dir),
                "subtitlesWillBeFetched": "subtitles" in modes,
                "audioWillBeDownloaded": "audio" in modes or "transcribe" in modes,
            })
        return {
            "ok": True,
            "summary": f"dry-run：{len(wanted)} 讲，模式 {', '.join(modes)}；没有下载、没有转写",
            "plan": plan,
            "warnings": [],
            "paths": {"sessionDir": str(session_dir), "courseJson": str(course_path)},
            "nextSteps": ["确认后去掉 dryRun 再跑一次"],
        }

    transcripts: list[dict[str, Any]] = []
    audios: list[dict[str, Any]] = []
    warnings: list[str] = []
    total = max(1, len(wanted))

    for position, part in enumerate(wanted):
        index = int(part["index"])
        base_percent = position / total * 100.0
        span = 100.0 / total
        job.update(percent=base_percent, message=f"第 {index} 讲：{part['title']}")
        stem = f"第{index:02d}讲 {_safe(part['title'])}"

        subtitle_info: dict[str, Any] | None = None
        if "subtitles" in modes:
            if platform == "bili" and part.get("cid"):
                subtitle_info = _fetch_bili_subtitles(config, course, index, subs_dir, stem, warnings)
            else:
                warnings.append(f"第 {index} 讲：{platform or '本地'} 没有原生字幕通道，直接转写")
        subtitle_ok = subtitle_info is not None
        if subtitle_ok:
            transcripts.append({"part": index, "source": "subtitle", **subtitle_info})

        if "transcribe" in modes and subtitle_ok:
            log(f"[ingest] 第 {index} 讲已有原生字幕，跳过转写（字幕优先）")

        if "transcribe" in modes and not subtitle_ok:
            audio_path = _ensure_audio(config, course, part, index, audio_dir, warnings)
            if audio_path:
                audios.append({"part": index, "path": str(audio_path)})
                from asr import transcribe  # noqa: PLC0415

                try:
                    result = transcribe(
                        config, audio_path, text_dir, stem, job=job,
                        percent_base=base_percent + span * 0.35, percent_span=span * 0.6,
                    )
                    transcripts.append({
                        "part": index,
                        "source": "asr",
                        "segments": result["segments"],
                        "duration_sec": result["duration_sec"],
                        "elapsed_sec": result["elapsed_sec"],
                        "device": result["device"],
                        **result["paths"],
                    })
                except LectureError as exc:
                    warnings.append(f"第 {index} 讲转写失败：{exc.reason}")
        elif "audio" in modes and not subtitle_ok:
            audio_path = _ensure_audio(config, course, part, index, audio_dir, warnings)
            if audio_path:
                audios.append({"part": index, "path": str(audio_path)})
        job.update(percent=base_percent + span, message=f"第 {index} 讲完成")

    summary = f"{len(wanted)} 讲：字幕 {sum(1 for t in transcripts if t.get('source') == 'subtitle')} 份、转写 {sum(1 for t in transcripts if t.get('source') == 'asr')} 份"
    return {
        "ok": bool(transcripts) or any(audios),
        "summary": summary if transcripts or audios else "没有产出任何文字或音频",
        "transcripts": transcripts,
        "audio": audios,
        "warnings": warnings,
        "paths": {"sessionDir": str(session_dir), "courseJson": str(course_path)},
        "nextSteps": [
            "检查 transcripts 里的 md 是否合理（转写原样，不修饰）",
            "接着跑 lecture_glossary mine 挖术语，纠错早于蒸馏",
        ],
    }


def _select_parts(parts: list[dict[str, Any]], items: list[int] | None) -> list[dict[str, Any]]:
    if not items:
        return parts
    wanted = []
    for index in items:
        match = next((part for part in parts if int(part.get("index") or 0) == int(index)), None)
        if not match:
            raise LectureError(f"第 {index} 讲不在 course.json 里", code="BAD_PART")
        wanted.append(match)
    return wanted


def _fetch_bili_subtitles(config: Config, course: dict[str, Any], index: int, subs_dir: Path,
                          stem: str, warnings: list[str]) -> dict[str, Any] | None:
    from platforms import bili  # noqa: PLC0415

    session = bili._session(config)  # noqa: SLF001 —— 同一个会话复用 cookie 与 UA
    try:
        tracks = bili.subtitle_list(config, course, index)
    except LectureError as exc:
        warnings.append(f"第 {index} 讲字幕清单失败：{exc.reason}")
        return None
    if not tracks:
        warnings.append(f"第 {index} 讲没有原生字幕（空数组就是没有，别再折腾 cookie）")
        return None

    def rank(track: dict[str, Any]) -> tuple[int, int]:
        lan = str(track.get("lan") or "")
        preferred = SUBTITLE_PREFERENCE.index(lan) if lan in SUBTITLE_PREFERENCE else len(SUBTITLE_PREFERENCE)
        return (0 if not track.get("ai") else 1, preferred)

    track = sorted(tracks, key=rank)[0]
    timeout_s = max(10.0, float(config["network"]["timeoutMs"]) / 1000.0)
    try:
        segments = bili.download_subtitle(session, track["url"], timeout_s)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"第 {index} 讲字幕下载失败：{type(exc).__name__}: {exc}")
        return None
    if not segments:
        warnings.append(f"第 {index} 讲字幕轨是空的（{track.get('lan')}）")
        return None

    subs_dir.mkdir(parents=True, exist_ok=True)
    md_path = subs_dir / f"{stem}.md"
    txt_path = subs_dir / f"{stem}.txt"
    jsonl_path = subs_dir / f"{stem}.jsonl"
    from asr import hhmmss  # noqa: PLC0415

    with md_path.open("w", encoding="utf-8") as md, txt_path.open("w", encoding="utf-8") as txt, \
            jsonl_path.open("w", encoding="utf-8") as jsonl:
        for segment in segments:
            md.write(f"[{hhmmss(segment['start'])}] {segment['text']}\n")
            txt.write(segment["text"] + "\n")
            jsonl.write(json.dumps(segment, ensure_ascii=False) + "\n")
    write_json(subs_dir / f"{stem}.meta.json", {"lan": track.get("lan"), "lan_doc": track.get("lan_doc"),
                                                "ai": track.get("ai"), "segments": len(segments)})
    log(f"[ingest] 第 {index} 讲字幕：{track.get('lan')}，{len(segments)} 段 -> {md_path}")
    return {
        "lan": track.get("lan"),
        "ai": bool(track.get("ai")),
        "segments": len(segments),
        "md": str(md_path),
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
    }


def _ensure_audio(config: Config, course: dict[str, Any], part: dict[str, Any], index: int,
                  audio_dir: Path, warnings: list[str]) -> Path | None:
    platform = str(course.get("platform") or "")
    if platform in ("local", "chaoxing"):
        path = Path(str(part.get("url") or ""))
        if path.exists():
            return path
        warnings.append(f"第 {index} 讲本地文件不存在：{path}")
        return None
    if platform != "bili":
        warnings.append(f"第 {index} 讲：{platform} 的音频通道还没实现")
        return None

    from platforms import bili  # noqa: PLC0415

    audio_dir.mkdir(parents=True, exist_ok=True)
    for existing in audio_dir.glob(f"第{index:02d}讲.*"):
        if existing.suffix.lower() in {".m4a", ".mp4", ".mp3", ".aac", ".webm"}:
            log(f"[ingest] 第 {index} 讲已有音频：{existing.name}")
            return existing
    session = bili._session(config)  # noqa: SLF001
    try:
        info = bili.audio_url(config, course, index)
        out_path = audio_dir / f"第{index:02d}讲.{'m4s' if info['kind'] == 'dash' else 'mp4'}"
        bili.download_audio(config, info["url"], out_path, session)
    except LectureError as exc:
        warnings.append(f"第 {index} 讲音频失败：{exc.reason}")
        return None
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"第 {index} 讲音频下载失败：{type(exc).__name__}: {exc}")
        return None

    # 串行下载 + ffprobe 校验：解码不了就是坏文件，别把坏音频丢给 ASR
    from platforms import local  # noqa: PLC0415

    try:
        duration, meta = local.ffprobe_duration(config, out_path)
        log(f"[ingest] ffprobe 校验通过：{out_path.name} {hhmm(duration)} codec={meta.get('audio_codec')}")
    except LectureError as exc:
        warnings.append(f"第 {index} 讲音频校验失败：{exc.reason}")
        return None
    return out_path


def hhmm(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _safe(text: str) -> str:
    import re

    return re.sub(r'[<>:"/\\|?*\r\n\t]+', "_", str(text or "part")).strip(" ._")[:80] or "part"
