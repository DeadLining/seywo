# Job Ledger Deletion (2026-10-29)

Scope: close the fruit-fly audit finding without changing extraction, graph
publication, source checks, shared-node reanchoring, model selection or quotas.

Contract: deleting an sg2_jobs parent deletes its recovery snapshots, resource
scopes, requests and grants in the same SQLite transaction. Rollback restores
both parent and children. Other jobs, including dismissed/complete jobs and
their budgets, remain untouched. Project/backend circuits are not job-owned.

Owner: job_lifecycle.py supplies per-table SQL triggers for new schemas and
installs guards on existing writable project databases. SQLite triggers protect
already-open old writers too, independent of their foreign_keys setting. Index
job lookups so lifecycle cleanup never scans every recovery/request per chat.

Historical repair: cleanup-orphan-job-ledgers previews by default; --apply uses
one immediate transaction after validating database ownership. Only absent-parent
ledgers are removed. Missing parent tables preserve data. There is no automatic
global scan, model invocation, budget reset, daemon or new Hook.

Acceptance: reproduce deletion, rollback and old-writer paths; preserve another
chat/shared evidence/live budgets/circuits; verify optional and legacy schemas,
idempotent repair and project isolation on L3090 CPU. Install only after this
gate, then repair this project's historical orphans and verify live ledgers.

Status: implemented, verified on L3090 CPU, installed and applied only to the
fruit-fly project. No claim of absolute zero regression or completed extraction
backlog. Existing dirty worktree preserved; no commit or push requested.

Verification feedback: the consolidated gate exposed an existing concurrent
Store initialization race in source_text.setup (unconditional DROP/CREATE of
sg_source_search_delete). Bounded scope adjustment: serialize inspection and
replacement with BEGIN IMMEDIATE, replace only changed definitions. Search
semantics stay identical and normal opens avoid repeated trigger DDL.

Closure evidence (2026-10-29 15:58 Asia/Shanghai):
- Sixteen new lifecycle/CLI/packaging/migration tests pass. Three concurrent
  initialization scenarios pass ten consecutive rounds (30 repeated cases).
- Final consolidated gate: 1493 pass, one failure, six macOS-only skips. The
  failure was the remote source-manifest fixture missing job_lifecycle.py;
  adding the omitted source file made that one test pass. Combined unique
  coverage: 1494 pass, six skip. Do not add overlapping suites to this count.
- Source, L3090 candidate, installed app and active runtime have identical
  70-file core SHA256 9a42f934df718ebd6c1c9338f29b654d27a13f267a6ccdda813c27776ccc6f74.
- Active runtime: a8b19a2b5eb8324d922b. Installed app signature verifies strictly.
- Preview and apply both selected exactly 50 recoveries, 21 scopes, 104
  requests and zero grants. All four orphan counts are now zero. All four
  cascade triggers are installed in the fruit-fly database.
- Project config, global model config, six Hook registrations and MCP config
  hashes are unchanged. The exhausted job's spending and all eight grants have
  the same before/after fingerprint. No budget was cleared or enlarged.
- Installed native settings/status and project-scoped MCP recall/expand succeed
  with zero model calls. Production checks are operational readbacks, not local
  test-suite runs. Training, simulators, TensorBoard and other projects untouched.
- Existing blocked jobs predate this installation and remain subject to their
  original evidence/budget gates; this repair does not recover them.

Rollback: the installer preserved the prior app at
/Applications/SessionGraph.app.backup-1793260503758 and launcher/runtime manifest
in the 20261029T075503Z-5ac411e0 runtime backup. Restore manifest and launcher
together with the prior app if needed. Cascades use SQLite-only operations and
are compatible with old writers; do not drop them during an app rollback.
Any failed deletion/repair transaction rolls back completely. Successfully
removed orphan snapshots are intentionally not copied into another archive.
No VACUUM or secure-erase claim: this is logical deletion, not physical-media
sanitization. No whole-database copy or recurring scan was introduced.

Machine-readable closure: docs/job-ledger-deletion.json and
output/job-ledger-deletion-20261029.json. Raw gate evidence is in
output/job-ledger-{initial,final,bundle-recheck,new,stress}-20261029.*.
