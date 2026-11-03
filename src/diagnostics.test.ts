import { describe, expect, it } from 'vitest'
import {
  deferredCodes,
  errorDescription,
  errorLabel,
  reasonLabel,
  stageLabel,
  traceReasonLabel,
  warningLabel,
} from './diagnostics'

describe('Chinese diagnostics', () => {
  it('translates the worker and graph failures shown in task status', () => {
    expect(errorLabel('worker_failed')).toBe('后台抽取进程意外退出')
    expect(errorLabel('graph_validation')).toBe('图谱抽取结果未通过校验')
    expect(errorLabel('graph_io')).toBe('图谱数据读写失败')
    expect(errorLabel('provider_http_502')).toBe('模型服务返回错误（状态码 502）')
    expect(errorLabel('jev_http_451')).toBe('Jev 服务拒绝了请求（状态码 451）')
    expect(errorLabel('Failure')).toBe('旧版清理失败（未记录具体原因）')
  })

  it('never exposes an unknown raw code as the primary label', () => {
    for (const code of ['future_error', 'another_provider_failure', undefined, '']) {
      const label = errorLabel(code)
      expect(label).not.toMatch(/[a-z]+_[a-z_]+/)
      expect(label).toMatch(/[\u3400-\u9fff]/)
    }
    expect(errorDescription('future_error')).toContain('未识别的后台任务错误')
    expect(errorDescription('future_error')).not.toContain('future_error')
  })

  it('translates deferred, candidate, stage, warning and trace reasons', () => {
    expect(deferredCodes.has('graph_call_budget_pending')).toBe(true)
    expect(reasonLabel('ambiguous_strong_identity')).toBe('对象身份含义不唯一，保留候选等待核对')
    expect(reasonLabel('graph_schema:output:fields:missing=relations:extra_count=0')).toBe('抽取结果字段不完整（缺少 relations）')
    expect(reasonLabel('graph_evidence:quote_not_located')).toBe('引用原文无法定位')
    expect(reasonLabel('future_reason')).toBe('其他候选原因（未记录具体说明）')
    expect(stageLabel('publish_relations')).toBe('关系入库')
    expect(stageLabel('future_stage')).toBe('处理')
    expect(warningLabel('future_warning')).toBe('其他诊断提示；相关记录已保留。')
    expect(traceReasonLabel('max_output_tokens')).toBe('达到输出上限')
    expect(traceReasonLabel('future_finish')).toBe('其他终止原因')
  })
})
