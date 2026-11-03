# Gateway SessionGraph

Gateway owns the Python core, MCP, lifecycle Hook and extraction/retry worker in this directory.
The old standalone HTTP server and browser UI have been removed. The native Gateway panel
calls `entrypoint.py native-read` using a bounded, one-shot subprocess. No listener or timer
is created by the panel. Existing project `.sessiongraph` databases/evidence/checkpoints stay
in place; only global registry/template/runtime ownership moves to Gateway Application Support.

## Runtime

Python >=3.11 and `mcp>=1.28,<2` are prerequisites. The explicit installer probes the selected
interpreter and persists its absolute path; request handlers never install dependencies.
Python itself is a shared host runtime, not copied from a development venv. Core/native reads
use the standard library. Packaged application resources contain this package, Hook and
entrypoint, not tests, reports, databases, transcripts, secrets or virtual environments.

`entrypoint.py` dispatches `mcp`, `hook-event`, `update`, `native-read`, `native-settings`, `runtime-status` and
ordinary CLI commands. MCP discovers the current project's config by walking up the client's
working directory; it fails closed instead of falling back to another project. `--config`
is available for clients that do not supply project cwd.

## Installation / migration

After building and installing Gateway, run the bundled `scripts/install_gateway.py --apply`
with `--python`, `--legacy-root` and `--project-config`. It backs up and transactionally changes
only the owned MCP/Hook registrations, copies the project registry and provider template, and
preserves the original enable timestamp. It does not alter Codex trust records. Review the
new Hook command in Codex. Existing chats may retain the old MCP process until restarted.
No automatic history extraction occurs during install.

## Energy and reliability

- Panel idle/hidden: no polling or resident worker; each read ends within 10s, up to four reads,
  output capped at 8MiB; failure and timeout kill/reap the child.
- MCP: one stdio process per connected client; idle waits for input, no database polling.
- Hook: short event process; only Compact events detach an updater. Prompt restore is local.
- Independent projects execute concurrently. Each project keeps one writer and one continuation
  runner; duplicate runners exit. Lock contention uses a kernel wait, not periodic polling.
- Normal updates process the frozen input frontier without call-count, round-count or total-time
  quotas. Each batch checkpoints progress. Legacy `hook.max_model_calls` is ignored.
- The runtime lock is shared during work and exclusive only for safe application replacement;
  it does not serialize projects. Workers keep niceness +10. Catalog/search never invoke models.
- Existing batch splitting, source validation, checkpoints, durable failed windows and bounded
  recovery remain intact. No new daemon or periodic retry service is installed.
- Continuation runners wake on queue writes or actual failure retry deadlines and exit when
  their project's queue is empty. Hung individual requests/workers and repeated lack of progress
  still fail visibly; failed tasks have bounded retries. Gateway need not be open for read/MCP/Hook;
  provider calls require its configured endpoint to be up.
- Indexes and audit logs remain project-owned; never remove them during code cleanup.

Development: `python -m pytest -q`; see Gateway `docs/sessiongraph.md` for acceptance boundaries.

### Opt-in lossless Hook receipt storage

`receipt-maintain` previews terminal Hook receipts without changing files or schema.
With `--apply`, it enables that project's receipt index in the existing database and
archives a bounded batch. It uses the existing SHA256/zlib payload archive, verifies
the exact original UTF-8 bytes, commits, then removes only the redundant hot file.
Active and pending receipts remain hot; malformed or conflicting originals are kept
and reported. No source entries, graph facts, model settings or failure outcomes change.
There are no provider calls, background cleanup timers or automatic VACUUM.

```sh
sessiongraph --config /absolute/project/.sessiongraph/config.json receipt-maintain
sessiongraph --config /absolute/project/.sessiongraph/config.json receipt-maintain --apply --limit 100
sessiongraph --config /absolute/project/.sessiongraph/config.json receipt-read RUN_ID
```

Take a consistent database backup and preserve `hook-state/runs` before first apply.
Use `--after` with the returned filename cursor and repeat explicit bounded batches.
After opt-in, new terminal runs use the same archive; status combines indexed cold
history with hot runs. Projects not explicitly opted in keep file receipts. Task
cleanup hides archived obsolete failure notices without deleting their history;
`receipt-read` can still expand them. Archives are database-owned: back up and restore
them together with the database, not as detached reference files. Old binaries or
scripts reading only `runs/*.json` cannot read cold history, so update all consumers
before opting in. This feature does not archive request-audit files or remove raw events.

`tests/rehearse_run_receipts.py --config CONFIG --snapshot BACKUP` tests only an
already-created isolated SQLite backup. It copies receipt files, hashes protected
source/graph/recovery tables before and after, verifies every receipt, compares status
outputs and reports all files' allocated bytes and read timings. It never writes to
the original project or invokes a provider; the snapshot parent must be a dedicated
rehearsal folder and must not already contain `hook-state/runs`.

### Missing Chat Cleanup (Global Default With Project Overrides)

Set `cleanup_missing_chats` to `true` in `$CODEX_HOME/sessiongraph.json`
(`~/.codex/sessiongraph.json` by default), with global Hooks enabled, to apply
archive-equals-delete cleanup to all existing and newly initialized projects.
Projects inherit the setting dynamically without config rewrites or historical
sweeps. An explicit project `hook.cleanup_missing_chats` boolean overrides the
global default; `false` opts that project out. With neither setting, cleanup stays
off. Disabled projects remain disabled.

A registered `SessionEnd` handler starts a detached, one-shot check
after two seconds. It ignores `reason`; a normal exit with the original source
present does not clean anything. Only an already-indexed chat whose registered
sources are all missing under accessible allowlisted roots is eligible. Unknown
paths, unavailable roots and filesystem errors preserve data. There is no model
call, background scan, polling daemon, or source-file deletion.

On macOS, enabled project Hooks also admit one singleton deletion observer.
It sleeps in `kqueue` and watches Codex desktop state changes, not transcripts
or graph contents. New `codex-writing-block-deleted-thread-v1:` markers cover
unarchive/read/delete, which does not emit `SessionEnd`. A marker is only a
trigger: the backend thread row must also be absent, and all original source
checks still apply. Atomic state replacement is supported. Startup snapshots
old markers without sweeping history. One small last-check receipt is retained
in `hook-state/last-delete-check.json`; no model calls or periodic timers occur.
Global uninstall and local disable both prevent cleanup. The private desktop
marker/state schema is version-specific; unknown formats preserve graph data.
Explicit, individually confirmed omissions can be reconciled with
`sessiongraph observe-deletions --config /absolute/project/.sessiongraph/config.json --chat CHAT_ID`.

Cleanup atomically removes that chat's entries, retained evidence, unsupported
nodes/relations, assertion history and extraction jobs. Shared graph objects are
reanchored to surviving evidence. Ordinary immutable-history guards are restored
within the same transaction, including rollback on failure. A small metadata
marker prevents late workers from reimporting the archived source; restoring a
source at an original path permits fresh ingestion. Projects with cleanup off
retain archive relocation/recovery. Existing backups/audit receipts are not secure
erased, and freed SQLite pages are reused rather than vacuuming on every event.

Hook registration still requires normal Codex trust review. The handler does not
enroll projects or grant itself trust. Set the project option to `false` to stop future
automatic cleanup; this does not restore data already intentionally removed.

### Independent critical evidence retention (opt-in)

`source-retain` previews a bounded batch of source records referenced by published
work memories, node evidence and relation evidence, including superseded history.
It does not extract, merge nodes, remove raw events, replace indexed text or scan
legacy transcript prefixes. Missing byte locators can use an exact legacy raw-event
snapshot; otherwise a missing/changed/unavailable source is an explicit gap.

```sh
sessiongraph --config /absolute/project/.sessiongraph/config.json source-retain
sessiongraph --config /absolute/project/.sessiongraph/config.json source-retain --apply --limit 100
sessiongraph --config /absolute/project/.sessiongraph/config.json source-read ENTRY_ID
```

The additive `sg_retained_sources` table owns one immutable record per entry and
reuses the existing SHA256/zlib payload archive. It preserves the verified record's
exact UTF-8 representation when no sanitization is needed (excluding JSONL line
terminators). Known credential fields/patterns and inline media carriers are
removed under `critical-record-v1`; original and snapshot hashes, both text hashes,
and redaction categories remain distinct. `retained_verified` and
`retained_sanitized` are different guarantees. This is not a universal secret
detector or a binary-media backup. Indexed source text is unchanged, including
existing sensitive content; this feature is not a cleanup of existing secrets.

`--after` is an exclusive entry-ID cursor. Each invocation limits candidate count
and reserves a source/snapshot payload-byte budget, including failed verification
attempts. These are not total physical-I/O limits: index work, SQLite pages, source
text comparison, compression verification and schema/index creation also cost work.
Records exceeding 8 MiB are explicit gaps; a temporarily insufficient batch budget
does not advance past the blocked entry. No model, network, timer or VACUUM is used.

Backfill alone does **not** enable automatic capture. After a compatible-writer
inventory, backup and project canary, explicitly add `--apply --enable-publication`
to require retention in the same transaction as new memory/node/relation publication.
Only that project is affected. SQL-only guards fence old writers attempting to
publish unretained sources; upgrade writers before opting in. A missing/corrupt or
oversized critical source fails publication without retrying models. Historical
gaps remain gaps, even after enabling; unresolved proposals and all noncritical
events are not claimed to be independently retained by this policy.

`--apply --disable-publication` stops capture requirements without deleting snapshots
or performing backfill. Existing snapshots remain expandable via `source-read` and
source-entry expansion, even when external transcripts disappear. Readers never
backfill; corrupt snapshots fail explicitly rather than falling back silently.
Schema-aware backup/restore preserves snapshots and guards together. Old readers
still have unchanged indexed text, but cannot attest retained-record integrity.
No archive garbage collection is provided; references must not be deleted manually.

`tests/rehearse_source_retention.py --config CONFIG --snapshot BACKUP` performs a
finite copy-only backfill, hashes protected tables and metadata before/after,
verifies all snapshots with external file reads disabled and reports retention
gaps plus incremental DB/WAL/sidecar allocated bytes. It never enables publication.

### Hook restoration acknowledgements

Restoration markers are peeked before reading, not consumed. Read/subprocess and
stdout/flush failures keep the opportunity for the next injection-capable event;
an initial SessionStart failure can retry on UserPromptSubmit. Successful empty
reads are distinguished from failures and do not repeat a resume on every prompt.
Acknowledgement happens after local stdout flush and only for the same marker
revision. A concurrent newer PostCompact is retained, with short filesystem-only
locking; no lock covers SQLite reads, subprocesses or models. Legacy timestamp
markers remain readable. This records local output, **not** host/model consumption.
No Hook registrations or event support claims change in this sub-slice.

`restoration.py` is the canonical owner for both Hook markers and background
`auto_update` rearming after newly saved nodes/relations. Both writers use the
same unique-revision format and lock as compare/remove acknowledgement. A
background writer cannot replace the marker between acknowledgement's comparison
and unlink. This source-level guarantee requires all installed writers to use the
new helper; compatibility with old timestamp files does not fence an old binary
that still writes without the lock. Mixed-version deployment remains a release gate.

The existing 6,000-character handoff selects whole items, retains source entry IDs
and reports selected items omitted for budget instead of slicing off a condition
or negation. This does not implement task-aware selection, effective-revision
delivery tracking, or live host-event acceptance; those remain separate gates.

## App-wide provider (2026-10-26)

BASE URL, API KEY and MODEL NAME are shared app settings, not project overrides.
The existing runtime `template.json` owns provider identity. Each worker resolves
it on load; older projects' endpoint/model fields no longer override it. Saving
does not rewrite project configs, graph data or checkpoints. Standalone configs
without an app template retain their previous behavior.

Project timeouts and output budgets remain local. Endpoint-bound keys keep the
existing protected credential store; an empty API KEY preserves saved credentials.
Changing identity drops old single-token route pins in the effective in-memory
config so classifications cannot reuse a previous backend's cached attestation.
Settings work without a selected project. Hook/MCP reinstall preserves the shared
provider. Retrieval and settings do not open model connections.

An open transport circuit yields until its future cooldown, including when stale
batch deadlines or fresh input would otherwise trigger immediate continuation.
Circuit waits do not accrue failure debt or failed-batch counts. A half-open HTTP
400 is a real HTTP response, not a missing transport response: the configuration
error remains visible without repeatedly reopening the connection circuit.

## Native settings (1.0.35)

Gateway → 能力拓展 → 项目记忆 → 设置 exposes a per-project model selector and
independent global Hook/MCP install buttons. Opening settings is read-only. Model saves
change only provider.model, not endpoints, secrets or past extraction. Installation is
explicit, idempotent and backed up; custom registrations are protected, and Hook trust
is never granted automatically. The installed bundle and host Python/MCP prerequisites
are required. No polling, provider calls or history backfill are added.

The model selector uses the same display-name/model and provider labels as ModelTrace;
requests use the selected alias through Gateway, with no second raw-ID catalog.
Install MCP also manages a concise `SESSIONGRAPH_START` / `SESSIONGRAPH_END` section
in `~/.codex/AGENTS.md`, with backup, idempotence and preservation of all other text.
Malformed/custom sections are not silently replaced. Registration and guidance status
are reported separately; AGENTS.md is guidance, not the MCP server registration itself.

`native-settings` also supports explicit `uninstall_hooks` and `uninstall_mcp`, globally
scoped without a project dependency. They preserve data/runtime/trust and unrelated settings,
with backups and rollback. Active jobs and connected MCP processes are not force-killed.
The managed AGENTS guidance is four short project-scoped bullets without chat_id.

### Typed model input boundary (1.0.52)

`content_boundary.py` classifies serialized media blocks before batching and returns
exact source ranges to exclude. Attachment paths, captions, textual results and
unknown text are preserved; binary carriers are not decoded or summarized. Raw
entries and byte/source locators remain unchanged. `selection_revision` records
the selection policy without invalidating the existing completed coverage ledger.
Legacy pending descriptors and related context are reprojected from full source
(including fragments starting inside Base64). Empty media-only batches complete
without provider calls. Only changed projections invalidate stage cache identities.
Both provider adapters reject residual binary before network I/O with
`model_input_contains_binary`; they do not silently retry or truncate evidence.
Already completed jobs are not backfilled, reset or deleted by this upgrade.

关系/身份判断的原文上下文也必须先通过完整原文投影，再裁剪所需范围，不能从原文直接截取以免丢失媒体头部。纯格式示例（如不含载荷的 Base64 URI）属于文本，不能按二进制拒绝。内部 `run_chat` 的 `job_ids` 仅用于精确恢复已存在的 pending 任务，必须配合 `pending_only=True`，校验项目/chat/segment 归属；不创建新批次、不迁移或改写未选择任务。

生成的节点标题/说明/别名可对敏感值作可计数的确定性脱敏，只有包含其他有效文字时才保留；身份键和原文证据不得改写以绕过验证。引号包裹的凭据字段也在输入阶段脱敏。Jev网络响应与自定义判断器共用严格概率验证，错误详情只包含安全的结构原因，不记录原始响应或密钥；格式异常进入现有有界重试，阈值与证据规则不变。未完成任务的关联请求诊断不参与轮换，完成后回到原保留策略。

### Stability boundaries (Gateway 1.0.55)

The provider prompt uses the same n1..n12 node slots as the wire schema. Each
extraction attempt now accounts for its actual DeepSeek invocation inside the
stage receipt. A successful candidate is checkpointed until validation commits
its output, so restart between those operations does not call the model again.
The checkpoint key includes the exact contract (including repair feedback) and
provider provenance. Rejected candidates are evicted; candidates still containing
sensitive values are never checkpointed. Existing completed coverage is unchanged.

Transport diagnostics tolerate malformed protocol shapes and record
`pipeline_revision` for separating deployments. Jev success is marked `validated`
after local validation. Failure attribution only touches a request issued for the
current job, preserving earlier successful receipts during cached recovery.
These changes add no periodic scan, normal scheduling quota, or retrieval-time
provider call. Upstream timeouts and invalid model output remain possible; existing
evidence-preserving splits and stage-specific retries continue to handle them.

The explicit worker `step_budget` applies across the entire invocation, including
all rollout segments. Empty segments consume no step; rule-only completion and
failed-job recovery do. The coordinator immediately dispatches the next runnable
batch; this handoff boundary is not a daily quota or cross-project serialization.


### Storage and retrieval (Gateway 1.0.56)

New imports no longer duplicate complete JSONL events in `raw_events`. Searchable
`entries`, graph evidence, source hashes and byte locators remain. Original files
are still needed to verify original-record bytes; missing files are reported, not
silently treated as verified. Legacy source metadata recovery is hash-checked and
fails closed instead of promoting unknown analysis to final output.

`storage-maintain --apply --vacuum` explicitly verifies each legacy transcript's
indexed prefix and raw records, backfills locators and removes only verified raw
copies. Missing/changed sources retain their snapshots. Take a verified backup
before maintenance and run it at a project worker boundary. Jobs, coverage,
cursors, assertions and unresolved candidates are retained. Stage checkpoints are
retained while a job is unfinished; after completion and after all attempts,
controls and reservations have cleared, replay caches are reclaimed. The stage
attempt ledger and job/part outputs stay available for audit and later resolution.
Term tables migrate losslessly to WITHOUT ROWID; no words or identity keys are
pruned. VACUUM reclaims pages after migration; it does not remove graph facts.

The dedicated `memory_search` FTS index uses SQL-only change tracking on inserts,
updates and deletes, including automatic node merges. Writable Store entry points
flush only dirty items. Read-only searches include unflushed changes, so older MCP
processes can still write and newly saved items are searchable before a flush. Read-only clients never migrate or
call providers. Old databases without this index keep the legacy MCP recall path
until a writable initialization; full-project UI search reports an unavailable
index rather than pretending recent items cover all history.

UI work_map supports query/chat/offset and reports matched count and next_offset.
Only one page is drawn; search covers the entire project. Graph revision is an
incremental change token, not a content hash. HTTP 451 remains a request failure,
not a quota diagnosis: bounded redacted non-JSON error bodies and request IDs are
retained for investigation. This does not establish another project's root cause.

### Terminal lifecycle and relation contracts (Gateway 1.0.57)

Completed jobs automatically release identity/relation replay checkpoints in the
publication receipt transaction. Maintenance also reclaims historical completed
caches; it refuses to run during an active project batch. Failed, blocked, split,
superseded and inconsistent jobs retain their caches. No timers, model calls or
new global scheduling quotas are introduced; freed SQLite pages are reused by
subsequent writes, with physical shrinking reserved for explicit VACUUM.

Unresolved relations bind temporary endpoint IDs to the original object
fingerprints. Split parts reusing n1/n2 cannot substitute unrelated endpoints.
Legacy candidates with missing job outputs are resolved only from unambiguous
matching part receipts; unknown candidates remain pending. Resolved candidates
retain their payload/audit linkage but release their live search terms.

The extraction prompt and relation validator now use one endpoint type matrix.
The allowed relationships and semantic evidence thresholds are unchanged. This
prevents prompt/validator drift; it is not evidence of improved model precision.

### Full ranking and lossless archive (Gateway 1.0.58)

Indexed recall ranks the complete matching set in SQLite using the same token
weights, literal phrase bonus and tie order. It returns only the requested page
for full provenance expansion. No candidate cap is used. Dirty rows from older
MCP writers use the original matcher until indexing; read-only retrieval never
invokes a model or migrates data. Legacy databases retain the Python fallback.

After a job and all its attempts complete, output_json/candidates_json and split
output payloads can become internal archive references. `storage.payload_text`
restores byte-identical JSON; `storage.load_payload` parses it. zlib blobs in
sg_payload_archive are keyed by SHA256, verify length/hash on read, and share
identical content. SQL consumers of these internal job columns must use the
accessor. User-facing node/assertion/evidence tables remain unchanged, including
for existing MCP readers. Missing/corrupt archives fail explicitly. A database
backup contains both references and blobs; no extra daemon or archive file is
needed. Failed jobs keep their original recovery payloads.

Maintenance archives historical completed payloads; future completions do it
transactionally. The archive reduces repeated task payloads, not the number of
historical facts. It does not perform semantic merging of distinct assertions.
Archived payloads referenced by pending relations stay readable. Relation
judgment merges overlapping original-source windows and includes endpoint source
evidence; generated node descriptions are still excluded from evidence. The
same support/mode thresholds apply.

### V2 status and selective recovery (1.0.59)

`status` and MCP `session_status` report the enabled V2 engine's nodes, relations,
job states and scheduler progress. Coverage counts only committed current-input
revision/source-hash receipts. `completed_entries` means fully covered original
entries, including rule exclusions; `semantic_entries` excludes entries containing
only excluded ranges. Partial ranges are reported separately. This is processing
coverage, not semantic accuracy. Legacy mode retains its legacy counters.
`sg_entry_sizes` contains only a derived character count, updated by SQL triggers
compatible with older writers, so coverage reads do not load all transcript bodies.

`recover-blocked` is a model-free preview by default. Use repeated `--job-id` to
narrow it. `--apply` holds the project graph writer lock, records the original
control in `sg2_recoveries`, makes eligible jobs ready, and wakes that project's
installed automatic worker if enabled. The recovery operation itself makes no
model call; resumed workers may call providers. Existing inputs, output caches,
claims, completed stages and historical failures are preserved.

Eligible cases are old `jev_malformed_response` controls explicitly saved with
`retryable=false`, and `jev_http_451` with a newer successful local Jev receipt.
For the latter, supply `--request-id <id>` for a `request-audit/request-<id>.json`
record belonging to this project's job, with HTTP 200, `validated` state, the
current endpoint/model, and timestamp within the last day and after the failure.
A fresh successful Jev judgment also applies this reconciliation automatically;
cached judgments and status reads never trigger it. Each job/error is reopened
only once per recovery policy revision to avoid repeated failure/success loops.
If all work is blocked and no new requests occur, use the explicit preview/apply
entry point with an existing valid receipt; there is no background probe loop.
401/403, exhausted current-policy retries, source-integrity errors and invalid
references are not blanket-requeued or marked successful.

Generated invalid JSON now reports `provider_output_json_invalid` with position
metadata, separate from malformed API envelopes and `provider_output_limit`.
V2 uses one persisted correction with feedback followed by existing adaptive
splitting if needed. Strict schema, source/identity/reference checks still apply;
this does not guarantee every provider output will be valid.

## Selected-model judgments (1.0.61)

Graph V2 extraction, identity resolution and relation evidence checks now share the
project's `provider` configuration selected in Gateway. No Jev service or separate
Jev credential is required. Existing Jev credentials and historical audits remain
untouched for compatibility; production does not construct a Jev client.

Judgments use one compact JSON batch request through the configured protocol,
endpoint and model, with the configured output-token budget. They are model-assessed
confidence scores, not calibrated probabilities. Local evidence and reference
validation, abstention and identity/relation thresholds still apply. Only rounding
within the existing 2% distribution tolerance is normalized, preserving score ratios.
There is no forced single-token or logprobs dependency. Retrieval remains model-free.

Judgment caches include backend revision, protocol, endpoint, model and evidence.
On the write path, unfinished Jev identity/relation failures are made due once,
with the previous control state saved in `sg2_judge_migrations`; completed graph
facts and extraction outputs are retained. This does not mark failures successful.
`judge_calls` is a subset of the legacy `deepseek_calls` total, while `jev_calls`
counts legacy/injected Jev adapters only. Do not add `judge_calls` twice.

### Single-token Shadow Classifier (Not Enabled for Publication)

`single_token_judge.py` is separate from the JSON Judge. It sends **one question
per request**, with `max_tokens=1` (or explicitly selected
`max_completion_tokens=1`) for Chat Completions, or `max_output_tokens=1` for
Responses. It never changes extraction budgets or falls back to another protocol,
JSON Judge, Jev, or a larger output limit. Unsupported routes fail closed.

Failed provider stages yield until the durable retry deadline; new Hook input is
retained without bypassing that deadline. A route must actually disable upstream
reasoning for a one-token classifier, not merely hide reasoning in its response.
Increasing the output cap is not a substitute for honoring that request.
An explicit Gateway empty-output error reporting one consumed output token becomes
`single_token_budget_incompatible`, not a generic transient HTTP502. New Hook
input is retained but cannot revive this incompatibility for the same provider
identity. A changed configured model/endpoint or an explicit manual run can retry;
no classifier budget, model substitution or graph progress is silently changed.

Only an exact legal label is accepted, including when the API reports a length
termination. Empty output, refusal, tool calls, multi-token logprobs, invalid
scores, an ignored output budget, or an unexpected pinned backend are failures.
Complete finite provider logprobs for every label yield a `label_distribution`,
normalized **within that label set**, not a calibrated correctness probability.
Missing scores or ambiguous whitespace-token variants yield `label_only`, with
`scores=null` and explicit coverage. No one-hot probabilities are fabricated.
Without token logprobs, actual single-token tokenization remains unverified;
without output usage, total output-budget compliance remains unverified.

The explicit CLI is a shadow-only rehearsal:

```sh
sessiongraph --config .sessiongraph/config.json judge-shadow manifest.json
sessiongraph --config .sessiongraph/config.json judge-shadow manifest.json --allow-network --call-budget 3
```

Preview uses a read-only connection: no provider credentials, schema changes,
lock files or model calls. The manifest has exactly `project_id`, `run_id`,
`budget`, and `questions`. Each question has `id`, `state` (candidate data),
`question` and `evidence`. Example question and source-reference shapes:

```json
{
  "question": {
    "revision": "support-v1",
    "prompt": "Does the original source support the candidate?",
    "criteria": {"supports": "Explicit support", "refutes": "Explicit refutation", "unknown": "Insufficient evidence"},
    "abstain": "unknown"
  },
  "evidence": [{"entry_id": "e_source", "source_hash": "sha256-of-whole-indexed-text", "start": 0, "end": 100}]
}
```

Evidence ranges use Python character offsets into the exact indexed text. The
source hash covers the whole text's UTF-8 bytes. Source role, time, references,
exact selected text, question revision and label meanings enter request identity.
Known secret-bearing and binary inputs are rejected, not silently rewritten.
Source snapshots are checked again after a response, before checkpointing.
This is not a universal secret detector or semantic-retention guarantee.

`budget` requires `max_requests` (1..32), `max_input_bytes` (1..2097152) and
`max_seconds` (1..300). The serialized request is at most 64 KiB; each source-read
pass is limited to 8 MiB of unique indexed text, not total physical SQLite I/O.
At most 3 requests run per invocation. Admission reservations are persisted before
I/O in the existing DB; yielding, explicit retries and crashes do not reset the
cumulative budget. Request reservations include failures before transmission;
`model_calls` separately counts attempted transports. Time is measured spending
and admission control, **not a hard wall deadline**: socket timeouts alone do not
bound every DNS/proxy/read phase. Reported provider token usage is retained; byte
budgets are not advertised as token budgets or electrical-energy measurements.

`sg_classifier_runs` owns frozen run budgets; `sg_classifier_results` owns compact
per-question receipts and source references, without copying source bodies. Same
run IDs cannot change manifests, routing, sampling or evidence. Completed receipts
replay historically with zero requests; they do not prove the alias still routes
to the same backend. Optional `graph_v2.single_token_judge.expected_model` and
`route_revision` pin observed model identity and an operator-controlled route
revision. There is no automatic cross-run cache, retry worker or idle polling.

This command never publishes nodes, applies supersession, changes hooks, or
selects itself as the default Judge. Old probability thresholds are deliberately
not reused. A successful local test or capability smoke case is not held-out
quality/cost acceptance. `tests/rehearse_classifier_shadow.py` provides a bounded
probe of the selected route on an isolated backup; production migration and
semantic publication remain separately gated.


## Durable Hook Ingress

`hook_ingress.py` admits `Stop`, `PreCompact` and `PostCompact` notifications
to the existing `continuation.py` project/chat queue. The app-wide template now
enables this for enrolled projects by default; explicit project opt-outs remain.
Ingress requires `hook.durable_ingress: true`, `graph_v2.enabled: true` and an
enabled `graph_v2.recovery_policy` (at most three provider calls per dispatch).
The existing global registration includes `Stop`; template changes do not edit
Hook commands or trust. Installed-host delivery is distinct from unit tests.
Ingress never returns a blocking decision or starts a chat.
Reads and `UserPromptSubmit` restoration do not enqueue extraction.

Notifications contain an allowlisted absolute transcript path and a stat
fingerprint, never source bodies. They read no transcript bytes or database
tables. Fingerprints coalesce notifications only; the importer still checks
chat identity, project binding and historical-prefix integrity. A missing source
is queued for explicit import failure classification. No all-history discovery
fallback is introduced. Unchanged Pre/Post notifications coalesce even if the
first dispatch ran between events; PreCompact no longer requires PostCompact to
arrive before opted-in work can start. Changed paths remain separate source
entries, so one segment cannot overwrite another. A checkpoint scales with
distinct segment paths, not turns or status reads; no source-checkpoint GC is
implemented. Completed checkpoints use the legacy non-runnable `dismissed`
state with reason `ingress_checkpoint`, hidden from task reads but not deleted.

Queue writes precede detached launch. Failed launch leaves `wake_pending` for
same-notification retry; successful duplicate notifications do not fork another
dispatcher. Each dispatch claims a unique lease and one source revision under
the short queue lock, then imports/extracts outside that lock. Completion can
acknowledge only that source revision, never newer input or an unattempted
source. Cleanup and explicit retry fence old results; leases are removed from
terminal/queued results, including legacy non-ingress work. A completed old
graph boundary cannot hide or clean pending notifications. Existing frozen
boundaries continue without rescanning once admitted source input is imported.
An import failure with an earlier target drains that target first, then remains
visibly blocked with its pending input; duplicate events do not reset retries.

This is ingress/queue safety, NOT full scheduling acceptance. Without the separate
stage-policy opt-in below, project update/graph locks still span model work. The queue runner remains
kernel-event-driven with no new idle polling, but no measured CPU/energy bound
is claimed. Per-dispatch call limits are not cumulative pipeline budgets;
Stage leases and cumulative job admission have separate opt-ins; total token/cost admission,
live host delivery and project canary/rollback remain outstanding. A process
crash after successful fork but before runner admission may require explicit
write-side scheduler restart; no read-side retry or polling is added. In-flight
old unlocked writers are not fenced by this opt-in. Before rollback, stop and
drain the new runner or explicitly reconcile pending sources; do not delete
accepted notifications or assume a config flag is a full rollback protocol.

## Cumulative Job Admission And Transport Circuits (Opt-In)

`resource_policy.py` is the single admission owner for the shared audited
transport. It is disabled by default and requires both `graph_v2.enabled` and
bounded `graph_v2.recovery_policy`. All six numeric fields are required, with
no inferred production budgets. Example only, not deployment approval:

```json
{
  "graph_v2": {
    "enabled": true,
    "recovery_policy": {"enabled": true},
    "resource_policy": {
      "enabled": true,
      "max_requests": 12,
      "max_input_bytes": 1048576,
      "max_output_tokens": 65536,
      "max_transport_seconds": 180,
      "circuit_failure_threshold": 2,
      "circuit_cooldown_seconds": 120
    }
  }
}
```

The existing DB gains `sg_resource_scopes`, `sg_resource_requests` and
`sg_resource_circuits` only on the explicitly enabled write-side transport path.
No prompt, source body, credentials or provider response is copied into these
tables. Numeric usage and hashed backend identity accompany the audit request ID.
Reads do not migrate, admit requests, call models or launch a worker.

A request must own a committed running extraction/identity/relation attempt for
this project's unfinished job. The entire policy freezes on first admission.
Repairs, recursive splits, retries, restarts and backend switches retain that
job's cumulative requests, serialized request-body bytes, explicit output-token
reservations and admitted/measured transport seconds. Changes to the policy
block the existing scope rather than replenish it. Backend circuit thresholds
and cooldown likewise cannot be changed to silently reset an existing circuit.
This slice provides no automatic quota reset, reset command or rollover.
Accounting starts at the first opted-in admission; pre-opt-in calls are not
backfilled, and old or unaudited writers are not fenced. A production rollout
must inventory legacy/in-flight jobs rather than claim a retroactive cost cap.

Admission uses a short SQLite transaction, closed before network I/O. It charges
before sending; crashes retain the whole reservation. Successful settlement
adjusts only transport seconds to the observed open/body-read duration, excluding
local audit writes on success. Output reservations are never refunded based on
low or missing provider usage. A reported above-ceiling output is additionally
charged and fails closed before publication. Settlement failure prevents a stage
checkpoint while keeping the precharge. Preflight denials create no provider
trace and count as zero model calls; an admitted local failure still consumes a
request reservation. Providers without an explicit output ceiling (audited Jev)
fail closed under this policy. An independent single-token shadow run has its
own existing budget and no graph-stage ownership, so it also fails closed if
this graph policy is enabled; do not enable it merely for shadow evaluation.

Transient transport failures and HTTP 401/403 contribute to the project/backend
circuit; deterministic HTTP 400/451 do not. At threshold, later calls wait until
cooldown without network I/O or consuming job failure retries. At expiry, one
half-open probe is admitted. Request generations fence late responses; a dead
probe process can be replaced. PID reuse is conservatively treated as live and
may require explicit write-side reconciliation. There is no periodic probe or
new wakeup loop; existing durable scheduling owns the recorded retry time.

These are per-job admission limits, NOT project/day/lifetime, input-token,
CPU/electricity or total-provider-cost bounds. Socket timeouts are not hard wall
deadlines (DNS/proxy/trickle reads can exceed the reserved duration); observed
overruns remain charged and stop subsequent admissions. Runtime tables grow
with admitted jobs/requests, capped per job but without ledger GC or whole-project
disk caps. Request-audit retention scans remain; long update/graph locks remain
unless the separate stage-policy opt-in below is also enabled.
Custom test adapters bypassing audited transport do not prove enforcement.
Drain workers and preserve the ledger before any deployment rollback; disabling
the opt-in restores legacy behavior and explicitly removes these protections,
it is not a safe in-flight quota-reset operation. Production enablement, quota
selection, host acceptance and rollback rehearsal remain separately gated.

## Stage Ownership And Short Writer Windows (Opt-In)

`stage_leases.py` is disabled by default. Adding
`graph_v2.stage_policy: {"enabled": true}` requires the complete explicit resource
policy above, including bounded graph recovery. This is a source-level capability,
not production enablement or permission to replace the installed app/Hook.

Each project admits one semantic stage at a time using `stage-execution.lock`.
The existing DB has one mutable `sg2_stage_lease` row per project, recording the
owner, generation, job/part/stage/attempt and diagnostic PID. Historical attempts
remain in their existing table. Acquiring the kernel permit proves the previous
executor released or died; neither TTL expiry nor a PID guess authorizes takeover.
No heartbeat, polling loop, extra queue, DB or source-body copy is added.

The graph writer is released only around shared audited transport, with no open
SQLite transaction. It is reacquired before semantic adapter parsing, checkpoints
and publication (transport diagnostics may inspect the untrusted JSON envelope).
The response must still own the exact live generation/attempt and match the
dependency revision. Existing-source updates/deletes and graph mutations invalidate
it; appending entries/new chat segments does not invalidate a frozen frontier.
Invalidation defers without increasing failure debt, discards derived checkpoints
and output, refreshes offered candidates and keeps the job/cost identity and
already-published nodes. Ownership loss aborts without overwriting successor job,
control or cache state. Old successful checkpoints remain usable after opt-in.

Another chat can import during model I/O. If the semantic permit is occupied it
records a retry wait, with zero calls and no additional failure debt, rather than
interrupting a live attempt. A killed process releases kernel permits; its durable
attempt is marked interrupted on the next stage claim and budget precharges remain.
There is no time-based takeover of a live but hung process; operator intervention
may still be needed. Conservative graph invalidation can spend another request,
which remains bounded by the same cumulative job budget.

The coordinator releases `hook-state/update.lock` only for the bounded child.
Same-chat coordinator/run locks and shared `stage-activity.lock` remain held.
Maintenance/recovery requires exclusive activity ownership, even after a reader's
config disables this policy while an opted-in worker drains. These are execution
and maintenance leases, not parallel model-stage support or removal of every lock.
Two hashed guard files per participating chat and fixed project locks remain;
there is no per-attempt lock-file growth, but lock/lease GC is not implemented.

Reads do not create the lease schema or launch work. Existing default behavior
remains unchanged. Mixed-version/unaudited writers are not fenced by this policy:
drain them before rollout, and drain new workers before rollback or changing
opt-ins. Installed-host delivery, migration/canary/rollback, CPU/energy/total-token
bounds and authority-aware assertion revisions remain separate acceptance gates.
`tests/rehearse_stage_leases.py --output PATH` exercises real adapters, locks and
importer/DB with synthetic provider responses only; its measurements do not prove
production model quality, latency, cost or electricity savings.

## Immutable Assertion Shadow Ledger (Opt-In)

`graph_v2.revision_policy: {"enabled": true}` requires `graph_v2.enabled` and is
disabled by default. It adds no provider call, new graph node, queue or worker.
On actual Claim/Decision publication, `revision_ledger.py` records a stable
version of type/title/content/execution-status, with a reference to the existing
immutable `sg2_evidence.assertion_json` payload. Repetition adds unique evidence
references, not another version or an independent support vote. Title changes
are preserved because conditions may occur in a title. Other node types and the
extraction wire schema remain unchanged.

Four additive tables in the existing DB own revision references, source
fingerprints/attribution, explicit local reviews and review-control deltas. UPDATE/DELETE and
INSERT OR REPLACE cannot overwrite captured history or its referenced evidence
payload, even with SQLite's default recursive-trigger setting. Before updating
an existing node, the publisher adopts only an exact prior evidence payload;
unavailable legacy provenance fails closed rather than inventing a baseline.
Capture, independent critical-record retention and graph writes share the
publication transaction. Retention uses the existing sanitized, hash-deduplicated
archive; no extra copy of source text or statement content is kept in the ledger.
Missing/changed originals or retention errors prevent that publication.

Explicit CLI operations (no network):

```sh
python -m sessiongraph --config /path/to/.sessiongraph/config.json revision-read NODE_ID --limit 20
python -m sessiongraph --config /path/to/.sessiongraph/config.json revision-assess review.json
python -m sessiongraph --config /path/to/.sessiongraph/config.json revision-assess review.json --apply
```

A review manifest has exactly these fields; IDs below are placeholders obtained
from `revision-read`, not invented database IDs:

```json
{
  "version": "assertion-shadow-v1",
  "project_id": "PROJECT_ID",
  "source": "NEW_REVISION_ID",
  "target": "OLD_REVISION_ID",
  "effect": "corrects",
  "facet": "held-out evaluation",
  "conditions": {"split": "unseen"},
  "evidence": "NEW_REVISION_EVIDENCE_ID",
  "authority": "source_report",
  "effective_from": "2026-10-24T00:00:00Z"
}
```

Effects are supports/disputes/corrects/replaces/withdraws. Both versions must
belong to the same project/node/type; cross-node identity is not inferred.
Evidence must belong to the source revision and match current text and source
metadata. Decision revocation requires explicitly reviewed direct_user authority
and a real user-message source. Assistant reports cannot erase user/tool sources,
and proposed statements cannot revoke existing ones. A user role or literal
quote alone does NOT prove the source is an actual directive rather than quoted
material: facet, same-condition and direct-authority judgments remain explicit
local-review inputs, not automatic classification or proof of user approval.
Credential-like review metadata is rejected, not persisted.

The reducer reports validity separately from execution status and separately
for each reviewed facet/condition set. Competing replacements remain disputed;
newer timestamps never select a winner. Support/replayed summaries do not revive
withdrawn versions. Self-replacement and replacement cycles are rejected;
correction of a correction appends a new edge while preserving the original.
Changed text/role/time/call attribution marks affected source effects needs_review
without deleting history. Original source attribution and fingerprints stay
visible. Independent support is not automatically revoked or voted on.

Reads use a consistent SQLite snapshot: 1..100 revisions per page, up to 64
sources per version, 32 effects per version and a 16 MiB source-read budget.
Excess source/byte budgets fail explicitly; excess effects are marked incomplete
and scope outcomes needs_review, never silently treated as complete. `--after`
paginates revisions; effect/source detail pagination is future scope. `--at`
filters effective time using currently recorded reviews, NOT what the system
knew at that historical date. Preview never writes or initializes schema; apply
requires exclusive maintenance ownership and one publication transaction.

**This is a shadow ledger, not a default-reader switch.** Normal MCP, native,
work-summary and restoration readers retain existing behavior, including legacy
timestamp-based compatibility text. Existing SUPERSEDES relations are not silently
promoted into reviews, and model outputs never automatically create a review.
Atomic predicate/value/unit extraction, stable identity acceptance, source/summary
lineage, classifier quality, cross-node corrections,
dependency propagation and effective default readers remain gated. Disabling the
flag stops new captures/reviews but does not remove immutability guards or erase
history. Mixed-version writes and a production migration/rollback still need
draining/compatibility acceptance; a flag toggle is not a full rollback.

`tests/rehearse_revision_ledger.py --output PATH` exercises real publication,
retention and reduction with synthetic canonical identities and explicit fixture
reviews. It measures schema/DB/WAL costs, not real semantic quality, electricity
or whole-project space savings. Production opt-ins remain unchanged.

### Correcting A Mistaken Shadow Review

`revision-control` explicitly retracts, restores or amends an existing review.
It uses the same default-off revision policy, source retention, original-role
hash checks and exclusive maintenance/write-transaction boundary. There is no
model, new graph node, copied text, polling task or automatic review approval.
An older shadow ledger remains readable without migration; controls fail with
`revision_control_ledger_unavailable` until explicit write-side graph setup adds
the table. Normal readers still do not consume these shadow records.

```sh
python -m sessiongraph --config /path/to/.sessiongraph/config.json revision-review-read REVIEW_ID --limit 20
python -m sessiongraph --config /path/to/.sessiongraph/config.json revision-control control.json
python -m sessiongraph --config /path/to/.sessiongraph/config.json revision-control control.json --apply
```

The control manifest has exactly these fields. For the first control, `previous`
is the original review ID. Thereafter use `control.recorded_head` returned by
`revision-review-read`. It includes scheduled future controls, whereas `head`
reports only the control effective at the requested time:

```json
{
  "version": "assertion-review-control-v1",
  "project_id": "PROJECT_ID",
  "review": "REVIEW_ID",
  "previous": "REVIEW_ID_OR_LAST_CONTROL_ID",
  "action": "retract",
  "source": "AUDIT_REVISION_ID",
  "evidence": "AUDIT_REVISION_EVIDENCE_ID",
  "authority": "source_report",
  "effective_from": "2026-10-24T01:00:00Z",
  "replacement": null
}
```

The cited evidence must be valid, belong to the source revision and concern the
same project/node/type as the reviewed assertion. Proposed/reference material
cannot control a review. Decision/direct-user reviews and preceding direct-user
controls require an explicitly reviewed direct-user message. Assistant reports
cannot erase user/tool evidence or override a preceding user/tool control. These
are structural guards, not proof of source meaning or user approval.

`previous` is an explicit compare-and-swap predecessor, not a newest-time winner.
Stale submissions fail. Effective times must be nondecreasing along that chain;
equal times follow the explicit predecessor order. A scheduled future control
cannot be silently reordered or backdated by another control. Exact retries,
including equivalent timezone forms, return the existing receipt and never
reactivate an older control. `applied` describes recording/replaying that event,
not whether it currently participates; inspect `control` to see current state.

For `amend`, `replacement` is a complete `assertion-shadow-v1` review manifest.
It must use the same target, source evidence, authority and normalized effective
time as the control. Its facet/conditions/effect may differ. The replacement must
be new and nonidentical to the original. A savepoint atomically appends both the
replacement and original-review retraction, even if the caller catches an error
inside an outer transaction. Replaying the original review cannot undo this.
Restoring the original does not implicitly retract the separate replacement;
both may then participate and remain disputed rather than silently pick a winner.
The conservative destructive-cycle guard still includes all historical review
edges, including retracted ones; reversal of such an edge is not yet supported.

Retraction removes only that review's effect. It never proves the old assertion
true, removes independent support, or changes execution status. Invalid current
control evidence yields `needs_review`, not fallback to a prior active effect.
A fresh explicit control can correct a mistaken or invalid prior control.
Restoration still requires the original review's evidence to validate before its
effect is reviewed. All old events and original attribution remain inspectable.
UPDATE/DELETE and REPLACE cannot mutate controls, including REPLACE through the
alternate `(project, review, previous)` unique key with recursive triggers off.

There are at most 64 controls per review and 4096 scanned control rows per read.
Overflow fails explicitly; there is no silent truncation, rollover, compaction
or GC. Nested review/control reads share the existing 16 MiB source-byte budget
and consistent snapshot, but independent stores never share snapshot caches.
`revision-review-read` details paginate 1..100 immutable actions using `--after`;
details include future events. `--at` selects the effective control using all
currently known events, not historical knowledge-as-of. An original review not
yet effective is marked `not_effective`. Normal `revision-read` keeps its 32-effect
limit and marks incomplete validity `needs_review`, even if every visible effect
is retracted; source/effect detail pagination remains a separate open item.

`tests/rehearse_review_control.py --output PATH` runs real publisher, retention,
SQLite and reducer on synthetic identities/reviews; 100 old-event replay cycles
must preserve three controls, two reviews and one graph node. Production config,
installed application and Hook remain unchanged. Disablement preserves history
and SQL guards; old shadow readers do not understand controls, so compatibility
draining and production migration/rollback still require separate acceptance.

## Unified read projection (Gateway 1.0.66)

`read_projection.py` maintains disposable alias/mention/path indexes and literal
source validity. SQL-only dirty triggers cover older writers and original changes.
`relation_evidence.py` batches relation checks and caches per-evidence validity;
changing one source invalidates its dependent proofs. Both publish inside the
fact transaction; readers on another connection see the old or new complete
snapshot. A legacy dirty/unmigrated database uses deterministic multi-pattern
matching without writes, model calls or implicit migration. No source bodies are
copied into these indexes. Trigram Bloom signatures only select candidates;
original literal boundaries still decide matches. New aliases revisit matching
historical candidates. The index can be rebuilt from existing work memory.

Store initialization on a writable entry point performs the additive migration.
Read-only MCP/UI entry points never migrate. Existing IDs, evidence, original
files and historical failure receipts are retained. No extraction replay is
required. Source-path recovery remains separately validated; missing sources are
not silently marked successful. A continuation with a frozen imported row boundary
uses that indexed boundary without rediscovering historical transcripts.

The native canvas response is explicitly `canvas_preview_v1`: item content is a
180-character preview, while `memory` / `evidence` return the full details. The
underlying `work_map` contract and MCP evidence remain complete. Native reads emit
bounded allowlisted phase markers on stderr and elapsed timings in the response;
no SQL, prompts, credentials or original text is included in telemetry.


### Explicit task cleanup

Gateway → 项目记忆 → 后台任务与诊断 → 清理失效任务 invokes `clear_tasks` for the selected project. It dismisses blocked continuation entries and moves failed/cancelled/stale dead-worker receipts from `hook-state/runs` to `hook-state/cleared-runs`. It never marks work complete or deletes graph facts, source evidence, extraction checkpoints, or provider audit history. Queued/running continuations are retained; graph batches are not cancelled by this action.

Cleanup acquires the project update and graph locks non-blockingly, then the shared queue lock. A dismissed queue entry retains its original reason and input boundary and fences late receipts by their creation time. New events created after dismissal may schedule work normally. No models are called. The archive is an atomic move rather than a second copy; repeated cleanup is idempotent. The UI reports any receipts it could not archive rather than hiding them.

## Effective Atomic Revisions (Opt-In)

The new-input mainline reuses the existing extraction job, stage checkpoints,
evidence owner, revision ledger and read indexes. It does not add a worker or a
queue. Enable it only in an isolated/canary configuration with:

```json
{"graph_v2":{"enabled":true,"revision_policy":{"enabled":true,"mode":"effective"}}}
```

Claim/Decision candidates carry one `assertion` with subject, predicate, value,
unit (`none` for nonnumeric assertions), and exact condition qualifiers. The
object key excludes the value, so a changed value is a version, not a new graph
node. Keys require literal identity/condition anchors. Unresolvable scope stays
unresolved; different seeds/conditions do not merge. Extraction distinguishes
direct evidence from derived summaries, but that semantic distinction is still
model-assessed, not an independently proven source lineage. All cited originals
remain retained; no unique evidence is discarded to meet a node-count target.

Only a changed value in the same atomic scope needs a revision question. Compare
current heads, not every historical pair; the immutable representative question
and existing judgment cache avoid new verdicts for paraphrases or replay. Sources,
roles and local authority rules are checked before applying the effect. A later
timestamp alone cannot revoke a version. Assistant classifiers cannot override a
direct user Decision. Ambiguity is `needs_review`; unresolved contradictory reports
are `disputed`. Semantic classifier output is not proof or human approval.

A small `sg_revision_projection` row contains IDs/state, not copied payloads. It
is rebuilt on publication/review/control writes. `work_memory` is materialized
only when one eligible head remains. Recall, resume, expand, native map metadata
and Hook restoration expose validity; historical summary materials keep the old
payload and separately label its `assertion_state` and `current_validity`. Map and
summary pages batch these metadata reads. Source attribution changes invalidate
the row. A scheduled effect reaching its time marks a stale projection
`needs_review` until a writer rebuilds it; reads never migrate, walk the full
revision ledger, call a model or schedule polling.

`graph_v2.judge_backend="single_token"` explicitly selects the existing adapter
for identity/relation/revision questions. It generates one letter per independent
request, with per-question judgment caching and the existing request budget.
Labels remain labels: missing logprobs are allowed, no one-hot probabilities are
invented, and probability-backend thresholds cannot be bypassed by a response
claiming to be label-only. Multiple label requests may cost more input tokens and
latency than one batched judge; total-cost/held-out acceptance is required before
deployment. The default judge and non-opted-in wire contract are unchanged.

`tests/test_effective_revisions.py` and `tests/rehearse_effective_revisions.py`
exercise importer -> actual graph stages -> publication -> ordinary readers on
synthetic cases. This is not classifier-quality or installed-host acceptance.
Normal Hook processing never starts historical re-extraction/backfill.
Older compound records remain compatible evidence, not silently atomic facts.
Effective-mode extraction selects atomic fields with request-local
`{anchor_id, text}` references. Text must be an exact contiguous substring of an
anchor cited by the same node; units use zero or one reference. The wire assertion
is one object (not an array), or null for other node types; independent facts
require independent nodes. The adapter
restores the existing persisted string schema without synonym/numeric repair or
additional model calls. Bare deictic subjects stay unresolved rather than becoming
project-wide identities. Literal anchoring is not proof of semantic correctness;
the existing authority, identity and revision checks still apply.
Do not roll back a materialized database by merely disabling the flag or using
an old timestamp-winner writer. Drain writers and use a compatible reader/writer
or an explicitly verified database restore/migration; preserve immutable history.

### Explicit Legacy Source Migration

`legacy-migrate --node-id ID` previews a bounded primary-source window without
writes or model calls. `--apply` freezes a manual-only job; adding `--allow-network`
runs the existing graph stages under `--call-budget` (default 3). Repeating the
same selection resumes its existing job and cumulative budget, never creates a
new retry allowance. `--limit` (1..16) and `--after` paginate old Claim/Decision
records. Each frozen window binds original text and attribution hashes.
Only atomic Claim/Decision candidates are offered as migration identities; old
compound assertions and execution summaries are not fed back to extraction.
Unatomic migration output stays unresolved, and publication independently rejects
cached bindings to old compound assertions. Normal live processing is unchanged.
After an extraction-contract upgrade, `--apply --node-id ID --refresh-contract`
explicitly requeues a completed manual replay once for the current contract.
It keeps the same job, immutable evidence, attempts and cumulative resource
ledger; add `--allow-network` to execute. Ordinary completed replays stay free,
and refresh cannot override blocked retry controls or target an implicit batch.

Migration keeps old IDs, payloads and relations. It neither advances live cursors
nor changes input coverage; automatic workers do not execute manual jobs. New
relations must pass normal evidence/type/judge checks rather than copying old
edges. Expanded old-node details expose source replay lineage and its published
objects/relations. `source_reprocessed` means the selected source window passed
through the pipeline, NOT semantic equivalence, complete history migration or
permission to hide the old compound node. Unresolved evidence remains unresolved.
No automatic full-history rebuild or old-node suppression is implemented.

### V2 引用修复与“清理失效任务”（2026-10-27）

- 原子断言字段若把逐字原文指向了相邻锚点，仅在**同一输入条目、该节点已引用的锚点**中唯一定位到相同原文时纠正地址。不开启跨说话人搜索，不扩充证据，不改写或模糊匹配字段；不满足条件仍拒绝，并准确报告 `invalid_atomic_source_ref`，不再统称“未知锚点”。格式错误最多一次持久化反馈重试，不扩大单 token 分类预算。
- “清理失效任务”现在同时处理当前项目的受阻续跑队列、受阻 V2 图谱批次以及失败/中断运行记录。V2 批次明确变为 `dismissed`，不是 `complete`；不计作已完成覆盖，也不会被后续 Hook 重新执行。运行中、待执行、等待重试批次不受影响。
- 清理不删除原文、记忆、图谱事实、失败原因、重试账本或已保存的批次载荷/检查点。保留原输入预留，防止旧批次自动重建，新追加输入仍可处理。`sg2_task_dismissals` 仅保存清理时间和原调度状态；恢复时可按该回执还原 job/control 状态，载荷仍在原处。删除原 chat 时此回执随该 chat 级联删除。
- 这是显式停止旧受阻工作的操作，不是“修复并重试”，也不是清空图谱。安装修复不会自动替用户按下清理按钮。
- 并发补充：已启用阶段租约的项目，清理只取得短时 `graph-v2.lock` 写窗口，不再要求整个自动续跑期间静止；它会跳过活动 attempt 与活动 stage lease，保留正在请求模型的任务和响应。只变更调度/归档状态，不变更图谱依赖版本。磁盘压缩仍要求项目静止；未启用阶段租约的旧路径仍保持原互斥保护。

### 无损压缩的物理回收边界（2026-10-27）

- 载荷压缩继续逐项验证解压与 SHA256，并在同一事务内核对事实/来源计数。结构检查仅覆盖实际涉及的 `sg2_jobs`、`sg2_job_parts`、`sg_payload_archive`、`sg2_checkpoints`；返回 `integrity_scope`，不冒充全库完整性审计，不扫描无关原文/FTS 数据。
- 仅当空闲页达到 **8 MiB 与逻辑库大小 1% 中的较大值**时执行整库 `VACUUM`。更少的页面留作后续写入复用，仍执行载荷压缩、去重与可用 WAL 回收。避免为几十 KB 空闲页重写数 GB 数据。
- 先用 PASSIVE checkpoint，不等待活动读事务；确需重写时，SQLite 进度处理器使用本次维护开始后 240 秒的截止时间。读事务占用或回收被中断时，保留已提交压缩并明确返回延后回收提示；不把磁盘满/数据库损坏等其他错误隐藏成成功。静止锁、事实边界和单 token 分类策略不变。

### 图谱生命周期与旧身份兼容（2026-10-28）

- 独立图谱压缩已退役：删除数量触发模块、历史候选扫描与分类、归并/撤销命令和 App 面板。旧的 graph_preview / graph_assess / graph_compact / graph_undo 请求在 Python 和 Rust 原生契约层均被拒绝，所有原生读取/设置操作保持零模型调用。
- 旧 auto_compaction 配置不再继承或生效；抽取成功后不再调用维护，不改变抽取指纹或重抽旧 chat。图谱初始化只移除旧 sg_auto_compaction_insert 触发器，不再维护增长计数。
- graph_compaction.py 仅保留历史身份的兼容读取与 schema：sg_graph_redirects、已有回执和原始节点/证据/修订不被移除。检索、旧 ID 展开、分页、关系和修订投影沿用统一身份。无新候选扫描、判别、归并或撤销写入。
- 原 chat 确认永久删除后，既有级联流程解除受影响的旧身份映射，再按原证据归属清理和重选来源。其他 chat 的证据仍在则保留对应知识；失去证据的修订效果重新计算。归档不等于删除。
- 存储维护保持显式、零模型、无损，只处理载荷和空闲页。删除后空闲页可供新写入复用，不承诺数据库文件立即缩小；不为此次退役执行 VACUUM。
- docs/graph-compaction.json 与此前凝练证据是历史记录，不代表仍启用独立压缩。当前约定与验收见 docs/condensation-mainline.json。

### Current-source delta vs. historical background

After validation, the original extraction stays in the job receipt. Publication
projects it onto current input: an object or relation whose evidence is entirely
`context_only` is not a new graph event and does not trigger identity/revision
judgments, publication, or routine pending records. A current fact may still cite
old context for scope and conditions. A current relation may reference a uniquely
resolved offered old object without rewriting that object; ambiguous references
remain pending. Atomic lane keys alone do not establish same-value equivalence.
Publication also fences stale checkpoints. This is not historical deletion:
background-only provenance does not prove two existing facts are equivalent.
See `docs/context-delta-fix.json` for the retained write-side behavior.

### Source-Backed Transcript Storage

Every project's next writable Store initialization now enables source-backed
future imports, contentless search writes and lossless terminal receipt archival.
This is a runtime default, independent of project age or copied templates; it
does not require changing each project's config. New empty databases use only
the unified search index. Existing databases keep mixed reads until their legacy
index is explicitly migrated. Read-only MCP/UI access never upgrades a database.
Activation is atomic, idempotent and does not read historical source files or
perform full-history migration inside Hooks.

`source-externalize --apply` migrates a bounded historical batch (default 256
entries / 16 MiB). Preview is
read-only. Continue with the returned numeric `--after` cursor. Missing, changed
or inaccessible originals retain their inline copy. Legacy locators recover from
the nearest known predecessor within the same byte budget; Hooks never migrate
historical data. Entry IDs, knowledge, revisions and source-deletion rules do not
change. No provider is called.

Externalized entries retain only metadata, hashes, sizes and a 32-character
preview. Search uses contentless-delete FTS5 (SQLite >= 3.43); exact text is read
by verified byte location from Codex. Mixed old/new indexes remain searchable.
Unavailable sources are explicit gaps, not deletion signals. Existing critical
retained snapshots remain immutable and usable; sanitized snapshots cannot
substitute for a different exact source hash. This mode is not an independent
chat backup. Keep the Codex transcript store available.

Default activation raises the project schema to v4, so old immutable runtimes fail closed
instead of treating externalized text as empty. Install the new runtime before
migrating, then reconnect existing MCP clients. Search IDs are explicit stable
keys independent of entry rowids, including across VACUUM. Physical migration is not
automatically reversible by an old app; restoring inline bodies requires the
verified originals and a compatible migration, not merely downgrading the flag.

`receipt-maintain --apply --cleared` losslessly archives dismissed terminal Hook
receipts, verifies exact decompression and hashes before unlinking duplicate JSON
files, and keeps them hidden in status. New terminal receipts are automatically
archived after writable Store initialization, without manual opt-in. Active and
pending runs remain hot. No graph facts or original Codex files are removed.
Freed SQLite pages are reusable immediately; shrinking the physical file is a
separate explicit reclamation step, not a per-Hook full-database rewrite.

### Legacy Storage Retirement

`legacy-optimize --phase search --apply` moves a bounded batch of legacy entry
and record search tokens into the same contentless FTS5 index. Stable
`sg_search_keys` map search IDs independently of text storage: unavailable-source
entries keep their inline text and remain searchable. The old `docs` index stays
readable during migration; future writes already use only the new index. Once
empty, its residual postings are rebuilt empty and legacy reads stop. Memory recall already uses its own
canonical index, so obsolete `docs` memory duplicates are not copied. The empty
compatibility table remains for non-migrated projects and legacy cleanup SQL.

`legacy-optimize --phase events --apply` losslessly compresses bounded raw-event
batches into `sg_raw_archive`, preserving session/line keys, exact original bytes,
lengths and hashes. Metadata recovery and evidence snapshots read either format;
permanent-chat deletion removes archived events and unified search keys as well.
Missing source files are never inferred deletions. Corruption fails closed and
a failed batch rolls back; history, graph semantics and provider settings do not
change. Both commands accept `--limit`, `--max-bytes` and numeric `--after` cursors,
have read-only preview defaults, and never call a model. Do not VACUUM between
cursor batches; reclaim physical space once the migration completes. Schema v4
fences old runtimes; install the matching app before explicit project migration.

### 写入侧凝练（2026-10-28）

- `pipeline_settings.py` 从共享 `template.json` 继承图谱、凝练、单 token 判别、有限恢复、有效修订、阶段/资源控制及 durable ingress；新项目模板保留完整可移植选项，已有项目加载时合并，不逐项目改文件。项目明确关闭及预算优先；关闭依赖项不会意外继承失效的下游策略。模型/端点仍由全局 provider 统一决定，不跨项目复制旧模型路由 pin、项目路径或凭据。
- 有效配置 `graph_v2.condensation.enabled=true` 启用新版写入侧规则，全局模板现已默认启用。新响应必须包含 `retention=knowledge|record_only`，在原有结构化抽取中完成分类，不增加每条消息的分类请求。完整输出仍留在 job 回执；过程记录、轮询和重复播报不发布为知识对象。独特数值、条件、失败、决策仍应提取；原文始终可查。未知旧回执不强制全历史重抽。原有 SQLite B-tree/FTS5 索引和增量游标继续使用，不新建正文副本或读时模型。
- 不再只依赖批次 top12 和标题相似：每个入图对象另按原始证据索引、内容词索引召回最多4个相关规范候选。历史自由文本先通过数值/版本/条件和保守内容覆盖门槛，再比较完整描述；门槛只是拒绝条件，不是等价证明。相关候选超限且已检查者均不同，转 pending，不能据此认定“这是新知识”。索引召回本身仍有边界，不能宣称穷尽所有语义近义；改写较大的内容可能保守保留。
- 原子断言 lane 和纠错账本沿用既有规则：不同值、条件、观测/权威或后续推翻，不因标题接近而合并。背景投影与 stale-checkpoint 提交护栏继续生效。
- 单 token 身份判别仅服务新输入的既有入图流程，不再扫描历史节点寻找归并。数值、条件、权威或独立观测不一致时不得合并；不确定项保留待判断。此处的写入侧凝练不等同于已取消的独立图谱压缩。


### 图谱层次投影（tiering-v3）

`tier-graph` 是只读默认的确定性审计，`--apply` 只物化 `sg_node_tiers` 影子映射，不修改 `work_memory`、`sg2_nodes`、证据、关系或修订。它不会删除、合并或压缩节点，也不调用模型。

- `canonical` 是可复用知识层，不是“唯一默认展示节点”或已完成的图谱凝练结果。默认视图与知识层将来仍可分离。
- `canonical` 可收：完整且可靠锚定的原子断言；有终态量化结果和明确适用范围的诊断/审计结论；带具体控制的预注册协议；明确的 schema/接口/配置规格；部署验收；稳定报告、模型、数据集等知识对象。
- `Execution`/`Activity` 仍以过程记录为默认。运行、进度、单次探针、状态播报和传输回执归入 `episode`；只有终态且有 standing scope/结论，或带具体约束的协议，才进入 `canonical`。
- 标题优先表达对象用途。正文中的上传、哈希或测试数字不会单独把稳定知识改成回执；只有空标题或裸文件引用才回退读取正文。
- 单次“8/8 通过”、误差为 0、退出码或 run id 本身不构成知识；失败现场、隔离修复和事故叙事也没有自动提升通道。
- 只有“通过/失败/存活 + N/M”这类**计数结论**才算单次观测。正文里当作指标引用的裸分数（如 `transition 199/200 errors`、`rmse ~0.003–0.017`）不再因此被降级。
- 留出集必须与它自己的计数结果相邻才算评估结论；只提一句“留出集未使用”“仍看留出种子的结果”不提升。
- 正文自述“重建中/重训中/复算中”的 active 记录，在其生产者跑完前是移动中的数字，留在 `episode`；一旦同一条正文已给出结论、边界或约束，则保留在 `canonical`。
- “最大差值为 0”“平均速度仍低于所请求的 0.3”这类对照目标的量化偏差属于诊断结果，可进入 `canonical`。
- `episode`：执行/活动过程、上下文未定、无稳定身份、或早于对象图谱但仍保留的历史事项。用于过程记录层。
- `record_only`：抽取时明确标为仅回执的内容。
- 不同 subject/predicate/value 生成不同 identity key；冲突继续由 revision ledger 表达，不能借 tier 合并。
- 原文锚定分两种事实报告：`anchor_scope=full_source|quote_only|none` 与 `full_source_recoverable`。只有 `full_source` 才表示完整原文可恢复；引用片段存在不等于全文可恢复。
- 时间不确定、原子断言缺失或原文锚定失败时保守归入 `episode`，不伪造 `canonical`。
- 三条写入口都必须在同一事务内投影 tier：图谱发布（`graph_v2` 的 `tiering.project`）、显式记忆写入（`session_remember` / `memory.remember`）、以及全量回填（`tier-graph --apply`）。漏掉任意一条都会让新节点没有映射行，UI 立刻显示“层次尚未全量回填”，且该节点在两个层次视图中都不可见。

CLI：

```bash
sessiongraph --config <project>/.sessiongraph/config.json tier-graph
sessiongraph --config <project>/.sessiongraph/config.json tier-graph --apply --limit 2000
```

`work_map` 的 `view=knowledge|episode|all` 只过滤展示；MCP recall/search/expand 仍检索完整历史。`--apply --limit` 使用 `next_after` 作为下一批 `--after` 游标；回填幂等。Project lock 串行化写入；不要全库连写或 `VACUUM`。
