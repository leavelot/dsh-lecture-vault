"""asr.py — 本地转写（faster-whisper）。

GPU 用配置里的 model / compute（large-v3-turbo / int8_float16）；CUDA 不可用时自动退
CPU + cpuModel（small），并在结果里说明代价（大约慢一个数量级）。

输出三份，下游分工明确：
  - `.md`  带 `[HH:MM:SS]` 前缀，给 LLM 读、给讲义挂时间戳
  - `.txt` 纯文本，通读用
  - `.jsonl` 每行 `{start,end,text}`，给 lint / glossary 精确定位
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from common import Config, LectureError, log


def _activate_cuda(config: Config) -> None:
    """把 pip 装的 nvidia-* DLL 目录挂上，否则 ctranslate2 起不了 CUDA。"""
    venv_root = Path(config.python_exe).parent.parent
    for directory in glob.glob(str(venv_root / "Lib" / "site-packages" / "nvidia" / "*" / "bin")):
        try:
            os.add_dll_directory(directory)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")


def cuda_available(config: Config) -> bool:
    _activate_cuda(config)
    try:
        import ctranslate2  # noqa: PLC0415

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        return False


def model_dir(config: Config) -> Path:
    return config.asr_model_dir()


def hhmmss(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def transcribe(config: Config, audio_path: Path, out_dir: Path, stem: str,
               job: Any | None = None, percent_base: float = 0.0, percent_span: float = 100.0) -> dict[str, Any]:
    from faster_whisper import WhisperModel  # noqa: PLC0415

    directory = model_dir(config)
    if not (directory / "model.bin").exists():
        raise LectureError(
            f"没有 Whisper 权重：{directory}",
            next_steps=["跑 scripts/install-env.ps1 拉权重，或 scripts/fetch-model.mjs（网络差时）"],
            code="NO_MODEL",
        )

    use_gpu = cuda_available(config)
    model_name = str(config["asr"]["model"]) if use_gpu else str(config["asr"]["cpuModel"])
    compute = str(config["asr"]["compute"]) if use_gpu else "int8"
    device = "cuda" if use_gpu else "cpu"

    if use_gpu:
        model_path: str | Path = directory
    else:
        # CPU 时模型名交给 faster-whisper 自己找（HF 缓存里可能只有 GPU 那份）；找不到就用现有目录
        model_path = directory if (directory / "model.bin").exists() else model_name

    log(f"[asr] device={device} compute={compute} model={model_path}")
    started = time.time()
    model = WhisperModel(str(model_path), device=device, compute_type=compute)
    segments_iter, info = model.transcribe(
        str(audio_path),
        language="zh",
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    total = float(getattr(info, "duration", 0.0) or 0.0)
    segments: list[dict[str, Any]] = []
    for segment in segments_iter:
        text = (segment.text or "").strip()
        if not text:
            continue
        segments.append({"start": round(float(segment.start), 2), "end": round(float(segment.end), 2), "text": text})
        if job is not None and total > 0:
            ratio = min(1.0, float(segment.end) / total)
            job.update(percent=percent_base + percent_span * ratio, message=f"转写 {hhmmss(segment.end)}/{hhmmss(total)}")

    if not segments:
        raise LectureError(
            f"{audio_path.name} 没转出任何文字（音轨可能是静音或纯音乐）",
            next_steps=["换一讲试；或用 lecture_ingest 的 subtitles 模式看有没有原生字幕"],
            code="EMPTY_TRANSCRIPT",
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{stem}.md"
    txt_path = out_dir / f"{stem}.txt"
    jsonl_path = out_dir / f"{stem}.jsonl"
    with md_path.open("w", encoding="utf-8") as md, txt_path.open("w", encoding="utf-8") as txt, \
            jsonl_path.open("w", encoding="utf-8") as jsonl:
        for segment in segments:
            md.write(f"[{hhmmss(segment['start'])}] {segment['text']}\n")
            txt.write(segment["text"] + "\n")
            jsonl.write(json.dumps(segment, ensure_ascii=False) + "\n")

    elapsed = time.time() - started
    log(f"[asr] {stem}：{len(segments)} 段，耗时 {elapsed:.0f}s（音频 {hhmmss(total)}）")
    return {
        "ok": True,
        "stem": stem,
        "segments": len(segments),
        "duration_sec": int(total),
        "elapsed_sec": int(elapsed),
        "device": device,
        "compute": compute,
        "model": str(model_path),
        "paths": {"md": str(md_path), "txt": str(txt_path), "jsonl": str(jsonl_path)},
    }
