/**
 * index.js — 听课入库（@local/dsh-lecture-vault）插件入口。
 *
 * 只做三件事：
 *   ① 解析配置（默认值 ← 插件行 config）；
 *   ② 注册 8 个工具（确定性脚本的入口，全部走 py/lecture.py）；
 *   ③ 把 skills/lecture-to-kb/SKILL.md 原样注册成内置 skill（工作流正文，逐字使用）。
 *
 * 不写 profile 目录、不改别人的插件行、不在这里跑安装：安装交给 plugin_manager。
 */

import { readFileSync } from 'node:fs'
import path from 'node:path'
import { resolveConfig } from './lib/config.js'
import { PACKAGE_DIR } from './lib/python.js'
import { createDoctorTool } from './lib/tools/doctor.js'
import { createStatusTool } from './lib/tools/status.js'
import {
  createCommitTool,
  createCoursewareTool,
  createGlossaryTool,
  createIngestTool,
  createLintTool,
  createProbeTool,
} from './lib/tools/pending.js'

/** `tools` 是硬依赖；`skills` 走可选解析（没有 skill 注册表的部署里工具照样能用）。 */
export const inject = ['tools']

const SKILL_FILE = path.join(PACKAGE_DIR, 'skills', 'lecture-to-kb', 'SKILL.md')

/**
 * SKILL.md 带 YAML frontmatter（name / description / whenToUse）。
 * 注册时这些字段单独传，正文只取 frontmatter 之后的部分 —— 文件本身保持逐字不变。
 */
function readSkill() {
  const raw = readFileSync(SKILL_FILE, 'utf8')
  const match = raw.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/)
  if (!match) return { name: 'lecture-to-kb', description: 'Lecture to knowledge base workflow.', content: raw.trim() }

  const fields = {}
  for (const line of match[1].split(/\r?\n/)) {
    const kv = line.match(/^([A-Za-z][\w-]*):\s*(.*)$/)
    if (!kv) continue
    fields[kv[1]] = kv[2].trim()
  }
  return {
    name: fields.name || 'lecture-to-kb',
    description: fields.description || 'Lecture to knowledge base workflow.',
    whenToUse: fields.whenToUse,
    content: raw.slice(match[0].length).trim(),
  }
}

export function apply(ctx, rawConfig) {
  const { config, warnings } = resolveConfig(rawConfig)
  for (const warning of warnings) {
    try {
      ctx?.logger?.warn?.(`[lecture-vault] ${warning}`)
    } catch {
      /* 日志写不了就算了 */
    }
  }

  const disposers = []

  // ① 工具注册。逐个登记，一个坏了不要连累其它七个。
  const toolFactories = [
    () => createDoctorTool(config),
    () => createStatusTool(config),
    () => createProbeTool(config),
    () => createIngestTool(config),
    () => createGlossaryTool(config),
    () => createCoursewareTool(config),
    () => createCommitTool(config),
    () => createLintTool(config),
  ]
  const registered = []
  for (const factory of toolFactories) {
    try {
      const tool = factory(config)
      disposers.push(ctx.tools.register(tool))
      registered.push(tool.name)
    } catch (error) {
      try {
        ctx?.logger?.warn?.(`[lecture-vault] 工具注册失败：${error?.message ?? error}`)
      } catch {
        /* ignore */
      }
    }
  }

  // ② 内置 skill：原样注册工作流正文。
  // `skills` 服务在部分部署里不存在，所以用 ctx.get 探测而不是写进 inject ——
  // 缺它不该让整个插件停在等待态（工具本身就够启动了）。
  try {
    const skills = typeof ctx.get === 'function' ? ctx.get('skills') : undefined
    if (skills && typeof skills.register === 'function') {
      const skill = readSkill()
      disposers.push(ctx.effect(() => skills.register({
        name: skill.name,
        description: skill.description,
        ...(skill.whenToUse ? { whenToUse: skill.whenToUse } : {}),
        source: 'runtime',
        content: skill.content,
        resourceBase: { kind: 'directory', path: path.dirname(SKILL_FILE) },
      })))
    }
  } catch (error) {
    try {
      ctx?.logger?.warn?.(`[lecture-vault] skill 注册失败：${error?.message ?? error}`)
    } catch {
      /* ignore */
    }
  }

  try {
    ctx?.logger?.info?.(`[lecture-vault] 已注册工具：${registered.join(', ')}`)
  } catch {
    /* ignore */
  }

  return () => {
    for (const dispose of disposers.reverse()) {
      try {
        if (typeof dispose === 'function') dispose()
      } catch {
        /* 卸载阶段的异常不该阻断其它清理 */
      }
    }
  }
}
