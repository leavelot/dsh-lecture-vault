/**
 * tool-kit.js — 把作者的「属性表」写法编译成宿主 tools.register() 认的 JSON Schema。
 *
 * 为什么不用宿主的 `defineTool`：`@deepseek-ai/dsh-tools` 被打进宿主 app.asar，
 * 从 profile 里的插件往上解析不到（dsh-video-storyboard 的 CHANGELOG 记过这次事故：
 * import 抛错 → apply 被吞 → 整个插件静默不注册）。所以这里只保留「编译」这一步：
 * author 属性表 → 标准 JSON Schema，工具定义直接交给注册表，零依赖、零运行时风险。
 *
 * 作者写法：
 *   parameters: {
 *     input: { type: 'string', required: true, description: '…' },
 *     kind:  { type: 'string', enum: ['auto', 'bili'], description: '…' },
 *   }
 * `required: true` 会提升到父级 required 数组并从属性里删掉；`type: 'json'` 退化成任意 JSON。
 */

const JSON_NODE = 'json'

function compilePropertyMap(map, path) {
  if (!map || typeof map !== 'object' || Array.isArray(map)) {
    throw new Error(`${path} 必须是「属性名 → 值 schema」的对象`)
  }
  const properties = {}
  const required = []
  for (const [key, spec] of Object.entries(map)) {
    if (!spec || typeof spec !== 'object' || Array.isArray(spec)) {
      throw new Error(`${path}.${key} 必须是值 schema 对象`)
    }
    const { required: mark, ...rest } = spec
    if (mark !== undefined && mark !== true) throw new Error(`${path}.${key}.required 写了就只能是 true`)
    if (mark === true) required.push(key)
    properties[key] = compileValue(rest, `${path}.${key}`)
  }
  return required.length > 0 ? { properties, required } : { properties }
}

function compileValue(spec, path) {
  if (!spec || typeof spec !== 'object' || Array.isArray(spec)) {
    throw new Error(`${path} 必须是值 schema 对象`)
  }
  const out = {}
  for (const [key, value] of Object.entries(spec)) {
    if (key === 'required') continue
    if (key === 'properties') {
      const compiled = compilePropertyMap(value, `${path}.properties`)
      out.properties = compiled.properties
      if (compiled.required) out.required = compiled.required
      continue
    }
    if (key === 'items') {
      out.items = compileValue(value, `${path}.items`)
      continue
    }
    if (key === 'type' && value === JSON_NODE) continue
    out[key] = value
  }
  return out
}

export function compileParameters(spec) {
  const { properties, required } = compilePropertyMap(spec ?? {}, 'parameters')
  return { type: 'object', properties, ...(required ? { required } : {}) }
}

export function compileOutputSchema(spec) {
  return compileValue(spec ?? {}, 'output.schema')
}

/**
 * 定义一个工具。签名与宿主 defineTool 一致（少掉运行时参数校验那一层）。
 * @param {object} spec
 * @returns {object} 可直接交给 ctx.tools.register() 的定义
 */
export function defineTool(spec) {
  const { name, description, parameters, output, execute, timeoutMs } = spec
  if (typeof name !== 'string' || !name) throw new Error('defineTool: name 必须是非空字符串')
  if (!output || typeof output.render !== 'function') throw new Error(`defineTool(${name}): output.render 必须是函数`)
  if (typeof execute !== 'function') throw new Error(`defineTool(${name}): execute 必须是函数`)
  if (timeoutMs !== undefined && (!Number.isFinite(timeoutMs) || timeoutMs <= 0)) {
    throw new Error(`defineTool(${name}): timeoutMs 必须是正数`)
  }
  const tool = {
    name,
    description,
    parameters: compileParameters(parameters),
    output: {
      schema: compileOutputSchema(output.schema),
      render: output.render,
    },
    execute: (args, exec) => execute(args ?? {}, exec),
  }
  if (timeoutMs !== undefined) tool.timeoutMs = timeoutMs
  if (typeof spec.isConcurrencySafe === 'function') tool.isConcurrencySafe = spec.isConcurrencySafe
  return tool
}

/** 统一的文字结果块。 */
export function textBlock(text) {
  return [{ type: 'text', text }]
}

/**
 * 所有工具共用的渲染：结构化摘要 + 路径 + 警告 + 下一步。
 * 只吐模型需要看的字段，原始日志留在 logPath 里。
 */
export function renderReport(value) {
  const v = value && typeof value === 'object' ? value : { summary: String(value) }
  const lines = []
  const head = v.ok === false ? '✗ ' : '✓ '
  lines.push(head + (v.summary || v.reason || '完成'))

  if (Array.isArray(v.checks) && v.checks.length > 0) {
    lines.push('')
    for (const check of v.checks) {
      const mark = check.ok ? '✓' : check.level === 'optional' ? '!' : '✗'
      const why = check.why ? ` — ${check.why}` : ''
      lines.push(`${mark} ${check.title ?? check.id}${why}`)
    }
  }

  if (Array.isArray(v.items) && v.items.length > 0) {
    lines.push('')
    for (const item of v.items) lines.push(`- ${typeof item === 'string' ? item : item.line ?? JSON.stringify(item)}`)
  }

  if (v.diff) lines.push('', v.diff)

  if (v.paths && typeof v.paths === 'object') {
    const entries = Object.entries(v.paths).filter(([, p]) => typeof p === 'string' && p)
    if (entries.length > 0) {
      lines.push('', '路径：')
      for (const [key, p] of entries) lines.push(`- ${key}: ${p}`)
    }
  }

  if (Array.isArray(v.warnings) && v.warnings.length > 0) {
    lines.push('', '警告：')
    for (const w of v.warnings) lines.push(`- ${w}`)
  }

  if (Array.isArray(v.nextSteps) && v.nextSteps.length > 0) {
    lines.push('', '下一步：')
    for (const step of v.nextSteps) lines.push(`- ${step}`)
  }

  return textBlock(lines.join('\n'))
}
