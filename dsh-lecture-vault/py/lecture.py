"""lecture.py — 唯一入口：每个子命令对应一个 ``lecture_*`` 工具。

用法（宿主插件就是这么调的）::

    python lecture.py doctor --network --json
    python lecture.py probe --input BV1... --json
    python lecture.py ingest --course-json <path> --modes subtitles transcribe --job-id <id>

约定：
  - stdout 只留一行 ``@@LECTURE_RESULT@@ {json}``，其余日志全走 stderr；
  - 任何失败都变成结构化结果（ok:false + reason + nextSteps），不抛到宿主；
  - ``--json`` 只是给调用方一个明确的形状，输出本来就一直是 JSON。
"""

from __future__ import annotations

import sys

# Windows 控制台默认是 GBK：先钉成 UTF-8，再导入其它模块（否则中文日志会炸）。
for stream_name in ("stdout", "stderr"):
    stream = getattr(sys, stream_name, None)
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

import argparse  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import doctor  # noqa: E402


def _pending(phase: str, detail: str):
    def handler(config, args, job):  # noqa: ARG001
        raise common.LectureError(
            f"该子命令在 {phase} 实现（P0 只交付环境自检与插件骨架）",
            next_steps=[detail, "先跑 lecture_doctor 确认环境，再按工作流分期推进"],
            code="NOT_IMPLEMENTED",
        )
    return handler


def cmd_doctor(config, args, job):  # noqa: ARG001
    return doctor.run(config, check_network=args.network)


def cmd_probe(config, args, job):  # noqa: ARG001
    import probe  # noqa: PLC0415

    return probe.run(config, args.input, args.kind)


def cmd_ingest(config, args, job):
    import ingest  # noqa: PLC0415

    return ingest.run(config, args.course_json, args.items, args.modes, args.dry_run, job)


def cmd_glossary(config, args, job):  # noqa: ARG001
    import glossary  # noqa: PLC0415

    from pathlib import Path as _Path

    directory = _Path(args.transcripts_dir)
    if not directory.is_dir():
        raise common.LectureError(f"transcriptsDir 不存在：{directory}", code="NO_DIR")
    if args.mode == "mine":
        return glossary.mine(directory)
    return glossary.apply(directory, args.glossary_path, args.dry_run)


def cmd_commit(config, args, job):
    import commit  # noqa: PLC0415

    return commit.run(config, args.vault_path, args.course, args.lectures, args.dry_run, job)


def cmd_lint(config, args, job):  # noqa: ARG001
    import lint  # noqa: PLC0415

    return lint.run(args.target_path, args.checks)


def cmd_status(config, args, job):  # noqa: ARG001
    """只读状态文件：给 jobId 就那一条，不给就列最近的。"""
    jobs_dir = config.jobs_dir
    if args.job_id:
        target = jobs_dir / f"{args.job_id}.json"
        if not target.exists():
            raise common.LectureError(
                f"没有这个任务：{args.job_id}",
                next_steps=[f"不带 jobId 再调一次可以看到最近的任务（{jobs_dir}）"],
                code="UNKNOWN_JOB",
            )
        return {"ok": True, "summary": f"任务 {args.job_id}", "job": common.read_json(target),
                "paths": {"jobsDir": str(jobs_dir)}, "warnings": []}
    if not jobs_dir.exists():
        return {"ok": True, "summary": "还没有后台任务", "items": [],
                "paths": {"jobsDir": str(jobs_dir)}, "warnings": []}
    items = sorted(jobs_dir.glob("*.json"), reverse=True)[:10]
    return {"ok": True, "summary": f"最近 {len(items)} 个任务",
            "items": [item.stem for item in items],
            "paths": {"jobsDir": str(jobs_dir)}, "warnings": []}


def build_parser() -> argparse.ArgumentParser:
    # `--job-id` / `--json` 既能在子命令前、也能在子命令后出现：
    # 宿主后台任务调的是 `lecture.py ingest … --job-id <id>`。
    common_opts = argparse.ArgumentParser(add_help=False)
    common_opts.add_argument("--job-id", dest="job_id", default=None, help="后台任务 id（写了就更新进度文件）")
    common_opts.add_argument("--json", action="store_true", help="结果始终是 JSON；这个开关只为可读性保留")

    parser = argparse.ArgumentParser(prog="lecture.py", description="听课入库：确定性工具入口", parents=[common_opts])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", parents=[common_opts], help="环境自检")
    p.add_argument("--network", dest="network", action="store_true", default=True, help="探测平台连通性（默认开）")
    p.add_argument("--no-network", dest="network", action="store_false", help="跳过网络探测")
    p.set_defaults(handler=cmd_doctor)

    p = sub.add_parser("status", parents=[common_opts], help="查后台任务")
    p.set_defaults(handler=cmd_status)

    p = sub.add_parser("probe", parents=[common_opts], help="解析课程输入")
    p.add_argument("--input", required=True)
    p.add_argument("--kind", default="auto", choices=["auto", "bili", "youtube", "hls", "file"])
    p.set_defaults(handler=cmd_probe)

    p = sub.add_parser("ingest", parents=[common_opts], help="取字幕 / 下载音频 / 转写")
    p.add_argument("--course-json", required=True)
    p.add_argument("--items", nargs="*", type=int, default=None)
    p.add_argument("--modes", nargs="*", choices=["subtitles", "audio", "transcribe", "hls"],
                   default=["subtitles", "transcribe"])
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(handler=cmd_ingest)

    p = sub.add_parser("glossary", parents=[common_opts], help="挖术语 / 应用纠错")
    p.add_argument("--transcripts-dir", required=True)
    p.add_argument("--mode", required=True, choices=["mine", "apply"])
    p.add_argument("--glossary-path", default=None)
    p.add_argument("--dry-run", action="store_true", help="只统计命中数，不改文件（宿主默认带上）")
    p.set_defaults(handler=cmd_glossary)

    p = sub.add_parser("courseware", parents=[common_opts], help="解析课件")
    p.add_argument("--source-dir", required=True)
    p.add_argument("--out-dir", default=None)
    p.add_argument("--min-chars", type=int, default=30)
    p.add_argument("--min-coverage", type=float, default=0.6)
    p.set_defaults(handler=_pending("P1（课件解析）", "courseware 只取课件里已有的图，扫描件只报 needsCloudOcr"))

    p = sub.add_parser("commit", parents=[common_opts], help="写进 vault")
    p.add_argument("--vault-path", default=None)
    p.add_argument("--course", required=True)
    p.add_argument("--lectures", nargs="+", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(handler=cmd_commit)

    p = sub.add_parser("lint", parents=[common_opts], help="校验讲义")
    p.add_argument("--target-path", required=True)
    p.add_argument("--checks", nargs="*", choices=["timestamp", "fx", "links", "orphan"], default=None)
    p.set_defaults(handler=cmd_lint)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    return common.run_main(list(args))


if __name__ == "__main__":
    raise SystemExit(main())
