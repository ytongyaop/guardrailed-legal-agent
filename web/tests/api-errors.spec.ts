/** 错误体映射：六种输入形状都要落到可渲染的 `{code,message,action}`。 */

import { describe, expect, it } from 'vitest'

import { ApiError, fallbackFor, parseErrorBody } from '@/api/errors'

describe('parseErrorBody', () => {
  it('标准错误体：code、message、details、action、request_id 全部保留', () => {
    const parsed = parseErrorBody(403, {
      error: {
        code: 'CSRF_FAILED',
        message: 'CSRF token 不匹配，请刷新页面后重试',
        details: { fields: ['x'] },
        action: { type: 'retry', label: '重试' },
      },
      request_id: 'req_1',
    })

    expect(parsed.code).toBe('CSRF_FAILED')
    expect(parsed.message).toBe('CSRF token 不匹配，请刷新页面后重试')
    expect(parsed.details).toEqual({ fields: ['x'] })
    expect(parsed.action).toEqual({ type: 'retry', label: '重试' })
    expect(parsed.requestId).toBe('req_1')
  })

  it('缺少 action 时保持 null，不编造按钮', () => {
    const parsed = parseErrorBody(404, {
      error: { code: 'NOT_FOUND', message: '合同不存在', details: null, action: null },
      request_id: 'req_2',
    })
    expect(parsed.action).toBeNull()
    expect(parsed.details).toBeNull()
  })

  it('未知 action 类型收敛成 none，避免渲染出不存在的动作', () => {
    const parsed = parseErrorBody(409, {
      error: { code: 'NO_MODEL', message: '未配置模型', action: { type: 'teleport', label: '跳转' } },
    })
    expect(parsed.action).toEqual({ type: 'none', label: '跳转' })
  })

  it('响应体是 HTML 或空串时按状态码兜底（反向代理 502）', () => {
    for (const payload of ['<html>bad gateway</html>', '', null, undefined]) {
      const parsed = parseErrorBody(502, payload)
      expect(parsed.code).toBe('MODEL_PROBE_FAILED')
      expect(parsed.message.length).toBeGreaterThan(0)
      expect(parsed.requestId).toBe('')
    }
  })

  it('兜底表覆盖 006 的状态码，未知状态码落到 INTERNAL_ERROR', () => {
    expect(fallbackFor(400).code).toBe('VALIDATION_ERROR')
    expect(fallbackFor(401).code).toBe('UNAUTHENTICATED')
    expect(fallbackFor(403).code).toBe('FORBIDDEN')
    expect(fallbackFor(404).code).toBe('NOT_FOUND')
    expect(fallbackFor(409).code).toBe('STATE_CONFLICT')
    expect(fallbackFor(413).code).toBe('PAYLOAD_TOO_LARGE')
    expect(fallbackFor(415).code).toBe('UNSUPPORTED_FILE_TYPE')
    expect(fallbackFor(422).code).toBe('FILE_PARSE_ERROR')
    expect(fallbackFor(418).code).toBe('INTERNAL_ERROR')
  })
})

describe('ApiError', () => {
  it('message 为空串时用兜底文案，不让界面出现空白错误', () => {
    const error = ApiError.fromResponse(400, { error: { code: 'VALIDATION_ERROR', message: '  ' } })
    expect(error.message).toBe('请求参数不合法，请检查后重试')
    expect(error.code).toBe('VALIDATION_ERROR')
  })

  it('fromStatus 对 5xx 给 retry 动作，对 4xx 不给', () => {
    expect(ApiError.fromStatus(500).action?.type).toBe('retry')
    expect(ApiError.fromStatus(404).action).toBeNull()
  })

  it('fieldNames 只挑字符串，形状不对时返回空数组', () => {
    const withFields = ApiError.fromResponse(400, {
      error: { code: 'VALIDATION_ERROR', message: '缺字段', details: { fields: ['a', 2, 'b'] } },
    })
    expect(withFields.fieldNames).toEqual(['a', 'b'])

    const withoutFields = ApiError.fromResponse(400, {
      error: { code: 'VALIDATION_ERROR', message: '缺字段', details: { fields: 'a' } },
    })
    expect(withoutFields.fieldNames).toEqual([])
  })
})
