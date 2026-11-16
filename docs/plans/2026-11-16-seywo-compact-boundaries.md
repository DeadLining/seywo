# Seywo complete-turn and current-mainline redesign

Goal: commit complete dialogue turns, publish a concise current view separately from immutable historical evidence, and audit bounded native model work without a second organizer call.

Constraints: preserve existing MCP tools, search/source coordinates, human locks, historical revisions, standalone Responses backend, plugin isolation and bounded receipt retention. Do not enable untrusted hooks or delete history. No global registration changes.

- [x] Reproduce mid-turn Compact followed by final reply, interrupted turns, legacy transcripts, historical cursor inside a native turn, no-work/repeated Hooks, and failure retry.
- [x] Retain native lifecycle metadata in deterministic projection. Select a closed prefix of dialogue; leave incomplete turns pending and rehydrate legacy partial turns before committing. No tool bodies in semantic input.
- [x] Separate current view from historical item status. Model returns bounded current item indexes in the existing call; runtime validates identity/type/state and derives overview from those identities. Historical open items stay searchable without polluting current focus. Preserve locks.
- [x] Select relevant history by current phase/lexical relevance/recency within the existing 12-item budget. Avoid old-first truncation and duplicate schema in prompts.
- [x] Freeze native model selection; store backend/model/provider and token accounting in bounded receipt metadata, including failures. Keep raw prompts and model output out of receipts.
- [x] Run regression suite, fixture lifecycle, native real model semantics and usage comparison; install matching runtime without editing hook definitions/trust; verify installed MCP and package.

Acceptance: incomplete native turn -> zero calls and no semantic cursor advance; completed turn -> one call with both user and final answer; repeated hook -> zero calls. Current view <= 1 goal + 3 questions + 3 next steps; history/evidence preserved. Invalid current indexes -> atomic rejection. Prompt duplication removed and real before/after token usage measured rather than estimated. Existing tests plus new regressions pass; installed runtime and package match source.

Delivery: Seywo 0.4.0 installed and final bundle/source/installed files match (73 plugin files). Python 556 and UI 16 passed. Latest desktop host 0.169.0-alpha.11.1 reports all four hooks trusted. Real model input 5143 -> 2487 tokens in the same-dialogue sample; latency did not improve. Current project explicit installed-kernel run published v77 and preserved 324 history items.

Remaining desktop acceptance: next real Compact in the current human chat; no new trust approval required. Native desktop panel visuals were not re-tested in this change. See docs/seywo-compact-redesign-20261116.md for evidence and boundaries.
