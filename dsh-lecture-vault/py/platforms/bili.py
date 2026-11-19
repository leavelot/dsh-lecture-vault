"""platforms/bili.py — B站：view（分P）→ player/v2（字幕）→ playurl（音频）。

412 是常态（网页端风控 / TLS 指纹）：按配置的 delayMs 降速重试一次，再失败就把
原因和下一步交回去，不静默换通道、不急着上 cookie。cookie 只从环境变量读。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from common import Config, LectureError, log, write_json

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.bilibili.com",
}


def _session(config: Config) -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    cookie_name = str(config["bili"].get("cookieEnv") or "BILI_SESSDATA")
    token = None
    import os

    token = os.environ.get(cookie_name) or os.environ.get("BILI_COOKIE")
    if token:
        # 只写 SESSDATA 这一项，不落盘、不进日志
        session.cookies.set("SESSDATA", token, domain=".bilibili.com")
        log(f"[bili] 使用环境变量 {cookie_name} 里的登录态")
    return session


def _get_json(config: Config, session: requests.Session, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    timeout_s = max(5.0, float(config["network"]["timeoutMs"]) / 1000.0)
    attempts = max(1, int(config["bili"]["maxRetry"]) + 1)
    delay = max(0.3, float(config["bili"]["delayMs"]) / 1000.0)
    last: str = ""
    for attempt in range(attempts):
        try:
            response = session.get(url, params=params, timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            log(f"[bili] 请求失败（第 {attempt + 1}/{attempts} 次）：{last}")
            time.sleep(delay)
            continue
        if response.status_code == 412:
            last = "HTTP 412（风控）"
            log(f"[bili] 412 风控（第 {attempt + 1}/{attempts} 次），降速重试")
            time.sleep(delay * (attempt + 1))
            continue
        if response.status_code != 200:
            last = f"HTTP {response.status_code}"
            time.sleep(delay)
            continue
        try:
            payload = response.json()
        except ValueError:
            last = "返回的不是 JSON（可能被风控页拦截）"
            time.sleep(delay)
            continue
        code = payload.get("code")
        if code == 0:
            return payload
        last = f"code={code} message={payload.get('message')}"
        if code in (-412, -352, -401):
            time.sleep(delay * (attempt + 1))
            continue
        break
    raise LectureError(
        f"B站接口拿不到数据：{last}",
        next_steps=[
            "412 属常态：隔几分钟重试；仍失败就给本机配 BILI_SESSDATA 环境变量（只从环境变量读）",
            "链接确认是公开视频（付费/会员课需要你已登录的 cookie）",
        ],
        code="BILI_HTTP",
    )


def probe(config: Config, bvid: str) -> dict[str, Any]:
    """分P 表 + 标题/作者/时长。返回统一中间产物（course dict）。"""
    session = _session(config)
    payload = _get_json(config, session, "https://api.bilibili.com/x/web-interface/view", {"bvid": bvid})
    data = payload.get("data") or {}
    pages = data.get("pages") or []
    title = str(data.get("title") or bvid)
    pages_out = []
    if pages:
        for page in pages:
            pages_out.append({
                "index": int(page.get("page") or len(pages_out) + 1),
                "title": str(page.get("part") or title),
                "duration_sec": int(page.get("duration") or 0),
                "cid": str(page.get("cid") or ""),
                "url": f"https://www.bilibili.com/video/{bvid}" + (f"?p={page.get('page')}" if len(pages) > 1 else ""),
            })
    else:
        pages_out.append({
            "index": 1,
            "title": title,
            "duration_sec": int(data.get("duration") or 0),
            "cid": "",
            "url": f"https://www.bilibili.com/video/{bvid}",
        })
    total = sum(item["duration_sec"] for item in pages_out)
    return {
        "platform": "bili",
        "input": bvid,
        "title": title,
        "uploader": str((data.get("owner") or {}).get("name") or ""),
        "published": time.strftime("%Y-%m-%d", time.localtime(int(data.get("pubdate") or 0))) if data.get("pubdate") else "",
        "duration_sec": total,
        "parts": pages_out,
        "subtitles": {},
        "grabbed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "aid": str(data.get("aid") or ""),
        "bvid": bvid,
        "desc": str(data.get("desc") or ""),
        "cover": str(data.get("pic") or ""),
    }


def subtitle_list(config: Config, course: dict[str, Any], part_index: int) -> list[dict[str, Any]]:
    """原生字幕轨清单。空数组就是真的没有，不要再去折腾 cookie。"""
    part = _part(course, part_index)
    session = _session(config)
    payload = _get_json(
        config,
        session,
        "https://api.bilibili.com/x/player/v2",
        {"bvid": course.get("bvid") or course.get("input"), "cid": part.get("cid") or ""},
    )
    subtitles = ((payload.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    out = []
    for item in subtitles:
        url = str(item.get("subtitle_url") or "")
        if url.startswith("//"):
            url = "https:" + url
        out.append({
            "lan": str(item.get("lan") or ""),
            "lan_doc": str(item.get("lan_doc") or ""),
            "url": url,
            "ai": bool(item.get("ai_type")) or "ai" in str(item.get("lan") or ""),
        })
    log(f"[bili] 第 {part_index} 讲字幕轨：{[s['lan'] for s in out] or '无'}")
    return out


def download_subtitle(session: requests.Session, url: str, timeout_s: float) -> list[dict[str, Any]]:
    response = session.get(url, timeout=timeout_s, headers={"Referer": "https://www.bilibili.com/"})
    response.raise_for_status()
    payload = response.json()
    body = payload.get("body") or []
    segments = []
    for item in body:
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        segments.append({
            "start": float(item.get("from") or 0.0),
            "end": float(item.get("to") or 0.0),
            "text": content,
        })
    return segments


def audio_url(config: Config, course: dict[str, Any], part_index: int) -> dict[str, Any]:
    """playurl 取音频直链（dash 优先，回退 durl），下载时仍需 Referer。"""
    part = _part(course, part_index)
    session = _session(config)
    payload = _get_json(
        config,
        session,
        "https://api.bilibili.com/x/player/playurl",
        {"bvid": course.get("bvid") or course.get("input"), "cid": part.get("cid") or "", "fnval": 16, "fourk": 1},
    )
    data = payload.get("data") or {}
    dash = data.get("dash") or {}
    audios = dash.get("audio") or []
    if audios:
        best = max(audios, key=lambda item: int(item.get("bandwidth") or 0))
        return {"url": str(best.get("baseUrl") or best.get("base_url") or ""), "kind": "dash", "codec": str(best.get("codecs") or "")}
    durl = data.get("durl") or []
    if durl:
        return {"url": str(durl[0].get("url") or ""), "kind": "durl", "codec": "mp4"}
    raise LectureError(
        f"第 {part_index} 讲拿不到音频直链（可能需要在播页面/登录态）",
        next_steps=["确认该视频可公开播放", "付费/会员课需要 BILI_SESSDATA 环境变量里的登录态"],
        code="BILI_NO_AUDIO",
    )


def download_audio(config: Config, url: str, out_path: Path, session: requests.Session) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    timeout_s = max(10.0, float(config["network"]["timeoutMs"]) / 1000.0)
    headers = {"Referer": "https://www.bilibili.com/", "User-Agent": UA}
    with session.get(url, headers=headers, stream=True, timeout=timeout_s) as response:
        response.raise_for_status()
        with out_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                if chunk:
                    handle.write(chunk)
    log(f"[bili] 已下载 {out_path.name}（{out_path.stat().st_size / 1048576:.1f} MB）")
    return out_path


def _part(course: dict[str, Any], part_index: int) -> dict[str, Any]:
    for part in course.get("parts") or []:
        if int(part.get("index") or 0) == int(part_index):
            return part
    raise LectureError(f"course.json 里没有第 {part_index} 讲", code="BAD_PART")
