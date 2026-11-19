/**
 * jobs.js — 后台任务的登记与查询。
 *
 * 长任务（下载 / 转写）转后台：工具立刻返回 jobId，Python 进程自己把进度写进
 * `<workDir>\logs\jobs\<jobId>.json`，`lecture_status` 只读这份文件 ——
 * 宿主重启、换个会话来问，状态都还在。
 */

import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { jobsDir } from './config.js'
import { LECTURE_PY, buildEnv } from './python.js'

export function newJobId(kind) {
  const now = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  const rand = Math.random().toString(36).slice(2, 6)
  return `${kind}-${stamp}-${rand}`
}

export function jobStatusPath(config, jobId) {
  return path.join(jobsDir(config), `${jobId}.json`)
}

export function writeJobStatus(config, jobId, patch) {
  const dir = jobsDir(config)
  mkdirSync(dir, { recursive: true })
  const file = jobStatusPath(config, jobId)
  let current = {}
  if (existsSync(file)) {
    try {
      current = JSON.parse(readFileSync(file, 'utf8'))
    } catch {
      current = {}
    }
  }
  const next = { ...current, ...patch, jobId, updatedAt: new Date().toISOString() }
  writeFileSync(file, `${JSON.stringify(next, null, 2)}\n`, 'utf8')
  return next
}

/** 读一个任务的状态；不存在时返回 undefined（lecture_status 据此给原因，不抛异常）。 */
export function readJob(config, jobId) {
  const file = jobStatusPath(config, jobId)
  if (!existsSync(file)) return undefined
  try {
    return JSON.parse(readFileSync(file, 'utf8'))
  } catch {
    return { jobId, state: 'unreadable', logPath: file }
  }
}

/** 最近的任务（按文件名里的时间戳倒序，不解析文件内容）。 */
export function listJobs(config, limit = 10) {
  const dir = jobsDir(config)
  if (!existsSync(dir)) return []
  const files = readdirSync(dir).filter((name) => name.endsWith('.json')).sort().reverse().slice(0, limit)
  const jobs = []
  for (const name of files) {
    const jobId = name.slice(0, -'.json'.length)
    const job = readJob(config, jobId)
    if (job) jobs.push(job)
  }
  return jobs
}

/**
 * 起一个后台任务：立刻返回，不阻塞工具调用。
 * Python 侧收到 `--job-id` 会自己更新状态文件（含 percent 与 message）。
 */
export function startJob(config, { kind, args, label }) {
  const jobId = newJobId(kind)
  const logDir = path.join(config.workDir, 'logs')
  mkdirSync(logDir, { recursive: true })
  const logPath = path.join(logDir, `${jobId}.log`)
  writeJobStatus(config, jobId, {
    state: 'running',
    kind,
    label: label ?? kind,
    percent: 0,
    message: '已启动',
    logPath,
    startedAt: new Date().toISOString(),
  })

  const child = spawn(config.pythonExe, [LECTURE_PY, ...args, '--job-id', jobId], {
    cwd: config.workDir,
    env: buildEnv(config),
    windowsHide: true,
    stdio: ['ignore', 'ignore', 'ignore'],
    detached: true,
  })
  child.on('error', (error) => {
    writeJobStatus(config, jobId, {
      state: 'failed',
      percent: 100,
      message: `启动失败：${error?.message ?? error}`,
      finishedAt: new Date().toISOString(),
    })
  })
  child.on('close', (code) => {
    const current = readJob(config, jobId)
    if (current && current.state === 'running') {
      writeJobStatus(config, jobId, {
        state: code === 0 ? 'done' : 'failed',
        percent: 100,
        message: code === 0 ? '完成' : `进程退出（code ${code}）`,
        finishedAt: new Date().toISOString(),
      })
    }
  })
  child.unref()

  return { jobId, logPath }
}
