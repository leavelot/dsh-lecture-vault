// fetch-wheels.mjs — 网络很差时的离线安装路径。
//
// 直连 PyPI 时，长连接经常在下载中途被掐断（uv 报 network timeout、pip 报 ReadTimeout），
// 但「一次几 MB 的 Range 请求」是稳的。所以这里把解析好的依赖清单按 2MB 分块、
// 在多镜像之间轮换重试，全部落到本地 wheelhouse；之后离线安装：
//
//   uv pip compile py/requirements.txt --python <venv python> -o resolved.txt
//   node scripts/fetch-wheels.mjs resolved.txt .wheels
//   uv pip install --python <venv python> --no-index --find-links .wheels -r py/requirements.txt
//
// usage: node fetch-wheels.mjs <resolved.txt> <wheelDir> [pkg,pkg]
import { open, mkdir, readdir, readFile, stat } from 'node:fs/promises'
import path from 'node:path'

const MIRRORS = [
  { name: 'tuna', simple: 'https://pypi.tuna.tsinghua.edu.cn/simple', packages: 'https://pypi.tuna.tsinghua.edu.cn/packages' },
  { name: 'aliyun', simple: 'https://mirrors.aliyun.com/pypi/simple', packages: 'https://mirrors.aliyun.com/pypi/packages' },
  { name: 'ustc', simple: 'https://mirrors.ustc.edu.cn/pypi/simple', packages: 'https://mirrors.ustc.edu.cn/pypi/packages' },
  { name: 'tencent', simple: 'https://mirrors.cloud.tencent.com/pypi/simple', packages: 'https://mirrors.cloud.tencent.com/pypi/packages' },
]
const CHUNK = 1024 * 1024
const RETRIES = 4
const PAUSE_MS = 120

const [, , resolvedPath, wheelDir, onlyArg] = process.argv
if (!resolvedPath || !wheelDir) {
  console.error('usage: node fetch-wheels.mjs <resolved.txt> <wheelDir> [pkg,pkg]')
  process.exit(2)
}
const only = onlyArg ? new Set(onlyArg.split(',').map((s) => s.trim().toLowerCase())) : null

const text = await readFile(resolvedPath, 'utf8')
const want = []
for (const raw of text.split(/\r?\n/)) {
  const m = raw.trim().match(/^([A-Za-z0-9._-]+)==([^\s;#]+)/)
  if (!m) continue
  if (only && !only.has(m[1].toLowerCase())) continue
  want.push({ name: m[1], version: m[2] })
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const normalized = (s) => s.toLowerCase().replace(/[-_.]/g, '')

function scoreWheel(packageName, filename, version) {
  if (!filename.endsWith('.whl')) return 0
  // 精确匹配「包名-版本-」前缀：只写 includes 会把 PyMuPDF-1.22.1 当成 2.1。
  const fnNorm = filename.toLowerCase().replace(/[-_.]+/g, '-')
  const wantPrefix = `${packageName.toLowerCase().replace(/[-_.]+/g, '-')}-${version.toLowerCase().replace(/[-_.]+/g, '-')}`
  if (!fnNorm.startsWith(`${wantPrefix}-`)) return 0
  if (filename.includes('win_amd64') && filename.includes('cp311')) return 100
  if (filename.includes('win_amd64') && filename.includes('abi3')) return 95
  if (filename.includes('win_amd64')) return 90
  if (filename.includes('none-any')) return 50
  return 0
}

async function findWheel(mirror, name, version) {
  const indexUrl = `${mirror.simple}/${name}/`
  const res = await fetch(indexUrl, { signal: AbortSignal.timeout(25000) })
  if (!res.ok) throw new Error(`index HTTP ${res.status}`)
  const html = await res.text()
  if (html.includes('<html') && !html.includes('.whl')) throw new Error('索引返回了 HTML 错误页')
  const links = [...html.matchAll(/href="([^"]+)"/g)].map((m) => m[1])
  let best
  let bestScore = 0
  for (const href of links) {
    const absolute = new URL(href.split('#')[0], indexUrl).href
    const filename = decodeURIComponent(absolute.split('/').pop() ?? '')
    const score = scoreWheel(name, filename, version)
    if (score > bestScore) {
      // 只保留 /packages/ 之后的路径，方便换镜像（各镜像的文件前缀不同）
      const marker = absolute.indexOf('/packages/')
      const suffix = marker >= 0 ? absolute.slice(marker + '/packages'.length) : new URL(absolute).pathname
      best = { filename, suffix }
      bestScore = score
    }
  }
  return best
}

async function resolveWheel(name, version) {
  for (const mirror of MIRRORS) {
    try {
      const wheel = await findWheel(mirror, name, version)
      if (wheel) return wheel
    } catch {
      /* 换下一个镜像 */
    }
  }
  return undefined
}

async function findExisting(dir, name, version) {
  let files = []
  try {
    files = await readdir(dir)
  } catch {
    return undefined
  }
  const wantPrefix = `${normalized(name)}-${version.toLowerCase().replace(/[-_.]+/g, '-')}-`
  const hit = files.find((f) => f.endsWith('.whl') && normalized(f).startsWith(wantPrefix))
  return hit
}

async function downloadFile(wheel, outPath) {
  await mkdir(path.dirname(outPath), { recursive: true })
  let pos = 0
  try {
    pos = (await stat(outPath)).size
  } catch {
    pos = 0
  }
  const handle = await open(outPath, pos > 0 ? 'r+' : 'w')
  let mirrorIndex = 0
  try {
    for (;;) {
      const end = pos + CHUNK - 1
      let chunk
      let lastError
      for (let attempt = 0; attempt < RETRIES; attempt += 1) {
        const mirror = MIRRORS[(mirrorIndex + attempt) % MIRRORS.length]
        const url = mirror.packages + wheel.suffix
        try {
          const res = await fetch(url, {
            headers: { Range: `bytes=${pos}-${end}` },
            signal: AbortSignal.timeout(45000),
          })
          if (res.status === 403 || res.status === 429) throw new Error(`HTTP ${res.status}（限流）`)
          if (res.status !== 206 && res.status !== 200) throw new Error(`HTTP ${res.status}`)
          if ((res.headers.get('content-type') ?? '').includes('text/html')) throw new Error('返回了 HTML')
          chunk = Buffer.from(await res.arrayBuffer())
          if (res.status === 200) chunk = chunk.subarray(pos)
          if (chunk.length === 0) throw new Error('空响应')
          mirrorIndex = (mirrorIndex + attempt) % MIRRORS.length
          break
        } catch (error) {
          lastError = error
          await sleep(400 * (attempt + 1))
        }
      }
      if (!chunk) throw new Error(`@${pos} 失败：${lastError?.message ?? lastError}`)
      await handle.write(chunk, 0, chunk.length, pos)
      pos += chunk.length
      process.stdout.write(`\r  ↓ ${path.basename(outPath)} ${(pos / 1048576).toFixed(1)}MB`)
      if (chunk.length < CHUNK) break
      if (pos > 4 * 1024 ** 3) throw new Error('文件异常大，放弃')
    }
    process.stdout.write(`\n  ✓ ${path.basename(outPath)} ${(pos / 1048576).toFixed(1)} MB\n`)
    return pos
  } finally {
    await handle.close()
  }
}

let failures = 0
for (const { name, version } of want) {
  try {
    const existing = await findExisting(wheelDir, name, version)
    if (existing) {
      console.log(`- ${name}==${version} 已存在（${existing}），跳过`)
      continue
    }
    const wheel = await resolveWheel(name, version)
    if (!wheel) {
      console.log(`  ! ${name}==${version} 所有镜像都没有合适轮子`)
      failures += 1
      continue
    }
    console.log(`- ${name}==${version} -> ${wheel.filename}`)
    await downloadFile(wheel, path.join(wheelDir, wheel.filename))
  } catch (error) {
    console.log(`\n  ! ${name} 失败：${String(error?.message ?? error).slice(0, 90)}`)
    failures += 1
  }
}

console.log(`done. failures=${failures}`)
