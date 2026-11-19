/**
 * pending.js — 六个「确定性脚本」工具的定义。
 *
 * 这里只做**接线**：参数 → Python 子命令 → 结构化结果。真正的逻辑都在 `py/` 里，
 * 好处很实际：改 Python 不需要重启 DSH，改这里的 JS 才需要。
 *
 * P1 落地状态（以 py 侧为准，不在这里重复说明）：
 *   probe / ingest  已实现
 *   glossary / courseware / commit / lint  仍返回「未实现 + 计划」
 */

import { startJob } from '../jobs.js'
import { runLecture } from '../python.js'
import { defineTool, renderReport } from '../tool-kit.js'

const FAIL_SCHEMA = {
  ok: { type: 'boolean' },
  summary: { type: 'string' },
  reason: { type: 'string' },
  paths: { type: 'json' },
  warnings: { type: 'array', items: { type: 'string' } },
  nextSteps: { type: 'array', items: { type: 'string' } },
}

/** 把子进程结果翻成工具结果：有结构化结果就用它，没有就给可诊断的失败。 */
function normalize(result) {
  if (result.value) {
    return { ...result.value, paths: { ...(result.value.paths ?? {}), ...(result.logPath ? { log: result.logPath } : {}) } }
  }
  const tail = typeof result.stderrTail === 'string' && result.stderrTail.trim()
    ? result.stderrTail.trim().split('\n').slice(-6).join(' / ')
    : ''
  return {
    ok: false,
    summary: 'Python 侧没有返回结构化结果',
    reason: result.reason,
    warnings: [],
    nextSteps: tail ? [tail] : [],
    paths: result.logPath ? { log: result.logPath } : {},
  }
}

function pythonBacked(config, spec) {
  return defineTool({
    name: spec.name,
    description: spec.description,
    parameters: spec.parameters,
    output: { schema: { type: 'object', properties: { ...FAIL_SCHEMA, ...spec.schema } }, render: (_args, value) => renderReport(value) },
    timeoutMs: spec.timeoutMs,
    async execute(args, exec) {
      const argv = spec.argv(args)
      const budget = spec.pythonTimeoutMs ?? Math.max(20000, (spec.timeoutMs ?? 120000) - 20000)
      const result = await runLecture(config, argv, { signal: exec.signal, timeoutMs: budget, logLabel: spec.logLabel ?? spec.name.replace('lecture_', '') })
      return normalize(result)
    },
  })
}

export function createProbeTool(config) {
  return pythonBacked(config, {
    name: 'lecture_probe',
    logLabel: 'probe',
    description:
      'Resolve a course input into a normalized course.json: title, uploader, platform, total duration and the part list. '
      + 'Accept a Bilibili BV id, a video URL, an m3u8 URL or a local file path. '
      + 'Every later tool reads the course.json this returns; nothing else re-probes the platform.',
    parameters: {
      input: {
        type: 'string',
        required: true,
        description: 'BV id, video URL (Bilibili / YouTube), signed m3u8 URL from DevTools, or an absolute local media path.',
      },
      kind: {
        type: 'string',
        enum: ['auto', 'bili', 'youtube', 'hls', 'file'],
        description: 'Force the platform channel instead of detecting it from the input. Default auto.',
      },
    },
    schema: {
      course: { type: 'json' },
      items: { type: 'array', items: { type: 'string' } },
    },
    timeoutMs: 180000,
    argv: (args) => {
      const argv = ['probe', '--input', String(args.input ?? '').trim()]
      if (args.kind) argv.push('--kind', String(args.kind))
      return argv
    },
  })
}

export function createIngestTool(config) {
  return defineTool({
    name: 'lecture_ingest',
    description:
      'Grab the text of a course: native subtitles first, then audio download plus local transcription, or resolve an HLS URL. '
      + 'Reads the course.json from lecture_probe and writes transcripts and audio under the session directory. '
      + 'Long work continues in the background and returns a jobId to poll with lecture_status.',
    parameters: {
      courseJsonPath: {
        type: 'string',
        required: true,
        description: 'The courseJsonPath returned by lecture_probe.',
      },
      items: {
        type: 'array',
        items: { type: 'number' },
        description: 'Part indexes to process, 1-based. Omit to process every part.',
      },
      modes: {
        type: 'array',
        items: { type: 'string', enum: ['subtitles', 'audio', 'transcribe', 'hls'] },
        description: 'Which stages to run. Default subtitles then transcribe; add audio to keep the audio file, hls for a signed m3u8 replay.',
      },
      dryRun: {
        type: 'boolean',
        description: 'Resolve only what would be fetched or transcribed and report it; download nothing.',
        default: false,
      },
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ...FAIL_SCHEMA,
          jobId: { type: 'string' },
          transcripts: { type: 'array', items: { type: 'json' } },
          audio: { type: 'array', items: { type: 'json' } },
          plan: { type: 'array', items: { type: 'json' } },
        },
      },
      render: (_args, value) => renderReport(value),
    },
    timeoutMs: 120000,
    async execute(args, exec) {
      const argv = ['ingest', '--course-json', String(args.courseJsonPath ?? '').trim()]
      if (Array.isArray(args.items) && args.items.length > 0) {
        argv.push('--items', ...args.items.map((value) => String(value)))
      }
      const modes = Array.isArray(args.modes) && args.modes.length > 0 ? args.modes.map(String) : ['subtitles', 'transcribe']
      argv.push('--modes', ...modes)
      if (args.dryRun) argv.push('--dry-run')

      const longRunning = !args.dryRun && modes.some((mode) => mode === 'transcribe' || mode === 'hls')
      if (longRunning) {
        const { jobId, logPath } = startJob(config, { kind: 'ingest', args: argv, label: '取字幕 / 转写' })
        return {
          ok: true,
          summary: `已转后台：${modes.join(' + ')}（jobId ${jobId}）`,
          jobId,
          paths: { log: logPath, jobsDir: `${config.workDir}\\logs\\jobs` },
          warnings: [],
          nextSteps: [
            '用 lecture_status 传 jobId 查进度（转写按音频时长走，别反复重发）',
            '完成后 transcripts[] 里是带时间戳的 md/txt/jsonl 路径',
          ],
        }
      }
      const result = await runLecture(config, argv, { signal: exec.signal, timeoutMs: 110000, logLabel: 'ingest' })
      return normalize(result)
    },
  })
}

/** 下面四个还是占位：Python 侧会返回 NOT_IMPLEMENTED（含计划），这里只接好线。 */
function pendingTool(config, spec) {
  return pythonBacked(config, {
    ...spec,
    logLabel: spec.name.replace('lecture_', ''),
    argv: spec.argv,
  })
}

export function createGlossaryTool(config) {
  return pendingTool(config, {
    name: 'lecture_glossary',
    description:
      'Mine same-sound term candidates from transcripts, or apply a reviewed glossary back onto them. '
      + 'Mining pairs homophones and one-character variants and produces an evidence table for human review; '
      + 'applying rewrites only the confirmed keys and reports per-key hit counts. Correcting terms must happen before writing lecture notes.',
    parameters: {
      transcriptsDir: { type: 'string', required: true, description: 'Directory holding the transcript files to mine or rewrite.' },
      mode: { type: 'string', enum: ['mine', 'apply'], required: true, description: 'mine = produce candidates plus an evidence table; apply = rewrite transcripts with the reviewed glossary.' },
      glossaryPath: { type: 'string', description: 'Glossary JSON to apply. Required for apply; defaults to the glossary mined in the same transcripts directory.' },
      dryRun: { type: 'boolean', description: 'For apply: return per-key hit counts without rewriting any file.', default: true },
    },
    schema: {
      glossaryPath: { type: 'string' },
      evidenceMdPath: { type: 'string' },
      hits: { type: 'array', items: { type: 'json' } },
    },
    timeoutMs: 180000,
    argv: (args) => {
      const argv = ['glossary', '--transcripts-dir', String(args.transcriptsDir ?? ''), '--mode', String(args.mode ?? 'mine')]
      if (args.glossaryPath) argv.push('--glossary-path', String(args.glossaryPath))
      if (args.dryRun !== false) argv.push('--dry-run')
      return argv
    },
  })
}

export function createCoursewareTool(config) {
  return pendingTool(config, {
    name: 'lecture_courseware',
    description:
      'Parse courseware (MD, TXT, PPTX, DOCX, text-layer PDF) into page-anchored notes plus an image index, extracting only images that already exist in the source files. '
      + 'Scanned pages are detected and reported as needing OCR; nothing is uploaded anywhere by this tool.',
    parameters: {
      sourceDir: { type: 'string', required: true, description: 'Directory containing the courseware files, or a single courseware file.' },
      outDir: { type: 'string', description: 'Where the parsed notes and image index go. Defaults to a courseware folder under the working directory.' },
      minChars: { type: 'number', description: 'Pages with fewer extracted characters than this are treated as scanned. Default 30.', default: 30 },
      minCoverage: { type: 'number', description: 'Minimum extracted-text coverage (0-1) before a document counts as parsed. Default 0.6.', default: 0.6 },
    },
    schema: {
      notesDir: { type: 'string' },
      imagesIndexPath: { type: 'string' },
      needsCloudOcr: { type: 'boolean' },
      pages: { type: 'array', items: { type: 'json' } },
    },
    timeoutMs: 240000,
    argv: (args) => {
      const argv = ['courseware', '--source-dir', String(args.sourceDir ?? '')]
      if (args.outDir) argv.push('--out-dir', String(args.outDir))
      if (Number.isFinite(args.minChars)) argv.push('--min-chars', String(args.minChars))
      if (Number.isFinite(args.minCoverage)) argv.push('--min-coverage', String(args.minCoverage))
      return argv
    },
  })
}

export function createCommitTool(config) {
  return pendingTool(config, {
    name: 'lecture_commit',
    description:
      'Write finished lecture notes into the Obsidian vault: YAML properties with native types, links, markers and backups. '
      + 'Only whitelisted vault directories are written, content outside the dsh:auto markers is preserved byte for byte, '
      + 'and a dry run returns the full diff before anything touches disk.',
    parameters: {
      vaultPath: { type: 'string', description: 'Obsidian vault root; must contain .obsidian. Defaults to the configured vaultPath.' },
      course: { type: 'string', required: true, description: 'Course name, also the folder under 10_课程 and the prefix of its MOC.' },
      lectures: { type: 'array', items: { type: 'string' }, required: true, description: 'Absolute paths of the finished lecture markdown files to commit.' },
      dryRun: { type: 'boolean', required: true, description: 'true returns the full diff and writes nothing. Always show it to the user before committing.' },
    },
    schema: {
      written: { type: 'array', items: { type: 'string' } },
      backups: { type: 'array', items: { type: 'string' } },
      diff: { type: 'string' },
    },
    timeoutMs: 180000,
    argv: (args) => {
      const argv = ['commit', '--course', String(args.course ?? '')]
      if (args.vaultPath) argv.push('--vault-path', String(args.vaultPath))
      const lectures = Array.isArray(args.lectures) ? args.lectures.map(String) : []
      if (lectures.length > 0) argv.push('--lectures', ...lectures)
      if (args.dryRun) argv.push('--dry-run')
      return argv
    },
  })
}

export function createLintTool(config) {
  return pendingTool(config, {
    name: 'lecture_lint',
    description:
      'Check finished notes for timestamp gaps, leftover LaTeX or HTML in formulas, broken links and orphan pages, and report each issue with file and line. '
      + "It only reports: fixing the notes is the caller's job.",
    parameters: {
      targetPath: { type: 'string', required: true, description: 'A note file or a course folder to lint.' },
      checks: { type: 'array', items: { type: 'string', enum: ['timestamp', 'fx', 'links', 'orphan'] }, description: 'Which checks to run. Default: all four.' },
    },
    schema: {
      issues: { type: 'array', items: { type: 'json' } },
    },
    timeoutMs: 120000,
    argv: (args) => {
      const argv = ['lint', '--target-path', String(args.targetPath ?? '')]
      if (Array.isArray(args.checks) && args.checks.length > 0) argv.push('--checks', ...args.checks.map(String))
      return argv
    },
  })
}
