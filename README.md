# SessionGraph

Independent local desktop app for source-backed project memory. The React graph UI, bounded Rust IPC and Python extraction/MCP runtime are owned here. Gateway is an optional Responses provider, not the application host.

## Development

`npm ci`, `npm test`, `npm run build`, `npm run tauri:dev`.

For browser-only local reading, start `SESSIONGRAPH_DEV_CONFIG=/absolute/project/.sessiongraph/config.json npm run dev`. The opt-in development bridge serves only that project on loopback, with read-only allowlisted operations, bounded process lifetime and no settings writes or model calls. Production bundles contain no HTTP bridge.

`npm run app:build` creates the macOS bundle; `npm run app:install` installs it and switches the existing launcher with a backed-up, atomic registration update. Python 3.11+ with the `mcp` package is required; no dependencies are downloaded at runtime.

Project `.sessiongraph` directories, SQLite files and source evidence are not moved or re-extracted. The existing `~/Library/Application Support/local-ai-gateway/sessiongraph` directory is deliberately retained as the shared compatibility state location, including locks, queues and private credentials. Only the runtime source owner changes. Existing Hook/MCP command paths remain valid. Reconnect existing Codex MCP sessions after installation to load the new runtime.

The model form contains only BASE URL, API KEY and MODEL NAME. BASE URL is the Responses API prefix (for example `https://provider.example/v1`); saving appends `/responses`, and existing complete endpoints remain accepted. An unchanged existing endpoint is preserved exactly so endpoint-bound credentials keep working. API KEY left blank preserves credentials; existing environment-variable configuration remains internal and unchanged. HTTPS is required except for loopback HTTP. The key is stored locally with mode 0600, bound to the exact endpoint; it is never returned to the UI. An explicitly named environment key takes precedence. Legacy Codex token reuse remains limited to the exact matching loopback provider. Reads, graph browsing and migration make no model calls.

Gateway's Work Summary UI, timers, model-generation flow and native commands are retired. Existing generated reports/caches are preserved. Codex can use recall/search/expand to compose summaries from original evidence.

## Rollback

The installer preserves the previous app bundle and registration backups. Restore the previous `runtime.json` and launcher together from the matching runtime `backups` manifest; restore the previous app if needed. Do not delete project databases, queues or credentials. The Gateway source retains no SessionGraph implementation after extraction.
