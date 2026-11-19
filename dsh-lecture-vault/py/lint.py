"""lint.py — 讲义校验：只报告，不改文件。

四类问题（对应工具参数 checks）：
  - timestamp：六节里每个列表项必须以 `HH:MM:SS` 结尾（`MM:SS` 不合格，自然段豁免）
  - fx：公式区残留 LaTeX / HTML（ASCII 记法是允许的，`e^{ikx}` 不算问题）
  - links：`[[链接]]` 指向的笔记在 vault 里不存在
  - orphan：vault 里没有任何笔记引用的页面
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from common import LectureError, log

SECTIONS = ("## 概念", "## 公式", "## 推导", "## 例题", "## 易混与坑", "## 考点")
LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
# 列表项结尾允许三种写法：裸 `00:12:34`、反引号包裹、或可点回链 `[00:12:34](https://…?t=754)`
TS_END_RE = re.compile(
    r"(\[\d{1,2}:\d{2}:\d{2}\]\([^)\s]+\)|\b\d{1,2}:\d{2}:\d{2}\b`?)\s*[）)]?\s*$"
)
LATEX_RE = re.compile(r"(\$[^$\n]+\$|\\[a-zA-Z]{2,}\{|\\[a-zA-Z]{2,}\b|\\begin\{|\\end\{)")
HTML_RE = re.compile(r"</?(?:sub|sup|b|i|span|div|br|font|code)\b[^>]*>|&(?:nbsp|amp|lt|gt);")
LINK_RE = re.compile(r"\[\[([^\]|#^]+)(?:[|#^][^\]]*)?\]\]")


def run(target_path: str, checks: list[str] | None) -> dict[str, Any]:
    target = Path(target_path)
    if not target.exists():
        raise LectureError(f"找不到目标：{target}", code="NO_TARGET")
    wanted = set(checks or ["timestamp", "fx", "links", "orphan"])
    files = [target] if target.is_file() else sorted(target.rglob("*.md"))
    if not files:
        raise LectureError(f"{target} 里没有 .md 文件", code="NO_NOTES")

    issues: list[dict[str, Any]] = []
    if "timestamp" in wanted:
        issues.extend(_check_timestamps(files))
    if "fx" in wanted:
        issues.extend(_check_formulas(files))
    if "links" in wanted or "orphan" in wanted:
        # 双链目标与「谁引用了谁」都要按**整个 vault** 算：
        # 只检查 10_课程/某课 的话，20_概念 里的概念页会被当成不存在。
        universe = _note_universe(target, files)
        names, inbound = _index_links(universe)
        if "links" in wanted:
            issues.extend(_check_links(files, names))
        if "orphan" in wanted:
            issues.extend(_check_orphans(files, inbound))

    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue["check"]] = counts.get(issue["check"], 0) + 1
    summary = f"{len(files)} 个文件 · 问题 {len(issues)} 条" + (
        "（" + "、".join(f"{key} {value}" for key, value in sorted(counts.items())) + "）" if counts else "")
    log(f"[lint] {summary}")
    return {
        "ok": len(issues) == 0,
        "summary": summary,
        "issues": issues,
        "warnings": [] if issues else [],
        "paths": {"target": str(target)},
        "nextSteps": ["按 file/line 回去补时间戳或改公式记法"] if issues else ["可以落盘了：lecture_commit（先 dryRun）"],
    }


def _check_timestamps(files: list[Path]) -> list[dict[str, Any]]:
    issues = []
    for path in files:
        section = ""
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("## "):
                section = next((name for name in SECTIONS if stripped.startswith(name)), "")
                continue
            if not section or not LIST_RE.match(line):
                continue
            if TS_END_RE.search(stripped):
                continue
            if stripped.startswith(">"):
                continue
            issues.append({
                "check": "timestamp", "file": str(path), "line": number,
                "message": f"{section} 的列表项没有以 HH:MM:SS 结尾",
                "snippet": stripped[:80],
            })
    return issues


def _check_formulas(files: list[Path]) -> list[dict[str, Any]]:
    issues = []
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if LATEX_RE.search(line):
                issues.append({
                    "check": "fx", "file": str(path), "line": number,
                    "message": "有 LaTeX 记法，改成 ASCII（例如 L̂_z、e^{ikx}）",
                    "snippet": line.strip()[:80],
                })
            elif HTML_RE.search(line):
                issues.append({
                    "check": "fx", "file": str(path), "line": number,
                    "message": "有 HTML 标签/实体，改成纯文本",
                    "snippet": line.strip()[:80],
                })
    return issues


def _note_universe(target: Path, files: list[Path]) -> list[Path]:
    """双链解析的搜索范围：vault 根（含 .obsidian 的那层）下的全部笔记。

    找不到 vault 标记就退回「目标所在目录的父级」，至少别只在一个子目录里找。
    """
    anchor = target if target.is_dir() else target.parent
    for candidate in [anchor, *anchor.parents]:
        if (candidate / ".obsidian").is_dir():
            universe = sorted(candidate.rglob("*.md"))[:5000]
            return universe or files
    fallback = sorted(anchor.rglob("*.md"))
    return fallback or files


def _index_links(files: list[Path]) -> tuple[set[str], dict[str, int]]:
    names = {path.stem for path in files}
    inbound: dict[str, int] = {name: 0 for name in names}
    for path in files:
        for match in LINK_RE.finditer(path.read_text(encoding="utf-8")):
            target = match.group(1).strip()
            inbound[target] = inbound.get(target, 0) + 1
    return names, inbound


def _check_links(files: list[Path], names: set[str]) -> list[dict[str, Any]]:
    issues = []
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for match in LINK_RE.finditer(line):
                target = match.group(1).strip()
                if target in names or target in ("", "…"):
                    continue
                issues.append({
                    "check": "links", "file": str(path), "line": number,
                    "message": f"双链指向的笔记不存在：[[{target}]]",
                    "snippet": line.strip()[:80],
                })
    return issues


def _check_orphans(files: list[Path], inbound: dict[str, int]) -> list[dict[str, Any]]:
    issues = []
    for path in files:
        if inbound.get(path.stem, 0) > 0:
            continue
        issues.append({
            "check": "orphan", "file": str(path), "line": 1,
            "message": "没有任何笔记引用它（孤儿页）",
            "snippet": path.stem,
        })
    return issues
