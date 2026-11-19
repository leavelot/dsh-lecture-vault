"""glossary.py — 术语纠错（本流程的胜负手）。

专业课 ASR 的错法是**同音替换**（「谐振子」→ 斜阵子 / 邪真子），正确写法常常一次都不出现，
而错法各有两三处 —— 所以能被机械修正。分三步，和 skill 里的规矩一致：

  1. `mine`：同音（拼音相同、字不同）+ 差一字（4 字以上）两类候选，三道闸压噪声
     （虚词不收、极低频不收、单字键不收），同时产出证据表给人审。
  2. 人审：看证据表，删掉不同意的条目 —— 脚本不替用户决定。
  3. `apply`：只改确认过的键；先 dryRun 看每条命中数（单条 > 200 告警）。
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from common import Config, LectureError, log, write_json

STOPWORDS = set(
    "我们 你们 他们 她们 这个 那个 就是 什么 所以 因为 但是 如果 然后 现在 可以 一个 一下 "
    "已经 还是 就是 这样 那样 知道 觉得 其实 可能 应该 真的 非常 特别 一些 这些 那些 不是 "
    "没有 很多 东西 时候 地方 感觉 问题 意思 音乐 因为 而已 一样 起来 出来 进去 的话".split()
)
CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
MIN_NGRAM = 2
MAX_NGRAM = 4
MIN_TOTAL = 5
MIN_FORM_COUNT = 2
MAX_CANDIDATES = 40


def _load_segments(transcripts_dir: Path) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for path in sorted(transcripts_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = str(item.get("text") or "").strip()
            if text:
                segments.append({"start": float(item.get("start") or 0), "text": text, "file": path.name})
    if not segments:
        # 没有 jsonl 就退回 md
        for path in sorted(transcripts_dir.glob("*.md")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                match = re.match(r"\[(\d{2}):(\d{2}):(\d{2})\]\s*(.+)$", line.strip())
                if match:
                    seconds = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
                    segments.append({"start": seconds, "text": match.group(4).strip(), "file": path.name})
    if not segments:
        raise LectureError(
            f"{transcripts_dir} 里没有转录（*.jsonl / *.md）",
            next_steps=["先跑 lecture_ingest 拿转录"],
            code="NO_TRANSCRIPTS",
        )
    return segments


def _pinyin(text: str) -> str:
    from pypinyin import Style, lazy_pinyin  # noqa: PLC0415

    return "".join(lazy_pinyin(text, style=Style.NORMAL))


def mine(transcripts_dir: Path) -> dict[str, Any]:
    segments = _load_segments(transcripts_dir)
    counts: Counter[str] = Counter()
    contexts: dict[str, list[str]] = defaultdict(list)
    for segment in segments:
        for run in CJK_RUN.findall(segment["text"]):
            for size in range(MIN_NGRAM, MAX_NGRAM + 1):
                for start in range(0, max(0, len(run) - size + 1)):
                    gram = run[start:start + size]
                    if gram in STOPWORDS:
                        continue
                    counts[gram] += 1
                    if len(contexts[gram]) < 2:
                        contexts[gram].append(f"[{_hhmmss(segment['start'])}] {segment['text'][:60]}")

    # 同音分组：拼音相同、字不同
    by_pinyin: dict[str, list[str]] = defaultdict(list)
    for gram, count in counts.items():
        if count < MIN_FORM_COUNT or len(gram) < MIN_NGRAM:
            continue
        by_pinyin[_pinyin(gram)].append(gram)

    candidates: list[dict[str, Any]] = []
    for pinyin, forms in by_pinyin.items():
        if len(forms) < 2:
            continue
        total = sum(counts[form] for form in forms)
        if total < MIN_TOTAL:
            continue
        # 「差一字」类：4 字以上、仅一处不同
        forms.sort(key=lambda item: -counts[item])
        candidates.append({
            "kind": "homophone",
            "pinyin": pinyin,
            "forms": [{"term": form, "count": counts[form], "contexts": contexts[form]} for form in forms],
            "total": total,
            "suggestion": "人审：选一个当正解，其余写进 variants",
        })
    # 差一字（4 字以上）
    long_forms = [gram for gram in counts if len(gram) >= 4 and counts[gram] >= MIN_FORM_COUNT]
    for index, left in enumerate(long_forms):
        for right in long_forms[index + 1:]:
            if len(left) != len(right):
                continue
            diff = sum(1 for a, b in zip(left, right) if a != b)
            if diff != 1:
                continue
            total = counts[left] + counts[right]
            if total < MIN_TOTAL:
                continue
            candidates.append({
                "kind": "one-char",
                "pinyin": _pinyin(left),
                "forms": [
                    {"term": left, "count": counts[left], "contexts": contexts[left]},
                    {"term": right, "count": counts[right], "contexts": contexts[right]},
                ],
                "total": total,
                "suggestion": "人审：判断哪个是正确写法",
            })

    candidates.sort(key=lambda item: -item["total"])
    candidates = candidates[:MAX_CANDIDATES]

    glossary_path = transcripts_dir / "glossary.json"
    evidence_path = transcripts_dir / "glossary-evidence.md"
    write_json(glossary_path, {"entries": candidates, "reviewed": False})
    evidence_path.write_text(_evidence_md(candidates), encoding="utf-8")
    log(f"[glossary] 候选 {len(candidates)} 条 -> {glossary_path}")
    return {
        "ok": True,
        "summary": f"挖出 {len(candidates)} 条同音/差一字候选（{len(segments)} 段转录）；未应用，等人工审",
        "glossaryPath": str(glossary_path),
        "evidenceMdPath": str(evidence_path),
        "hits": [{"pinyin": item["pinyin"], "forms": [form["term"] for form in item["forms"]],
                  "total": item["total"]} for item in candidates[:15]],
        "warnings": [] if candidates else ["没有候选：要么转录质量已经很好，要么术语只在课件里出现"],
        "paths": {"transcriptsDir": str(transcripts_dir)},
        "nextSteps": [
            "把 evidenceMdPath 给用户看，让他删掉不同意的条目（不确认就不应用）",
            "确认后 apply + dryRun 看命中数，再真正写入",
        ],
    }


def apply(transcripts_dir: Path, glossary_path: str | None, dry_run: bool) -> dict[str, Any]:
    path = Path(glossary_path) if glossary_path else transcripts_dir / "glossary.json"
    if not path.exists():
        raise LectureError(
            f"找不到词表：{path}",
            next_steps=["先跑 mine，并人工确认 glossary.json（把 reviewed 改成 true）"],
            code="NO_GLOSSARY",
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    mapping = _mapping_from(payload)
    if not mapping:
        raise LectureError(
            "词表里没有可应用的条目（需要 canonical + variants）",
            next_steps=['改成 {"entries":[{"canonical":"谐振子","variants":["斜阵子","邪真子"]}]} 这种形状'],
            code="EMPTY_GLOSSARY",
        )
    for wrong in mapping:
        if len(wrong) < 2:
            raise LectureError(f"单字键默认拒收：{wrong!r}", code="SINGLE_CHAR_KEY")

    hits: list[dict[str, Any]] = []
    files_changed = 0
    for target in sorted(list(transcripts_dir.glob("*.md")) + list(transcripts_dir.glob("*.txt"))
                         + list(transcripts_dir.glob("*.jsonl"))):
        if target.name.startswith("glossary"):
            continue
        text = target.read_text(encoding="utf-8")
        original = text
        file_hits = []
        for wrong, right in mapping.items():
            count = text.count(wrong)
            if count == 0:
                continue
            file_hits.append({"wrong": wrong, "right": right, "count": count})
            text = text.replace(wrong, right)
        if file_hits:
            hits.extend([{**item, "file": str(target)} for item in file_hits])
            if text != original:
                files_changed += 1
                if not dry_run:
                    target.write_text(text, encoding="utf-8")

    warnings = [f"{item['wrong']}→{item['right']} 命中 {item['count']} 次（单条 >200，确认没有误伤）"
                for item in hits if item["count"] > 200]
    summary = (f"{'dry-run：' if dry_run else ''}应用 {len(mapping)} 个键，命中 {sum(item['count'] for item in hits)} 处，"
               f"涉及 {files_changed} 个文件")
    log(f"[glossary] {summary}")
    return {
        "ok": True,
        "summary": summary,
        "hits": hits,
        "warnings": warnings,
        "paths": {"glossaryPath": str(path), "transcriptsDir": str(transcripts_dir)},
        "nextSteps": ["命中数看着合理就去掉 dryRun 真正写入", "写完后讲义还要做一次反向终检"] if dry_run else [
            "重读转录确认改对了；讲义写完后用同一份词表反扫一遍",
        ],
    }


def _mapping_from(payload: Any) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if isinstance(payload, dict) and isinstance(payload.get("entries"), list):
        for entry in payload["entries"]:
            if not isinstance(entry, dict):
                continue
            canonical = str(entry.get("canonical") or "").strip()
            variants = entry.get("variants") or []
            if canonical and isinstance(variants, list):
                for variant in variants:
                    variant = str(variant).strip()
                    if variant and variant != canonical:
                        mapping[variant] = canonical
    elif isinstance(payload, dict):
        for wrong, right in payload.items():
            if isinstance(right, str):
                mapping[str(wrong)] = right
    return mapping


def _evidence_md(candidates: list[dict[str, Any]]) -> str:
    lines = ["# 术语候选证据表", "",
             "审法：同一条里只留你认可的正确写法，其余写进 `variants`；不同意的整条删掉。", ""]
    for index, item in enumerate(candidates, start=1):
        lines.append(f"## {index}. {item['pinyin']}（{item['kind']}，共 {item['total']} 次）")
        for form in item["forms"]:
            lines.append(f"- **{form['term']}** × {form['count']}")
            for context in form["contexts"]:
                lines.append(f"  - {context}")
        lines.append("")
    lines.append("确认后在 `glossary.json` 里写成：")
    lines.append("")
    lines.append('```json')
    lines.append('{"entries": [{"canonical": "正确写法", "variants": ["错法1", "错法2"]}]}')
    lines.append("```")
    return "\n".join(lines) + "\n"


def _hhmmss(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"
