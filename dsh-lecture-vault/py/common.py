"""common.py — 配置、路径、日志、结果输出、任务状态。

所有子命令都从这里开始：配置来自环境变量 ``LECTURE_CONFIG``（宿主插件解析后注入），
没有它时用与 JS 侧一致的默认值，好让 ``python lecture.py doctor`` 能单独跑。
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

RESULT_PREFIX = "@@LECTURE_RESULT@@"

DEFAULT_ALLOWED_DIRS = [
    "00_Inbox",
    "10_课程",
    "20_概念",
    "30_题目",
    "40_复盘",
    "50_方法库",
    "60_看板",
    "90_附件",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "vaultPath": "D:\\工具\\笔记",
    "workDir": "D:\\工具\\课堂记录",
    "pythonExe": "",
    "ffmpegPath": "D:\\dsh插件\\ffmpeg\\bin\\ffmpeg.exe",
    "uvPath": "",
    "asr": {
        "model": "large-v3-turbo",
        "cpuModel": "small",
        "compute": "int8_float16",
        "hfEndpoint": "https://hf-mirror.com",
        "disableXet": True,
        "hfHome": "",
    },
    "bili": {"delayMs": 1000, "cookieEnv": "BILI_SESSDATA", "maxRetry": 2},
    "youtube": {"proxy": "", "subLangs": "zh-Hans,zh,en"},
    "dingtalk": {"referer": "", "cookiesFromBrowser": "edge"},
    "vault": {"allowedDirs": DEFAULT_ALLOWED_DIRS, "backupKeepDays": 30, "dryRun": False},
    "network": {"timeoutMs": 15000, "bypassDeadProxy": True},
}


class LectureError(Exception):
    """带「原因 + 下一步」的业务错误；不打印堆栈到结果里。"""

    def __init__(self, reason: str, next_steps: Iterable[str] | None = None, code: str = "LECTURE_ERROR"):
        super().__init__(reason)
        self.reason = reason
        self.next_steps = list(next_steps or [])
        self.code = code


def _merge(base: dict[str, Any], patch: Any) -> dict[str, Any]:
    if not isinstance(patch, dict):
        return dict(base)
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            out[key] = _merge(base[key], value)
        elif value is not None:
            out[key] = value
    return out


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        merged = _merge(DEFAULT_CONFIG, self.raw)
        work_dir = str(merged.get("workDir") or DEFAULT_CONFIG["workDir"])
        merged["workDir"] = work_dir
        merged["vaultPath"] = str(merged.get("vaultPath") or DEFAULT_CONFIG["vaultPath"])
        merged["pythonExe"] = str(merged.get("pythonExe") or os.path.join(work_dir, ".venv", "Scripts", "python.exe"))
        merged["uvPath"] = str(merged.get("uvPath") or os.path.join(work_dir, "bin", "uv.exe"))
        merged["ffmpegPath"] = str(merged.get("ffmpegPath") or DEFAULT_CONFIG["ffmpegPath"])
        asr = dict(DEFAULT_CONFIG["asr"])
        asr.update(merged.get("asr") or {})
        asr["hfHome"] = str(asr.get("hfHome") or os.path.join(work_dir, "models"))
        merged["asr"] = asr
        merged["bili"] = {**DEFAULT_CONFIG["bili"], **(merged.get("bili") or {})}
        merged["youtube"] = {**DEFAULT_CONFIG["youtube"], **(merged.get("youtube") or {})}
        merged["dingtalk"] = {**DEFAULT_CONFIG["dingtalk"], **(merged.get("dingtalk") or {})}
        vault = {**DEFAULT_CONFIG["vault"], **(merged.get("vault") or {})}
        allowed = vault.get("allowedDirs")
        vault["allowedDirs"] = [str(item) for item in allowed] if isinstance(allowed, list) and allowed else list(DEFAULT_ALLOWED_DIRS)
        merged["vault"] = vault
        merged["network"] = {**DEFAULT_CONFIG["network"], **(merged.get("network") or {})}
        self.raw = merged

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    # —— 派生路径 ——
    @property
    def work_dir(self) -> Path:
        return Path(self.raw["workDir"])

    @property
    def vault_path(self) -> Path:
        return Path(self.raw["vaultPath"])

    @property
    def logs_dir(self) -> Path:
        return self.work_dir / "logs"

    @property
    def jobs_dir(self) -> Path:
        return self.logs_dir / "jobs"

    @property
    def sessions_dir(self) -> Path:
        return self.work_dir / "sessions"

    @property
    def cache_dir(self) -> Path:
        return self.work_dir / "cache"

    @property
    def ffmpeg_path(self) -> Path:
        return Path(self.raw["ffmpegPath"])

    @property
    def python_exe(self) -> Path:
        return Path(self.raw["pythonExe"])

    def asr_model_dir(self, model: str | None = None) -> Path:
        name = model or str(self.raw["asr"]["model"])
        return Path(self.raw["asr"]["hfHome"]) / "whisper" / name


def load_config() -> Config:
    raw: Any = {}
    payload = os.environ.get("LECTURE_CONFIG")
    if payload:
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError:
            raw = {}
    return Config(raw)


PROXY_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _system_proxy() -> str:
    """读 WinINET（注册表）里的系统代理。requests / pip / huggingface_hub 都会用它。"""
    if os.name != "nt":
        return ""
    try:
        import winreg  # noqa: PLC0415

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
    except Exception:  # noqa: BLE001
        return ""
    return str(server) if enabled and server else ""


def _proxy_alive(proxy: str, timeout_s: float = 1.0) -> bool:
    """代理开着但端口没在听时，所有走代理的请求都会报 ProxyError —— 先探一次。"""
    import socket  # noqa: PLC0415

    for entry in str(proxy).replace(" ", "").split(";"):
        target = entry.split("=", 1)[1] if "=" in entry else entry
        target = target.split("://", 1)[-1]
        host, _, port_text = target.partition(":")
        try:
            port = int(port_text) if port_text else 80
        except ValueError:
            port = 80
        if not host:
            continue
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                return True
        except OSError:
            continue
    return False


def configure_network_env(config: Config) -> None:
    """死代理绕过：只在本次进程里设 NO_PROXY=*，不改注册表、不改系统设置。

    这台机器（以及不少国内机器）会有「代理软件关了但注册表代理还开着」的状态，
    此时 requests / pip / hf 下载会一律 ProxyError，看着像断网。
    """
    if not config["network"].get("bypassDeadProxy", True):
        return
    proxy = _system_proxy()
    if not proxy or _proxy_alive(proxy):
        return
    for name in PROXY_ENV_NAMES:
        os.environ.pop(name, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    log(f"[network] 系统代理 {proxy} 端口不可连，本次进程绕过它（NO_PROXY=*）")


# ---------------------------------------------------------------- 输出


def log(message: str) -> None:
    """人类可读日志走 stderr：宿主把它原样写进 <workDir>\\logs\\*.log。"""
    try:
        sys.stderr.write(str(message).rstrip() + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def emit(result: dict[str, Any]) -> None:
    """唯一的机读通道：一行哨兵 + 紧凑 JSON。"""
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write(RESULT_PREFIX + payload + "\n")
    sys.stdout.flush()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 任务状态


class JobStatus:
    """后台任务的进度文件：``<workDir>\\logs\\jobs\\<jobId>.json``，lecture_status 读它。"""

    def __init__(self, config: Config, job_id: str | None) -> None:
        self.config = config
        self.job_id = job_id
        self.path = config.jobs_dir / f"{job_id}.json" if job_id else None
        self.result_path = config.jobs_dir / f"{job_id}.result.json" if job_id else None
        self.log_path = config.jobs_dir / f"{job_id}.log" if job_id else None
        self._state: dict[str, Any] = {}
        self._last_flush = 0.0

    @property
    def active(self) -> bool:
        return self.path is not None

    def update(self, percent: float | None = None, message: str | None = None, state: str | None = None, **extra: Any) -> None:
        if not self.active:
            return
        self._state.update(extra)
        if percent is not None:
            self._state["percent"] = round(max(0.0, min(100.0, float(percent))), 1)
        if message is not None:
            self._state["message"] = message
            log(f"[job {self.job_id}] {self._state['percent'] if 'percent' in self._state else 0}% {message}")
        if state is not None:
            self._state["state"] = state
            if state in {"done", "failed", "cancelled"}:
                self._state["finishedAt"] = now_iso()
        # 限流：每 400ms 最多落一次盘，避免刷屏写
        now = time.time()
        if state is not None or now - self._last_flush > 0.4:
            self._last_flush = now
            self._flush()

    def _flush(self) -> None:
        try:
            current: dict[str, Any] = {}
            if self.path is not None and self.path.exists():
                current = read_json(self.path)
            current.update(self._state)
            current["jobId"] = self.job_id
            current.setdefault("startedAt", now_iso())
            current["updatedAt"] = now_iso()
            write_json(self.path, current)
        except Exception as exc:  # 进度写不进去不能拖垮任务
            log(f"[job] 写状态失败：{exc}")


# ---------------------------------------------------------------- 入口包装


class _TeeStderr:
    """后台任务的 stdio 由宿主忽略，所以自己把 stderr 抄一份进 jobs\\<jobId>.log。

    没有这份日志，后台任务失败时只能看到「state=failed」，什么原因都不知道。
    """

    def __init__(self, stream: Any, path: Path) -> None:
        self._stream = stream
        self._path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("a", encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            self._handle = None

    def write(self, text: str) -> int:
        try:
            self._stream.write(text)
        except Exception:  # noqa: BLE001
            pass
        if self._handle is not None:
            try:
                self._handle.write(text)
                self._handle.flush()
            except Exception:  # noqa: BLE001
                pass
        return len(text)

    def flush(self) -> None:
        for target in (self._stream, self._handle):
            try:
                if target is not None:
                    target.flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self) -> bool:
        return False

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except Exception:  # noqa: BLE001
                pass
            self._handle = None


def run_main(argv: list[str]) -> int:
    """argparse 之后统一在这里收口：成功 emit，失败 emit 结构化原因。"""
    from lecture import build_parser  # 延迟导入，避免循环

    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config()
    configure_network_env(config)
    job = JobStatus(config, getattr(args, "job_id", None))

    original_stderr = sys.stderr
    if job.log_path is not None:
        sys.stderr = _TeeStderr(original_stderr, job.log_path)  # type: ignore[assignment]
        log(f"[job {job.job_id}] 开始：{' '.join(argv)}")

    try:
        handler = args.handler
        result = handler(config, args, job)
        if not isinstance(result, dict):
            raise LectureError("内部错误：子命令没有返回结构化结果")
        if job.result_path is not None:
            try:
                write_json(job.result_path, result)
            except Exception as exc:  # noqa: BLE001
                log(f"[job] 写结果失败：{exc}")
            result = {**result, "paths": {**(result.get("paths") or {}), "result": str(job.result_path)}}
        job.update(state="done" if result.get("ok", True) else "failed", percent=100,
                   message=result.get("summary") or result.get("reason") or "完成",
                   resultPath=str(job.result_path) if job.result_path else None)
        emit(result)
        return 0
    except LectureError as exc:
        log(f"[error] {exc.reason}")
        job.update(state="failed", percent=100, message=exc.reason)
        emit({"ok": False, "reason": exc.reason, "code": exc.code,
              "nextSteps": exc.next_steps, "warnings": [], "paths": {}})
        return 0
    except KeyboardInterrupt:
        emit({"ok": False, "reason": "用户中断", "warnings": [], "paths": {}})
        return 130
    except Exception as exc:  # noqa: BLE001 —— 任何异常都要变成结构化结果
        log(traceback.format_exc())
        job.update(state="failed", percent=100, message=f"{type(exc).__name__}: {exc}")
        emit({"ok": False, "reason": f"{type(exc).__name__}: {exc}", "warnings": [], "paths": {}})
        return 0
    finally:
        if job.log_path is not None:
            sys.stderr = original_stderr
