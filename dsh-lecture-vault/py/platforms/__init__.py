"""platforms/__init__.py — 四个平台的接入点。

v1 的边界（写进代码而不是只写文档）：
  - bili：自研 HTTP（view → player/v2 字幕 → playurl 音频），412 降速重试
  - youtube：yt-dlp，代理只能来自配置；不通就明说「需代理」
  - dingtalk：只接受用户给的签名 m3u8（不做录屏兜底）
  - local：本地文件 / 课件目录，最简单也最稳
  - chaoxing（学习通）：**没有抓取代码**，只走 local 导入
"""

from __future__ import annotations

import re
from typing import Optional

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
BILI_URL_RE = re.compile(r"bilibili\.com/(?:video/)?(BV[0-9A-Za-z]{10})", re.I)
YT_RE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([0-9A-Za-z_-]{6,})", re.I)
M3U8_RE = re.compile(r"^https?://\S+\.m3u8(?:\?\S*)?$", re.I)


def detect_kind(raw: str) -> str:
    """auto 探测：bili / youtube / hls / file。"""
    text = (raw or "").strip()
    if not text:
        return "file"
    if BILI_URL_RE.search(text) or BV_RE.fullmatch(text):
        return "bili"
    if YT_RE.search(text):
        return "youtube"
    if M3U8_RE.match(text) or ".m3u8" in text.lower():
        return "hls"
    return "file"


def extract_bvid(raw: str) -> Optional[str]:
    text = (raw or "").strip()
    match = BILI_URL_RE.search(text) or BV_RE.search(text)
    return match.group(1) if match else None
