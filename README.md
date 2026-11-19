# dsh-lecture-vault · 听课入库

把一门网课（B 站 / YouTube / 钉钉回放 / 本地视频）变成 Obsidian 里的课程知识库：
探测分P → 确认范围 → 取字幕或本地转写 → 术语纠错 → 写六节讲义 → 时间戳回链与校验 → 按规范落进 vault。

这是一个 DeepSeek Harness 的 Host 插件包（bundle），装好后提供 8 个 `lecture_*` 工具和 1 个 `lecture-to-kb` 内置技能。

## 目录

| 路径 | 内容 |
|---|---|
| `听课入库-插件工作流.md` | 施工规格 + 运行时流程（agent 照这个跑） |
| `lecture-to-kb/SKILL.md` | 内置技能原文 |
| `dsh-lecture-vault/` | 插件源码（JS 工具层 + Python 逻辑层） |
| `dsh-lecture-vault/py/` | doctor / probe / ingest / asr / glossary / commit / lint |
| `dsh-lecture-vault/scripts/` | 环境安装、wheelhouse 与模型下载 |
| `恢复说明.md` | 本包的来源与完整性说明 |

## 安装

1. 把 `dsh-lecture-vault` 放到一个稳定位置，例如 `D:\tools\dsh-lecture-vault`。
2. 在 DSH 里安装本地链接包：`plugin_manager install_bundle`，target 填 `link:D:\tools\dsh-lecture-vault`。
3. 跑 `lecture_doctor` 自检；缺什么按提示补。

## 环境

- Node.js >= 20（DSH 自带）
- Python 3.11 + ffmpeg
- 语音转写：faster-whisper（CUDA 或 CPU）
- 一键安装：`powershell -NoProfile -ExecutionPolicy Bypass -File dsh-lecture-vault\scripts\install-env.ps1`

## 状态

P0 通过（doctor 7/7，8 工具 + 1 技能可见）；P1 的 B 站垂直切片端到端跑通。
未实现：`lecture_courseware`、YouTube 代理、钉钉 m3u8、学习通本地导入、闪卡 / 错题 / PDF 导出。

## 许可

未声明。使用前请与仓库作者确认。