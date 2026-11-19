# Seywo storage lifecycle

The user authorized fundamental remediation of the 742 MiB project store. Preserve the existing dirty app-server work. No new model calls, no commit/push, and no loss of history, exact source ranges, task retries or narrative versions.

## Architecture

1. Schema 8 separates stable source occurrence identities from shared compressed descriptors. A compatibility view exposes exactly the old fragment columns; foreign keys retain physical identities. Existing byte ranges and event/snapshot IDs do not change.
2. Execution task request/result bodies become content-addressed compressed documents with bounded delta chains, not duplicated TEXT. The logical task interface remains byte-exact. Every task state uses this representation; no unfinished task is discarded.
3. FTS keeps all searchable user, assistant and tool content but derives external FTS content from the existing compressed payload, avoiding duplicate stored text. Keep the original unicode61 tokenizer and positional index so identifier phrases and ranking retain their existing semantics; omit only the redundant per-row token-count table.
4. Raw archives remain lossless. Content-defined chunk boundaries allow unchanged internal histories to share blocks after insertions or rewrites. Existing manifest formats and exact logical source hashes remain readable; reblocking preserves source bytes and provenance.
5. One lifecycle collector protects all database-bound and retry-bound captures, prunes unbound terminal captures, then collects orphan payloads/documents/blocks. Successful compact/no-op, explicit deletion and admin maintenance use the same ownership rules. A collection failure is reported separately from an already committed publication. Expensive reclamation is conditional, never every-compact full VACUUM.
6. Storage migration remains exclusive, backed up, resumable, and checks logical hashes before reclaiming. Raw archive rewrite must retain source identity and use atomic publication. A failed migration retains its guard and rollback evidence.

## Acceptance

- All source and narrative logical hashes identical before/after migration; no foreign key violations; task bodies byte-identical.
- Identical-source replay adds no source records, revisions or blocks. Existing search/pairing/read/expand and retry tests pass.
- New tests cover shared descriptors, compact task bodies, identifier/CJK/tool search, shifted archive content reuse, preservation of failed tasks/snapshots and safe garbage collection.
- Quantify a real-data copy before touching current data. Target at least 40% project storage reduction, with measured results rather than promises.
- Full Python and panel test suites pass. Package/install updated runtime after data verification; never auto-approve Hooks or modify shared Codex databases.
