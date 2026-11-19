"""commit.py — 把写完的讲义按规范落进 vault。

输入是模型写好的讲义文件（front-matter + 六节正文），这里只做**确定性**的事：
  - front-matter 归一（日期/布尔/数字必须是 YAML 原生类型）
  - 正文包进 `<!-- dsh:auto:begin/end -->`，重跑只替换标记之间
  - 正文里的 `[[概念]]` 若笔记不存在，同时建 stub（避免 Graph 里断链）
  - 更新课程 MOC（也只在 marker 内追加）
  - 覆盖前 .bak，写入原子替换；`dryRun` 只出 diff
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from common import Config, LectureError, log
from vault_writer import (
    Vault, WritePlan, normalize_frontmatter, split_frontmatter, split_markers,
    wiki_links, wrap_markers,
)

CONCEPT_TEMPLATE = """---
type: concept
course: {course}
status: stub
prereq: []
aliases: []
tags: [概念/{course}]
review_due: {review_due}
---

# {name}

<!-- dsh:auto:begin -->
一句话定义：
出现于：{sources}
易错：
<!-- dsh:auto:end -->

## 我的理解
"""

MOC_TEMPLATE = """---
type: {moc_type}
course: {course}
status: growing
---

# {course} MOC

<!-- dsh:auto:begin -->
## 讲次
<!-- dsh:auto:end -->
"""


def run(config: Config, vault_path: str | None, course: str, lectures: list[str],
        dry_run: bool, job: Any) -> dict[str, Any]:
    if not course.strip():
        raise LectureError("course 不能为空（它同时是 10_课程 下的目录名与 MOC 名）", code="BAD_COURSE")
    if not lectures:
        raise LectureError("lectures 不能为空", next_steps=["传写完的讲义 md 绝对路径"], code="NO_LECTURES")

    vault = Vault(config) if not vault_path else Vault(_with_vault_path(config, vault_path))
    plans: list[WritePlan] = []
    warnings: list[str] = []
    lecture_names: list[str] = []

    for index, raw_path in enumerate(lectures, start=1):
        source = Path(raw_path)
        if not source.exists():
            raise LectureError(f"讲义文件不存在：{source}", code="NO_LECTURE_FILE")
        text = source.read_text(encoding="utf-8")
        normalized, notes = normalize_frontmatter(text)
        warnings.extend(f"{source.name}：{note}" for note in notes)
        block, body = split_frontmatter(normalized)
        if not block:
            raise LectureError(f"{source.name} 没有 front-matter，无法保证属性规范", code="NO_FRONTMATTER")

        stem = source.stem
        lecture_names.append(stem)
        relative = Path("10_课程") / course / f"{stem}.md"
        target = vault.ensure_allowed(relative)
        old = target.read_text(encoding="utf-8") if target.exists() else None

        plan = _plan_lecture(target, old, block, body)
        plans.append(plan)
        job.update(percent=index / max(1, len(lectures)) * 60, message=f"排好 {stem}")

        # 概念 stub：只补不存在的，已存在的概念笔记一个字都不动
        for name in wiki_links(body):
            if name.endswith("MOC") or name.startswith("第"):
                continue
            concept_relative = Path("20_概念") / f"{name}.md"
            concept_target = vault.ensure_allowed(concept_relative)
            if concept_target.exists():
                continue
            content = CONCEPT_TEMPLATE.format(
                course=course, name=name, review_due=time.strftime("%Y-%m-%d"),
                sources="、".join(f"[[{item}]]" for item in [stem]),
            )
            plans.append(WritePlan(path=concept_target, content=content, action="create",
                                   notes=[f"概念 stub：{name}"]))

    moc_plan = _plan_moc(vault, course, lecture_names)
    plans.append(moc_plan)
    job.update(percent=75, message="写盘")

    written: list[str] = []
    backups: list[str] = []
    diff_chunks: list[str] = []
    for plan in plans:
        diff_chunks.append(f"### {plan.path}\n{plan.diff or '（无变化）'}")
        outcome = vault.write(plan, dry_run)
        if outcome["action"].startswith("dry-run"):
            written.append(f"[dry-run] {outcome['path']}")
        else:
            written.append(outcome["path"])
        if outcome.get("backup"):
            backups.append(str(outcome["backup"]))
    job.update(percent=100, message="完成")

    changed = [plan for plan in plans if plan.action != "unchanged"]
    summary = (
        f"{'dry-run：' if dry_run else ''}{len(lectures)} 讲 · 计划改动 {len(changed)} 个文件"
        f"（新建 {sum(1 for p in plans if p.action == 'create')}、替换 {sum(1 for p in plans if p.action == 'replace-auto')}）"
    )
    return {
        "ok": True,
        "summary": summary,
        "written": written,
        "backups": backups,
        "diff": "\n\n".join(diff_chunks) if dry_run else "",
        "warnings": warnings,
        "paths": {"vault": str(vault.root), "courseDir": str(vault.root / "10_课程" / course),
                  "moc": str(moc_plan.path)},
        "nextSteps": ["确认 diff 没问题后，用 dryRun=false 真正写盘"] if dry_run else [
            "在 Obsidian 里打开讲次笔记与 MOC 检查双链与属性",
            "需要的话接着做错题 / 闪卡 / 复习页",
        ],
    }


def _with_vault_path(config: Config, vault_path: str) -> Config:
    from common import Config as ConfigClass

    merged = dict(config.raw)
    merged["vaultPath"] = vault_path
    return ConfigClass(merged)


def _join(block: str, body: str) -> str:
    """拼接 front-matter 与正文：只在缺换行时补，绝不多加空行（否则每跑一次就多一行）。"""
    text = block + body if body.startswith("\n") or not block.endswith("\n") else block + body.lstrip("\n")
    if not text.endswith("\n"):
        text += "\n"
    return text


def _plan_lecture(target: Path, old: str | None, block: str, body: str) -> WritePlan:
    markers_in_body = split_markers(body) is not None
    if old is None:
        # 草稿自己带标记（模型写的）就原样落盘：标记外是标题 / 一句话 / 疑点台账 / 导航
        content = _join(block, body if markers_in_body else wrap_markers(body))
        return WritePlan(path=target, content=content, action="create")

    old_block, old_body = split_frontmatter(old)
    markers = split_markers(old_body)
    if markers is not None:
        before, _inner, after = markers
        source = split_markers(body)
        new_inner = (source[1] if source else body).strip()
        content = _join(block, before + wrap_markers(new_inner) + after)
        action = "unchanged" if content == old else "replace-auto"
        return WritePlan(path=target, content=content, action=action, old=old,
                         notes=["只替换 marker 之间"] if action != "unchanged" else [])

    # 用户手写过、没有标记：一个字都不改，把自动区追加到末尾
    content = old.rstrip("\n") + "\n\n" + wrap_markers(new_inner) + "\n"
    return WritePlan(path=target, content=content, action="append-auto", old=old,
                     notes=["原文件没有 dsh:auto 标记：未改动任何已有内容，自动区追加在末尾"])


def _plan_moc(vault: Vault, course: str, lecture_names: list[str]) -> WritePlan:
    relative = Path("10_课程") / course / f"{course} MOC.md"
    target = vault.ensure_allowed(relative)
    old = target.read_text(encoding="utf-8") if target.exists() else None
    rows = "\n".join(f"- [[{name}]]" for name in lecture_names)

    if old is None:
        content = MOC_TEMPLATE.format(moc_type="moc", course=course).replace(
            "## 讲次\n<!-- dsh:auto:end -->",
            "## 讲次\n" + rows + "\n<!-- dsh:auto:end -->",
        )
        return WritePlan(path=target, content=content, action="create", notes=["MOC 新建 + 本讲一行"])

    markers = split_markers(split_frontmatter(old)[1])
    if markers is None:
        return WritePlan(path=target, content=old.rstrip("\n") + "\n\n" + wrap_markers("## 讲次\n" + rows) + "\n",
                         action="append-auto", old=old, notes=["MOC 没有标记：追加自动区"])
    before, inner, after = markers
    existing = [line for line in inner.splitlines() if line.strip()]
    merged = existing + [f"- [[{name}]]" for name in lecture_names
                         if not any(name in line for line in existing)]
    content = _join(split_frontmatter(old)[0], before + wrap_markers("\n".join(merged)) + after)
    return WritePlan(path=target, content=content, action="unchanged" if content == old else "replace-auto", old=old)
