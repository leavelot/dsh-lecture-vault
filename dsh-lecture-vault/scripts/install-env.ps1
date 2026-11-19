<#
.SYNOPSIS
  听课入库 · 一次性环境安装（uv → Python 3.11 → venv → 依赖 → Whisper 权重）。

.DESCRIPTION
  设计原则：
    - 每一步都先把命令打出来再执行，不静默改系统 PATH / 不装服务 / 不写注册表自启；
    - 可重跑：已经装好的步骤直接跳过（幂等），失败后从断点继续；
    - 所有产物都在 <Root> 下（默认 D:\工具\课堂记录），C 盘只落临时文件。

  为什么是 nuget 的 CPython 而不是 `uv python install`：
  本机开着 **Smart App Control**（VerifiedAndReputablePolicyState=1）。uv 托管的
  python-build-standalone 没有代码签名，执行时被策略拦掉
  （"An Application Control policy has blocked this file"）。
  nuget.org 上的 `python` 包（PSF 发布的同一份 CPython）里的 python.exe 能正常执行，
  而且带完整 Lib（venv / ensurepip 都在），解压即用、不需要安装器与管理员权限。
  python.org 的 MSI 安装器在本机以非提权身份装着会返回 0x5（拒绝访问），所以不走那条路。

.PARAMETER Root
  工作区根目录。venv / 模型 / 会话产物都在这里。

.PARAMETER NodeExe
  下载兜底用的 node.exe。本机 schannel 取不到凭证时，curl / Invoke-WebRequest 都会失败，
  这时用 node 自带的 CA 库下载。留空则自动在 PATH 里找。

.PARAMETER UvZip / PythonZip
  已经下好的 uv zip / python nupkg 路径（离线或重复安装时省一次下载）。

.PARAMETER IndexUrl
  pip 索引（默认 PyPI 官方；国内可换 https://pypi.tuna.tsinghua.edu.cn/simple）。

.PARAMETER Wheelhouse
  已经用 scripts\fetch-wheels.mjs 下好的本地轮子目录。给了就走 --no-index 离线安装，
  完全不碰网络（长连接被掐断时这是最稳的一条路）。

.PARAMETER SkipModel
  只装环境，不拉 1.6 GB 的 Whisper 权重。

.PARAMETER SkipCuda
  不装 CUDA 运行库（约 1.1 GB）。装了 GPU 才用得上；装不上也不影响 CPU 转写。

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-env.ps1 -Root 'D:\工具\课堂记录'

.NOTES
  未签名的 .ps1 在 RemoteSigned 策略下会被拒（"未对文件进行数字签名"），
  所以上面用 -ExecutionPolicy Bypass —— 只作用于这一次进程，不改系统策略。
#>
[CmdletBinding()]
param(
  [string]$Root = 'D:\工具\课堂记录',
  [string]$NodeExe = '',
  [string]$UvZip = '',
  [string]$PythonZip = '',
  [string]$IndexUrl = '',
  [string]$Wheelhouse = '',
  [switch]$SkipModel,
  [switch]$SkipCuda
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$packageDir = Split-Path -Parent $scriptDir
$downloadScript = Join-Path $scriptDir 'download.mjs'
$requirements = Join-Path $packageDir 'py\requirements.txt'
$requirementsCuda = Join-Path $packageDir 'py\requirements-cuda.txt'

$UV_URL = 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip'
$PY_VERSION = '3.11.9'
$PY_URL = "https://www.nuget.org/api/v2/package/python/$PY_VERSION"

function Write-Step([string]$Message) {
  Write-Host ''
  Write-Host "== $Message" -ForegroundColor Cyan
}

function Write-Cmd([string]$Message) {
  Write-Host "   > $Message" -ForegroundColor DarkGray
}

function Resolve-Node {
  if ($NodeExe -and (Test-Path -LiteralPath $NodeExe)) { return $NodeExe }
  $cmd = Get-Command node -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  foreach ($candidate in @('D:\dsh插件\nodejs\node.exe', 'C:\Program Files\nodejs\node.exe')) {
    if (Test-Path -LiteralPath $candidate) { return $candidate }
  }
  return ''
}

function Invoke-Download([string]$Url, [string]$OutFile) {
  if (Test-Path -LiteralPath $OutFile) { return }
  Write-Cmd "download $Url -> $OutFile"
  $parent = Split-Path -Parent $OutFile
  if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
  try {
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $Url -OutFile $OutFile -TimeoutSec 180 -MaximumRedirection 10
    return
  } catch {
    Write-Host "   Invoke-WebRequest 失败：$($_.Exception.Message)" -ForegroundColor Yellow
  }
  $node = Resolve-Node
  if (-not $node) {
    throw "下载失败，而且没找到 node.exe 可做兜底。请手动下载并放到：$OutFile（来源 $Url）"
  }
  Write-Cmd "node $downloadScript $Url $OutFile"
  & $node $downloadScript $Url $OutFile
  if ($LASTEXITCODE -ne 0) { throw "node 下载也失败：$Url" }
}

function Get-SystemProxy {
  try {
    $key = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction Stop
    if ($key.ProxyEnable -eq 1 -and $key.ProxyServer) { return [string]$key.ProxyServer }
  } catch {
    return ''
  }
  return ''
}

function Test-ProxyPort([string]$ProxyString) {
  foreach ($entry in ($ProxyString -replace ' ', '').Split(';')) {
    $target = if ($entry.Contains('=')) { $entry.Split('=', 2)[1] } else { $entry }
    $target = $target -replace '^https?://', ''
    $hostName = $target
    $port = 80
    if ($target.Contains(':')) {
      $hostName = $target.Split(':')[0]
      $portText = $target.Split(':')[1]
      if (-not [int]::TryParse($portText, [ref]$port)) { $port = 80 }
    }
    if (-not $hostName) { continue }
    try {
      $client = New-Object System.Net.Sockets.TcpClient
      $async = $client.BeginConnect($hostName, $port, $null, $null)
      if ($async.AsyncWaitHandle.WaitOne(1500)) {
        $client.EndConnect($async)
        $client.Close()
        return $true
      }
      $client.Close()
    } catch {
      # 连接被拒/解析失败都算不可用
    }
  }
  return $false
}

<#
  系统代理开着但端口没在监听时，pip / uv / requests 会一律报
  "ProxyError: Cannot connect to proxy"（注册表 WinINET 设置会被它们读走）。
  这里探测一次：不可连就只对本次安装进程绕过（NO_PROXY=*），并如实打印。
#>
function Resolve-ProxyEnv {
  $proxy = Get-SystemProxy
  if (-not $proxy) {
    Write-Host '   系统未启用代理，直连'
    return
  }
  if (Test-ProxyPort $proxy) {
    Write-Host "   系统代理 $proxy 可连，本次安装走代理"
    return
  }
  Write-Host "   系统代理 $proxy 端口不可连（没在监听），本次安装绕过它" -ForegroundColor Yellow
  foreach ($name in @('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')) {
    Remove-Item "Env:$name" -ErrorAction SilentlyContinue
  }
  $env:NO_PROXY = '*'
  $env:no_proxy = '*'
  Write-Host '   → 仅本次进程设置 NO_PROXY=*（不改注册表、不改系统设置）' -ForegroundColor DarkGray
}

Write-Host "听课入库 · 环境安装" -ForegroundColor Green
Write-Host "  Root          = $Root"
Write-Host "  包目录        = $packageDir"

Write-Step "0/6 目录"
foreach ($sub in @('bin', 'models', 'sessions', 'logs', 'logs\jobs', 'cache', 'downloads')) {
  $target = Join-Path $Root $sub
  Write-Cmd "mkdir $target"
  New-Item -ItemType Directory -Force -Path $target | Out-Null
}
$logFile = Join-Path $Root 'logs\install-env.log'
Start-Transcript -Path $logFile -Append | Out-Null

try {
  Write-Step "代理探测"
  Resolve-ProxyEnv

  Write-Step "1/6 uv"
  $uvExe = Join-Path $Root 'bin\uv.exe'
  if (Test-Path -LiteralPath $uvExe) {
    Write-Host "   已有 uv：$((& $uvExe --version) 2>&1)"
  } else {
    $zip = if ($UvZip) { $UvZip } else { Join-Path $Root 'downloads\uv.zip' }
    Invoke-Download -Url $UV_URL -OutFile $zip
    $unzipDir = Join-Path $Root 'downloads\uv'
    Write-Cmd "tar -xf $zip -C $unzipDir"
    New-Item -ItemType Directory -Force -Path $unzipDir | Out-Null
    & tar.exe -xf $zip -C $unzipDir
    Copy-Item (Join-Path $unzipDir 'uv.exe') $uvExe -Force
    Write-Host "   uv 就位：$((& $uvExe --version) 2>&1)"
  }

  Write-Step "2/6 Python $PY_VERSION（nuget 的 CPython，解压即用；避开 Smart App Control 与 MSI 提权）"
  $pyRoot = Join-Path $Root 'python'
  $pyExe = Join-Path $pyRoot 'python.exe'
  if (Test-Path -LiteralPath $pyExe) {
    Write-Host "   已有 Python：$((& $pyExe -V) 2>&1)"
  } else {
    $zip = if ($PythonZip) { $PythonZip } else { Join-Path $Root 'downloads\python.zip' }
    Invoke-Download -Url $PY_URL -OutFile $zip
    $staging = Join-Path $Root 'downloads\pynuget'
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    Write-Cmd "tar -xf $zip -C $staging"
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    & tar.exe -xf $zip -C $staging
    Write-Cmd "copy $staging\tools\* -> $pyRoot"
    New-Item -ItemType Directory -Force -Path $pyRoot | Out-Null
    Copy-Item (Join-Path $staging 'tools\*') $pyRoot -Recurse -Force
    if (-not (Test-Path -LiteralPath $pyExe)) { throw "解压后没找到 $pyExe" }
    Write-Host "   Python 就位：$((& $pyExe -V) 2>&1)"
  }

  Write-Step "3/6 venv"
  $venvDir = Join-Path $Root '.venv'
  $venvPy = Join-Path $venvDir 'Scripts\python.exe'
  if (Test-Path -LiteralPath $venvPy) {
    Write-Host "   已有 venv：$venvPy"
  } else {
    Write-Cmd "$pyExe -m venv $venvDir"
    & $pyExe -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw "venv 创建失败" }
  }
  Write-Host "   venv python：$((& $venvPy -V) 2>&1)"

  Write-Step "4/6 依赖（faster-whisper / yt-dlp / PDF）"
  $env:UV_CACHE_DIR = Join-Path $Root 'cache\uv'
  $pipArgs = @('pip', 'install', '--python', $venvPy, '-r', $requirements, '--no-progress')
  if ($Wheelhouse) {
    $pipArgs += @('--no-index', '--find-links', $Wheelhouse)
  } elseif ($IndexUrl) {
    $pipArgs += @('--index-url', $IndexUrl)
  }
  Write-Cmd "uv $($pipArgs -join ' ')"
  & $uvExe @pipArgs
  if ($LASTEXITCODE -ne 0) { throw "依赖安装失败（网络或索引问题；可用 -IndexUrl 换源，或先用 scripts\fetch-wheels.mjs 下到本地再 -Wheelhouse）" }

  if ($SkipCuda) {
    Write-Step "4b/6 跳过 CUDA 运行库（-SkipCuda）"
  } else {
    Write-Step "4b/6 CUDA 运行库（cuBLAS 12 + cuDNN 9 + nvrtc，约 1.2 GB；失败只影响 GPU，不影响 CPU 转写）"
    $cudaArgs = @('pip', 'install', '--python', $venvPy, '-r', $requirementsCuda, '--no-progress')
    if ($Wheelhouse) {
      $cudaArgs += @('--no-index', '--find-links', $Wheelhouse)
    } elseif ($IndexUrl) {
      $cudaArgs += @('--index-url', $IndexUrl)
    }
    Write-Cmd "uv $($cudaArgs -join ' ')"
    & $uvExe @cudaArgs
    if ($LASTEXITCODE -ne 0) {
      Write-Host '   CUDA 运行库没装成：GPU 转写会不可用，CPU small 照常能跑（稍后重跑本脚本即续上）' -ForegroundColor Yellow
    }
  }

  Write-Step "5/6 自检（导入 + GPU）"
  $env:HF_HOME = Join-Path $Root 'models'
  $probe = @'
import glob, json, os, sys
out = {}
try:
    import faster_whisper, ctranslate2
    out["faster_whisper"] = getattr(faster_whisper, "__version__", "?")
    out["ctranslate2"] = getattr(ctranslate2, "__version__", "?")
    nvidia = os.path.join(os.path.dirname(sys.executable), "..", "Lib", "site-packages", "nvidia")
    for d in glob.glob(os.path.join(nvidia, "*", "bin")):
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(d)
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    out["cuda_devices"] = ctranslate2.get_cuda_device_count()
except Exception as exc:
    out["error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out, ensure_ascii=False))
'@
  Write-Cmd "venv python -c <import faster_whisper / ctranslate2 probe>"
  $probeOut = & $venvPy -c $probe
  Write-Host "   $probeOut"

  if (-not $SkipModel) {
    Write-Step "6/6 Whisper 权重（约 1.6 GB，走镜像）"
    $modelDir = Join-Path $Root 'models\whisper\large-v3-turbo'
    $modelBin = Join-Path $modelDir 'model.bin'
    if (Test-Path -LiteralPath $modelBin) {
      Write-Host "   已有权重：$modelBin"
    } else {
      $env:HF_ENDPOINT = 'https://hf-mirror.com'
      $env:HF_HUB_DISABLE_XET = '1'
      $env:HF_HOME = Join-Path $Root 'models'
      $fetch = @"
from faster_whisper.utils import download_model
print(download_model('large-v3-turbo', output_dir=r'$modelDir'))
"@
      Write-Cmd "venv python -c <download_model large-v3-turbo>"
      & $venvPy -c $fetch
      if ($LASTEXITCODE -ne 0) { throw "权重下载失败：检查 HF_ENDPOINT 或手动下载到 $modelDir" }
    }
  } else {
    Write-Step "6/6 跳过权重下载（-SkipModel）"
  }

  Write-Host ''
  Write-Host "完成。下一步：在 DSH 里跑 lecture_doctor 验收（日志：$logFile）" -ForegroundColor Green
} catch {
  Write-Host ''
  Write-Host "失败：$($_.Exception.Message)" -ForegroundColor Red
  Write-Host "断点信息已在上面的步骤号里；修好后重跑同一命令即可（已完成的步骤会跳过）。" -ForegroundColor Yellow
  throw
} finally {
  Stop-Transcript | Out-Null
}
