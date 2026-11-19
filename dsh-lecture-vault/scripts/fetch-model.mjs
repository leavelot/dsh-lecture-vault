// 从 hf-mirror 拉 Whisper 权重：2MB 分块 + 重试 + 断点续传。
// huggingface_hub 的长连接在这条链路上会超时，这里自己下载再让 faster-whisper 直接读目录。
// usage: node fetch-model.mjs <repo> <outDir> [endpoint]
import { createWriteStream } from 'node:fs'
import { mkdir, open, readdir, stat } from 'node:fs/promises'
import path from 'node:path'
import { pipeline } from 'node:stream/promises'

const [, , repo = 'Systran/faster-whisper-large-v3-turbo', outDir, endpoint = 'https://hf-mirror.com'] = process.argv
if (!outDir) {
  console.error('usage: node fetch-model.mjs <repo> <outDir> [endpoint]')
  process.exit(2)
}
const CHUNK = 2 * 1024 * 1024
const RETRIES = 8

const api = `${endpoint}/api/models/${repo}`
const meta = await (await fetch(api, { signal: AbortSignal.timeout(30000) })).json()
const files = (meta.siblings ?? []).map((s) => s.rfilename).filter((n) => !n.includes('/') || n.endsWith('.json'))
console.log(`repo=${repo} files=${files.join(', ')}`)

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function download(relPath) {
  const url = `${endpoint}/${repo}/resolve/main/${relPath}`
  const outPath = path.join(outDir, relPath)
  await mkdir(path.dirname(outPath), { recursive: true })

  // 先拿总长度
  let total = 0
  try {
    const head = await fetch(url, { method: 'HEAD', redirect: 'follow', signal: AbortSignal.timeout(30000) })
    total = Number(head.headers.get('content-length') ?? 0)
  } catch {
    total = 0
  }
  let pos = 0
  try {
    pos = (await stat(outPath)).size
  } catch {
    pos = 0
  }
  if (total > 0 && pos === total) {
    console.log(`  = ${relPath} 已完整 ${(total / 1048576).toFixed(1)} MB`)
    return
  }
  if (total > 0 && pos > total) {
    pos = 0
  }

  const handle = await open(outPath, pos > 0 ? 'r+' : 'w')
  try {
    for (;;) {
      const end = pos + CHUNK - 1
      let chunk
      let lastError
      for (let attempt = 0; attempt < RETRIES; attempt += 1) {
        try {
          const res = await fetch(url, {
            headers: { Range: `bytes=${pos}-${end}` },
            redirect: 'follow',
            signal: AbortSignal.timeout(60000),
          })
          if (res.status !== 206 && res.status !== 200) throw new Error(`HTTP ${res.status}`)
          if ((res.headers.get('content-type') ?? '').includes('text/html')) throw new Error('HTML 响应')
          chunk = Buffer.from(await res.arrayBuffer())
          if (res.status === 200) chunk = chunk.subarray(pos)
          if (chunk.length === 0) throw new Error('空响应')
          break
        } catch (error) {
          lastError = error
          await sleep(500 * (attempt + 1))
        }
      }
      if (!chunk) throw new Error(`@${pos} 失败：${lastError?.message ?? lastError}`)
      await handle.write(chunk, 0, chunk.length, pos)
      pos += chunk.length
      const label = total > 0 ? `${(pos / 1048576).toFixed(1)}/${(total / 1048576).toFixed(1)} MB` : `${(pos / 1048576).toFixed(1)} MB`
      process.stdout.write(`\r  ↓ ${relPath} ${label}`)
      if (chunk.length < CHUNK) break
    }
    process.stdout.write(`\n  ✓ ${relPath} ${(pos / 1048576).toFixed(1)} MB\n`)
  } finally {
    await handle.close()
  }
}

let failures = 0
for (const relPath of files) {
  try {
    await download(relPath)
  } catch (error) {
    console.log(`\n  ! ${relPath} 失败：${String(error?.message ?? error).slice(0, 90)}`)
    failures += 1
  }
}
console.log(`done. failures=${failures}`)
