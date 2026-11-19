"""vault_writer.py — 写 Obsidian vault 的唯一入口（白名单 / marker / 原子替换 / .bak）。

规则（违反任何一条都算 bug）：
  1. 只写白名单目录，vault 其它区域只读；不删任何文件；
  2. 只替换 `<!-- dsh:auto:begin --> … <!-- dsh:auto:end -->` 之间的内容；
  3. 覆盖前把原文件复制到 `90_附件/.bak/<日期>/`，保留 `backupKeepDays` 天；
  4. 写入走「临时文件 → os.replace」，不留半截文件；
  5. 先算 diff，`dry_run=True` 时一个字节都不落盘。
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common import Config, LectureError, log

AUTO_BEGIN = "<!-- dsh:auto:begin -->"
AUTO_END = "<!-- dsh:auto:end -->"
FRONTMATTER_RE = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n?", re.M)


@dataclass
class WritePlan:
    path: Path
    content: str
    action: str  # create | replace-auto | append-auto | unchanged
    old: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def diff(self) -> str:
        if self.action == "create":
            body = "\n".join("+" + line for line in self.content.splitlines()[:400])
            return f"--- /dev/null\n+++ {self.path}\n{body}"
        if self.old is None or self.old == self.content:
            return ""
        diff = difflib.unified_diff(
            self.old.splitlines(), self.content.splitlines(),
            fromfile=str(self.path), tofile=str(self.path), lineterm="", n=2,
        )
        return "\n".join(diff)


class Vault:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.root = config.vault_path
        if not self.root.exists():
            raise LectureError(f"vault 不存在：{self.root}", next_steps=["检查配置里的 vaultPath"], code="NO_VAULT")
        if not (self.root / ".obsidian").is_dir():
            raise LectureError(
                f"{self.root} 里没有 .obsidian，拒绝写入",
                next_steps=["确认这是一个 Obsidian vault，或把 vaultPath 指到正确的目录"],
                code="NOT_A_VAULT",
            )
        self.allowed = [str(name) for name in config["vault"]["allowedDirs"]]

    # ---------------------------------------------------------------- 白名单
    def ensure_allowed(self, relative: Path) -> Path:
        parts = Path(relative).parts
        if not parts or parts[0] not in self.allowed:
            raise LectureError(
                f"拒绝写白名单外的路径：{relative}",
                next_steps=[f"白名单：{'、'.join(self.allowed)}"],
                code="OUTSIDE_WHITELIST",
            )
        target = (self.root / relative).resolve()
        root = self.root.resolve()
        if not str(target).startswith(str(root)):
            raise LectureError(f"路径逃出了 vault：{relative}", code="PATH_ESCAPE")
        return target

    def backup(self, target: Path) -> Path | None:
        if not target.exists():
            return None
        day = time.strftime("%Y%m%d")
        backup_dir = self.root / "90_附件" / ".bak" / day
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / target.name
        if backup_path.exists():
            backup_path = backup_dir / f"{target.stem}-{int(time.time())}{target.suffix}"
        shutil.copy2(target, backup_path)
        self._prune_backups()
        return backup_path

    def _prune_backups(self) -> None:
        keep_days = int(self.config["vault"].get("backupKeepDays") or 30)
        cutoff = time.time() - keep_days * 86400
        backup_root = self.root / "90_附件" / ".bak"
        if not backup_root.is_dir():
            return
        for child in backup_root.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue

    # ---------------------------------------------------------------- 写入
    def write(self, plan: WritePlan, dry_run: bool) -> dict[str, Any]:
        if plan.action == "unchanged":
            return {"path": str(plan.path), "action": "unchanged", "backup": None}
        if dry_run:
            return {"path": str(plan.path), "action": f"dry-run:{plan.action}", "backup": None}
        plan.path.parent.mkdir(parents=True, exist_ok=True)
        backup = self.backup(plan.path)
        tmp = plan.path.with_suffix(plan.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(plan.content)
        os.replace(tmp, plan.path)
        log(f"[vault] {plan.action} -> {plan.path}" + (f"（备份 {backup.name}）" if backup else ""))
        return {"path": str(plan.path), "action": plan.action, "backup": str(backup) if backup else None}


# ---------------------------------------------------------------- 文本工具


def split_frontmatter(text: str) -> tuple[str, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return "", text
    return match.group(0), text[match.end():]


def split_markers(body: str) -> tuple[str, str, str] | None:
    """(标记前, 标记内, 标记后)；没有标记就返回 None。"""
    start = body.find(AUTO_BEGIN)
    end = body.find(AUTO_END)
    if start < 0 or end < 0 or end < start:
        return None
    return body[:start], body[start + len(AUTO_BEGIN):end], body[end + len(AUTO_END):]


def wrap_markers(inner: str) -> str:
    return f"{AUTO_BEGIN}\n{inner.strip()}\n{AUTO_END}"


def coerce_yaml_scalar(key: str, value: str) -> tuple[str, list[str]]:
    """把 front-matter 里的「看起来是字符串的日期/布尔/数字」改回 YAML 原生类型。

    写成字符串会让 Bases / Dataview 的筛选全废（这是硬要求，不是洁癖）。
    """
    notes: list[str] = []
    raw = value.strip()
    if raw.startswith('"') or raw.startswith("'"):
        inner = raw.strip('"\'')
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", inner):
            notes.append(f"{key}: 日期去引号（原生 date）")
            return inner, notes
        if inner.lower() in ("true", "false"):
            notes.append(f"{key}: 布尔去引号")
            return inner.lower(), notes
        if re.fullmatch(r"-?\d+(\.\d+)?", inner):
            notes.append(f"{key}: 数字去引号")
            return inner, notes
    if raw in ("True", "False"):
        notes.append(f"{key}: 布尔改小写")
        return raw.lower(), notes
    if re.fullmatch(r"\[[^\]]*\]", raw) and not raw.startswith('["') and '",' in raw:
        notes.append(f"{key}: 列表里有中文逗号或多余引号，已按 [a, b] 规范化")
    return raw, notes


def normalize_frontmatter(text: str) -> tuple[str, list[str]]:
    block, rest = split_frontmatter(text)
    if not block:
        return text, ["源文件没有 front-matter，已原样保留（commit 不会替它编属性）"]
    lines = block.splitlines()
    inner = lines[1:-1] if len(lines) >= 2 else []
    notes: list[str] = []
    out: list[str] = ["---"]
    for line in inner:
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not match:
            out.append(line)
            continue
        key, value = match.group(1), match.group(2)
        if value == "":
            out.append(f"{key}:")
            continue
        coerced, more = coerce_yaml_scalar(key, value)
        notes.extend(more)
        out.append(f"{key}: {coerced}")
    out.append("---")
    return "\n".join(out) + "\n" + rest.lstrip("\n"), notes


def wiki_links(body: str) -> list[str]:
    names = []
    for match in re.finditer(r"\[\[([^\]|#^]+)(?:[|#^][^\]]*)?\]\]", body):
        name = match.group(1).strip()
        if name and name not in names:
            names.append(name)
    return names
