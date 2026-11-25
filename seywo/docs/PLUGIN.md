# Seywo Codex plugin

Version **0.5.6** packages the local runtime, STDIO MCP server, history Skill and a self-contained MCP App. Codex starts the runtime; the shipped UI requires no separate HTTP service or CDN. The model-facing `seywo_open` response is small; details travel through the app-only `seywo_panel_data` tool.

## Runtime prerequisites

One marketplace plugin uses small POSIX and Windows launchers to locate an **already prepared Codex workspace Python**. No uv, system Python, Node, or standalone Seywo app is required to start it. Seywo does not download an interpreter or modify Codex's Python installation. Node/npm and uv remain development tools only.

Discovery is bounded. An explicit `SEYWO_CODEX_RUNTIME` root takes precedence and fails closed if invalid. Otherwise Windows first reads the UTF-8 `selected-framework` marker in the user's `.cache/codex-runtimes/codex-primary-runtime` for the selected MSIX bundle. Then the launchers check `XDG_CACHE_HOME` and the user's `.cache`; Windows also checks `LOCALAPPDATA`. No drive scans or PATH Python fallback. Under `dependencies/python`, Windows checks `python.exe`, `python/python.exe`, then `bin/python.exe`; POSIX checks `bin/python3` then `bin/python`. The Windows entry uses the OS Windows PowerShell with profiles disabled and passes STDIO through to Python. The manifest must have bundle format 2, matching OS, architecture, Python version (3.11+) and interpreter location; SQLite FTS5 is required. These are observed runtime layouts, not a provisioning API. A missing host runtime must be prepared by Codex; `bin/seywo-host --doctor` validates identity without downloading dependencies.

Only MCP startup prepares dependencies. The shipped universal hash lock pins the SDK and its transitive packages; wheels for the running Python/OS/architecture are installed into `$CODEX_HOME/seywo/runtime/<lock-and-ABI-hash>` (default `~/.codex`). The installer executes the host's bundled pip wheel without installing pip into the host. First preparation needs access to PyPI; subsequent starts reuse validated private dependencies offline. No shared site-packages are imported. Hooks, maintenance and deletion workers use isolated stdlib-only Python and can run before MCP dependency preparation. Each worker receives the exact plugin source path, without inheriting `PYTHONPATH` or `PYTHONHOME`.

Preparation is serialized, staged and atomically published. Failed preparations leave no ready cache and retry on next connection; `dependencies_prepare_failed`, `dependencies_prepare_timeout` and `dependencies_busy` distinguish failure states. The preparation budget is bounded within the MCP startup timeout. Runtime/lock changes select a separate cache; inactive older directories are collected, while running MCP servers retain their dependency leases. No project/chat data is removed by dependency cleanup.

`runtime_platform.py` owns locking, native paths, directory persistence and process lifetime. `win32_native.py` is loaded only on Windows: LockFileEx preserves shared/exclusive and nonblocking leases without writing lock contents; suspended model/catalog processes enter a kill-on-close Job before any child can run. Detached maintenance workers outlive Hooks and own their model process scopes. POSIX retains flock, fsync and process groups. Windows optional Responses credentials are current-user DPAPI blobs bound to their endpoint; Unix retains owner/mode-protected files. Native Windows npm shims resolve to the packaged Codex executable without invoking cmd.exe.

Project-local `mcp-install` and the marketplace use the same bootstrap; the former keeps its project-bound tool set. CLI help and local searches (including the literal query `mcp`) do not prepare the SDK. Cache location follows the same verified Codex-home rules as settings, including when `CODEX_HOME` is filtered. `dependencies_cleanup_deferred` on stderr means an obsolete cache could not be removed; the healthy current server starts and cleanup retries on later startups.

The native CI matrix is `.github/workflows/portable-runtime.yml`. It runs all Python tests (including the host entrypoint with a fixture runtime, offline compact-worker smoke and SDK-cache tests), UI contracts and archive building on all three OSes. Fixtures prove the startup contract on a runner, not the presence or layout of a real Codex desktop runtime. Local acceptance used the actual macOS arm64 Codex Python and desktop backend, an isolated marketplace installation, no uv on PATH and an initially empty SDK cache. Native Windows/Linux desktop runtime discovery remains unverified; a macOS run does not establish that acceptance.

The `.codex-plugin/plugin.json` manifest keeps the Codex loader entrypoint and native Hooks at the plugin root. The loader resolves the MCP `cwd`, shell entrypoint and bundled resources relative to the installed plugin directory. Native Hooks remain subject to host review/trust. The plugin never grants its own trust or installs global Hook fallbacks.

## Default model

The Codex settings include a reasoning-effort selector based on the selected model's advertised capabilities. The default `auto` keeps the low-cost policy; explicit efforts are persisted in the current Codex user home at `seywo/settings.json` and applied identically to probes and compact workers. Selecting `auto` removes that override. An explicit effort requires a successful catalog lookup for the exact target and is rejected before generation if unsupported or unverifiable. Changing the model refreshes the choices and resets an incompatible effort to `auto` with a visible explanation. Catalog failures preserve saved choices for retry.

The runtime first uses `CODEX_APP_RUNTIME_CLI_PATH`, then PATH and known application locations. The default backend uses **app-server JSON-RPC** for the catalog, account state, model probes and compact generation. Each owning MCP or maintenance process reuses one serialized worker for a Codex home/configuration; it is not a shared cross-process daemon. Each generation starts an ephemeral thread and releases it after completion, without a persistent analysis Chat or context carried between requests. Config changes recreate the worker; generation uses a catalog cache of up to 60 seconds, while explicit model refresh bypasses it.

There is no `codex exec` or `codex login status` call. The installed Codex binary is still required to run `app-server --stdio`. The worker uses the global Codex provider/default model plus the user-level Seywo model and reasoning selections shared across all projects; it does not inherit a Chat's temporary model or access the current App Chat's internal model session. It does not read private App OAuth tokens. The official app-server account API owns authorization: when needed, the panel offers browser login and an explicit authorization check. App login alone does not prove that this separate runtime has an account. Account actions and catalog reads make zero model calls.

The same `maintain → compact_pipeline` and domain validator remain authoritative. Rules filter tools and internal instructions; the model receives dialogue and short prior state and returns compact titles, operations and source indexes. The output schema is sent once through `turn/start.outputSchema`, with no schema/output temporary files. Existing semantic repair, retry, publication and source/version idempotency contracts remain in the compact pipeline. The optional Responses backend is unchanged. Oversized input follows existing batching rules.

The worker uses a temporary empty workspace and read-only sandbox, disables Hooks, plugins, MCP, host skills, memories, shell, image/goal tools and search. Hosts may still advertise `request_user_input`, and older hosts also advertise `apply_patch`; any tool invocation is rejected. Byte/queue/deadline bounds apply to writes, messages and reads. A timed-out turn is interrupted and its process tree closed; a broken worker restarts on the next request, with no hidden generation replay. Schema/domain validation and byte/time bounds do not provide a hard `max_output_tokens` guarantee. Errors are fixed safe codes/messages; raw stderr, provider text and credentials are never returned.

The optional custom Responses API backend can be saved/tested from the panel. Model source, model and reasoning selection share one user-level configuration at `CODEX_HOME/seywo/settings.json`; credentials live in `CODEX_HOME/seywo/credentials/`. Both backends can be saved before project initialization. Reading, saving and changing selection make zero model calls; only an explicit probe generates one request. Project configuration and `hook.model` never override this user selection. Existing project selections are not automatically migrated: save once in the new panel after upgrading. Close and reopen model settings to read changes made in another panel.

## Install and bind a project

Add the repository root (containing `.agents/plugins/marketplace.json`) as a local marketplace in Codex, then install **Seywo** from **seywo-local**. Equivalent CLI:

```sh
codex plugin marketplace add /absolute/path/to/repository
codex plugin add seywo@seywo-local
```

Reconnect MCP/start a new chat if tools are stale. An existing independent `seywo` registration must be reconciled before global migration; the acceptance script uses isolated configuration.

The host starts the process in its installation directory. Request metadata identifies the current Chat and project automatically; unambiguous MCP roots are another supported binding. Only if both are unavailable call `seywo_select_project` with the current project's known absolute path. Selection does not write or scan. A process cannot switch to another project; reconnect to change projects. The installation directory cannot be selected.

Verified Git linked worktrees of a primary checkout with a local `.git` directory share that checkout's Seywo project. The Hook, plugin binding and source admission use the same ownership rule; moving a Chat to or from a linked worktree does not reset its mainline. Git repository boundaries and separately initialized nested scopes stay independent. Existing worktree-root indexes from older versions are left intact, not merged or deleted; use the standalone CLI with an explicit `--config` to read one separately. No checkout scanning, Git subprocess, or model call is needed to resolve ownership.

After one global plugin installation, the first `seywo_open` in each bound project automatically initializes project memory. It creates `.seywo/.gitignore`, `.seywo/config.json` and the empty index database, with the bundled project Hook enabled. No “enable Seywo” chat instruction is needed. Later projects initialize only when their own panel is first opened; installation and MCP connection/listing do not initialize projects.

Existing `.seywo/config.json`, project identity, custom files and databases are reused without rewriting them on panel open. A failed first database creation can be retried using the already-created identity and configuration. New configuration and Hook defaults are published together under the initialization lock. Project, state, config and database symlinks are refused, including dangling config links.

Initialization neither imports history nor calls a model, and never searches other projects or scans the disk. If the current project's root cannot be determined, the panel retains the project picker without writing. Errors show the filesystem/validation cause and a Retry action; success reloads the project and exact current Chat. An unimported Chat keeps its existing empty state. `seywo_initialize_project` remains available for explicit setup/diagnostics. Project Hook enablement does not grant native Hook trust; host review/trust still applies.

## Explicit batch setup

Use `seywo init-batch /absolute/project-a /absolute/project-b` (or the bundled `seywo/bin/seywo` launcher) to initialize a list of explicitly named existing directories. `--dry-run` prints the plan without prompting or writing. There is no implicit current-directory default, parent traversal, recursive project discovery or install-time disk scan.

The command prints each project's files to create/modify and directories to create, including `.init.lock` and `storage-migration.lock`. Existing custom files are preserved, so the modification list is normally empty. Atomic-write temporary files and SQLite journals can appear transiently during execution. Type `yes` after reviewing the plan to initialize each ready project and enable its project Hook. Other input, EOF or Ctrl-C at confirmation cancels without project writes.

Any existing `config.json` is skipped without parsing, repairing, enabling or changing that project. Duplicate roots are deduplicated. Roots are revalidated after confirmation; newly present configuration is skipped, and newly required writes outside the reviewed plan require a new preview. The initializer also rechecks config presence under its project lock. Symlink roots/state/config are refused. Errors are reported per project and do not stop later roots. Exit status is 0 for success/all skipped/cancellation, 1 for any project failure (including preview failures), and 2 for invalid command arguments. Batch initialization never imports history or calls a model, and adds no MCP tools.

## Embedded view

The render tool declares `openai/ui.entrypoints: [{type: "thread"}]` with the title **Seywo**, for the native conversation-panel entrypoint. An unbound open returns a renderable project-picker state instead of a tool error; the picker calls the existing project-selection tool. A selected but uninitialized project initializes automatically on open, then loads the current Chat; a failed initialization shows its cause and Retry.

Call `seywo_open` without arguments to open the current Codex Chat by default. Codex supplies `thread_id`/`threadId` in request metadata. The runtime locates the host directory using explicit CODEX_HOME, or the verified Codex PLUGIN_DATA layout when that environment variable is filtered. It reads only that ID's `cwd` from the active local `state_N.sqlite` catalog in read-only mode, validates the project, then matches the identity against already imported human Chats. This does not import or scan any transcript or other project. It never picks the first or latest Chat. A current Chat absent from Seywo displays an explicit empty state. An explicit verified `chat_id` can override the default. MCP roots remain available for hosts without Codex metadata; explicit project selection is a fallback. The local catalog is a version-dependent Codex adapter, not a standard MCP workspace API.

- **对话主线:** the current goal, open questions and next steps, followed by ordered stage titles. Branches are folded. No node-count dashboard, version selector or Compact diary.
- **阶段明细:** fetched only after a stage or branch is selected, paged and pinned to the mainline revision being viewed. History stays available to MCP recall/search.
- **节点来源:** evidence drawer, source excerpts loaded only when expanded, pinned event/snapshot/range, continuation for long excerpts. Source text is rendered as plain text.
- Chat lists load additional pages explicitly. Opening a selected Chat requests its current mainline directly, without fetching the version directory. Missing/unpublished data and recoverable failures have readable states without backend traces.

Compact is a processing trigger. Its execution table stores no duplicate segments/changes payload; at most the latest success and latest failure per Chat are retained. A successful commit clears old failures. Setup trims pre-existing execution rows transactionally when the writer next runs. Source-backed narrative revisions remain compressed deltas for stable MCP IDs, pinned pagination and manual-edit concurrency. Older version/diff read APIs remain compatible, but are not UI entrypoints. Read-only views never run cleanup or models.

The plugin exposes 19 tools: nine history tools, project selection/init, `seywo_open`, app-only `seywo_panel_data`, four app-only model settings tools, app-only `seywo_codex_account`, and app-only `seywo_retry_maintenance`. Legacy `seywo --config … mcp` exposes the nine history tools with strict startup validation. Panel opening may initialize the bound project; subsequent data views are read-only and make zero model calls. The data tools do not schedule extraction or scan history. Codex and optional Responses API model status/testing use the same user-level Seywo configuration, and only an explicit model test makes one model call.

## Build and verify

```sh
npm ci --prefix tools/seywo-ui
npm test --prefix tools/seywo-ui
npm run build --prefix tools/seywo-ui
python3 -m pytest seywo/tests -q
# Optional: real installed app-server + local fake provider, no model/account calls
python3 scripts/verify-seywo-app-server.py
python3 scripts/verify-seywo-plugin.py --report docs/seywo-plugin-ui-verification-20261113.json
```

The built HTML and third-party notices are shipped under `seywo/ui/`; `tools/seywo-ui/node_modules` stays outside the installable source. The UI test suite resolves JSDOM from `tools/seywo-ui/node_modules`; no root Node project is required.

For development only, `python3 tools/seywo-ui/preview.py --project /absolute/project/path` starts a temporary loopback host using the official MCP Apps AppBridge and the actual read-only panel tool. It requires an already initialized project. This preview service is not part of the installed plugin.

## Persistence and delivery boundary

Project data remains under the selected project's `.seywo`; the plugin cache is executable code. Uninstalling should preserve project data. Source-backed memory still requires exact indexed evidence.

The isolated real-client verifier checks actual installation, host MCP connection, native thread-entrypoint metadata, unbound selection and first-open initialization, UI metadata and app-only visibility, resource reading, short open results, unpublished Chat state, published fixture versions, diffs and source retrieval. Browser verification checks the SDK handshake and rendered UI using real project data.

These do not prove desktop menu placement or rendering in a Codex conversation panel. The desktop panel has been observed rendered in the actual client; each updated installation still needs its own binding/rendering check. Plugin-owned Compact Hooks use the user-level Codex model selection or optional Responses API provider saved from the panel. Existing separately configured hooks are outside this plugin's registration and are preserved.


## 0.4.0: complete dialogue and concise current state (2026-11-16)

Native lifecycle metadata seals each turn. A mid-turn Compact imports source but defers semantic publication; the eventual closed turn carries the original question and final answer together. Old cursors inside a turn rehydrate only that turn. Aborted user requests remain recorded. Tool bodies and intermediate commentary never enter Compact extraction.

The same model call returns bounded current indexes (one goal, three questions, three next actions) alongside short changes. Runtime validates indexes against offered history and new items, derives the current overview from identities and preserves all historical items/revisions, source references, manual locks and MCP APIs. Old open items no longer automatically become current todos. Current-only changes also publish a new revision.

History selection reserves the current goal and ranks remaining short items by dialogue relevance, focus and recency within twelve items. Native and Responses backends send the output schema once via structured output. The app-server worker disables discovered Skills catalogs and image tools with process-local overrides; user settings, credentials and normal Chats remain intact. The host may still include a minimal noninteractive user-input tool definition; no claim of zero host overhead is made.

Each bounded latest-success/failure receipt stores model selection/provider and aggregated input/output/cache tokens, without credentials, prompts or duplicated semantic output. A selected Codex model is the explicit request target, not proof of a gateway's private upstream routing. Existing Hooks retain their entrypoint definitions and the history tool surface stays unchanged.

Turn completion also has a small `(chat_id, turn_id, end-coordinate)` progress record, committed with the narrative and physical cursor. This is not a Compact history or transcript copy: it distinguishes an already-published interrupted request from a newly terminal old partial turn. Unterminated JSONL records never seal a turn or advance its semantic boundary. Snapshot adapter revisions retain unchanged per-record body versions, so lifecycle upgrades reuse historical body payloads.


## Maintenance visibility and retry (2026-11-17)

The panel reads `seywo_panel_data(view="maintenance")` for the selected Chat,
including a queued Chat not yet imported when it is the exact host identity.
A compact status row shows queued/running/completed, incomplete, failed, blocked,
or interrupted work. A failed update preserves the published mainline. Messages
use fixed local wording, never raw model errors, paths, credentials or tracebacks.
The public `seywo_status` tool includes a compact maintenance state per paged Chat.
All status reads and polling make zero model calls and never initialize storage.

`seywo_retry_maintenance` is app-only and explicit. It requeues failed durable
boundaries and schedules the existing single-writer maintainer. Repeated clicks
while queued/running do not start more workers. The action returns immediately;
subsequent extraction may call the configured model as usual. It preserves
published versions, manual edits, source references and the semantic cursor.

Each Chat uses one atomically replaced small `.seywo/maintenance/CHAT.json`
receipt, with a safe state/cause, timestamp and last successful completion.
No new Compact history, transcript copy or heartbeat log is stored. Existing
writer locks identify interrupted processes; a queued process that does not
start within 60 seconds becomes retryable. The panel polls active work every
four seconds for at most five minutes, pausing when hidden; it then offers
manual status refresh. Completion refreshes the mainline once.

Failures produce a bounded native Hook `systemMessage` at the next
SessionStart/UserPromptSubmit, once per failure. Immediate queue/launch errors
can report at the Compact boundary. Hook exit remains zero so Codex can continue.
The Hook does not call a model. Waiting for a native turn to close is an expected
incomplete state, not a failure notification or a reason to spend another call.
A `Stop` Hook recaptures the now-complete native transcript and wakes the existing
compact worker only while a compact boundary remains pending/claimed. Ordinary
turns without unfinished compact work do not capture sources or start extraction.
The frozen boundary is preserved; the turn ledger prevents replaying committed
turns. Explicit retry also recaptures the source for this waiting state, instead
of replaying the same incomplete snapshot. Waiting receipts remain retryable and
the panel polls them through publication without showing a success checkmark.
A process that fails before Seywo's Hook runtime starts is outside these receipts;
Codex owns that command-execution error.


## Storage lifecycle (schema 8)

Stable event/snapshot identities and exact source ranges reference shared compressed descriptors. Legacy task request/result documents are content-addressed with checksum-verified delta chains of at most 32 documents; original SQL task reads reconstruct exact bytes. External-content FTS derives tokens from compressed source payloads and retains the original unicode61 tokenizer and phrase positions. Explicit imports and existing indexes retain their tool-search coverage.

Automatic captures freeze dialogue before the background worker starts, so a subsequent Codex rewrite cannot change the input being processed or retried. They keep user/assistant text, turn metadata, and original record hashes and ranges, but omit attachment payloads, tool arguments/results and Compact replacement histories before writing any archive block. The original Codex log is never edited. Projected archive locators are not native rollout paths: evidence ranges marked `byte_space=original_transcript` describe the original bytes, and evidence excerpts read the safe indexed text instead. Explicit raw import APIs still retain their existing behavior.

New indexes are created directly at schema 8. Existing schema 8 indexes open without rewriting; other schemas are refused without modifying the index. The runtime does not migrate storage or make full database backups. `seywo/bin/seywo --config /absolute/project/.seywo/config.json storage verify` performs read-only database and archive validation.

After successful/no-op maintenance and explicit deletion, one nonblocking collector protects database references and pending/claimed/failed boundaries before removing detached captures, payloads, descriptors and unreachable task document chains. Archive GC and VACUUM are thresholded; use `storage gc` for explicit forced reclamation. Active writers defer collection. One small storage receipt replaces previous results; reclamation failure is visible separately from successful narrative publication and is retried after subsequent successful maintenance.

After successful Compact maintenance, the collector detaches projected archives that are no longer needed by the inbox. Indexed dialogue and native coordinates serve evidence reads and append recognition without the capture. Existing archive files that the current index still references are retained.

Session hooks and the Compact worker reconcile confirmed Desktop deletions for the bound project. A local Desktop deletion marker and absence from the backend thread catalog are both required. Archive or SessionEnd alone cannot authorize cleanup. Without a lifecycle event, a deletion is reconciled on the project's next activity; there is no polling daemon or model call. The original transcripts and host databases remain outside Seywo reclamation.


## Upgrading the plugin

Updating Git source alone does not replace an installed plugin cache or reload an existing MCP process. Install the new plugin version, then reconnect existing chats (or restart Codex). The new model settings show that the choice applies to all local projects and chats. `scripts/verify-installed-seywo-plugin.py` compares installed runtime, entrypoint, Skill and UI bytes against the current checkout before testing a fresh host connection; a matching manifest version alone is insufficient.
