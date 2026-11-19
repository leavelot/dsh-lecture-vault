/**
 * lecture_status — 后台任务进度。只读状态文件，不认识 jobId 就给原因、不抛异常。
 */

import { defineTool, renderReport } from '../tool-kit.js'
import { listJobs, readJob } from '../jobs.js'

export function createStatusTool(config) {
  return defineTool({
    name: 'lecture_status',
    description:
      'Report progress for a background lecture job, or list the most recent jobs when no id is given. '
      + 'Long steps (download, transcription) return a jobId instead of blocking; poll this until state is done or failed, '
      + 'and read the log path when a job failed.',
    parameters: {
      jobId: {
        type: 'string',
        description: 'Job id returned by lecture_ingest. Omit it to list the most recent jobs with their state and progress.',
      },
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          summary: { type: 'string' },
          state: { type: 'string' },
          percent: { type: 'number' },
          message: { type: 'string' },
          jobId: { type: 'string' },
          paths: { type: 'json' },
          items: { type: 'array', items: { type: 'string' } },
          warnings: { type: 'array', items: { type: 'string' } },
          nextSteps: { type: 'array', items: { type: 'string' } },
        },
      },
      render: (_args, value) => renderReport(value),
    },
    timeoutMs: 20000,
    async execute(args) {
      const jobId = typeof args.jobId === 'string' ? args.jobId.trim() : ''
      if (!jobId) {
        const jobs = listJobs(config, 10)
        if (jobs.length === 0) {
          return {
            ok: true,
            summary: '没有后台任务记录',
            items: [],
            paths: { jobsDir: `${config.workDir}\\logs\\jobs` },
            warnings: [],
          }
        }
        return {
          ok: true,
          summary: `最近 ${jobs.length} 个任务`,
          items: jobs.map((job) => `${job.jobId} · ${job.state ?? '?'} · ${job.percent ?? 0}% · ${job.message ?? ''}`),
          paths: { jobsDir: `${config.workDir}\\logs\\jobs` },
          warnings: [],
        }
      }
      const job = readJob(config, jobId)
      if (!job) {
        return {
          ok: false,
          summary: `没有这个任务：${jobId}`,
          reason: '未知的 jobId（可能已被清理，或拼错了）',
          paths: { jobsDir: `${config.workDir}\\logs\\jobs` },
          warnings: [],
          nextSteps: ['不带 jobId 调用 lecture_status 可以看到最近的任务列表'],
        }
      }
      return {
        ok: job.state !== 'failed',
        summary: `${job.jobId} · ${job.state ?? '?'} · ${job.percent ?? 0}% · ${job.message ?? ''}`,
        jobId: job.jobId,
        state: job.state,
        percent: job.percent ?? 0,
        message: job.message ?? '',
        paths: {
          ...(job.logPath ? { log: job.logPath } : {}),
          ...(job.resultPath ? { result: job.resultPath } : {}),
        },
        warnings: job.state === 'failed' ? ['任务失败：看 log 的末尾几行找原因'] : [],
      }
    },
  })
}
