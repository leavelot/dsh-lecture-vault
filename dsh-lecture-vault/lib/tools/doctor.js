/**
 * lecture_doctor — 环境自检。P0 的唯一实链路：它绿了，后面的工具才值得用。
 *
 * 检查项分两级：
 *  - required：缺了就不能工作，必须绿（Python / ffmpeg / GPU / vault 可写 / 工作目录 / 模型）；
 *  - optional：本机条件可能就不满足（YouTube 直连、cookie），只提示不判负。
 * 整体 ok = 所有 required 项都绿。
 */

import { defineTool, renderReport } from '../tool-kit.js'
import { runLecture } from '../python.js'

export function createDoctorTool(config) {
  return defineTool({
    name: 'lecture_doctor',
    description:
      'Run the environment self-check for the lecture pipeline and return one row per check (OK or why not). '
      + 'Covers Python, ffmpeg, GPU, the Obsidian vault write path, the working directory, the Whisper model and each platform probe. '
      + 'Run it first: it is the only source of truth about whether this machine can transcribe and where it may write.',
    parameters: {
      checkNetwork: {
        type: 'boolean',
        description: 'Also probe platform reachability (Bilibili API, YouTube, model mirror). Set false to skip the slow probes when offline or when debugging local pieces only.',
        default: true,
      },
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean', description: 'All required checks are green.' },
          summary: { type: 'string' },
          checks: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                id: { type: 'string' },
                title: { type: 'string' },
                level: { type: 'string', enum: ['required', 'optional'] },
                ok: { type: 'boolean' },
                why: { type: 'string' },
                detail: { type: 'string' },
              },
            },
          },
          paths: { type: 'json' },
          warnings: { type: 'array', items: { type: 'string' } },
          nextSteps: { type: 'array', items: { type: 'string' } },
        },
      },
      render: (_args, value) => renderReport(value),
    },
    timeoutMs: 240000,
    async execute(args, exec) {
      const result = await runLecture(
        config,
        ['doctor', args.checkNetwork === false ? '--no-network' : '--network'],
        { signal: exec.signal, timeoutMs: 210000, logLabel: 'doctor' },
      )
      if (!result.value) {
        return {
          ok: false,
          summary: '环境自检没能跑起来',
          checks: [],
          reason: result.reason,
          paths: result.logPath ? { log: result.logPath } : {},
          warnings: [],
          nextSteps: [
            `先装环境：${config.workDir}\\bin\\uv.exe 与 ${config.pythonExe}`,
            '或运行包里的 scripts/install-env.ps1（会打印每一步命令，不静默装）',
          ],
        }
      }
      return { ...result.value, paths: { ...(result.value.paths ?? {}), log: result.logPath } }
    },
  })
}
