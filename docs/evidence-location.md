# Exact Atomic Evidence Location

Scope: repair request-local evidence addresses at artificial 480-character cuts.
Do not change provider settings, persistent schemas, atomic identities, source
files, existing graph data, scheduling or blocked task state.

- The request builder owns region identity and character offsets. They are not
  transmitted to the model or stored in evidence rows.
- A repair must overlap an already cited anchor and match an unchanged literal
  exactly once in its bounded neighborhood within one continuous source region.
- Message boundaries, filtered fragments and redactions always separate regions.
- Retain the original citation and extend its quote only; never concatenate
  unrelated spans, invent text, normalize whitespace or infer a fact.
- All combined field extensions share the existing 600-character quote limit.
- Legacy anchor maps without trusted positions retain the previous strict path.
- Existing final schema, fragment and semantic validation still apply.

Verification (2026-10-29): 192 tests passed on L3090 for both the source tree and
the exact installed candidate. Two fruit-fly failed-node responses pass decoding,
fragment validation and atomic anchoring with unchanged assertion text. Their
source text and record hashes were verified against the local Codex transcript
before making the minimal fixtures. Tests include rejection of ambiguity,
uncited neighbors, legacy maps without locations, synthetic joins and oversized
combined quotes. Receipt: `output/evidence-location-20261029.json`.

The shared launcher and manifest now select immutable runtime
`221937bb3136a5eafbe7`. Only `sessiongraph/graph_contract.py` differs from the prior
installed runtime. No signed app resources or trust hashes were modified; no
process was restarted. Existing processes can retain their old imports until
they exit. Codex may request review of the changed launcher.

No model calls, graph writes, queue resets or bulk replays were performed. Old
blocked tasks were not recovered. Desktop rebundling and scheduler changes are
outside this slice. The existing dirty worktree was preserved; no commit made.
