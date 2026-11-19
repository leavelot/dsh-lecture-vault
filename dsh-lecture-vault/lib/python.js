/**
 * python.js — 唯一的「JS → Python」通道。
 *
 * 约定：
 *  - 所有确定性工作都在 py/lecture.py 里，按子命令分发 (`lecture.py doctor --json`)；
 *  - Python 把人类可读日志写 stderr，最后在 stdout 打一行
 *    `@@LECTURE_RESULT@@ {json}` 作为结构化结果；
 *  - 这里负责：拼环境变量、写日志文件、超时与取消、解析那一行 JSON。
 *
 * env 里带上 `LECTURE_CONFIG`（解析后的配置 JSON），Python 侧不再自己猜路径。
 */

import { spawn } from 'node:child_process'
import { createWriteStream, existsSync, mkdirSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const RESULT_PREFIX = '@@LECTURE_RESULT@@'
const STDERR_TAIL_LINES = 40

export const PACKAGE_DIR = path.resolve(fileURLToPath(new URL('..', import.meta.url)))
export const LECTURE_PY = path.join(PACKAGE_DIR, 'py', 'lecture.py')

/**
 * CUDA 的 DLL 在 venv 的 site-packages\nvidia\*\bin 里（pip 装的 nvidia-* 轮子）。
 * ctranslate2 换 cuDNN 9 之后必须靠 PATH 找到它们，否则 GPU 直接不可用。
 */
function nvidiaDllDirs(config) {
  const venvRoot = path.dirname(path.dirname(config.pythonExe)) // .venv
  const nvidiaRoot = path.join(venvRoot, 'Lib', 'site-packages', 'nvidia')
  if (!existsSync(nvidiaRoot)) return []
  const dirs = []
  try {
    for (const name of readdirSync(nvidiaRoot)) {
      const bin = path.join(nvidiaRoot, name, 'bin')
      if (existsSync(bin)) dirs.push(bin)
    }
  } catch {
    /* 读不到就当没有 */
  }
  return dirs
}

/** 组装子进程环境：UTF-8、HF 镜像、ffmpeg/uv/nvidia DLL 都放进 PATH。 */
export function buildEnv(config) {
  const env = { ...process.env }
  env.PYTHONIOENCODING = 'utf-8'
  env.PYTHONUTF8 = '1'
  env.LECTURE_CONFIG = JSON.stringify(config)
  env.HF_HOME = config.asr.hfHome
  env.HF_ENDPOINT = config.asr.hfEndpoint
  if (config.asr.disableXet) env.HF_HUB_DISABLE_XET = '1'

  const pathParts = []
  const ffmpegDir = config.ffmpegPath ? path.dirname(config.ffmpegPath) : ''
  if (ffmpegDir) pathParts.push(ffmpegDir)
  const uvDir = config.uvPath ? path.dirname(config.uvPath) : ''
  if (uvDir) pathParts.push(uvDir)
  pathParts.push(...nvidiaDllDirs(config))
  if (process.env.PATH) pathParts.push(process.env.PATH)
  env.PATH = pathParts.join(';')
  return env
}

function stamp() {
  return new Date().toISOString().replace(/[:.]/g, '-')
}

/**
 * 同步跑一个子命令。
 * @param {object} config 解析后的配置
 * @param {string[]} args lecture.py 的子命令与参数
 * @param {{signal?: AbortSignal, timeoutMs?: number, logLabel?: string}} [options]
 * @returns {Promise<{ok: boolean, value?: object, reason?: string, logPath?: string, exitCode?: number}>}
 */
export async function runLecture(config, args, options = {}) {
  const label = options.logLabel || args[0] || 'run'
  const logDir = path.join(config.workDir, 'logs')
  mkdirSync(logDir, { recursive: true })
  const logPath = path.join(logDir, `${label}-${stamp()}.log`)
  const logStream = createWriteStream(logPath, { flags: 'a' })

  const write = (chunk) => {
    try {
      logStream.write(chunk)
    } catch {
      /* 日志写不进去不该影响主流程 */
    }
  }

  write(`# ${new Date().toISOString()} ${path.basename(config.pythonExe)} ${[LECTURE_PY, ...args].join(' ')}\n`)

  return await new Promise((resolve) => {
    let settled = false
    let stdout = ''
    let stderrTail = []
    let partial = ''
    let timer

    const finish = (result) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      try {
        logStream.end()
      } catch {
        /* ignore */
      }
      resolve({ logPath, ...result })
    }

    const child = spawn(config.pythonExe, [LECTURE_PY, ...args], {
      cwd: config.workDir,
      env: buildEnv(config),
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    })

    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      stdout += chunk
      if (stdout.length > 4 * 1024 * 1024) stdout = stdout.slice(-2 * 1024 * 1024)
    })
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (chunk) => {
      write(chunk)
      partial += chunk
      const lines = partial.split(/\r?\n/)
      partial = lines.pop() ?? ''
      stderrTail.push(...lines)
      if (stderrTail.length > STDERR_TAIL_LINES * 3) stderrTail = stderrTail.slice(-STDERR_TAIL_LINES)
    })

    const onAbort = () => {
      try {
        child.kill()
      } catch {
        /* ignore */
      }
      finish({ ok: false, reason: '任务被取消（exec.signal）' })
    }
    if (options.signal) {
      if (options.signal.aborted) {
        onAbort()
      } else {
        options.signal.addEventListener('abort', onAbort, { once: true })
      }
    }

    if (options.timeoutMs) {
      timer = setTimeout(() => {
        try {
          child.kill()
        } catch {
          /* ignore */
        }
        finish({ ok: false, reason: `超过 ${Math.round(options.timeoutMs / 1000)}s 未完成，已终止` })
      }, options.timeoutMs)
    }

    child.on('error', (error) => {
      finish({
        ok: false,
        reason: error?.code === 'ENOENT'
          ? `找不到 Python：${config.pythonExe}（环境还没装好？先跑 lecture_doctor）`
          : `启动 Python 失败：${error?.message ?? error}`,
      })
    })

    child.on('close', (code) => {
      const parsed = parseResult(stdout)
      if (parsed) {
        finish({ ok: parsed.ok !== false, value: parsed, exitCode: code ?? 0 })
        return
      }
      const tail = stderrTail.slice(-STDERR_TAIL_LINES).join('\n').trim()
      finish({
        ok: false,
        exitCode: code ?? -1,
        reason: `Python 没有给出结构化结果（退出码 ${code ?? -1}）`,
        stderrTail: tail,
      })
    })
  })
}

/** 从 stdout 里取最后一行哨兵 JSON。 */
export function parseResult(stdout) {
  if (typeof stdout !== 'string' || stdout.length === 0) return undefined
  const lines = stdout.split(/\r?\n/)
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const line = lines[i].trim()
    if (!line.startsWith(RESULT_PREFIX)) continue
    try {
      return JSON.parse(line.slice(RESULT_PREFIX.length).trim())
    } catch {
      return undefined
    }
  }
  return undefined
}

export { RESULT_PREFIX }
