/**
 * User-facing diagnostics stay Chinese while persisted error codes remain stable.
 * Raw codes are only exposed by explicitly labelled technical sections.
 */

export const deferredCodes = new Set([
  'graph_call_budget_pending',
  'graph_continuation_pending',
  'runtime_upgrade_pending',
  'daily_call_budget_pending',
  'resource_circuit_open',
])

const errorLabels: Record<string, string> = {
  worker_failed: '后台抽取进程意外退出',
  worker_no_terminal_status: '后台进程未报告结束状态',
  graph_validation: '图谱抽取结果未通过校验',
  graph_io: '图谱数据读写失败',
  graph_internal: '图谱处理出现内部错误',
  graph_database: '图谱数据库操作失败',
  provider_evidence_reference: '证据引用未通过校验',
  graph_source_changed: '原文已变化，批次已保留',
  cleanup_shared_node_without_evidence: '共享节点已无可用原文依据，清理事务已回滚',
  source_missing: '原始 chat 文件不存在',
  source_unavailable: '原始来源暂时不可读，图谱已保留',
  graph_facts_changed: '图谱事实已变化，需要重新核对',
  graph_archive_collision: '图谱归档记录冲突',
  graph_archive_corrupt_payload: '图谱归档内容已损坏',
  graph_archive_hash_mismatch: '图谱归档校验值不一致',
  graph_archive_invalid_reference: '图谱归档引用无效',
  graph_archive_missing_payload: '图谱归档内容缺失',
  graph_archive_roundtrip: '图谱归档校验失败',
  graph_archive_size_limit: '图谱归档超过大小限制',
  graph_archive_size_or_stream_invalid: '图谱归档大小或数据流无效',
  resource_budget_exhausted: '本批模型调用额度已用完',
  resource_circuit_open: '上游连接冷却中',
  resource_circuit_policy_changed: '连接冷却策略已变化',
  resource_endpoint_invalid: '模型服务端点无效',
  resource_output_limit_unknown: '无法确认模型输出上限',
  resource_policy_changed: '资源策略已变化',
  resource_policy_invalid: '资源策略配置无效',
  resource_policy_requires_bounded_graph: '资源策略要求限制图谱规模',
  resource_provider_budget_violation: '模型服务超出允许的调用额度',
  resource_request_invalid: '资源配置请求无效',
  resource_scope_invalid: '资源范围配置无效',
  resource_scope_missing: '缺少资源范围配置',
  resource_timeout_invalid: '超时配置无效',
  graph_call_budget_pending: '旧版本的分轮记录',
  daily_call_budget_pending: '旧版每日调用限制',
  graph_continuation_pending: '达到本轮处理上限，等待继续',
  runtime_upgrade_pending: '等待新版运行时继续',
  provider_http_400: '模型服务拒绝了请求（状态码 400）',
  provider_http_error: '模型服务返回错误',
  provider_timeout: '模型服务响应超时',
  provider_transport: '模型服务连接失败',
  provider_empty_output: '模型服务没有返回内容',
  provider_incomplete: '模型输出不完整',
  provider_malformed_response: '模型服务响应结构异常',
  provider_output_json_invalid: '模型输出的 JSON 格式无效',
  provider_output_limit: '模型输出达到上限',
  provider_refused: '模型服务拒绝处理',
  provider_response_too_large: '模型服务响应超过大小限制',
  provider_probability: '模型返回的概率结果无效',
  provider_top_logprobs: '模型返回的分类概率无效',
  judge_schema_invalid: '判断结果格式无效',
  single_token_label_invalid: '分类标签无效',
  single_token_budget_incompatible: '当前模型不支持单标签分类',
  jev_http_451: 'Jev 服务拒绝了请求（状态码 451）',
  jev_malformed_response: 'Jev 返回结果未通过结构或概率校验',
  jev_transport: 'Jev 连接失败',
  jev_tls_handshake: 'Jev 安全连接握手失败',
  jev_tls_certificate: 'Jev 安全证书校验失败',
  jev_tls_trust_unavailable: 'Jev 安全连接信任配置不可用',
  jev_credentials_invalid: 'Jev 凭据无效',
  jev_credentials_permissions: 'Jev 凭据文件权限不安全',
  jev_endpoint_not_allowed: 'Jev 服务地址不被允许',
  jev_input_budget: 'Jev 输入超过允许额度',
  jev_not_configured: 'Jev 尚未配置',
  jev_question_limit: 'Jev 判断次数超过上限',
  jev_response_too_large: 'Jev 响应超过大小限制',
  jev_single_judgment_budget: 'Jev 单次判断额度不足',
  jev_unavailable: 'Jev 服务不可用',
  import_sources_not_found: '未找到该 chat 的原始文件',
  import_failed: '导入原文失败',
  import_incomplete_tail: '原文末尾尚未完整导入',
  import_project_binding_conflict: '原文与当前项目绑定冲突',
  import_project_boundary_unconfirmed: '无法确认原文所属项目',
  extraction_failed: '抽取失败',
  extraction_partial_failure: '部分内容抽取失败',
  queue_failed: '后台任务排队失败',
  continuation_failed: '自动续跑失败',
  continuation_no_progress: '自动续跑连续没有进展',
  continuation_state_unreadable: '自动续跑状态无法读取',
  ingress_cancelled: '任务入口已取消',
  source_cleanup: '原始来源已清理',
  stage_snapshot_changed: '抽取依据的原文快照已变化',
  stage_lease_busy: '另一个抽取阶段正在使用该项目',
  stage_lease_lost: '抽取阶段占用已失效',
  stage_lease_missing: '抽取阶段占用记录缺失',
  stage_revision_unavailable: '抽取阶段版本不可用',
  stage_session_missing: '抽取阶段会话缺失',
  stage_session_nested: '抽取阶段会话嵌套异常',
  stage_transport_transaction_open: '抽取阶段的网络事务尚未结束',
  stage_dependency_revision_v1: '抽取阶段依赖版本不匹配',
  model_input_contains_binary: '待抽取内容包含无法处理的二进制数据',
  local_read_timeout: '本地图谱读取超时',
  local_read_locked: '本地图谱数据库正在被占用',
  local_read_failed: '本地图谱读取失败',
  material_revision_changed: '材料版本已变化',
  timeout: '请求超时',
}

const errorDetails: Record<string, string> = {
  worker_failed: '后台抽取进程未正常结束；已保存的进度与原文证据保留，请查看任务记录后重试或清理。',
  worker_no_terminal_status: '后台抽取进程没有报告最终状态；已保存的进度与原文证据保留。',
  graph_validation: '图谱抽取结果未通过结构或证据校验；相关原文与批次已保留。',
  graph_io: '读取或写入图谱数据时发生错误；相关原文与批次已保留。',
  graph_internal: '图谱处理过程中出现内部错误；相关原文与批次已保留。',
  graph_database: '图谱数据库操作失败；相关原文与批次已保留。',
  provider_evidence_reference: '抽取结果中的证据引用无法通过校验；相关原文与批次已保留。',
  graph_source_changed: '抽取依据的原文已经变化；旧批次已保留，不会覆盖当前原文。',
  resource_budget_exhausted: '本批允许的模型调用额度已经用完；已保存的进度保留。',
  resource_circuit_open: '上游连接冷却中，已暂停请求；到期后再试，进度保留。',
  graph_call_budget_pending: '旧版本的分轮记录：进度已保存，剩余内容由后台继续处理。',
  daily_call_budget_pending: '旧版每日调用限制已移除，剩余任务由后台继续处理。',
  graph_continuation_pending: '已保存进度，达到本轮时间或步数上限。自动续跑时间见任务列表。',
  runtime_upgrade_pending: '旧版任务已停止，已保存进度；等待下次压缩或手动触发后由新版继续。',
  provider_http_400: '模型服务拒绝了请求（状态码 400）；请检查全局模型名称和端点。',
  provider_timeout: '模型服务在限定时间内没有响应；已保存的进度与原文证据保留。',
  provider_transport: '无法连接模型服务；已保存的进度与原文证据保留。',
  provider_malformed_response: '供应商 API 响应外层结构异常，尚未取得可用的模型输出。',
  provider_output_json_invalid: '模型生成的 JSON 语法错误；响应协议已返回，具体行列位置见批次诊断。',
  provider_output_limit: '模型输出 token 预算耗尽，和输入上下文容量是不同限制。',
  jev_http_451: 'Jev 请求返回 HTTP 451，相关批次已保留；需结合响应正文和实际代理路径定位拒绝原因。',
  jev_malformed_response: 'Jev 返回未通过结构或概率校验，具体失败字段见批次诊断。',
  jev_transport: '无法连接 Jev 服务；已保存的进度与原文证据保留。',
  jev_tls_handshake: '与 Jev 建立安全连接时握手失败；请检查代理、证书与网络路径。',
  single_token_budget_incompatible: '当前模型路由耗尽单标签判断预算却未返回分类标签；已暂停自动重试。请使用支持无思考分类的模型路由。',
  single_token_label_invalid: '模型返回的分类标签不在允许范围内；相关原文与批次已保留。',
  import_sources_not_found: '未找到该 chat 的原始文件；未发起模型请求。',
}

function dynamicErrorLabel(code: string): string | undefined {
  // Pre-fix receipts persisted only the exception class name. Keep that
  // historical evidence understandable without presenting it as a new cause.
  if (code === 'Failure') return '旧版清理失败（未记录具体原因）'
  const status = code.match(/^(provider|jev)_http_(\d{3})$/)
  if (status) {
    const service = status[1] === 'jev' ? 'Jev 服务' : '模型服务'
    return `${service}返回错误（状态码 ${status[2]}）`
  }
  if (code.startsWith('provider_')) return '模型服务处理失败'
  if (code.startsWith('jev_')) return 'Jev 服务处理失败'
  if (code.startsWith('graph_')) return '图谱抽取处理失败'
  if (code.startsWith('import_')) return '导入原文失败'
  if (code.startsWith('stage_')) return '抽取阶段状态异常'
  if (code.startsWith('resource_')) return '模型调用资源或连接策略受阻'
  if (code.startsWith('single_token_')) return '分类模型判断失败'
  if (code.startsWith('continuation_')) return '自动续跑失败'
  if (code.startsWith('queue_')) return '后台任务排队失败'
  if (code.startsWith('local_read_')) return '本地图谱读取失败'
  return undefined
}

export function errorLabel(code?: string): string {
  if (!code) return '需要检查原因'
  return errorLabels[code] || dynamicErrorLabel(code) || '未识别的后台任务错误'
}

export function errorDescription(code?: string): string {
  if (!code) return '没有记录具体错误；已保存的进度与原文证据保留。'
  return errorDetails[code] || `${errorLabel(code)}；已保存的进度与原文证据保留。`
}

const reasonLabels: Record<string, string> = {
  deduplicated_self_edge: '对象合并后关系指向自身，不保留自连线',
  missing_endpoint: '关系缺少可定位的对象，暂不能连线',
  relation_contradicts: '关系存在矛盾，等待补充证据核对',
  ambiguous_reference: '指代对象不明确，保留候选等待补充证据',
  ambiguous_strong_identity: '对象身份含义不唯一，保留候选等待核对',
  example_only: '示例对象，仅保留原文与候选记录',
  example_only_relation: '示例中的关系，不进入正式工作图',
  context_uncertain: '无法确认是实际工作还是示例，等待核对',
  identity_pending: '关系端点尚未确认，暂不能连线',
  identity_uncertain: '对象身份尚不能确认，暂不新增或合并',
  existing_identity_conflict: '候选与已有对象身份冲突，保留原对象并等待核对',
  identity_candidate_budget: '对象身份候选过多，已保留最优候选',
  identity_cluster_conflict: '对象身份聚类出现冲突，等待核对',
  identity_dependency_pending: '对象身份依赖其他候选，暂不能确认',
  insufficient_context: '原文上下文不足，等待补充证据',
  relation_type_mismatch: '关系类型与端点类型不匹配，需重新核对',
  unanchored_assertion: '结论缺少可定位的原文依据，暂不采用',
  unanchored_identity: '标识或所属范围没有原文依据，暂不采用',
  derived_only: '仅有转述来源，暂不作为独立结论',
  relation_insufficient_evidence: '原文不足以支持这条关系',
  relation_uncertain: '关系判断不确定，暂不连线',
  atomic_scope_unresolved: '无法确定原子事实所属范围，暂不发布',
  invalid_endpoint: '关系端点无效',
  invalid_node_ids: '节点标识无效或重复',
  duplicate_node_ids: '节点标识重复',
  incomplete_coverage: '抽取结果未覆盖全部输入',
  inconsistent_coverage: '抽取结果的覆盖状态不一致',
  retention_required: '需要保留原文后才能继续',
  record_only_publication: '仅有回执的记录不能进入正式图谱',
  context_only_publication: '仅有上下文的记录不能进入正式图谱',
  unknown_source_anchor: '引用了未知的原文锚点',
  invalid_atomic_source_ref: '原子事实的原文引用无效',
  invalid_reference_shape: '证据引用格式无效',
  invalid_output_shape: '抽取输出结构无效',
  quote_not_located: '引用原文无法定位',
  quote_crosses_fragment: '引用原文跨越了多个片段',
  source_changed: '原文已变化，批次已保留',
  graph_source_changed: '原文已变化，批次已保留',
  retention: '需要保留原文后才能继续',
}

function schemaReason(reason: string): string {
  const missing = reason.match(/missing=([^:]+)/)
  if (missing) return `抽取结果字段不完整（缺少 ${missing[1].split(',').filter(Boolean).join('、')}）`
  if (reason.includes('coverage')) return '抽取结果的覆盖范围不符合要求'
  if (reason.includes('invalid_node_ids')) return '节点标识无效或重复'
  if (reason.includes('duplicate_node_ids')) return '节点标识重复'
  if (reason.includes('assertion')) return '节点断言字段格式无效'
  if (reason.includes('retention_required')) return '需要保留原文后才能继续'
  return '图谱结构校验未通过'
}

export function reasonLabel(reason?: string): string {
  const value = typeof reason === 'string' ? reason.trim() : ''
  if (!value) return '未记录具体原因'
  if (reasonLabels[value]) return reasonLabels[value]
  const bare = value.replace(/^graph_(?:schema|evidence):/, '')
  if (reasonLabels[bare]) return reasonLabels[bare]
  if (value.startsWith('graph_schema:')) return schemaReason(value)
  if (value.startsWith('graph_evidence:')) return '原文证据校验未通过'
  if (value.startsWith('graph_identity:')) return '对象身份与其他项目冲突'
  if (value.startsWith('graph_migration:')) return '历史图谱迁移条件不满足'
  if (value.startsWith('graph_source')) return '原文来源已变化，批次已保留'
  return '其他候选原因（未记录具体说明）'
}

const warningLabels: Record<string, string> = {
  import_cross_project_records_excluded: '已过滤其他项目的记录。',
  import_sources_excluded: '已排除不属于当前项目的来源文件。',
}

export function warningLabel(code?: string): string {
  if (!code) return ''
  return warningLabels[code] || '其他诊断提示；相关记录已保留。'
}

const stageLabels: Record<string, string> = {
  extract: '抽取',
  validate: '校验',
  identity: '身份判断',
  relations: '关系判断',
  publish_nodes: '节点入库',
  publish_relations: '关系入库',
  worker: '后台处理',
  import: '导入原文',
}

export function stageLabel(stage?: string): string {
  if (!stage) return ''
  return stageLabels[stage] || '处理'
}

const traceReasonLabels: Record<string, string> = {
  stop: '正常结束',
  length: '达到输出上限',
  max_tokens: '达到输出上限',
  max_output_tokens: '达到输出上限',
  content_filter: '内容被过滤',
  tool_calls: '等待工具调用',
  function_call: '等待函数调用',
  incomplete: '输出不完整',
}

export function traceReasonLabel(reason?: string | null): string {
  if (!reason) return '见请求日志'
  return traceReasonLabels[reason] || '其他终止原因'
}
