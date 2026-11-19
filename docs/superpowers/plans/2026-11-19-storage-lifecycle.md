# Seywo Storage Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Eliminate avoidable storage accumulation while preserving source-backed retrieval and retry semantics.
**Architecture:** Stable identity tables reference shared compressed descriptors and task documents; FTS and raw archives reuse content; a unified collector follows committed publication under existing locks.
**Tech Stack:** Python 3.11+, SQLite/FTS5, zlib, existing portable locks. No new dependencies or model requests.
**Spec:** docs/superpowers/specs/2026-11-19-storage-lifecycle.md

## Global Constraints

Preserve all existing dirty work; no commit/push. No deletion of original Codex transcripts or shared host databases. Migration must preserve logical source/narrative hashes and task request/result bytes. No Hook trust changes. Test and measure a copy before migrating production.

### Task 1: Shared database representations
Files: new `storage_v8.py`, `execution_documents.py`, tests `test_storage_v3.py`; modify `store.py`, `storage_schema.py`, `storage_admin.py`.
- [x] Add failing schema, source identity, task round-trip, compressed size and search regression tests.
- [x] Implement bounded compressed execution documents and source descriptors, including transactional migration and compatibility views/triggers.
- [x] Replace repeated FTS token storage with external compressed content; preserve query behavior.
- [x] Run storage, retrieval, understanding, narrative and migration tests.

### Task 2: Lossless archive sharing
Files: new `archive_chunks.py`; modify `archive.py`, `storage_admin.py`; extend storage tests.
- [x] Add a shifted-history reuse regression using deterministic content.
- [x] Implement streaming content-defined chunks and atomic reblock for existing manifests; keep full-byte digest verification.
- [x] Test exact binary round trips, content reuse and interruption recovery.

### Task 3: Publication and reclamation
Files: new `storage_lifecycle.py`; modify `maintain.py`, `store.py`, `storage_admin.py`, `inbox.py`; extend tests.
- [x] Add tests for terminal unbound capture reclamation, failed/pending protection, idempotency and failure visibility.
- [x] Wire one collector after committed maintenance, explicit deletion and admin maintenance; protect shared archive publication and DB references.
- [x] Reclaim detached payloads and execution document chains only after checking live references; condition physical DB shrinking on actual freed pages under exclusive ownership.

### Task 4: Real-data acceptance and delivery
Files: documentation and reproducible verification script.
- [x] Create private real-data copy; migrate with backup, compare source/narrative/task hashes and search results, measure sizes.
- [x] Run full Python and UI tests; revise any failed contract.
- [x] Safely migrate actual project, package/install runtime, verify installed files and retain rollback receipts.
- [x] Report exact savings, tests and any remaining runtime/Hook boundaries.

## Verified outcome

See `docs/superpowers/evidence/2026-11-19-storage-acceptance.md`. Active storage reduced 47.83%; total including retained rollback reduced 22.96%. Final Python 939 passed/2 skipped, UI 63 passed. Schema 8 applied to the explicitly selected current project; plugin 0.5.1 installed and fresh-host protocol verified. Existing desktop connections need restart/reconnect. No commit/push.
