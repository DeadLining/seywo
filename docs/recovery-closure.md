# Recovery Closure (2026-10-29)

Scope: the fruit-fly project's evidence failures, bounded continuation and installed
desktop runtime consistency. Preserve source integrity, graph data, provider
selection, deletion behavior and unrelated projects. No training services touched.

Implementation:
- Desktop Python bootstrap delegates to the immutable manifest runtime. Missing
  active runtime fails closed. Settings retain the installed app's identity.
- Continuation counts committed judgments, validated outputs, completed input,
  splits and repairs, not replayed input or transport calls. Three truly empty
  rounds still stop; cached successful judgments permit bounded forward progress.
- Evidence-reference failures get the same bounded split after the existing
  one-time correction as other repairable schema failures. Integrity errors do not.
- Explicit `recover-blocked --repair-evidence` revalidates exact project-local
  audited responses against rehydrated source and original candidates, keeping
  audit history. No provider calls during recovery itself.
- `--retry-invalid` permits one diagnosed retry, never an automatic retry loop.
- Literal-only assertion failures isolate that proposal as uncertain. Existing
  identity gates retain its content and source evidence in unresolved, without
  publishing its node or incident relations. The strict decoder is unchanged;
  malformed structures and unknown source anchors still reject the batch.
- Deleted optional context is removed only when its entry is absent and not in
  source coverage. Revalidate retained source before refreshing candidates.
- `--grant-budget` records one additional original-sized allowance only for
  exhausted jobs. Spending is not erased; other jobs get no extra allowance.
- Reanchored primary evidence cannot hide an exact existing revision baseline:
  the fallback requires an identical type/title/content/status payload in the
  immutable ledger plus a currently valid source. Changed payload, changed
  attribution and missing sources still reject. No history is synthesized.
- Recovery outbox and existing writer locks own wakeups. Dismissed/completed
  jobs are never reopened. `--no-launch` separates repair from execution.

Verification and deployment:
- Exact installed candidate critical suite: 233 passed, no skips, on L3090 CPU.
- Consolidated suite: 1468 passed, 4 failed, 6 skipped. The four failures were
  test subprocess import paths and macOS transfer sidecars; with the isolated
  test path corrected and sidecars removed, all four passed. Combined result:
  1472 passed, 6 macOS-only integration tests skipped on Linux. Do not add the
  overlapping critical suite to this count. Raw XML/log and rerun XML are in
  output/recovery-closure-*-20261029.*.
- Initial runtime 2596548d52144b632f01 passed that full suite. The follow-up
  baseline fix in final runtime 189dfaedb3371051f29b passed a 245-case related
  regression suite: initially 244 passed and one fixture hit the immutability
  trigger while arranging missing-source corruption; corrected fixture rerun
  passed all 83 revision/recovery cases, including the six added cases. Keep
  these overlapping counts separate from the full suite.
- Activated runtime 189dfaedb3371051f29b; installed signed desktop bundle has
  identical contents to the active runtime across all 69 core Python/JSON files.
  codesign --verify --deep --strict passed. The effective global model remains
  deepseek-v4.1-flash-1017-nothink. Hook trust hashes were not edited.
- Explicit fruit-fly recovery restored 127 jobs: 67 reused exact audited outputs,
  54 received one diagnosed retry (30 deleted-context refreshes), and 6 received
  recorded bounded budget grants. Recovery itself made zero model calls. Invalid
  literal candidates were isolated rather than published as supported facts.
- Four further recorded recovery events restored two diagnosed baseline jobs
  and granted two newly exhausted jobs one allowance each. These are existing
  members of the original 127-job cohort, not four additional jobs. Total grants
  are eight; no job gets repeated grants under this recovery revision.
- Live processing proved persisted single-token judgments, saved graph nodes,
  and completed recovered jobs. At 2026-10-29 12:12:54 Asia/Shanghai, the recovered
  cohort had 11 complete, 64 judging and 52 pending. One pending job was waiting
  on an existing stage lease; no project job was blocked. The actual baseline
  failure f1aed215... completed with zero extra model calls. Historical
  backlog processing is NOT complete and is not a condition to erase old errors.
- Existing bounded runner continues the four affected fruit-fly chats. No quota,
  audit history, dismissed job or primary source-integrity check was reset.
  No GPU, training, simulator or TensorBoard service was used or interrupted.

Delivery boundary: code fixes, installed-runtime consistency and Linux regression
verification are complete. Historical backlog drain remains in progress. The six
macOS-specific test cases were not executed under the L3090-only test constraint;
the local installed-app signature/runtime/settings checks are separate evidence,
not substitutes for those integration tests. No absolute zero-regression claim.
