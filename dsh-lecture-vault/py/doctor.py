"""doctor.py — 环境自检。

返回一张检查表，每行 ``{id, title, level, ok, why}``：
  - ``level='required'``：缺了就不能工作，必须绿；
  - ``level='optional'``：本机条件可能本来就不满足（YouTube 直连、cookie），只提示不判负。
整体 ``ok`` = 所有 required 项都绿。

这里同时承担 P0 的关键实测：**插件进程能不能直接写 vault**（写探测文件再删掉）。
"""

from __future__ import annotations

import importlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from common import Config, log

REQUIRED_MODULES = [
    "faster_whisper",
    "ctranslate2",
    "yt_dlp",
    "pypdf",
    "fitz",
    "pypinyin",
    "PIL",
    "requests",
    "certifi",
]

MODEL_FILES = ("model.bin",)
MODEL_MIN_BYTES = 100 * 1024 * 1024


def _check(check_id: str, title: str, level: str, ok: bool, why: str,
           next_steps: list[str] | None = None, detail: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"id": check_id, "title": title, "level": level, "ok": bool(ok), "why": why}
    if detail:
        item["detail"] = detail
    if not ok and next_steps:
        item["nextSteps"] = next_steps
    return item


def _run(cmd: list[str], timeout: float = 20.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return -1, "not found"
    except subprocess.TimeoutExpired:
        return -2, "timeout"
    except Exception as exc:  # noqa: BLE001
        return -3, str(exc)


# ------------------------------------------------------------------ 单项检查


def check_python(config: Config) -> dict[str, Any]:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    exe = Path(sys.executable)
    detail = f"{exe} | {platform.platform()}"
    problems: list[str] = []

    if sys.version_info < (3, 10):
        problems.append(f"Python {version} 太旧（需要 3.10+）")
    if config.python_exe.exists() and exe.resolve() != config.python_exe.resolve():
        problems.append(f"当前解释器不是配置里的 venv（{config.python_exe}）")

    missing: list[str] = []
    for module in REQUIRED_MODULES:
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{module}（{type(exc).__name__}: {exc}）" if str(exc) else module)

    if missing:
        problems.append("缺依赖：" + "、".join(missing))

    if problems:
        sac_hint = _smart_app_control_hint(missing)
        return _check(
            "python", "Python 运行时", "required", False, "；".join(problems),
            next_steps=sac_hint or [
                f"缺依赖时装：\"{config.raw['uvPath']}\" pip install --python \"{config.python_exe}\" -r py/requirements.txt",
                "或运行包里的 scripts/install-env.ps1（只打印并执行安装步骤，不静默改系统配置）",
            ],
            detail=detail,
        )
    return _check("python", "Python 运行时", "required", True,
                  f"Python {version}，9 个依赖全部可导入", detail=detail)


def _smart_app_control_hint(missing: list[str]) -> list[str] | None:
    """Smart App Control 拦未签名扩展模块时，给的是「怎么办」，不是「重装」。

    这不是依赖没装好：`ctranslate2\\_ext.cp311-win_amd64.pyd` 没有代码签名，
    SAC 会直接拒绝加载（WinError 4551），重装同一个轮子没用。
    """
    hit = any("应用程序控制策略" in item or "4551" in item for item in missing)
    if not hit:
        return None
    return [
        "原因不是依赖缺失：ctranslate2 的 _ext.pyd 未签名，被 Smart App Control 拒绝加载（WinError 4551）",
        "路线 A（推荐，质量最好）：Windows 安全中心 → 应用和浏览器控制 → 智能应用控制 → 关闭。"
        "注意：关掉后只能重装 Windows 才能再开启，请自己决定",
        "路线 B（不动系统）：在环境变量里给 BILI_SESSDATA，走 B 站 AI 字幕，不跑本地 ASR",
        "路线 C：换一个有原生字幕的课程，先跑通后面的链路",
    ]


def check_ffmpeg(config: Config) -> dict[str, Any]:
    exe = config.ffmpeg_path
    if not exe.exists():
        return _check("ffmpeg", "ffmpeg / ffprobe", "required", False,
                      f"找不到 {exe}",
                      next_steps=["把 ffmpegPath 指向 ffmpeg.exe，或放一个 ffmpeg 到 <workDir>\\bin\\"])
    code, out = _run([str(exe), "-version"], timeout=20)
    if code != 0:
        return _check("ffmpeg", "ffmpeg / ffprobe", "required", False,
                      f"ffmpeg 跑不起来（退出码 {code}）：{out.strip()[:160]}")
    first = out.strip().splitlines()[0] if out.strip() else "ffmpeg"
    ffprobe = exe.with_name("ffprobe.exe")
    if not ffprobe.exists():
        return _check("ffmpeg", "ffmpeg / ffprobe", "required", False,
                      f"有 ffmpeg 但同目录缺 ffprobe.exe：{exe.parent}")
    return _check("ffmpeg", "ffmpeg / ffprobe", "required", True,
                  f"{first.split(' Copyright')[0]}；ffprobe 同目录可用", detail=str(exe))


def _cuda_dll_dirs(config: Config) -> list[Path]:
    """pip 装的 nvidia-* 轮子把 cuBLAS / cuDNN 的 DLL 放在 site-packages\\nvidia\\*\\bin。"""
    venv_root = config.python_exe.parent.parent
    nvidia_root = venv_root / "Lib" / "site-packages" / "nvidia"
    if not nvidia_root.is_dir():
        return []
    dirs = []
    for child in sorted(nvidia_root.iterdir()):
        bin_dir = child / "bin"
        if bin_dir.is_dir():
            dirs.append(bin_dir)
    return dirs


def _activate_cuda_dlls(config: Config) -> list[str]:
    added: list[str] = []
    for directory in _cuda_dll_dirs(config):
        try:
            os.add_dll_directory(str(directory))  # type: ignore[attr-defined]
            added.append(str(directory))
        except Exception:  # noqa: BLE001 —— 非 Windows 或目录不可用时退回 PATH
            pass
        os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
    return added


def _nvidia_smi() -> tuple[str, str]:
    candidates = ["nvidia-smi"]
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates.append(os.path.join(program_files, "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe"))
    for candidate in candidates:
        exe = shutil.which(candidate) if not os.path.isabs(candidate) else candidate
        if not exe:
            continue
        code, out = _run([exe, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"], timeout=20)
        if code == 0 and out.strip():
            return out.strip().splitlines()[0], ""
        return "", out.strip()
    return "", "nvidia-smi 不存在"


def check_gpu(config: Config) -> dict[str, Any]:
    gpu_line, error = _nvidia_smi()
    if not gpu_line:
        return _check("gpu", "GPU 转写", "optional", True,
                      f"未检出 NVIDIA GPU（{error}）→ 转写走 CPU {config['asr']['cpuModel']}，大约慢一个数量级")
    detail = gpu_line.strip()
    _activate_cuda_dlls(config)
    try:
        import ctranslate2  # noqa: PLC0415

        count = ctranslate2.get_cuda_device_count()
        version = getattr(ctranslate2, "__version__", "?")
    except Exception as exc:  # noqa: BLE001
        return _check("gpu", "GPU 转写", "required", False,
                      f"有 GPU（{detail}）但 ctranslate2 不可用：{type(exc).__name__}: {exc}",
                      next_steps=["重装 ctranslate2：uv pip install --force-reinstall ctranslate2"],
                      detail=detail)
    if count < 1:
        return _check("gpu", "GPU 转写", "required", False,
                      f"有 GPU（{detail}）但 CUDA 不可用：ctranslate2 看不到设备（缺 cuBLAS / cuDNN 9）",
                      next_steps=[
                          "装 CUDA 运行库轮子：uv pip install -r py/requirements-cuda.txt",
                          "装完重跑 lecture_doctor",
                      ],
                      detail=detail)
    # 设备数只看驱动；真正推理还要 cuDNN 9 的 DLL，缺了会在第一段音频上炸 —— 这里先探到。
    cudnn_dlls = []
    for directory in _cuda_dll_dirs(config):
        if "cudnn" in str(directory).lower():
            cudnn_dlls.extend(str(item) for item in Path(directory).glob("cudnn*.dll"))
    if not cudnn_dlls:
        return _check("gpu", "GPU 转写", "required", False,
                      f"有 GPU（{detail}）且驱动可用，但没找到 cuDNN 的 DLL（推理会失败）",
                      next_steps=["装 CUDA 运行库轮子：uv pip install -r py/requirements-cuda.txt"],
                      detail=detail)
    try:
        supported = sorted(ctranslate2.get_supported_compute_types("cuda"))
    except Exception:  # noqa: BLE001
        supported = []
    return _check("gpu", "GPU 转写", "required", True,
                  f"{detail}；ctranslate2 {version} 可用设备 {count} 个，compute={config['asr']['compute']}",
                  detail="支持的计算类型：" + (", ".join(supported) if supported else "未报告"))


def check_vault(config: Config) -> dict[str, Any]:
    vault = config.vault_path
    if not vault.exists():
        return _check("vault", "Obsidian vault", "required", False, f"vault 不存在：{vault}",
                      next_steps=[f"新建 vault 或在配置里改 vaultPath（当前 {vault}）"])
    if not (vault / ".obsidian").is_dir():
        return _check("vault", "Obsidian vault", "required", False,
                      f"{vault} 里没有 .obsidian，拒绝写入",
                      next_steps=["确认这是一个 Obsidian vault；找不到就改 vaultPath"])

    created: list[str] = []
    try:
        for name in config["vault"]["allowedDirs"]:
            target = vault / name
            if not target.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                created.append(name)
    except Exception as exc:  # noqa: BLE001
        return _check("vault", "Obsidian vault", "required", False,
                      f"建白名单目录失败：{type(exc).__name__}: {exc}",
                      next_steps=["用当前用户手动建一次这些目录，或检查磁盘/权限"])

    probe_dir = vault / "00_Inbox"
    try:
        with tempfile.NamedTemporaryFile("w", prefix=".dsh-write-probe-", suffix=".tmp",
                                         dir=str(probe_dir), delete=False, encoding="utf-8") as handle:
            handle.write("probe")
            probe_path = Path(handle.name)
        content = probe_path.read_text(encoding="utf-8")
        probe_path.unlink()
        if content != "probe":
            raise OSError("写入的内容读回来不一致")
    except Exception as exc:  # noqa: BLE001
        return _check("vault", "Obsidian vault", "required", False,
                      f"能读到 vault，但写不进去：{type(exc).__name__}: {exc}",
                      next_steps=[
                          "宿主进程被文件沙箱拦住了：改用「写 workspace + 一键导入」的落盘通道",
                          "或在 DSH 里给本插件所在进程放行该目录",
                      ],
                      detail=str(probe_dir))

    note = f"可写（已实测写探测文件）" + (f"；新建目录：{'、'.join(created)}" if created else "")
    return _check("vault", "Obsidian vault", "required", True, note, detail=str(vault))


def check_workdir(config: Config) -> dict[str, Any]:
    work = config.work_dir
    try:
        for sub in ("logs", "logs\\jobs", "sessions", "cache", "models", "bin"):
            (work / sub).mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", prefix=".probe-", suffix=".tmp", dir=str(work), delete=False) as handle:
            handle.write("probe")
            probe = Path(handle.name)
        probe.unlink()
    except Exception as exc:  # noqa: BLE001
        return _check("workdir", "工作目录", "required", False,
                      f"{work} 不可写：{type(exc).__name__}: {exc}",
                      next_steps=["改 workDir 到一个可写的大盘目录"])

    try:
        usage = shutil.disk_usage(str(work))
        free_gb = usage.free / (1024 ** 3)
    except Exception:  # noqa: BLE001
        free_gb = -1.0

    if 0 <= free_gb < 8:
        return _check("workdir", "工作目录", "required", False,
                      f"{work} 只剩 {free_gb:.1f} GB，模型加会话产物不够",
                      next_steps=["清盘或把 workDir 换到空间更大的盘（需要 ≥ 8 GB）"])
    why = f"{work} 可写" + (f"，剩余 {free_gb:.0f} GB" if free_gb >= 0 else "")
    return _check("workdir", "工作目录", "required", True, why, detail=str(work))


def check_model(config: Config) -> dict[str, Any]:
    model_dir = config.asr_model_dir()
    model_bin = model_dir / "model.bin"
    if model_bin.exists() and model_bin.stat().st_size > MODEL_MIN_BYTES:
        size_gb = model_bin.stat().st_size / (1024 ** 3)
        return _check("model", "Whisper 权重", "required", True,
                      f"{config['asr']['model']} 已就位（model.bin {size_gb:.2f} GB）", detail=str(model_dir))
    hint = (
        "拉权重（走镜像、关 Xet）："
        f"$env:HF_ENDPOINT='{config['asr']['hfEndpoint']}'; "
        "$env:HF_HUB_DISABLE_XET='1'; "
        f"$env:HF_HOME='{config['asr']['hfHome']}'; "
        f"\"{config.python_exe}\" -c \"from faster_whisper.utils import download_model; "
        f"download_model('{config['asr']['model']}', output_dir=r'{model_dir}')\""
    )
    if model_dir.exists():
        return _check("model", "Whisper 权重", "required", False,
                      f"{model_dir} 存在但缺 model.bin（或文件过小）", next_steps=[hint])
    return _check("model", "Whisper 权重", "required", False,
                  f"还没有下载 {config['asr']['model']}（约 1.6 GB）", next_steps=[hint])


def check_proxy(config: Config) -> dict[str, Any]:
    """系统代理的实况：注册表里开着但端口没在听，是最容易被误判成「断网」的坑。"""
    from common import _proxy_alive, _system_proxy  # noqa: PLC0415

    proxy = _system_proxy()
    if not proxy:
        return _check("proxy", "系统代理", "optional", True, "未启用系统代理，直连")
    if _proxy_alive(proxy):
        return _check("proxy", "系统代理", "optional", True, f"{proxy} 可连", detail=proxy)
    bypassed = bool(config["network"].get("bypassDeadProxy", True))
    return _check("proxy", "系统代理", "optional", False,
                  f"注册表代理 {proxy} 端口不可连（没在监听）"
                  + ("；本插件已只在自己的进程里用 NO_PROXY=* 绕过" if bypassed else "；未启用绕过"),
                  next_steps=[
                      "要么启动代理软件（它没在跑），要么在 Windows 设置里关掉系统代理",
                      "插件不会改注册表；bypassDeadProxy=false 可以关掉绕过行为",
                  ],
                  detail=proxy)


def check_uv(config: Config) -> dict[str, Any]:
    uv = Path(config.raw["uvPath"])
    if uv.exists():
        code, out = _run([str(uv), "--version"], timeout=20)
        if code == 0:
            return _check("uv", "uv（装环境用）", "optional", True, out.strip().splitlines()[0], detail=str(uv))
        return _check("uv", "uv（装环境用）", "optional", False, f"uv 跑不起来：{out.strip()[:120]}")
    return _check("uv", "uv（装环境用）", "optional", False,
                  f"没有 {uv}（只有重装环境时才需要）",
                  next_steps=["运行包里的 scripts/install-env.ps1 会下载 uv 到 <workDir>\\bin\\"])


# ------------------------------------------------------------------ 网络


def _http_get(url: str, timeout_s: float, headers: dict[str, str] | None = None,
              proxy: str | None = None) -> tuple[bool, str, int]:
    import requests  # noqa: PLC0415

    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        response = requests.get(url, headers=headers or {}, timeout=timeout_s, proxies=proxies)
        return True, f"HTTP {response.status_code}", response.status_code
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", 0


def check_bili(config: Config) -> dict[str, Any]:
    import time as _time  # noqa: PLC0415
    import json as _json  # noqa: PLC0415

    timeout_s = max(5.0, float(config["network"]["timeoutMs"]) / 1000.0)
    url = "https://api.bilibili.com/x/web-interface/view?bvid=BV1GJ411x7h7"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    }
    attempts = max(1, int(config["bili"]["maxRetry"]) + 1)
    last = ""
    for attempt in range(attempts):
        ok, why, status = _http_get(url, timeout_s, headers)
        if ok and status == 200:
            return _check("net.bili", "B站接口", "required", True, why, detail=url)
        last = why
        if status == 412 and attempt + 1 < attempts:
            _time.sleep(max(0.5, float(config["bili"]["delayMs"]) / 1000.0))
            continue
        break
    return _check("net.bili", "B站接口", "required", False, last or "不可达",
                  next_steps=["412 是常态：隔几分钟再试，或给本机配 BILI_SESSDATA 环境变量（只从环境变量读）"],
                  detail=url)


def check_hf(config: Config) -> dict[str, Any]:
    timeout_s = max(5.0, float(config["network"]["timeoutMs"]) / 1000.0)
    endpoint = str(config["asr"]["hfEndpoint"]).rstrip("/") + "/"
    ok, why, _status = _http_get(endpoint, timeout_s)
    return _check("net.hf", "模型镜像", "optional", ok, why if ok else f"{endpoint} 不可达：{why}",
                  detail=endpoint)


def check_youtube(config: Config) -> dict[str, Any]:
    timeout_s = max(5.0, float(config["network"]["timeoutMs"]) / 1000.0)
    proxy = str(config["youtube"]["proxy"] or "").strip() or None
    ok, why, _status = _http_get("https://www.youtube.com/", timeout_s, proxy=proxy)
    if ok:
        return _check("net.youtube", "YouTube", "optional", True,
                      f"{why}" + ("（走代理）" if proxy else ""))
    if proxy:
        return _check("net.youtube", "YouTube", "optional", False,
                      f"配了代理 {proxy} 仍不可达：{why}",
                      next_steps=["检查代理端口是否在监听", "改 youtube.proxy 后重跑 lecture_doctor"])
    return _check("net.youtube", "YouTube", "optional", False,
                  f"直连不可达：{why}",
                  next_steps=["需要代理：在插件配置里设 youtube.proxy（如 http://127.0.0.1:7890）",
                              "不通时 YouTube 工具会直接报「需代理」，不会假装成功"])


def check_pypi(config: Config) -> dict[str, Any]:
    timeout_s = max(5.0, float(config["network"]["timeoutMs"]) / 1000.0)
    ok, why, _status = _http_get("https://pypi.org/simple/pip/", timeout_s)
    return _check("net.pypi", "PyPI", "optional", ok, why if ok else f"不可达：{why}")


# ------------------------------------------------------------------ 主流程


def run(config: Config, check_network: bool = True) -> dict[str, Any]:
    checks: list[dict[str, Any]] = [
        check_python(config),
        check_ffmpeg(config),
        check_gpu(config),
        check_vault(config),
        check_workdir(config),
        check_model(config),
        check_uv(config),
        check_proxy(config),
    ]
    if check_network:
        checks.extend([check_bili(config), check_hf(config), check_youtube(config), check_pypi(config)])

    required = [item for item in checks if item["level"] == "required"]
    failed = [item for item in required if not item["ok"]]
    optional_failed = [item for item in checks if item["level"] == "optional" and not item["ok"]]
    ok = not failed

    parts = [f"必需项 {len(required) - len(failed)}/{len(required)} 通过"]
    if optional_failed:
        parts.append(f"提示 {len(optional_failed)} 项")
    if not check_network:
        parts.append("已跳过网络探测")

    next_steps: list[str] = []
    for item in failed:
        for step in item.get("nextSteps", []):
            next_steps.append(f"[{item['title']}] {step}")

    warnings: list[str] = []
    for item in optional_failed:
        warnings.append(f"{item['title']}：{item['why']}")

    return {
        "ok": ok,
        "summary": "环境自检通过：" + "，".join(parts) if ok else "环境自检未通过：" + "，".join(parts),
        "checks": checks,
        "warnings": warnings,
        "nextSteps": next_steps,
        "paths": {
            "vault": str(config.vault_path),
            "workDir": str(config.work_dir),
            "python": str(config.python_exe),
            "ffmpeg": str(config.ffmpeg_path),
            "model": str(config.asr_model_dir()),
            "logs": str(config.logs_dir),
            "uv": str(config.raw["uvPath"]),
        },
    }
