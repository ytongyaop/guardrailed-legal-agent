/** 展示层的小工具：链接白名单、数值格式化。不做任何业务计算。 */

/** 来源链接只允许 http(s)；其他协议（javascript:、data:）一律不生成链接。 */
export function safeSourceUrl(value: string | null | undefined): string | null {
  if (!value) return null
  try {
    const parsed = new URL(value)
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') return parsed.href
    return null
  } catch {
    return null
  }
}

/** 结果页的核对时间：`2026-11-18 20:31:54`，去掉微秒（对读者无意义的精度）。 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '时间未记录'
  const matched = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})/.exec(value)
  return matched ? `${matched[1]} ${matched[2]}` : value
}

/** 评测指标：浮点保留 4 位，整数原样。 */
export function formatMetricValue(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(4)
}

export function formatThreshold(value: number | null): string {
  if (value === null) return '未设'
  return Number.isInteger(value) ? String(value) : value.toFixed(2)
}

/** 评测达标口径：true 达标、false 未达、null 未设。 */
export function describePassed(passed: boolean | null): string {
  if (passed === true) return '达标'
  if (passed === false) return '未达'
  return '未设'
}

/** 规则条件展示：`op threshold unit`，与旧界面同一拼法。 */
export function describeCondition(op: string, threshold: number | string, unit: string): string {
  return `${op} ${threshold}${unit}`
}
