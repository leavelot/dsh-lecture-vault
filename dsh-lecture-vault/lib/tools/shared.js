/**
 * shared.js — 工具结果里反复出现的两种形状：失败、以及还没实现的占位。
 *
 * 统一的返回契约（§5）：`ok` / `summary` / `paths` / `warnings[]`，
 * 失败时 `ok:false` + `reason` + `nextSteps[]`。
 */

export function failure(reason, nextSteps = [], extra = {}) {
  return { ok: false, reason, nextSteps, warnings: [], paths: {}, ...extra }
}

/**
 * 分期交付的占位结果：工具已经登记（模型看得见、参数正确），
 * 但这一期不做事 —— 宁可明说没实现，也不返回一段编造的数据。
 */
export function notImplemented(tool, phase, plan = []) {
  return {
    ok: false,
    reason: `${tool} 已在 P0 登记，具体实现在 ${phase}`,
    summary: `${tool}：P0 仅登记契约，未实现`,
    warnings: ['P0 的验收只要求环境自检与插件骨架；这是占位结果，不是失败数据。'],
    nextSteps: plan,
    paths: {},
  }
}
