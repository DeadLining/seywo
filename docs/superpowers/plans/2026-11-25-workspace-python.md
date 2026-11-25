# Codex workspace Python startup

Scope: remove runtime dependency on uv by reusing a validated Codex-managed
Python. Preserve model, compact, source storage, retrieval and deletion semantics.
No writes to the host interpreter, no interpreter download, no system-Python fallback.

## Acceptance sequence

1. Install a disposable marketplace using the running desktop's Codex backend;
   verify relative MCP command, Hook expansion, tool calls and UI resources with
   uv removed from PATH. Keep actual host runtime but isolate CODEX_HOME/project.
2. Establish a common host-callable bootstrap across POSIX and Windows. Do not
   claim Windows/Linux native acceptance based on POSIX or mocked paths.
3. Bounded runtime discovery, metadata/architecture/version/SQLite validation,
   explicit diagnostics; independent dependency path and worker propagation.
4. Pin dependency artifacts, isolate them outside project data and shared Python;
   validate concurrent preparation, corruption, failure and upgrade behavior.
5. Run targeted and complete tests plus installed-host acceptance. Change public
   manifests only when the bootstrap and dependency distribution are concrete.

## Evidence so far

- Desktop backend 0.171.0-alpha.13 accepts relative MCP executable paths.
- It does not substitute `${PLUGIN_ROOT}` inside MCP `command`; Hook expansion
  is separate and does substitute it.
- Disposable marketplace, existing host Python 3.12.14, private copied MCP
  dependencies: 19 tools, panel resource, search/evidence/narrative read and
  native SessionStart/UserPromptSubmit Hooks passed without uv in PATH.
- This is warm-runtime/isolated-install acceptance, not fresh-machine provisioning.
- Source manifests now select the host bootstrap. Installed production payload remains unchanged.
- Actual macOS desktop installation from an initially empty private SDK cache passed; Hook/maintenance standard-library isolation has dedicated subprocess regression coverage.
