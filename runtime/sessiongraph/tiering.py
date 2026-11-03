"""Deterministic canonical/episode tiering over existing graph evidence.

This is a write-side projection, not graph compaction. It never deletes a node,
memory, relation or source citation. The tier table answers one narrow question:
which evidence-backed objects belong in the default knowledge view, and which are
useful process records that must remain searchable and expandable.

No model, historical pair scan or fuzzy title merge is used. Unknown identity is
kept separate and explicitly pending rather than guessed equivalent.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

from .core import digest

VERSION = 'tiering-v3'
TIERS = ('canonical', 'episode', 'record_only')
STRONG = {
    'Source': {'doi', 'url'},
    'Execution': {'run_id'},
    'Artifact': {'artifact_version'},
    'Activity': {'url'},
}
KNOWLEDGE_TYPES = {'Claim', 'Decision', 'Artifact', 'Source'}
RECORD_TYPES = {'Execution', 'Activity'}

# ---------------------------------------------------------------- signals ---
# Tiering answers a narrower question than graph publication: would this object
# be useful in the default knowledge view?  A complete assertion or a strong
# identity proves addressability, not long-term value.  These deterministic
# purpose signals keep one-off test output, live status and transfer receipts
# out of that view.  They never remove the object or its evidence.
_LIVE = re.compile(
    r'(?i)(?:no such file or directory|查询返回|未找到|不存在|missing|'
    r'尚未(?:运行|完成|验证|执行|开始)|not_run|not\s+started|not\s+implemented|'
    r'运行中|仍在运行|仍在跑|等待|排队|重试|接续|继续暂停|未启动|未开始|'
    r'尚无[^\n。；]{0,40}(?:目录|文件)|没有检出|无输出|未见对应(?:工具)?结果|'
    r'转录仅显示|进行中(?:运行)?|采集进行中|stage\s+collecting|'
    r'in_progress|in-progress|\bstate\s*=\s*(?:running|pending|queued|waiting))'
)
_TEST_RUN = re.compile(
    r'(?i)(?:'
    r'\b\d+\s*(?:项|个|套)\s*(?:不同\s*)?[^\n。；]{0,40}(?:测试|用例|回归|检查|验证)'
    r'[^\n。；]{0,30}(?:通过|失败|ok|passed)|'
    r'\b\d+\s*(?:passed|failed)\b|'
    r'\b(?:pytest|unittest|runtime\s*测试|前端回归|全量回归|回归测试|测试套件|契约测试|合同测试)\b'
    r'[^\n。；]{0,100}(?:passed|failed|通过|失败|ok)|'
    r'(?:全量|前端|运行时|Python|CPU|单元|契约|合同)?\s*(?:回归|测试)'
    r'[^\n。；]{0,60}\b\d+\s*(?:项|个|套)?[^\n。；]{0,20}(?:通过|失败)|'
    r'\bRun\s+[0-9a-f]{8,}[^\n。；]{0,100}(?:失败|failed)|'
    r'\b(?:state\s*=\s*(?:failed|blocked)|error_code\s*=|exit\s*(?:code\s*)?[1-9]\b|'
    r'provider_http_\d+|http\s*4\d\d))'
)
_RECEIPT = re.compile(
    r'(?i)(?:上传|下载|同步|封包|传输|回执|推送|拷贝|'
    r'upload|download|receipt|sync|transfer|sha256\s*=\s*[0-9a-f]{16}|'
    r'tmux|pid\b|stdout|stderr|\.part\b|\.lock\b)'
)
_INVENTORY = re.compile(
    r'(?i)(?:规模快照|实测规模|规模[^\n。；]{0,20}(?:为|是|显示)|快照显示|体积|占用|计数|盘点|'
    r'记录数量|清理释放|释放约|\b\d+\s*(?:files?|文件|行)\b[^\n。；]{0,20}(?:大小|占用|总计))'
)
_PLAN = re.compile(
    r'(?i)(?:\b(?:next_step|proposed_not_started|todo)\b|下一步|下一项|后续计划|待办|待开始|'
    r'尚未开始|建议从|计划[^\n。；]{0,30}(?:执行|验证|开始))'
)
# An operational title narrates *what the process did*, not *what was learned*.
# Only the title is consulted: a durable finding legitimately reports run volume
# in its body.  This is the main guard against "N cases / N updates finished"
# records entering the knowledge view.
_OPERATIONAL_TITLE = re.compile(
    r'(?i)(?:'
    # a process verb close to the start of the title, followed by a completion
    # verb; "未通过"/"不通过" are findings and are excluded
    r'^(?:[^\n。；：（）]{0,28}?)(?:评估|训练|审计|回归|测试|检查|校验|核验|扫描|提取|抽取|'
    r'复核|重放|排查|重跑|pipeline|pytest|pre-?flight)[^\n。；]{0,20}?'
    r'(?:完成|结束|退出|跑完|finished|completed|(?<!未)(?<!不)通过|(?<!未)失败)'
    r'(?:[^\n。；]{0,20}(?:\d|fail|通过|失败|结束))?|'
    # completion immediately followed by a counted volume
    r'(?:完成|通过|失败|结束|finished|completed)[^\n。；]{0,4}'
    r'\d[\d,.]*\s*(?:项|个|次|案例|cases?|步|轮|条|updates?)|'
    r'\d[\d,.]*\s*(?:项|个|套|条路径|条轨迹)[^\n。；]{0,14}(?<!未)(?:通过|失败|passed|failed)|'
    r'\d[\d,.]*\s*项[^\n。；]{0,8}(?:测试|用例|检查|回归)[^\n。；]{0,4}(?<!未)(?:通过|失败)|'
    r'进度[:：]|进度[^\n。；]{0,10}\d+\s*/\s*\d+|'
    r'(?:实测)?(?:规模|计数|盘点|记录数量|体积|占用|记账)[^\n。；]{0,14}(?:快照|一致|为|是|显示)|'
    r'(?:仍有|剩余|当前)[^\n。；]{0,10}\d[\d,.]*\s*(?:万|千)?\s*(?:节点|条|行|文件|记录)|'
    r'运行状态|运行时状态|状态记录|现场快照|状态快照|状态校验|收尾状态|归档校验|'
    r'环境核验|环境与[^\n。；]{0,10}核验|进程存活|GPU\s*\d[^\n。；]{0,10}利用率|'
    r'pre-?flight|asset\s+scan|attempt\s+failed|in[_-]progress|'
    r'(?:回执|同步核验|上传|下载|封包|传输|部署收尾)'
    r')'
)
# Process artifacts are lifecycle records: the same file name can be a durable
# protocol or a one-shot transfer receipt.  Mark only names/purposes that are
# intrinsically transient; delivery status elsewhere must not erase identity.
_EPHEMERAL_ARTIFACT = re.compile(
    r'(?i)(?:progress[_-]?\w*\.json|status[_-]?\w*\.json|metrics_audit|run_handles|'
    r'next_step[_-]?\w*\.json|\.part\b|\.lock\b|\.log\b|heartbeat|'
    r'现场快照|下载回执|上传回执|同步核验|receipt|/tmp/|tmux|pid\b|stdout|stderr|'
    r'running_zh|status_zh|进度|状态记录|运行记录|运行状态|状态快照|安装文件|备份|'
    r'monitor|initial_monitor|snapshot|基线快照|初始快照|状态快照|training_finished)'
)
_STABLE_ARTIFACT = re.compile(
    r'(?i)(?:\.(?:onnx|pt|pth|ckpt|safetensors|zip|tar(?:\.gz|\.xz)?|pdf|md|json|csv|'
    r'yaml|yml|npy|npz|parquet|py|sqlite3?|toml|txt)\b|'
    r'交付包|交付归档|交付物|导出包|检查点|checkpoint|数据集|报告|协议|策略|教师|模型|'
    r'dataset|report|protocol|manifest|artifact|provenance|来源记录|文档|论文|源码|脚本|'
    r'交付物|产物|资产清单|清单|神经图|连接组|项目配置|实现|模块|接口|分析目录|'
    r'结果与审计|审计文件|文件包|核查报告|代码目录|工具|documentation|tensorboard\s+run)'
)
# A conclusion states a relationship, boundary or outcome that remains true
# beyond the execution.  Test counts and completion verbs deliberately do not
# qualify: those describe what happened, not what was learned.
_RELATION = re.compile(
    r'(?i)(?:结论|根因|原因|表明|说明|证明|意味着|因此|从而|导致|归因|'
    r'一致|不一致|相同|不同|高于|低于|大于|小于|超过|不足|优于|劣于|好于|差于|倍数|约\\s*\\d|'
    r'阻断|阻止|防止|保证|确保|决定|取决于|主导|集中|残留|主导残差|'
    r'增加|降低|下降|提升|改善|恶化|变差|变好|放大|缩小|'
    r'边界|约束|限制|仅限|仅能|只能|不能|无法|不得|禁止|不允许|不授权|'
    r'未通过|不通过|失败|不成立|成立|未成立|'
    r'通过率|准确率|误差|偏差|阈值|容差|门槛|标准|判据|条件|前提|预算|协议|口径|'
    r'显著|可复现|不可复现|过拟合|泛化|收敛|发散|饱和|守恒|不变量|'
    r'not\s+sufficient|cannot|can\s+not|insufficient|only\s+|limited|indicates?|'
    r'suggests?|consistent|inconsistent|reject|support|outperform|underperform|'
    r'dominates?|residual|correlat|causal|invariant|below|above|exceed)'
)
_NUMERIC = re.compile(
    r'\d[\d,.]*\s*(?:%|％|倍|毫秒|ms|us|µs|e[-+]?\d+|gi?b|mib|kib|项|个|次|帧|步|条|组|轮|'
    r'维|层|字节|bytes?|秒|s\b|分钟|小时)?', re.I)
# A single-run outcome ("4/4 passed", "error exit", "replay 9,600 slots") is a
# record unless the title also states a scope, limit or explicit conclusion.
_SINGLE_RUN_RESULT = re.compile(
    r'(?i)(?:'
    r'\d+\s*/\s*\d+[^\n。；]{0,12}(?<!未)(?:通过|失败|passed|failed)|'
    # a run/check verb adjacent to its outcome word
    r'(?:检查|复核|核验|重放|校验|扫描|评估|审计|运行|执行|测试)[^\n。；]{0,12}'
    r'(?:通过|失败|退出|超时|AssertionError|exit_code)|'
    r'(?:AssertionError|exit_code\s*=)|退出码\s*[1-9]|进程退出|'
    r'(?:运行|执行|命令|脚本|审计|检查|训练|作业)[^\n。；]{0,16}(?:退出|失败|超时)|'
    r'\b(?:run|job)\b[^\n。；]{0,40}\b(?:failed|completed)\b|'
    r'(?:可行性)?运行$|检查与[^\n。；]{0,12}复核|记为[^\n。；]{0,8}记账)'
)
_SCOPE_OR_CONCLUSION = re.compile(
    r'(?i)(?:整体|最终|门槛|阈值|边界|范围|限制|仅限|仅能|只能|不能|无法|不得|因此|从而|'
    r'结论|根因|表明|说明|证明|判据|外推|适用|前提|条件|候选|模型|路线|方案|'
    r'overall|final|threshold|boundary|conclude|indicates?)'
)
# An explicit conclusion marker is stronger than incidental relational wording.
# Structural markers of an execution record.  A run id, a state field or a
# not-run marker describes one attempt; no wording inside it can turn the record
# itself into a standing fact (the reusable finding must be its own object).
_RUN_MARKER = re.compile(
    r'(?i)(?:\bRun\s+[0-9a-f]{6,}|\brun_id\b|\btask_id\b|\bjob\s+[0-9a-f]{6,}|'
    r'\bstate\s*=\s*(?:failed|blocked|running|pending|queued|waiting|not_run)|'
    r'\berror_code\s*=|\bexit\s*(?:code\s*)?[1-9]\b|provider_http_\d+|'
    r'\bnot_run\b|未运行|状态均为\s*not_run)'
)
# A transfer action names what was moved, not what the object establishes.  If
# the only description is transfer metadata the object is a receipt; a bundle
# that also states its audited contents stays a durable object.
_TRANSFER_ACTION = re.compile(
    r'(?i)(?:上传|下载|同步|封包|传输|推送|拷贝|'
    r'uploaded|downloaded|synced|transferred|transfer\s+sha256)'
)
_CONTENT_MARK = re.compile(
    r'(?i)(?:审计|核验|预检|descriptive|manifest|清单|报告|结果|数据集|模型|协议|策略|'
    r'检查点|checkpoint|来源|provenance|论文|源码|图示|对比|指标|误差|曲线|日志归档|'
    r'audit|report|dataset|protocol|model|evidence)'
)
# A claim whose subject is the live state of a run, lease, marker or markerless
# index describes one observation, not a standing fact.
_STATE_ASSERTION = re.compile(
    r'(?i)(?:\bstate\s*(?:=|为|是)|lease|租约|已释放|released|'
    r'cleanup|清理标记|删除标记|marker|索引已为\s*0|truncated|'
    r'\bsegments\s*=\s*0|\bmemories\s*=\s*0|\bevidence\s*=\s*0|'
    r'(?:仍有|剩余|当前)[^\n。；]{0,12}\d[\d,.]*\s*(?:万|千)?\s*(?:节点|条|行|文件|记录))'
)
_CONCLUSION_MARK = re.compile(
    r'(?i)(?:结论|根因|表明|说明|证明|发现|意味着|因此|从而|导致|归因于|'
    r'判据|断言|不能|无法|仅能|只能|不足|不支持|support|reject|conclude)'
)

# A terminal, evidence-labelled finding can itself be the reusable conclusion:
# the extractor already chose ``retention=knowledge`` and the phrase states a
# relationship, failure, boundary or reproducibility result. This does not
# promote a bare completion count or a single tolerance pass. Those remain run
# observations and stay in the episode layer.
_RESULT_STATEMENT = re.compile(
    r'(?i)(?:'
    r'不可靠|未显示[^\n。；]{0,30}(?:普遍|收益|改善|提升|差异)|'
    r'精确一致|完全一致|逐位一致|'
    r'(?:全部|四个|四组|各组|均)[^\n。；]{0,30}(?:失败|不通过|未满足|违反)|'
    r'(?:违反|未满足|不符合)[^\n。；]{0,24}(?:条件|约束|门槛|要求)|'
    r'留出[^\n。；]{0,30}(?:通过|失败)|\b\d+\s*/\s*\d+\b[^\n。；]{0,24}(?:通过|失败)|'
    r'泛化[^\n。；]{0,24}(?:失败|通过|不足)|'
    r'不受[^\n。；]{0,16}(?:控制|影响)|归因|根因(?:是|为)|'
    r'(?:cannot|does\s+not|failed\s+to)\s+[^\n。；]{0,40}(?:establish|generalize|hold|pass)'
    r')'
)


# A few conclusions use comparison or correction language that the base signal
# intentionally did not treat as durable. Keep these additions narrow: they
# describe a relation between observations, not a completion count.
_RELATION_EXTRA = re.compile(
    r'(?i)(?:增至|升至|降至|减少|相对|相比|对照|上一轮|旧场景|留出集|开发集|验证集|'
    r'不合格|越界|超出|无(?:合格|通过|候选)|没有(?:合格|通过|候选)|未产生|未创建|'
    r'没有做|未做|噪声|演化|分叉|重建|可重建|一致|相等|不等|误差|偏差|'
    r'tolerance|exact|mismatch|degrad|improv|regress|worse|better)'
)
# Scope is stronger than a passing threshold: it names where the observation
# holds, where it does not, or what it establishes. A tolerance check on one
# replay is a run record; "only on fixed initial states" is reusable knowledge.
_STANDING_SCOPE = re.compile(
    r'(?i)(?:仅限|仅限于|只在|仅在|仅对|只对|范围(?:仅|为|是)|边界|不能外推|不可外推|'
    r'不适用于|适用于|前提(?:是|为)|条件(?:是|为)|结论(?:是|为)|根因(?:是|为)|'
    r'表明|说明|证明|意味着|因此|从而|导致|归因于|仍无|没有(?:通过|合格)|无(?:通过|合格)|'
    r'原门槛不变|门槛保持不变|不放宽|不修改|不改变|保持不变|仅作|非[^\n。；]{0,16}验收|'
    r'候选未采用|未采用|保留原|未创建|没有做|未做|尚未最终确认|'
    r'scope(?:d)?\s+to|only\s+(?:on|for|when)|does\s+not\s+generalize|not\s+substituted|'
    r'fixed\s+invariant|cannot\s+be\s+inferred|therefore|indicates?)'
)
# A single replay/check/probe can establish something only when it also states
# a standing scope or causal conclusion. Otherwise it belongs to episode even
# when a complete atomic tuple is available.
#
# Only a *counted check outcome* counts as one observation here. A bare ratio
# elsewhere in a sentence is usually a metric quoted by a finding ("first-step
# error 199/200", "within-segment rmse ~0.003"), not a pass/fail tally, and
# demoting those loses real results. The 'N/M' branches are therefore anchored
# to an outcome word on one side or the other instead of matching any fraction.
_SINGLE_OBSERVATION = re.compile(
    r'(?i)(?:'
    r'(?:通过|失败|passed|failed|存活)\s*[:：]?\s*\d+\s*/\s*\d+|'
    r'\d+\s*/\s*\d+\s*(?:通过|失败|passed|failed|存活)|'
    r'误差(?:为|是|=|\s*)?\s*0(?:\.0+)?(?:\b|$)|最大(?:绝对)?差|'
    r'通过\s*(?:原\s*)?(?:atol|rtol|容差|tolerance)|超时|timeout|退出码|exit\s*code|'
    r'AssertionError|provider_http_\d+|(?:passed|failed)\s*=\s*\d+[^\n。；]{0,24}total\s*=\s*\d+)'
)
# A pre-registered protocol is durable even before it finishes: fixed data
# splits, seeds, budgets and non-goals remain useful after the run. This does
# not turn a bare "next step" into knowledge because several concrete controls
# and a quantified design are required by _protocol_note().
_PROTOCOL = re.compile(
    r'(?i)(?:预注册|预先注册|协议|固定预算|fixed[-\s]?budget|对照(?:实验|组|协议)|'
    r'no_retry|no_threshold|no_unqualified|required_recordings|separate_questions|'
    r'initialization_seeds?|max_updates|eval_every|batch\s*\d+|lr\s*\d|'
    r'seed|pilot|实验设计|训练集|验证集|开发集|留出集|原门槛|参数不变|'
    r'保留同样|同样的\s*\d+|从零训练|尚未启动|proposed_not_started)'
)
# Technical specifications are durable facts even without a numeric effect:
# schema contracts, call order, configuration semantics and invariants.
_SPECIFICATION = re.compile(
    r'(?i)(?:schema|字段|枚举|必须(?:含|提供|是|为)|接口|api|配置|参数|默认|'
    r'调用顺序|执行顺序|forward\s+order|runs?\s+|then\s+|unless\s+|'
    r'状态机|生命周期|semantics?|invariant|fixed\s+invariant|not\s+substituted|'
    r'只在|仅在|仅当|只有[^\n。；]{0,30}才|逻辑(?:先|是)|行为(?:是|为)|'
    r'documentation|文档说明|definition|定义|解析出|resolved_target_ids|'
    r'actuator_names|observation|观测项|动作顺序)'
)
# Process-only surfaces that should not become knowledge merely because they
# contain a complete tuple.
_PROCESS_ONLY = re.compile(
    r'(?i)(?:单元测试|合同测试|契约测试|集成测试|unittest|pytest|'
    r'\b\d+\s*项测试|测试通过|\d+\s*项(?:测试|用例)|'
    r'report\.py|报告读取|读取核验|文案修复|运行状态|运行时状态|状态记录|'
    r'进度轮询|监控同步|pipeline\s+status|audit\s+inspection|GPU\s*占用|'
    r'环境与\s*GPU|进程存活|部署收尾|安装文件|回执|上传|下载|封包|传输)'
)
_BARE_FILE = re.compile(
    r'(?i)^\s*(?:[A-Za-z]:)?(?:[^\s/]+/)*[^\s/]+'
    r'(?:\.(?:onnx|pt|pth|ckpt|safetensors|zip|tar(?:\.gz|\.xz)?|pdf|md|json|csv|'
    r'yaml|yml|npy|npz|parquet|py|sqlite3?|toml|txt|gz|zst))?\s*$'
)
_FINAL_DELIVERY = re.compile(
    r'(?i)(?:交付包|交付目录|交付归档|文件包|归档包|核查报告|逐文件核验|'
    r'all_packaged_size_and_hash_checks_passed|existing_sources_unchanged|'
    r'previous_scientific_snapshots_preserved)'
)
# A lifecycle snapshot of a training input is provenance, not a generated
# status file. The title must name both the object kind and the provenance, so
# a generic "状态快照" still stays in episode.
_INPUT_SNAPSHOT = re.compile(
    r'(?i)(?:初始(?:状态|输入|权重|参数)(?:快照)?|initial[_\s-]?(?:state|input|weights?)|'
    r'(?:输入|来源|初始)(?:状态)?(?:文件|快照|权重)|'
    r'raw_(?:teacher|student|policy|model)\w*\s+初始状态快照)'
)
_DIAGNOSTIC_RESULT = re.compile(
    r'(?i)(?:max_abs|max_error|mismatch|equal\s*=|exact|不一致|'
    r'不合格|越界|超出|未采用|无候选|通过\s*\d+\s*/\s*\d+|passed\s*=|failed\s*=|'
    r'验收通过|部署验收|完成验收|审计结果|诊断结果|对照结果|结果[:：]|'
    # A measured deviation stated against a target is a diagnostic outcome too:
    # "所有有效控制步 ... 最大差值为0", "平均速度 ... 仍低于所请求的0.3".
    r'最大差值(?:为|=|是)\s*\d|'
    r'低于[^\n。；]{0,12}(?:请求|所请求|下限|阈值|门槛)|'
    r'(?:均|都|仍)低于[^\n。；]{0,10}\d)'
)
_DEPLOYMENT_ACCEPTANCE = re.compile(
    r'(?i)(?:部署验收|验收通过|部署验收通过|acceptance\s+passed|service\s+enabled)'
)
# A result on a pre-registered held-out split is an evaluation outcome whose
# scope is already fixed. The test runner wording must not erase that result.
_HELD_OUT_RESULT = re.compile(
    r'(?i)(?:留出|holdout|held[-\s]?out|盲测|独立测试|预注册(?:的)?独立评估|'
    # A training/validation split comparison states where the result did not
    # transfer; that is a reusable boundary, not a generic test count.
    r'训练集[^\n。；]{0,24}验证集|验证集[^\n。；]{0,24}(?:未通过|通过|失败))'
)
# A *held-out evaluation result* is an outcome whose population was fixed before
# the run, so the count itself is reusable evidence. This is deliberately
# stricter than ``_HELD_OUT_RESULT``, which also matches a plain mention of a
# split ("留出集未使用", "仍看留出种子的结果"). Only an explicit
# split<->counted-outcome adjacency qualifies.
_HELD_OUT_EVALUATION = re.compile(
    r'(?i)(?:'
    r'(?:留出|holdout|held[-\s]?out|盲测|独立测试|预注册独立评估|验证集)'
    r'[^\n。；]{0,30}?(?:\d+\s*/\s*\d+|\d+\s*(?:项|个|条|例))'
    r'[^\n。；]{0,12}?(?:通过|失败|passed|failed|存活)|'
    r'\d+\s*/\s*\d+[^\n。；]{0,20}?(?:留出|holdout|held[-\s]?out|盲测|独立测试)|'
    r'(?:留出|holdout|held[-\s]?out|盲测|独立测试|预注册独立评估)'
    r'[^\n。；]{0,20}?(?:通过|失败|passed|failed)'
    r')'
)
# A record whose own body says the producing run is still being rebuilt or
# recomputed describes a moving number. Until that run finishes, the number is
# not a standing conclusion, even when the extractor labelled it knowledge.
_LIVE_PRODUCER = re.compile(
    r'(?i)(?:重建中|重训中|重新训练中|重启中|重跑中|重算中|补训中|复算中)'
)
# Language that already carries a standing boundary, so a live-producer mention
# elsewhere in the same object cannot erase it.
_STANDING_OBSERVATION = re.compile(
    r'(?i)(?:结论|根因|表明|说明|证明|意味着|因此|从而|导致|'
    r'未改变|不等于|不能等同|仍需|验收|边界|仅限|门槛|判据)'
)
_FAILURE_RECORD = re.compile(
    r'(?i)(?:AssertionError|Traceback|exit\s*code\s*[1-9]|退出码\s*[1-9]|'
    r'存在性断言失败|断言失败|运行失败|执行失败)'
)


def _relation(text):
    return bool(_RELATION.search(text) or _RELATION_EXTRA.search(text))


def _protocol_note(row):
    """Whether the object is a concrete pre-registered protocol.

    A plan alone is not enough. Require both protocol language and at least two
    concrete controls, or a body that explicitly fixes budgets, seeds, splits or
    non-goals. This keeps "next step" records in episode while preserving
    reusable experimental designs.
    """
    text = _text(row)
    title = row.get('title') or ''
    if _PROCESS_ONLY.search(title):
        return False
    if re.search(r'(?i)(?:进行中运行|采集进行中|stage\s+collecting|'
                 r'\bstate\s*=\s*running\b|进度\s*\d+\s*/\s*\d+'
                 r'|completed\s*=\s*\d+[^\n。；]{0,16}planned\s*=\s*\d+)', text) \
            and not re.search(r'(?i)(?:协议|预注册|边界|仅限|不得|保持|不超过)', title + '\n' + text):
        return False
    if not _PROTOCOL.search(text):
        return False
    controls = len(re.findall(
        r'(?i)(?:seed|max_updates|eval_every|batch\s*\d+|lr\s*\d|'
        r'固定预算|固定\s*\d+|每终点|每批|从零训练|'
        r'仅限|只|不(?:做|调用|重抽|修改|放大|放宽|改变)|'
        r'尚未启动|proposed_not_started|training_updates\s*=\s*0|'
        # Concrete control values that are already fixed even without an
        # explicit English field name.
        r'最多\s*\d+|至少\s*\d+|不超过\s*\d+|'
        r'原(?:门槛|预算|参数|策略|判据)(?:不变|保持不变)|'
        r'保留(?:同样|原)|独立于[^\n。；]{0,20}预算|'
        r'\b\d+\s*(?:维|D)\b|50\s*hz|不裁剪|0\s*(?:优化|重试)|'
        r'单固定(?:回合|初态)|固定初态|新初态)', text))
    # A protocol title plus any two concrete controls is a durable design. A
    # single control word (for example just "预注册" or just "seed") remains a
    # plan/status record until the comparison is concretely specified.
    return controls >= 2 or (controls >= 1 and _constraint_bearing(text)
                             and bool(re.search(r'(?i)(?:预注册|对照(?:实验|组|协议)|'
                                                r'fixed[-\s]?budget)', text)))


def _specification_note(row):
    """A concrete schema/API/order/config fact, not a process narration."""
    text = _text(row)
    title = row.get('title') or ''
    if _PROCESS_ONLY.search(title):
        return False
    if _LIVE.search(text) or re.search(r'(?i)(?:未见对应(?:工具)?结果|转录仅显示|'
                                        r'进行中运行|stage\s+collecting)', text):
        return False
    hits = len(_SPECIFICATION.findall(text))
    if hits < 2:
        return False
    # A live run or a missing object is not a specification. A named schema,
    # documented interface, or parsed configuration is.
    if _RUN_MARKER.search(text) and not re.search(r'(?i)(?:schema|字段|枚举|接口|配置|文档|definition)', text):
        return False
    return True


def _diagnostic_note(row):
    """A terminal diagnostic whose *title* names the investigation.

    The diagnostic verb must be in the title. A generic object titled
    "核验结果" or a run status is not an investigation, and a live or proposed
    record is not a terminal result.
    """
    text = _text(row)
    title = row.get('title') or ''
    if not _terminal(row):
        return False
    if _PROCESS_ONLY.search(title):
        return False
    if not re.search(r'(?i)(?:诊断|探针|审计|对照|实测|校准|核验|验证)', title):
        return False
    if not _DIAGNOSTIC_RESULT.search(text):
        return False
    if (_FAILURE_RECORD.search(text) and not _STANDING_SCOPE.search(text)
            and not _CONCLUSION_MARK.search(text)):
        return False
    if re.search(r'(?i)(?:失败现场|隔离修复|未自动重跑|仍有[^\n。；]{0,20}(?:未启动|未完成)|'
                 r'实际产生\s*0\s*次环境交互)', text) and not _CONCLUSION_MARK.search(text):
        return False
    if (re.search(r'(?i)(?:exploratory_gate_passed\s*=\s*false|探索性门槛未通过)', text)
            and not _STANDING_SCOPE.search(text)):
        return False
    # A bare progress counter is not a result. Require a quantified result plus
    # a relation or standing scope, or an explicit multi-branch outcome.
    if not (_NUMERIC.search(text) and (_relation(text) or _STANDING_SCOPE.search(text))):
        return False
    return True


def _text(row):
    return ((row.get('title') or '') + '\n' + (row.get('content') or '')).strip()


def _purpose(row):
    """Return a conservative presentation purpose for the object's text.

    Purpose is evaluated on the title first. A durable object can legitimately
    mention an upload or hash in its body; that does not turn the object into a
    receipt. The body is consulted only when the title is empty or is itself a
    bare file reference. This keeps incidental mentions in a finding from
    re-labelling an object whose title already states what it is.
    """
    title = (row.get('title') or '').strip()

    def probe(candidate):
        if not candidate:
            return None
        if _LIVE.search(candidate):
            return 'live_or_query'
        if _TEST_RUN.search(candidate):
            return 'test_or_run'
        if _RECEIPT.search(candidate):
            return 'receipt'
        if _INVENTORY.search(candidate):
            return 'inventory'
        if _PLAN.search(candidate):
            return 'plan'
        return None

    # The title names the object. Body text is consulted only when the title
    # cannot name a purpose by itself: an empty title or a literal file/path.
    # A short but meaningful sentence such as "稳定结论：仅限固定初态" must not
    # be reclassified because its body happens to mention an upload or hash.
    found = probe(title)
    # A stable checkpoint name is an object, not a receipt. Its body may cite
    # the receipt that referenced it, but the title contains no transfer verb.
    if (found == 'receipt'
            and re.search(r'(?i)\.(?:onnx|pt|pth|ckpt|safetensors)\b', title)
            and not _TRANSFER_ACTION.search(title)):
        found = None
    if found:
        return found
    bare_reference = bool(title and re.fullmatch(
        r'(?i)(?:[A-Za-z]:)?(?:[^\s/]+/)*[^\s/]+\.'
        r'(?:onnx|pt|pth|ckpt|safetensors|zip|tar(?:\.gz|\.xz)?|pdf|md|json|csv|'
        r'yaml|yml|npy|npz|parquet|py|sqlite3?|toml|txt|gz|zst)',
        title))
    if not title or bare_reference:
        return probe((row.get('content') or '').strip()[:160])
    return None


def _kind(row):
    return (row.get('kind') or '').strip().casefold()


def _terminal(row):
    return row.get('status') in {'completed', 'active'}


def _live_producer_only(row, text, title, *, held_out=False):
    """Whether this object reports a number from a run that is still rebuilding.

    The guard is intentionally narrow: the object must still be active, its own
    body must say the producing run is being rebuilt or recomputed, and nothing
    in it may already state a conclusion, boundary or reusable result. A plain
    mention of an in-progress job inside an otherwise finished finding therefore
    keeps its knowledge status.
    """
    if row.get('status') not in {'active', 'in_progress'}:
        return False
    if held_out or not _LIVE_PRODUCER.search(text):
        return False
    if (_STANDING_OBSERVATION.search(title + '\n' + text)
            or _STANDING_SCOPE.search(text)
            or _CONCLUSION_MARK.search(text)
            or _RESULT_STATEMENT.search(text)
            or _constraint_bearing(text)):
        return False
    return True


def _durable_conclusion(row):
    """Whether text expresses a reusable conclusion rather than a run record.

    Used when the extractor did not provide a complete atomic assertion.  It
    requires an explicit semantic relation plus a quantitative, negative,
    scoped or explicitly concluded payload; a bare narrative of what ran does
    not qualify.
    """
    text = _text(row)
    title = row.get('title') or ''
    if not _relation(text):
        return False
    # Pure process narration is excluded even when it contains relational words
    # such as "通过" or "审计".  A finding may still report run volume inside a
    # title that names the question, which is why only the title is checked.
    if _OPERATIONAL_TITLE.search(title) and not _CONCLUSION_MARK.search(title):
        return False
    single_run = (_SINGLE_RUN_RESULT.search(title)
                  and not _CONCLUSION_MARK.search(title)
                  and not _SCOPE_OR_CONCLUSION.search(title))
    if single_run:
        # A counted result on a pre-registered held-out split states a boundary
        # of applicability rather than one run's tally. A plain mention of a
        # split is not enough: the split must be adjacent to its outcome.
        if not (_HELD_OUT_EVALUATION.search(text)
                and _RESULT_STATEMENT.search(text)):
            return False
    if _CONCLUSION_MARK.search(text):
        return True
    if _NUMERIC.search(text) and _relation(text):
        return True
    return bool(_INVENTORY.search(text) and _NUMERIC.search(text))

SCHEMA = '''
CREATE TABLE IF NOT EXISTS sg_node_tiers(
 node TEXT PRIMARY KEY REFERENCES work_memory(id) ON DELETE CASCADE,
 project TEXT NOT NULL,
 tier TEXT NOT NULL CHECK(tier IN ('canonical','episode','record_only')),
 canonical_id TEXT REFERENCES work_memory(id) ON DELETE SET NULL,
 identity_key TEXT NOT NULL DEFAULT '',
 reason TEXT NOT NULL,
 revision TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sg_node_tiers_project ON sg_node_tiers(project,tier);
CREATE INDEX IF NOT EXISTS sg_node_tiers_canonical ON sg_node_tiers(project,canonical_id);
'''


def setup(store):
    """Create only the shadow mapping. No sg2/work_memory rows are rewritten.

    Creating the table must not advertise an empty or partially backfilled table
    as the current rule generation. The revision is advanced only by ``run`` or
    by publication of a projected row; until then an existing v1/v2 mapping stays
    explicitly versioned and mixed rows cannot masquerade as v3.
    """
    store._ensure_writable()

    def install():
        # execute() one statement at a time: executescript() implicitly commits and
        # would leak a locking transaction into a caller that owns the connection.
        for statement in (part.strip() for part in SCHEMA.split(';')):
            if statement:
                store.db.execute(statement)
        columns = {row[1] for row in store.db.execute('PRAGMA table_info(sg_node_tiers)')}
        if 'identity_key' not in columns:
            store.db.execute("ALTER TABLE sg_node_tiers ADD COLUMN identity_key TEXT NOT NULL DEFAULT ''")
        store.db.execute('CREATE INDEX IF NOT EXISTS sg_node_tiers_identity ON sg_node_tiers(project,identity_key)')

    # Do not commit an outer publication transaction. When setup starts its own
    # implicit transaction, close it before returning the connection to callers.
    if store.db.in_transaction:
        install()
    else:
        with store.db:
            install()


def available(store):
    return bool(store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sg_node_tiers'"
    ).fetchone())


def _json(raw, default):
    try:
        value = json.loads(raw or '')
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _atomic(payload):
    value = payload.get('assertion')
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        return None
    atom = value[0]
    required = ('subject', 'predicate', 'value', 'unit', 'conditions')
    if any(key not in atom for key in required):
        return None
    if not isinstance(atom['conditions'], list):
        return None
    return atom


def _literal(text, value):
    return isinstance(value, str) and bool(value.strip()) and value in (text or '')


def conclusion_hint(identity, node_type):
    """Deterministic grouping for derived Claim/Decision conclusions.

    The extractor's atomic lane is stronger; this only groups nodes that carry
    the literal, experiment-scoped conclusion identity. The tier itself is not
    changed by this hint.
    """
    from .conclusion import key_values
    values = sorted({
        key_values(i) for i in identity
        if isinstance(i, dict) and i.get('namespace') == 'claim_conclusion'
    })
    if node_type not in {'Claim', 'Decision'} or len(values) != 1:
        return None
    value, scope = values[0]
    return 'cc_' + digest(json.dumps([node_type, scope, value], ensure_ascii=False,
                                    sort_keys=True, separators=(',', ':')))[:40]


def _identity_strong(row, identity, node_type):
    """A namespaced identity plus scope, or an explicit stable lifecycle object.

    Strong identity makes an object addressable, not automatically durable; the
    caller combines it with the artifact lifecycle decision below.
    """
    strong = [i for i in identity
              if isinstance(i, dict) and i.get('namespace') in STRONG.get(node_type, set())
              and i.get('value') and i.get('scope')]
    return bool(strong)


def _record_note(row):
    """Process records stay in the episode layer, but a terminal Execution that
    carries a reusable conclusion is first-class knowledge.

    Execution/Activity are the extractor's *process* classes.  Demoting all of
    them loses real experimental results that were stored as progress records;
    promoting all of them re-inflates the knowledge view with run narration.
    The textual conclusion contract is the discriminator, so a run that merely
    finished stays a record and a run that established something does not.
    """
    if not _terminal(row):
        return None
    if _OPERATIONAL_TITLE.search(row.get('title') or ''):
        return None
    if _RUN_MARKER.search(_text(row)):
        return None
    if re.search(r'(?i)(?:单元测试|合同测试|契约测试|集成测试|unittest|pytest|'
                 r'报告读取|读取核验|evaluate|report\.py|文案修复)', _text(row)):
        return None
    # An incident narrative that ends with "isolated repair" is still a run
    # record even when it reports a successful re-check afterwards.
    if re.search(r'(?i)(?:失败现场|隔离修复|未自动重跑|仍无新的恢复结果)', _text(row)):
        return None
    # A diagnostic/audit title plus a quantified, scoped result is reusable even
    # without an explicit "therefore"/"结论" phrase. A failed attempt followed by
    # isolated repair is still a run record; the reusable finding must be
    # expressed as a result, not as the incident narrative.
    if _diagnostic_note(row) and not re.search(
            r'(?i)(?:失败现场|隔离修复|未自动重跑|实际产生\s*0\s*次环境交互)', _text(row)):
        return 'execution_conclusion'
    if not _durable_conclusion(row):
        return None
    # A numeral plus a relational word is not enough: the record must state a
    # conclusion, a boundary or a scoped result.  Otherwise a generic run such as
    # "核验结果 / 误差数值为1" would be promoted on its run identity alone.
    if not (_CONCLUSION_MARK.search(_text(row)) or _STANDING_SCOPE.search(_text(row))
            or _SCOPE_OR_CONCLUSION.search(row.get('title') or '')):
        return None
    if not _NUMERIC.search(_text(row)):
        return None
    return 'execution_conclusion'



def _constraint_bearing(text):
    """A standing rule, not merely a passing mention of a protocol or budget.

    Bare nouns such as 协议/标准 caused status records that merely include a
    protocol hash to be promoted. Require an explicit requirement, prohibition,
    preserved boundary or fixed invariant instead.
    """
    return bool(re.search(
        r'(?i)(?:必须|不得|禁止|只允许|仅允许|仅限|'
        r'不做|不重抽|不调用|不修改|不改变|不放宽|不放大|不扩展|不授权|不要求|不允许|不准|不可用|'
        r'原门槛不变|门槛保持不变|参数保持不变|'
        r'原子提交且可撤销|每批原子提交|可撤销|'
        r'不能外推|不可外推|非验收|仅作[^\n。；]{0,12}(?:诊断|参考|预检)|'
        r'invariant|fixed\s+invariant|not\s+substituted|do_not|prohibit|'
        r'must\s+(?:not|remain|keep)|shall\s+(?:not|remain)|'
        r'constraint(?:\s*[:：]|\s+is|\s+requires)|'
        r'protocol\s+(?:fixed|requires|unchanged)|threshold\s+(?:fixed|unchanged))',
        text or ''))


def _standing_result(row):
    """A quantified result plus an explicit scope/limit/conclusion.

    This is the bridge between a process narration and knowledge. It does not
    require a complete atomic tuple, but it does require more than a completion
    count: numbers alone are deliberately insufficient.
    """
    text = _text(row)
    if not _NUMERIC.search(text) or not _relation(text):
        return False
    if not (_STANDING_SCOPE.search(text) or _CONCLUSION_MARK.search(text)):
        return False
    return True


def _standing_activity(row):
    """An Activity is knowledge only when it states a scoped outcome.

    Activities are normally plans/logs. The narrow exception is a bounded
    investigation that produced a reusable result and explicitly limits where
    that result applies.
    """
    if row.get('type') != 'Activity' and _kind(row) != 'goal':
        return None
    if _PROCESS_ONLY.search(_text(row)):
        return None
    if _protocol_note(row):
        return 'protocol'
    if not _terminal(row):
        return None
    if _standing_result(row) and _STANDING_SCOPE.search(_text(row)):
        return 'scoped_result'
    return None


def _knowledge_note(row, *, purpose, operational, lifecycle_veto, durable, terminal):
    """Shared non-process knowledge contract for Claim/Decision/finding.

    Returns a reason string when the object is durable knowledge, otherwise
    None so the caller can report the most specific process reason.
    """
    text = _text(row)
    title = row.get('title') or ''

    # Explicit standing constraints and specifications remain knowledge even
    # when the surrounding sentence is a plan, a failed check or a test record.
    # A standing constraint is knowledge once the object has a terminal/current
    # state. A live progress narration can mention a constraint without turning
    # the narration itself into a decision.
    if (terminal and _constraint_bearing(text)
            and (_relation(text) or _NUMERIC.search(text)
                 or _SPECIFICATION.search(text))):
        return 'decision_knowledge'
    if _specification_note(row):
        return 'specification'
    if re.search(r'(?i)(?:充要条件|充分条件|必要条件|未找到实例|未证明|交集为空)', text) \
            and _relation(text):
        return 'specification'
    if _protocol_note(row):
        return 'protocol'
    # A statement about the deletion boundary itself is reusable product
    # behaviour, not a transfer receipt. The trigger is the defect being
    # described (deleted source still readable / cleanup marker missing), not
    # the word "receipt" appearing in the evidence.
    if (re.search(r'(?i)(?:已清理|删除后|删除边界|cleanup[_ ]marker|清理标记|'
                  r'源文件[^\n。；]{0,20}(?:缺失|不存在)|仍可(?:读取|访问))', text)
            and re.search(r'(?i)(?:回执|读取接口|run_receipts|hidden\s*=|'
                          r'不检查|cleanup[_ ]marker|清理标记)', text)):
        return 'specification'
    if _DEPLOYMENT_ACCEPTANCE.search(title) and _NUMERIC.search(text):
        return 'deployment_acceptance'
    # A complete atomic assertion is the strongest identity, but a single
    # observation still needs a standing scope or explicit conclusion.
    atom = _atomic(_json(row.get('assertion_json'), {}))
    if atom is not None and not row.get('assertion_unusable'):
        # A single observation is normally a record, not knowledge. The narrow
        # exception is a terminal finding that the extractor marked as knowledge
        # and whose title/body states a reusable result (failure, invariance,
        # reproducibility or a held-out outcome). A bare run count or a one-off
        # tolerance pass does not satisfy ``_RESULT_STATEMENT``.
        terminal_result = (
            terminal and _RESULT_STATEMENT.search(title + '\n' + text)
            and not _OPERATIONAL_TITLE.search(title)
        )
        if (_SINGLE_OBSERVATION.search(text)
                and not _STANDING_SCOPE.search(text)
                and not _CONCLUSION_MARK.search(text)
                and not terminal_result):
            return None
        if (_STATE_ASSERTION.search(title) and not durable
                and not _STANDING_SCOPE.search(text)):
            return None
        return 'atomic_assertion'

    # No complete tuple: a terminal scoped conclusion can still be reusable.
    # A bare tolerance pass is a one-off check, not a reusable conclusion. A
    # result statement (failure, invariance, held-out outcome) is allowed to
    # establish knowledge even when the extractor did not produce an atomic
    # tuple.
    tolerance_only = bool(
        _SINGLE_OBSERVATION.search(text)
        and not _RESULT_STATEMENT.search(text)
        and not _STANDING_SCOPE.search(text)
        and not _CONCLUSION_MARK.search(text))
    # A result statement plus extractor-retention knowledge is a terminal
    # conclusion even when the numeric shape looks like a single observation.
    retention_knowledge = _json(row.get('assertion_json'), {}).get('retention') == 'knowledge'
    if terminal and durable and (not tolerance_only or (
            retention_knowledge and _RESULT_STATEMENT.search(text))):
        if retention := _json(row.get('assertion_json'), {}).get('retention'):
            if retention == 'knowledge':
                return 'knowledge_retention'
        if _kind(row) == 'finding':
            return 'finding_conclusion'
        if _kind(row) == 'progress' and _NUMERIC.search(text):
            return 'progress_conclusion'
        if row.get('type') == 'Claim':
            return 'finding_conclusion'
    # A proposed/active analytical conclusion with an explicit standing scope
    # is knowledge even before a run finishes (e.g. a bound causal result).
    if terminal and durable and _STANDING_SCOPE.search(text) and _NUMERIC.search(text):
        return 'scoped_conclusion'
    if _diagnostic_note(row):
        return 'diagnostic_conclusion'
    return None

def _classify(row):
    """Return (tier, reason, canonical_hint) from one joined node/evidence row.

    canonical_hint is a deterministic identity hash only for a fully anchored
    assertion. It is not a semantic-equivalence claim; conflicting values never
    share the hint.

    Channels, in order:
      1. extractor vetoes   - record_only / example / uncertain
      2. history            - LegacyMemory has no sg2 identity, textual contract
      3. process classes    - Execution/Activity/goal unless a scoped result
      4. decisions          - durable constraints, protocols, specifications
      5. Artifact/Source    - object lifecycle, not transfer wording
      6. Claim/finding      - purpose veto, atomic assertion, terminal conclusion
    """
    payload = _json(row.get('assertion_json'), {})
    identity = _json(row.get('identity_json'), [])
    node_type = row.get('type') or ''
    retention = payload.get('retention')
    context = payload.get('context_kind')
    kind = _kind(row)
    purpose = _purpose(row)
    text = _text(row)
    title = row.get('title') or ''
    terminal = _terminal(row)
    durable = _durable_conclusion(row)
    operational = bool(_OPERATIONAL_TITLE.search(title))
    lifecycle_veto = purpose in {'test_or_run', 'live_or_query', 'receipt', 'inventory'}

    # 1. Extractor vetoes are record classes, not semantic judgments.
    if retention == 'record_only':
        return 'record_only', 'extractor_record_only', None
    if context == 'example':
        return 'episode', 'example_context', None
    if context == 'uncertain':
        return 'episode', 'context_uncertain', None

    # 2. Historical memories have no sg2 identity/assertion.  Reuse the same
    #    textual contract instead of blanket-demoting every legacy conclusion.
    if node_type == 'LegacyMemory':
        if kind == 'decision':
            if _constraint_bearing(text) or _protocol_note(row):
                return 'canonical', 'legacy_decision', None
            if purpose == 'plan':
                return 'episode', 'legacy_plan_only', None
            if lifecycle_veto and not durable:
                return 'episode', 'legacy_process_record', None
            return 'canonical', 'legacy_decision', None
        if kind == 'artifact':
            if _EPHEMERAL_ARTIFACT.search(title):
                return 'episode', 'legacy_ephemeral_artifact', None
            if _STABLE_ARTIFACT.search(title) and terminal:
                return 'canonical', 'legacy_stable_artifact', None
            return 'episode', 'legacy_artifact', None
        if kind in {'finding', 'progress'}:
            if _protocol_note(row):
                return 'canonical', 'legacy_protocol', None
            if _specification_note(row):
                return 'canonical', 'legacy_specification', None
            if purpose == 'plan' and not _constraint_bearing(text):
                return 'episode', 'legacy_plan_only', None
            if lifecycle_veto and not durable:
                return 'episode', 'legacy_process_record', None
            if operational and not _CONCLUSION_MARK.search(text):
                return 'episode', 'legacy_process_record', None
            if (_SINGLE_RUN_RESULT.search(title) and not _CONCLUSION_MARK.search(title)
                    and not _STANDING_SCOPE.search(text)):
                return 'episode', 'legacy_process_record', None
            if (_STATE_ASSERTION.search(title) and not _STANDING_SCOPE.search(text)
                    and not _CONCLUSION_MARK.search(text)):
                return 'episode', 'legacy_process_record', None
            if terminal and durable and not _INVENTORY.search(title):
                return 'canonical', 'legacy_conclusion', None
            return 'episode', 'pre_graph_memory', None
        if retention == 'knowledge' and row.get('linked') and purpose is None:
            return 'canonical', 'knowledge_linked', None
        return 'episode', 'pre_graph_memory', None

    # 3. Activity/goal describe how work was organised. A terminal activity can
    #    still carry a scoped result or a concrete protocol; otherwise episode.
    if node_type == 'Activity' or kind == 'goal':
        note = _standing_activity(row)
        if note is None:
            return 'episode', 'process_record', None
        return 'canonical', note, None

    # 4. Execution and progress records: a terminal diagnostic with a standing
    #    result is knowledge; a run/progress narration stays episode.
    if node_type == 'Execution' or kind == 'progress':
        note = _record_note(row)
        if note is not None:
            return 'canonical', note, None
        note = _knowledge_note(row, purpose=purpose, operational=operational,
                               lifecycle_veto=lifecycle_veto, durable=durable,
                               terminal=terminal)
        # A progress record can carry a durable technical specification or a
        # pre-registered protocol even when the run itself is only a process
        # record. Those are reusable facts about the system or the experiment,
        # not claims that the run produced a scientific result.
        # A non-terminal progress entry may carry a concrete active decision or
        # scoped conclusion. Diagnostic promotion remains terminal-only because
        # its helper already enforces the completed/active state.
        if note in {'protocol', 'specification', 'deployment_acceptance',
                    'decision_knowledge', 'scoped_conclusion'}:
            return 'canonical', note, None
        if note == 'diagnostic_conclusion' and terminal:
            return 'canonical', note, None
        return 'episode', 'process_record', None

    # 5. Decisions are standing rules. A concrete constraint, protocol or
    #    specification is durable even when the note is phrased as a next step.
    if node_type == 'Decision' or kind == 'decision':
        protocol = _protocol_note(row)
        specification = _specification_note(row)
        if protocol or specification:
            return 'canonical', ('protocol' if protocol else 'specification'), None
        # An explicit control design is a standing rule even when its title is
        # phrased as "next step". The plan veto applies only after the durable
        # protocol/spec channels have had a chance to fire.
        if (_constraint_bearing(text) and _NUMERIC.search(text)
                and re.search(r'(?i)(?:固定初态|固定回合|seed|预算|门槛|判据|'
                              r'gate|threshold|budget|protocol|对照)', text)):
            return 'canonical', 'decision_knowledge', None
        # A live/query narration is not a decision unless it states a standing
        # prohibition/invariant; "not implemented" alone is status, while
        # "must not substitute" remains a durable rule.
        if lifecycle_veto or _RUN_MARKER.search(title):
            if (_constraint_bearing(text) and _STANDING_SCOPE.search(text)
                    and not purpose == 'plan'):
                return 'canonical', 'decision_knowledge', None
            return 'episode', 'decision_status', None
        if _constraint_bearing(text) and (_relation(text) or _NUMERIC.search(text)
                                          or _SPECIFICATION.search(text)):
            return 'canonical', 'decision_knowledge', None
        if purpose == 'plan' or (purpose == 'receipt' and not _relation(text)
                                  and not _NUMERIC.search(text)):
            return 'episode', 'plan_only', None
        return 'canonical', 'decision_knowledge', None

    # 6. Artifact lifecycle decides the default view. Transfer wording in the
    #    body cannot demote a report/model/dataset; a progress or monitor file
    #    cannot be promoted by a hash alone.
    if node_type in {'Artifact', 'Source'}:
        # A final delivery bundle or an explicit audit report is a stable object
        # even when its generated directory name contains "snapshot" or its file
        # name starts with "progress_". The delivery/audit identity is stronger
        # than those generic generation markers.
        final_object = bool(_FINAL_DELIVERY.search(title + '\n' + text))
        input_snapshot = bool(node_type == 'Source' and _INPUT_SNAPSHOT.search(title))
        if _EPHEMERAL_ARTIFACT.search(title) and not final_object and not input_snapshot:
            return 'episode', 'ephemeral_artifact', None
        if _FAILURE_RECORD.search(text) and _BARE_FILE.search(title.split()[0] if title else ''):
            return 'episode', 'unstable_identity', None
        durable_transfer = bool(
            _FINAL_DELIVERY.search(title + '\n' + text)
            or re.search(r'(?i)\.md\b', title)
            or re.search(r'(?i)(?:证据(?:包|压缩包)|审计(?:包|压缩包)|evidence[_ -]?bundle)', title)
            or (re.search(r'(?i)\.(?:zip|tar(?:\.gz|\.xz)?)\b', title)
                and re.search(r'(?i)(?:含|包含|includes?|manifest|审计|descriptive)', text))
        )
        # Stable model/checkpoint files retain object identity even if the body
        # records how they were transferred. A transferred source script may be
        # stable too, but a bare file title with only transfer metadata and no
        # content description is a receipt, so require an explicit content mark
        # for the transfer exception.
        checkpoint_object = bool(re.search(
            r'(?i)(?:\.(?:onnx|pt|pth|ckpt|safetensors)\b|检查点|checkpoint|模型权重)',
            title))
        if (_BARE_FILE.search(title) and _TRANSFER_ACTION.search(text)
                and not durable_transfer
                and not checkpoint_object):
            return 'episode', 'transfer_receipt', None
        if (purpose == 'receipt' and not _CONTENT_MARK.search(title)
                and not checkpoint_object):
            return 'episode', 'transfer_receipt', None
        if (node_type != 'Source' and _TRANSFER_ACTION.search(title)
                and not _CONTENT_MARK.search(title)):
            return 'episode', 'transfer_receipt', None
        # A Source is an input/source object by definition. Its lifecycle words
        # (for example an "initial state snapshot") describe provenance, not an
        # ephemeral generated status file. A stable archive/script name likewise
        # keeps identity even when the body records an upload.
        durable_object = (node_type == 'Source'
                          or bool(_STABLE_ARTIFACT.search(title))
                          or bool(_FINAL_DELIVERY.search(title + '\n' + text)))
        if not durable_object:
            if retention == 'knowledge' and row.get('linked') and purpose is None:
                return 'canonical', 'knowledge_linked', None
            return 'episode', 'unstable_identity', None
        if purpose == 'plan' and not terminal and not _constraint_bearing(text):
            return 'episode', 'plan_only', None
        if _identity_strong(row, identity, node_type) or retention == 'knowledge' or terminal:
            return 'canonical', 'stable_artifact', None
        if not _LIVE.search(text) or _FINAL_DELIVERY.search(text):
            return 'canonical', 'stable_artifact', None
        return 'episode', 'unstable_identity', None

    # 7. Claims/findings.
    if node_type in {'Claim', 'Decision'} or kind in {'finding', 'progress'}:
        if row.get('source_payload_exact') is False:
            return 'episode', 'source_payload_not_current', None
        if re.search(r'(?i)(?:\battempt\s+failed\b|extract\s+attempt\s+failed|'
                     r'failed\s+at\s+extract\s+stage|provider_evidence_reference)', text):
            return 'episode', 'process_finding', None
        evidence_bug = bool(re.search(r'(?i)(?:仍可(?:读取|访问)|可读取|不存在但|hidden\s*=\s*0|'
                                      r'不检查[^\n。；]{0,12}删除标记)', text)
                             and _NUMERIC.search(text) and _relation(text))
        held_out = bool(
            _HELD_OUT_RESULT.search(title + '\n' + text)
            and _NUMERIC.search(text)
            and (_RESULT_STATEMENT.search(text) or _CONCLUSION_MARK.search(text))
        )
        if (lifecycle_veto and not held_out
                and not (_constraint_bearing(text)
                         and _STANDING_SCOPE.search(text)) and not evidence_bug):
            return 'episode', 'process_finding', None
        if _PROCESS_ONLY.search(title) and not _constraint_bearing(text):
            if lifecycle_veto or operational:
                return 'episode', 'process_finding', None
        # A run id / state field / not-run marker is an attempt, not a finding.
        if _RUN_MARKER.search(title) and not _STANDING_SCOPE.search(text):
            return 'episode', 'process_finding', None
        if purpose == 'plan' and not terminal and not _constraint_bearing(text):
            return 'episode', 'plan_only', None
        if re.search(r'(?i)(?:apply_patch|补丁|diff\s+(?:校验|应用)|patch)', title):
            return 'episode', 'process_finding', None
        if _STATE_ASSERTION.search(title) and not durable and not _STANDING_SCOPE.search(text):
            return 'episode', 'state_assertion', None
        # A result on a named held-out split is a reusable evaluation outcome,
        # not a generic "N/N passed" run record. The held-out wording must be in
        # the title and the body must still contain a quantified outcome.
        held_out_result = bool(
            re.search(r'(?i)(?:留出|holdout|held[-\s]?out|独立测试|盲测)', title)
            and _NUMERIC.search(text)
            and _RESULT_STATEMENT.search(text))
        if _live_producer_only(row, text, title, held_out=held_out_result):
            return 'episode', 'live_producer_observation', None
        if (_SINGLE_RUN_RESULT.search(title) and not _STANDING_SCOPE.search(text)
                and not _CONCLUSION_MARK.search(text) and not held_out_result):
            return 'episode', 'single_run_result', None

        atom = _atomic(payload)
        if atom is not None:
            literals = [atom['subject'], atom['predicate'], *atom['conditions']]
            if atom['unit'] not in {'', 'none'}:
                literals.append(atom['unit'])
            if not all(_literal(row.get('evidence_text') or '', value) for value in literals):
                row['assertion_unusable'] = True
            else:
                subject = atom['subject'].strip().casefold()
                if subject in {'这次', '此次', '本次', '旧版', '新版', '之前', '现在', '它', '这个',
                               '那个', 'this', 'it', 'previous', 'current', 'old', 'new',
                               'this run', 'previous run'}:
                    return 'episode', 'atomic_scope_unresolved', None
                if (_STATE_ASSERTION.search(title) and not _STANDING_SCOPE.search(text)
                        and not _CONCLUSION_MARK.search(text)):
                    return 'episode', 'state_assertion', None
                if not (_SINGLE_OBSERVATION.search(text)
                        and not _STANDING_SCOPE.search(text)
                        and not _CONCLUSION_MARK.search(text)
                        and not re.search(r'(?i)(?:留出|验证集|开发集|独立评估|预注册)', text)):
                    hint = json.dumps(['atomic', node_type, atom['subject'], atom['predicate'],
                                       atom['unit'], sorted(atom['conditions']), atom['value']],
                                      ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                    return 'canonical', 'atomic_assertion', 'ak_' + digest(hint)[:40]

        note = _knowledge_note(row, purpose=purpose, operational=operational,
                               lifecycle_veto=lifecycle_veto, durable=durable,
                               terminal=terminal)
        if note is not None:
            return 'canonical', note, None
        if atom is not None and row.get('assertion_unusable'):
            return 'episode', 'atomic_assertion_unanchored', None
        return 'episode', 'atomic_assertion_missing', None

    if retention == 'knowledge' and row.get('linked') and purpose is None:
        return 'canonical', 'knowledge_linked', None
    return 'episode', 'process_record', None


def classify(row):
    """Classify one row and attach a deterministic grouping hint if available.

    Tier and reason come from the textual/atomic contract above. When that
    contract yields no hint, a literal experiment-scoped conclusion identity can
    still group the same finding across chats without changing its tier.
    """
    tier, reason, hint = _classify(row)
    # canonical_id only describes a representative inside the knowledge view.
    # An episode/record row keeps its empty hint even if it carries a derived
    # conclusion identity, so the projection cannot imply a canonical group.
    if hint is None and tier == 'canonical':
        hint = conclusion_hint(_json(row.get('identity_json'), []), row.get('type') or '')
    return tier, reason, hint


def _rows(store, *, limit=None, offset=0, after='', chat='', ids=None):
    """Bounded rows over every project work item; graph evidence is optional.

    Historical work memory can predate the object graph. It still belongs in the
    projection: without an sg2 node it has no durable graph identity and is
    explicitly classified as an episode record, never silently dropped.
    """
    args = [store.config['project_id']]
    where = ['m.project_id=?']
    if chat:
        where.append('m.chat_id=?')
        args.append(chat)
    if after:
        where.append('m.id>?')
        args.append(after)
    if ids is not None:
        selected = list(dict.fromkeys(ids))
        if not selected:
            return
        where.append('m.id IN (' + ','.join('?' for _ in selected) + ')')
        args.extend(selected)
    graph = bool(store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sg2_nodes'").fetchone())
    evidence_table = bool(store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sg2_evidence'").fetchone())
    relation_table = bool(store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sg2_relations'").fetchone())
    node_type = "coalesce(n.type,'LegacyMemory')" if graph else "'LegacyMemory'"
    identity = "coalesce(n.identity_json,'[]')" if graph else "'[]'"
    join = 'LEFT JOIN sg2_nodes n ON n.id=m.id' if graph else ''
    sql = f'''SELECT m.id,{node_type} type,{identity} identity_json,m.paths_json,
        m.chat_id,m.kind,m.title,m.content,m.status,m.source_entry_id,m.source_quote
        FROM work_memory m {join}
        WHERE {' AND '.join(where)} ORDER BY m.id'''
    if limit is not None:
        args.extend([limit, offset])
        sql += ' LIMIT ? OFFSET ?'
    rows = [dict(row) for row in store.db.execute(sql, args)]
    if not rows:
        return
    node_ids = [row['id'] for row in rows]
    linked = set()
    if relation_table:
        for start in range(0, len(node_ids), 400):
            chunk = node_ids[start:start + 400]
            marks = ','.join('?' for _ in chunk)
            linked.update(r[0] for r in store.db.execute(
                f'SELECT DISTINCT source FROM sg2_relations WHERE project=? AND source IN ({marks})',
                (store.config['project_id'], *chunk)))
            linked.update(r[0] for r in store.db.execute(
                f'SELECT DISTINCT target FROM sg2_relations WHERE project=? AND target IN ({marks})',
                (store.config['project_id'], *chunk)))
    evidence = {}
    if evidence_table:
        for start in range(0, len(node_ids), 400):
            chunk = node_ids[start:start + 400]
            marks = ','.join('?' for _ in chunk)
            for row in store.db.execute(
                f'''SELECT node,entry_id,quote,assertion_json FROM sg2_evidence
                   WHERE node IN ({marks}) ORDER BY node,id''', chunk):
                evidence.setdefault(row['node'], []).append(dict(row))
    # Only an atomic Claim/Decision needs anchoring text. Load it lazily with a
    # small cache instead of retaining every cited source in memory.
    #
    # Two different facts are reported separately and must never be conflated:
    #   * ``quote_anchored``  - the atomic literals occur in an exact evidence
    #     quote attributed to this node's evidence rows.
    #   * ``full_source_recoverable`` - the complete original entry text was
    #     read back from a live source. A quote is not the full original.
    text_cache = {}
    for item in rows:
        values = evidence.get(item['id'], [])
        exact = [e for e in values
                 if e['entry_id'] == item['source_entry_id']
                 and e['quote'] == item['source_quote']]
        chosen = exact[0] if exact else (values[0] if values else None)
        item['entry_id'] = chosen['entry_id'] if chosen else None
        item['quote'] = chosen['quote'] if chosen else ''
        item['assertion_json'] = chosen['assertion_json'] if chosen else None
        item['source_payload_exact'] = bool(exact)
        item['linked'] = item['id'] in linked
        payload = _json(item['assertion_json'], {})
        item['evidence_text'] = ''
        item['full_source_recoverable'] = False
        item['anchor_scope'] = 'none'
        if item['type'] in {'Claim', 'Decision'} and _atomic(payload) is not None:
            entry = item['entry_id']
            if entry not in text_cache:
                if len(text_cache) >= 512:
                    text_cache.clear()
                source = store.db.execute(
                    'SELECT sg_source_text(id,text) FROM entries WHERE id=?', (entry,)).fetchone()
                text_cache[entry] = source[0] if source else ''
            full = text_cache[entry]
            item['full_source_recoverable'] = bool(full)
            # An atomic assertion may be anchored either in the recoverable
            # original text or in the node's own cited evidence quotes (which
            # often carry the machine output the assertion was read from).  Both
            # are real citations; only ``full_source_recoverable`` claims the
            # complete original, and it stays a separate field.
            quotes = [q for q in [item['quote'], *(e['quote'] for e in values)] if q]
            item['evidence_text'] = '\n'.join([full or '', *quotes]).strip()
            item['anchor_scope'] = ('full_source' if full else
                                    ('quote_only' if quotes else 'none'))
        yield item

def summary(store):
    """Small read-only counts and honest revision coverage for the UI.

    Revision is a property of each stored tier row, not a single global flag:
    a bounded backfill legitimately mixes old and new rule generations. A
    project is current only when every work item has a row and every row uses
    the active rule version. The UI must not label a partial backfill as v3.
    """
    if not available(store):
        return dict(available=False, revision='', current=False, canonical=0,
                    episode=0, record_only=0, covered=0, total=0, complete=False,
                    revisions={})
    project = store.config['project_id']
    counts = {row[0]: row[1] for row in store.db.execute(
        'SELECT tier,count(*) FROM sg_node_tiers WHERE project=? GROUP BY tier', (project,))}
    revisions = {row[0] or '': row[1] for row in store.db.execute(
        'SELECT revision,count(*) FROM sg_node_tiers WHERE project=? GROUP BY revision', (project,))}
    covered = sum(revisions.values())
    total = store.db.execute('SELECT count(*) FROM work_memory WHERE project_id=?', (project,)).fetchone()[0]
    complete = covered == total
    current = complete and (total == 0 or revisions == {VERSION: total})
    if current:
        revision = VERSION
    elif len(revisions) == 1:
        revision = next(iter(revisions))
    elif revisions:
        revision = 'mixed'
    else:
        revision = ''
    return dict(available=True, revision=revision, current=current,
                canonical=counts.get('canonical', 0), episode=counts.get('episode', 0),
                record_only=counts.get('record_only', 0),
                covered=covered, total=total, complete=complete, revisions=revisions)


def audit(store, *, limit=None, max_samples=20, after='', chat=''):
    """Bounded, model-free preview. Always reports applied=false."""
    if type(limit) not in {int, type(None)} or (limit is not None and not 1 <= limit <= 100000):
        raise ValueError('limit must be between 1 and 100000')
    if type(max_samples) is not int or not 0 <= max_samples <= 100:
        raise ValueError('max_samples must be between 0 and 100')
    if not isinstance(after, str) or len(after) > 256 or not isinstance(chat, str) or len(chat) > 256:
        raise ValueError('invalid audit scope')
    counts = Counter()
    types = Counter()
    reasons = Counter()
    identity_empty = 0
    strong_identity = 0
    conclusion_identity = 0
    paths_present = 0
    atomic_complete = 0
    hints = defaultdict(list)
    conflicts = defaultdict(set)
    anchor_scope = Counter()
    canonical_anchor_scope = Counter()
    full_source_recoverable = Counter()
    samples = []
    total = 0
    last_id = ''
    for row in _rows(store, limit=limit, after=after, chat=chat):
        total += 1
        last_id = row['id']
        tier, reason, hint = classify(row)
        counts[tier] += 1
        types[row['type']] += 1
        reasons[reason] += 1
        identity = _json(row.get('identity_json'), [])
        if not identity:
            identity_empty += 1
        if any(isinstance(i, dict) and i.get('namespace') == 'claim_conclusion' for i in identity):
            conclusion_identity += 1
        if _json(row.get('paths_json'), []):
            paths_present += 1
        raw = _json(row.get('assertion_json'), {})
        atom = _atomic(raw)
        if atom is not None:
            atomic_complete += 1
            scope_name = row.get('anchor_scope') or 'none'
            anchor_scope[scope_name] += 1
            if tier == 'canonical':
                canonical_anchor_scope[scope_name] += 1
            full_source_recoverable[str(bool(row.get('full_source_recoverable'))).lower()] += 1
            key = json.dumps(['lane', row['type'], atom['subject'].strip().casefold(),
                              atom['predicate'].strip().casefold(), atom['unit'],
                              sorted(atom['conditions'])], ensure_ascii=False,
                             sort_keys=True, separators=(',', ':'))
            conflicts[key].add(atom['value'])
        if any(isinstance(i, dict) and i.get('namespace') in STRONG.get(row['type'], set())
               and i.get('value') and i.get('scope') for i in identity):
            strong_identity += 1
        if hint:
            hints[hint].append(row['id'])
        if len(samples) < max_samples:
            samples.append({
                'node': row['id'], 'type': row['type'], 'chat': row['chat_id'],
                'title': row['title'][:160], 'tier': tier, 'reason': reason,
                'identity': identity[:4], 'assertion': atom,
                'linked': bool(row['linked']),
                'anchor_scope': row.get('anchor_scope'),
                'full_source_recoverable': bool(row.get('full_source_recoverable')),
            })
    duplicate_groups = {key: ids for key, ids in hints.items() if len(ids) > 1}
    conflict_groups = {key: sorted(values, key=str) for key, values in conflicts.items()
                       if len(values) > 1}
    canonical = counts['canonical']
    return {
        'version': VERSION,
        'project_id': store.config['project_id'],
        'applied': False,
        'readonly': True,
        'scope': {'after': after, 'chat': chat, 'limit': limit},
        'scanned': total,
        'next_after': last_id,
        'tiers': {tier: counts[tier] for tier in TIERS},
        'types': dict(types),
        'reasons': dict(reasons),
        'identity_empty': identity_empty,
        'strong_identity': strong_identity,
        'conclusion_identity': conclusion_identity,
        'paths_present': paths_present,
        'atomic_complete': atomic_complete,
        # Quote anchoring and full-source recovery are distinct facts. A
        # quote_only assertion is still anchored, but is not proof that the
        # complete original entry remains readable.
        'anchor_scope': dict(anchor_scope),
        'canonical_anchor_scope': dict(canonical_anchor_scope),
        'full_source_recoverable': dict(full_source_recoverable),
        'exact_identity_groups': len(duplicate_groups),
        'exact_identity_duplicate_nodes': sum(len(ids) for ids in duplicate_groups.values()),
        'merge_savings_upper_bound': sum(len(ids) - 1 for ids in duplicate_groups.values()),
        'conflict_groups': len(conflict_groups),
        'canonical_ratio': round(canonical / total, 6) if total else 0.0,
        'tiering_available': available(store),
        'samples': samples,
        'model_calls': 0,
    }


def run(store, *, apply=False, limit=None, max_samples=20, after='', chat=''):
    """Preview or materialize mapping. Apply never edits graph facts or evidence."""
    if not apply:
        return audit(store, limit=limit, max_samples=max_samples, after=after, chat=chat)
    if store.readonly:
        raise ValueError('tiering apply requires a writable store')
    setup(store)
    from .graph_v2 import now
    project = store.config['project_id']
    written = Counter()
    created = now()
    last_id = ''
    whole_project = limit is None and not after and not chat
    with store.db:
        for row in _rows(store, limit=limit, after=after, chat=chat):
            last_id = row['id']
            tier, reason, hint = classify(row)
            canonical_id = row['id'] if hint else None
            if hint:
                # The exact atomic key (including value) reuses the lowest member.
                # A changed value produces a different key, never an alias.
                prior = store.db.execute(
                    '''SELECT node FROM sg_node_tiers WHERE project=? AND identity_key=?
                       AND node<? ORDER BY node LIMIT 1''',
                    (project, hint, row['id'])).fetchone()
                if prior:
                    canonical_id = prior[0]
            store.db.execute(
                '''INSERT INTO sg_node_tiers(node,project,tier,canonical_id,identity_key,reason,revision,created_at)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(node) DO UPDATE SET
                   tier=excluded.tier,canonical_id=excluded.canonical_id,
                   identity_key=excluded.identity_key,reason=excluded.reason,
                   revision=excluded.revision''',
                (row['id'], project, tier, canonical_id, hint or '', reason, VERSION, created))
            written[tier] += 1
        if whole_project:
            store.db.execute("INSERT OR REPLACE INTO metadata VALUES('tiering_revision',?)", (VERSION,))
    return {
        'version': VERSION,
        'project_id': project,
        'applied': True,
        'tiers': {tier: written[tier] for tier in TIERS},
        'scanned': sum(written.values()),
        'next_after': last_id,
        'model_calls': 0,
        'graph_facts_changed': False,
        'evidence_changed': False,
    }


def project(store, node_ids):
    """Upsert tiers for the nodes published by the current write transaction.

    Caller owns the outer transaction. No graph payload, evidence or relation is
    modified. Missing table means the project has not enabled this projection.
    """
    if not available(store):
        return 0
    from .graph_v2 import now
    project_id = store.config['project_id']
    created = now()
    count = 0
    for row in _rows(store, ids=node_ids):
        tier, reason, hint = classify(row)
        canonical_id = row['id'] if hint else None
        if hint:
            prior = store.db.execute(
                '''SELECT node FROM sg_node_tiers WHERE project=? AND identity_key=?
                   AND node<? ORDER BY node LIMIT 1''',
                (project_id, hint, row['id'])).fetchone()
            if prior:
                canonical_id = prior[0]
        store.db.execute(
            '''INSERT INTO sg_node_tiers(node,project,tier,canonical_id,identity_key,reason,revision,created_at)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(node) DO UPDATE SET
               tier=excluded.tier,canonical_id=excluded.canonical_id,
               identity_key=excluded.identity_key,reason=excluded.reason,
               revision=excluded.revision''',
            (row['id'], project_id, tier, canonical_id, hint or '', reason, VERSION, created))
        count += 1
    return count
