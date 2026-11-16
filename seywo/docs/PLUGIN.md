# Seywo Codex plugin

Version **0.4.0** packages the existing local runtime, STDIO MCP server, history Skill and a self-contained MCP App. Codex starts the runtime; the shipped UI requires no separate HTTP service or CDN. The model-facing `seywo_open` response is small; details travel through the app-only `seywo_panel_data` tool.

## Runtime prerequisites

Initial platform: macOS/Linux. The bundled launcher uses Python 3.11+ with an available MCP SDK, or `uv run` with `mcp==1.28.1`. With Python but no SDK/uv it provisions a private reusable venv. First provisioning requires network access; it never installs into the system interpreter. A machine with neither Python 3.11+ nor uv must install one. `fcntl` prevents claiming Windows support. Node/npm are build-only dependencies.

The compatible `.codex-plugin/plugin.json` manifest is intentional: Codex 0.152.1 skips hooks from Agent Plugins manifests. The compatible loader resolves MCP `cwd`; the shell entrypoint and all resources are relative to that installed directory. Four native Hooks remain subject to host review/trust. The plugin never grants its own trust or installs global Hook fallbacks.

## Default model

The default backend runs an ephemeral read-only `codex exec`, reusing the existing Codex login/provider and global default model. It does not inherit a Chat-specific temporary model override. It disables Hooks, plugins, MCP, tools, host-skill discovery and memories, uses a temporary empty workspace and deletes intermediate schema/output files. No application bundle or extra key is required. The same `maintain → compact_pipeline` is used for both plugin and standalone. Rules remove tool calls/results; the model receives user/assistant dialogue and short narrative state, and returns compact titles, operations and source indexes. A bounded batch normally makes one call; oversized content follows the existing batching limits. Codex CLI schema constraints are not a hard `max_output_tokens` guarantee.

The optional custom Responses API backend can be saved/tested from the panel. Saving or reading settings makes zero model calls. Legacy standalone settings stay compatible only for configurations without `model_runtime`; plugin initialization selects Codex explicitly. A real two-turn acceptance used one call (5,085 input / 139 output tokens); native Codex has roughly 4k tokens of base context overhead.

## Install and bind a project

Add the repository root (containing `.agents/plugins/marketplace.json`) as a local marketplace in Codex, then install **Seywo** from **seywo-local**. Equivalent CLI:

```sh
codex plugin marketplace add /absolute/path/to/repository
codex plugin add seywo@seywo-local
```

Reconnect MCP/start a new chat if tools are stale. An existing independent `seywo` registration must be reconciled before global migration; the acceptance script uses isolated configuration.

The host starts the process in its installation directory. Request metadata identifies the current Chat and project automatically; unambiguous MCP roots are another supported binding. Only if both are unavailable call `seywo_select_project` with the current project's known absolute path. Selection does not write or scan. A process cannot switch to another project; reconnect to change projects. The installation directory cannot be selected.

Existing `.seywo/config.json` is reused. For a new project, explicitly authorize enabling Seywo, then call `seywo_initialize_project`. It creates `.seywo/.gitignore` before config/database writes, enables the bundled Hook, and neither imports history nor calls a model.

## Embedded view

The render tool declares `openai/ui.entrypoints: [{type: "thread"}]` with the title **Seywo**, for the native conversation-panel entrypoint. An unbound open returns a renderable project-picker state instead of a tool error; the picker calls the existing project-selection tool. A selected but uninitialized project shows instructions without creating data.

Call `seywo_open` without arguments to open the current Codex Chat by default. Codex supplies `thread_id`/`threadId` in request metadata. The runtime locates the host directory using explicit CODEX_HOME, or the verified Codex PLUGIN_DATA layout when that environment variable is filtered. It reads only that ID's `cwd` from the active local `state_N.sqlite` catalog in read-only mode, validates the project, then matches the identity against already imported human Chats. This does not import or scan any transcript or other project. It never picks the first or latest Chat. A current Chat absent from Seywo displays an explicit empty state. An explicit verified `chat_id` can override the default. MCP roots remain available for hosts without Codex metadata; explicit project selection is a fallback. The local catalog is a version-dependent Codex adapter, not a standard MCP workspace API.

- **对话主线:** the current goal, open questions and next steps, followed by ordered stage titles. Branches are folded. No node-count dashboard, version selector or Compact diary.
- **阶段明细:** fetched only after a stage or branch is selected, paged and pinned to the mainline revision being viewed. History stays available to MCP recall/search.
- **节点来源:** evidence drawer, source excerpts loaded only when expanded, pinned event/snapshot/range, continuation for long excerpts. Source text is rendered as plain text.
- Chat lists load additional pages explicitly. Opening a selected Chat requests its current mainline directly, without fetching the version directory. Missing/unpublished data and recoverable failures have readable states without backend traces.

Compact is a processing trigger. Its execution table stores no duplicate segments/changes payload; at most the latest success and latest failure per Chat are retained. A successful commit clears old failures. Setup trims pre-existing execution rows transactionally when the writer next runs. Source-backed narrative revisions remain compressed deltas for stable MCP IDs, pinned pagination and manual-edit concurrency. Older version/diff read APIs remain compatible, but are not UI entrypoints. Read-only views never run cleanup or models.

The plugin exposes 16 tools: the original nine, project selection/init, `seywo_open`, app-only `seywo_panel_data`, and three app-only model settings tools. Legacy `seywo --config … mcp` still exposes the original nine with strict startup validation. All view operations are read-only and make zero model calls. The new data tools do not schedule extraction or scan history. The app-only `seywo_model_status`, `seywo_model_save` and `seywo_model_test` tools manage the project-local provider; only an explicit test makes one model call.

## Build and verify

```sh
npm ci --prefix tools/seywo-ui
npm test --prefix tools/seywo-ui
npm run build --prefix tools/seywo-ui
python3 -m pytest seywo/tests -q
python3 scripts/verify-seywo-plugin.py --report docs/seywo-plugin-ui-verification-20261113.json
```

The built HTML and third-party notices are shipped under `seywo/ui/`; `tools/seywo-ui/node_modules` stays outside the installable source. The UI test suite currently also uses the repository's JSDOM development dependency (`npm ci` at the repository root).

For development only, `python3 tools/seywo-ui/preview.py --project /absolute/project/path` starts a temporary loopback host using the official MCP Apps AppBridge and the actual read-only panel tool. It requires an already initialized project. This preview service is not part of the installed plugin.

## Persistence and delivery boundary

Project data remains under the selected project's `.seywo`; the plugin cache is executable code. Uninstalling should preserve project data. Source-backed memory still requires exact indexed evidence.

The isolated real-client verifier checks actual installation, host MCP connection, native thread-entrypoint metadata, unbound/uninitialized open states, UI metadata and app-only visibility, resource reading, short open results, unpublished Chat state, published fixture versions, diffs and source retrieval. Browser verification checks the SDK handshake and rendered UI using real project data.

These do not prove desktop menu placement or rendering in a Codex conversation panel. The desktop panel has been observed rendered in the actual client; each updated installation still needs its own binding/rendering check. Plugin-owned Compact Hooks use the default Codex model or the optional project-local provider saved from the panel. Existing separately configured hooks are outside this plugin's registration and are preserved.


## 0.4.0: complete dialogue and concise current state (2026-11-16)

Native lifecycle metadata seals each turn. A mid-turn Compact imports source but defers semantic publication; the eventual closed turn carries the original question and final answer together. Old cursors inside a turn rehydrate only that turn. Aborted user requests remain recorded. Tool bodies and intermediate commentary never enter Compact extraction.

The same model call returns bounded current indexes (one goal, three questions, three next actions) alongside short changes. Runtime validates indexes against offered history and new items, derives the current overview from identities and preserves all historical items/revisions, source references, manual locks and MCP APIs. Old open items no longer automatically become current todos. Current-only changes also publish a new revision.

History selection reserves the current goal and ranks remaining short items by dialogue relevance, focus and recency within twelve items. Native and Responses backends send the output schema once via structured output. The Codex subprocess disables discovered Skills catalogs and image tools with process-local overrides; user settings, credentials and normal Chats remain intact. The host may still include a minimal noninteractive user-input tool definition; no claim of zero host overhead is made.

Each bounded latest-success/failure receipt stores model selection/provider and aggregated input/output/cache tokens, without credentials, prompts or duplicated semantic output. A selected Codex model is the explicit request target, not proof of a gateway's private upstream routing. Existing Hooks retain their entrypoint definitions; no new hooks or tools are added.

Turn completion also has a small `(chat_id, turn_id, end-coordinate)` progress record, committed with the narrative and physical cursor. This is not a Compact history or transcript copy: it distinguishes an already-published interrupted request from a newly terminal old partial turn. Unterminated JSONL records never seal a turn or advance its semantic boundary. Snapshot adapter revisions retain unchanged per-record body versions, so lifecycle upgrades reuse historical body payloads.
