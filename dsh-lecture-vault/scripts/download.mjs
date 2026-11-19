// 下载兜底：某些环境里 schannel 取不到凭证（curl / Invoke-WebRequest 都报
// SEC_E_NO_CREDENTIALS），而 node 自带 CA 库能正常走 HTTPS。
// usage: node download.mjs <url> <outPath>
import { rename, stat, writeFile } from 'node:fs/promises'

const [, , url, out] = process.argv
if (!url || !out) {
  console.error('usage: node download.mjs <url> <outPath>')
  process.exit(2)
}

const response = await fetch(url, { redirect: 'follow' })
if (!response.ok) {
  console.error(`HTTP ${response.status} for ${url}`)
  process.exit(1)
}
const buffer = Buffer.from(await response.arrayBuffer())
const tmp = `${out}.part`
await writeFile(tmp, buffer)
await rename(tmp, out)
const info = await stat(out)
console.log(`ok ${out} ${info.size} bytes`)
