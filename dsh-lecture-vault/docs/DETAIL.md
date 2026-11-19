# 听课入库（lecture-to-kb）

把一门网课变成 Obsidian 里的个人知识库。插件只做**确定性的事**：探测、取字幕、转写、词表、课件解析、校验、落盘；蒸馏与写作交给模型按内置 skill `lecture-to-kb` 的流程做。

## 功能

八个工具，一条流水线：

| 工具 | 做什么 | 状态 |
|---|---|---|
| `lecture_doctor` | 环境自检：Python、ffmpeg、GPU、vault 可写性、工作目录、Whisper 权重、平台连通性 | 已实现 |
| `lecture_probe` | 把 BV / 链接 / m3u8 / 本地文件解析成 `course.json`（标题、作者、平台、总时长、分P 表） | P1 |
| `lecture_ingest` | 取原生字幕 → 没有才下音频转写；HLS 直链；长任务转后台 | P1 |
| `lecture_glossary` | 同音 / 差一字两类候选，产出证据表；审过的词表再应用回转录 | P1 |
| `lecture_courseware` | 解析 MD/TXT/PPTX/DOCX/文本型 PDF，抽出**课件里原有的图**与页码锚点 | P1 |
| `lecture_commit` | 按属性规范、双链、marker、`.bak` 写进 vault，先给完整 diff | P1 |
| `lecture_lint` | 时间戳缺口、公式残留、断链、孤儿页 —— 只报告，不改文件 | P1 |
| `lecture_status` | 查后台任务的 state / percent / 日志路径 | 已实现 |

加一个内置 skill **`lecture-to-kb`**：工作流正文（何时停、怎么纠错、六节怎么排、落盘禁忌）。模型按它跑，不靠工具描述里塞流程。

## 用法

1. 先跑 `lecture_doctor`。它绿了才有必要往下走；缺什么它会给出可复制的命令，不静默安装。
2. 用 `lecture_probe` 拿到分P 表，把表给用户看并**停下来问读哪几讲**。
3. 按 `lecture_ingest`（字幕优先）→ `lecture_glossary`（纠错早于蒸馏）→ `lecture_courseware`（补「不读画面」的缺口）→ 写六节讲义 → `lecture_lint` → `lecture_commit`（先 dry-run）的顺序推进。
4. 教学习惯与禁忌写在 skill 里：六节骨架、每条 `HH:MM:SS`、零防御语、疑点收折叠块。

典型输入：`BV1…`、`bilibili.com/video/…`、`youtube.com/watch?v=…`、钉钉回放的签名 m3u8、本地 mp4/mp3、一个课件目录。

## 配置

默认值在插件里，可用 `cordis.patch.yml` 的 `config` 覆盖（深合并）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `vaultPath` | `D:\工具\笔记` | 必须含 `.obsidian`，否则拒绝写入 |
| `workDir` | `D:\工具\课堂记录` | venv、模型、会话产物、日志都在这 |
| `pythonExe` | `<workDir>\.venv\Scripts\python.exe` | venv 解释器 |
| `ffmpegPath` | `D:\dsh插件\ffmpeg\bin\ffmpeg.exe` | ffprobe 与它同目录 |
| `asr` | `large-v3-turbo` / CPU `small` / `int8_float16` / `https://hf-mirror.com` / 关 Xet | 首次要拉约 1.6 GB 权重 |
| `bili` | `delayMs 1000`、`cookieEnv BILI_SESSDATA`、`maxRetry 2` | cookie **只从环境变量读** |
| `youtube` | `proxy: ""`、`subLangs "zh-Hans,zh,en"` | 不通就明说「需代理」 |
| `dingtalk` | `referer`、`cookiesFromBrowser: edge` | 签名 URL 会过期 |
| `vault` | 白名单目录、`.bak` 保留 30 天、`dryRun` | 白名单外只读 |
| `network` | `timeoutMs 15000`、`bypassDeadProxy true` | 平台探测超时；系统代理端口没在听时只对插件进程绕过 |

凭据只从环境变量或 `$DSH_HOME/.env` 读；不进配置、不进日志、不进笔记。

## 限制（v1 明确的边界）

- **不读视频画面**：不抽帧、不做视觉理解。缺的板书与公式用课件补，补不上写章末「注：」。
- **YouTube 需先配代理**：本机直连 `youtube.com` 超时。doctor 先探测，不通就直接说「需配置 `youtube.proxy`」，不假装成功、不产出半成品。
- **学习通只做本地导入**：不抓取、不刷课时、不自动答题、不绕任何限制。
- **钉钉回放**靠用户从 DevTools 复制含签名的 m3u8；过期就得重抓。v1 不做录屏兜底。
- 不做云 ASR；云 OCR 默认关且必须用户显式同意（上传第三方 = 用户决定）。
- 不做 GUI、不做 Obsidian 插件、不做 MCP server、不做向量库 RAG。
- 音视频、字幕、课件原件**不进 vault**，只进 `<workDir>\sessions\`。

## 环境安装（P0）

包里的 `scripts/install-env.ps1` 做一次性安装：uv → Python 3.11 → venv → 依赖 → Whisper 权重，全部落在 `workDir`，幂等可重跑，每步先打印命令。

两处与常见做法不同的地方，都是本机实测逼出来的：

- **Python 用 nuget 的 CPython（解压即用），既不用 `uv python install` 也不用 MSI 安装器**。这台机器开着 Smart App Control（`VerifiedAndReputablePolicyState=1`）：uv 托管的 python-build-standalone 没有代码签名，一执行就被策略拦掉（`An Application Control policy has blocked this file`）；python.org 的安装器以非提权身份装到自定义目录时返回 `0x80070005`（拒绝访问）。nuget.org 上的 `python` 包是同一份 CPython（带 venv / ensurepip），解压即可用。
- **下载走 node 的 CA 库**。本机子进程里 schannel 取不到凭证（`SEC_E_NO_CREDENTIALS`），`curl` / `Invoke-WebRequest` 一律失败；`node` 自带 CA 能正常访问 PyPI / GitHub / hf-mirror。
- **死代理绕过**。本机注册表里的系统代理是开着的（`ProxyEnable=1`，指向一个没在监听的本机端口），`pip` / `uv` / `requests` 都会读它，于是全部报 `ProxyError: Cannot connect to proxy`，看着像断网。安装脚本和插件进程都会先探测该端口：连不上就只在本次进程里设 `NO_PROXY=*`，不改注册表、不改系统设置；`lecture_doctor` 会把这件事作为一行检查报出来。
- **网络很差时的离线路径**。长连接经常在下载中途被掐断（`uv` 报 network timeout、`pip` 报 ReadTimeout），但几 MB 的 Range 请求是稳的。这时用包里的两个脚本：

  ```powershell
  # 1) 把依赖解析成清单，再按 2MB 分块、多镜像轮换下到本地 wheelhouse
  uv pip compile py\requirements.txt --python <venv>\Scripts\python.exe -o resolved.txt
  node scripts\fetch-wheels.mjs resolved.txt .wheels
  # 2) 离线安装（install-env.ps1 也接受 -Wheelhouse 参数）
  uv pip install --python <venv>\Scripts\python.exe --no-index --find-links .wheels -r py\requirements.txt
  uv pip install --python <venv>\Scripts\python.exe --no-index --find-links .wheels -r py\requirements-cuda.txt
  # 3) 权重同理（huggingface_hub 的长连接也会超时）
  node scripts\fetch-model.mjs mobiuslabsgmbh/faster-whisper-large-v3-turbo <workDir>\models\whisper\large-v3-turbo
  ```

GPU 转写需要 cuBLAS 12、cuDNN 9 与 nvrtc 的 DLL（`py/requirements-cuda.txt`）；它们作为 `nvidia-*` 轮子装进 venv，插件在起 Python 子进程时会把 `site-packages\nvidia\*\bin` 加进 PATH。`lecture_doctor` 会分别检查「驱动看得到设备」和「cuDNN 的 DLL 在」——只看设备数会在缺 cuDNN 时误报绿灯。

## 已知未验证

- 插件详情页里标题 / 描述 / 图标的**目视**效果：桌面端输入策略不允许代理操作 DSH 自己的窗口（`HOST_INPUT_DENIED`），需要人眼在 Plugin Manager 里看一眼。资源本身已核对：`locale/{zh,en}.json` 可解析、`meta.detail` 指向的 `docs/DETAIL.md` 存在、`icon.svg` 是合法 SVG 且 852 字节。
- P1 起才有真实课程的端到端链路（转写耗时、token 估算）；P0 只验证环境与落盘通道。

## 沙箱实测结论（P0 验收项）

在**插件进程**里跑 `lecture_doctor`，vault 写探测是通过的：插件自己的 `child_process` 不受 DSH 文件沙箱限制，可以直接写 `D:\工具\笔记` 的白名单目录。所以不需要「写 workspace + 一键导入」那条兜底路径。
（同一份检查用 `pwsh` 工具的子进程跑会拿到 `PermissionError [WinError 5]` —— 沙箱只作用于 DSH 的工具调用，不作用于插件进程。）

## 数据与隐私

音频、字幕、课件原件只写 `<workDir>\sessions\<课程>\`，vault 里只放笔记与从课件里取出的原图。任何上传都要用户当次明确同意。删除工作目录即等于删掉本地原始材料。
