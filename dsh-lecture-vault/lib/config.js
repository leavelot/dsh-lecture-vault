/**
 * config.js — 配置默认值与派生路径。
 *
 * 插件的 Config 不导出 schema（宿主 Config 声明要用 schemastery，profile 里的插件解析不到），
 * 所以这里只做一件事：把 cordis.patch.yml 里那一行 `config` 深合并到默认值上，
 * 并把「依赖 workDir 的派生路径」（venv、模型、uv）算出来。
 * 所有路径都来自配置，代码里没有写死的用户名路径。
 */

import path from 'node:path'

export const DEFAULT_CONFIG = {
  vaultPath: 'D:\\工具\\笔记',
  workDir: 'D:\\工具\\课堂记录',
  /** 留空 = <workDir>\.venv\Scripts\python.exe */
  pythonExe: '',
  ffmpegPath: 'D:\\dsh插件\\ffmpeg\\bin\\ffmpeg.exe',
  /** 留空 = <workDir>\bin\uv.exe */
  uvPath: '',
  asr: {
    model: 'large-v3-turbo',
    cpuModel: 'small',
    compute: 'int8_float16',
    hfEndpoint: 'https://hf-mirror.com',
    disableXet: true,
    /** 留空 = <workDir>\models */
    hfHome: '',
  },
  bili: {
    delayMs: 1000,
    cookieEnv: 'BILI_SESSDATA',
    maxRetry: 2,
  },
  youtube: {
    proxy: '',
    subLangs: 'zh-Hans,zh,en',
  },
  dingtalk: {
    referer: '',
    cookiesFromBrowser: 'edge',
  },
  vault: {
    allowedDirs: ['00_Inbox', '10_课程', '20_概念', '30_题目', '40_复盘', '50_方法库', '60_看板', '90_附件'],
    backupKeepDays: 30,
    dryRun: false,
  },
  network: {
    timeoutMs: 15000,
    /**
     * 系统代理开着但端口没在听时，requests / pip / huggingface_hub 会一律 ProxyError
     * （它们读 Windows 注册表里的 WinINET 设置）。true = 只在插件自己的 Python 进程里
     * 设 NO_PROXY=* 绕过，不改注册表、不改系统设置。
     */
    bypassDeadProxy: true,
  },
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function deepMerge(base, patch) {
  if (!isPlainObject(patch)) return base
  const out = { ...base }
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined) continue
    out[key] = isPlainObject(value) && isPlainObject(base?.[key]) ? deepMerge(base[key], value) : value
  }
  return out
}

/**
 * 解析最终配置：默认值 ← 插件行 config，再补齐派生路径。
 * @param {unknown} raw 插件行 config
 * @returns {{config: object, warnings: string[]}}
 */
export function resolveConfig(raw) {
  const warnings = []
  const merged = deepMerge(DEFAULT_CONFIG, isPlainObject(raw) ? raw : {})

  const workDir = String(merged.workDir || DEFAULT_CONFIG.workDir)
  const config = {
    ...merged,
    workDir,
    vaultPath: String(merged.vaultPath || DEFAULT_CONFIG.vaultPath),
    pythonExe: String(merged.pythonExe || path.join(workDir, '.venv', 'Scripts', 'python.exe')),
    uvPath: String(merged.uvPath || path.join(workDir, 'bin', 'uv.exe')),
    ffmpegPath: String(merged.ffmpegPath || DEFAULT_CONFIG.ffmpegPath),
    asr: {
      ...merged.asr,
      hfHome: String(merged.asr?.hfHome || path.join(workDir, 'models')),
    },
    bili: { ...merged.bili },
    youtube: { ...merged.youtube },
    dingtalk: { ...merged.dingtalk },
    vault: {
      ...merged.vault,
      allowedDirs: Array.isArray(merged.vault?.allowedDirs) && merged.vault.allowedDirs.length > 0
        ? merged.vault.allowedDirs.map(String)
        : [...DEFAULT_CONFIG.vault.allowedDirs],
    },
    network: { ...merged.network },
  }

  // 只对「真的写错了」的情况出声，不要因为默认值没被改过就报警。
  if (!path.isAbsolute(config.vaultPath)) warnings.push(`vaultPath 不是绝对路径：${config.vaultPath}`)
  if (!path.isAbsolute(config.workDir)) warnings.push(`workDir 不是绝对路径：${config.workDir}`)

  return { config, warnings }
}

/** 模型目录：<workDir>\models\whisper\<model> */
export function whisperModelDir(config, model = config.asr.model) {
  return path.join(config.asr.hfHome, 'whisper', String(model))
}

/** 日志目录（每次任务一份）。 */
export function logsDir(config) {
  return path.join(config.workDir, 'logs')
}

/** 任务状态目录：lecture_status 读这里。 */
export function jobsDir(config) {
  return path.join(config.workDir, 'logs', 'jobs')
}
