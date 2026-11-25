# Codex workspace Python startup acceptance

Date: 2026-11-25. The initial checks used a disposable marketplace. The final
0.5.6 check used the installed local plugin, as recorded below.

## Local 0.5.6 release check

- Reinstalled `seywo@seywo-local` through the desktop backend. The installed
  launcher found Codex Python 3.12.14 on macOS arm64 without uv.
- A fresh desktop app-server loaded all 19 MCP tools and served project status,
  panel resources, mainline and maintenance data. The verifier compared 83
  installed files with the source and made no model calls.
- All six Hook declarations were enabled but had `trustStatus: modified`.
  Trust was not changed; automatic Compact execution in the production profile
  was not tested. Desktop visual rendering was not tested.
- Full regression: 942 Python tests passed, 5 skipped; 65 UI tests passed.
  The marketplace archive contained 89 files (413273 bytes zipped).

## Implemented contract

- One marketplace payload; POSIX shell and Windows CMD bootstrap entries.
- Bounded discovery of an existing host runtime; manifest, Python identity,
  platform/architecture, version and SQLite FTS5 validation before project access.
- No uv/PATH-Python fallback or interpreter download.
- Hash-locked MCP dependencies installed only into a user-level private cache.
  Preparation uses the host's bundled pip wheel without installing into the host.
- Isolated UTF-8 Python for Hooks and detached maintenance/deletion workers;
  neither needs MCP SDK preparation. Model/storage/retrieval contracts unchanged.
- Serialized preparation, atomic publication, retry after failure and shared leases
  protecting active dependencies during old-cache collection.

## Executed verification

| Verification | Result |
|---|---|
| `python3 -B -m pytest seywo/tests -q` | 939 passed, 2 skipped, 1 third-party dateutil deprecation warning |
| `node --test seywo/ui/panel.test.mjs` | 65 passed |
| Actual host `seywo/bin/seywo-host --doctor` | Python 3.12.14, bundle 26.1123.11515, darwin arm64 |
| Disposable marketplace install through desktop backend | Passed; Codex 0.171.0-alpha.13 |
| First SDK preparation with empty private cache and no uv on PATH | Passed |
| Host MCP connection / registered tools | Connected / 19 |
| Host search, source expansion, mainline, panel resources | Passed |
| Host SessionStart / UserPromptSubmit Hooks | Completed (174ms / 183ms in final run) |
| Offline PreCompact -> detached worker subprocess regression | Awaiting incomplete turn, zero model calls, no SDK cache created |
| Archive entrypoints, lock and exclusion checks | Passed; 87 files, 411567 bytes |
| `git diff --check` | Passed |

Startup regression tests cover missing/incompatible/malformed runtimes, injected
Python environment variables, absent installer, failed-install cleanup and retry,
concurrent cache preparation, corrupt-cache rebuild, active-cache protection and
symlink rejection. Installed payload verification now includes the runtime lock.

Raw local reports (temporary artifacts):

- `/tmp/seywo-full-runtime-tests-final.log`
- `/tmp/seywo-ui-runtime-tests.log`
- `/tmp/seywo-workspace-python-final.json`
- `/tmp/seywo-host-runtime-final-20261125/seywo-plugin-0.5.5.zip`

Reproduce host installation acceptance using the actual desktop backend:

```sh
python3 -B scripts/verify-seywo-plugin.py \
  --host /Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex \
  --workspace-runtime-probe "$HOME/.cache/codex-runtimes/codex-primary-runtime" \
  --report /tmp/seywo-workspace-python-final.json
```

## Limits

### Post-review recheck (2026-11-25)

- Full Python regression: **942 passed, 5 skipped** (three Windows framework-layout cases and two native Windows SQLite cases). UI: **65 passed**.
- macOS arm64: installed marketplace cold SDK startup and 19-tool connection passed after the fixes. See `/tmp/seywo-five-fixes-marketplace.json`.
- A separate disposable project used the real Codex Python, no uv on PATH, no `CODEX_HOME`, and a custom profile through `PLUGIN_DATA`. `mcp-install`, `search mcp` and `search --help` created no SDK cache. Executing exactly the generated TOML command completed the MCP handshake and `seywo_status`; only the selected profile gained a ready cache.
- Regressions cover obsolete-cache cleanup failure/retry and the two entrypoints' distinct tool contracts. The Windows selected-framework launcher and three layout cases are implemented, not native-desktop acceptance on macOS.

- The real host runtime already existed. This does not prove automatic provisioning
  on a fresh machine; the plugin deliberately cannot create a missing host Python.
- Native Windows/Linux Codex desktop runtime discovery is not verified. The CI
  matrix uses fixture runtimes; those are not evidence of a real host bundle layout.
  This matrix has not been run remotely for the current unpushed changes.
- The host acceptance did not call a model, run a user compact, or visually inspect
  the embedded desktop panel. The compact worker regression uses an incomplete
  turn specifically to avoid a model request. UI tests are contracts, not visual QA.
- First SDK setup requires PyPI/network and a compatible host ensurepip pip wheel;
  missing prerequisites produce startup diagnostics, not a different interpreter.
