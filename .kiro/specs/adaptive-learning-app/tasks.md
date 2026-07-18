# Implementation Plan: FastLearner Adaptive Learning Application

## Overview

Extend the existing npm, Python, Rust, React, Tauri, FastAPI, shared-package, contract, conformance, and CI baseline into the complete adaptive learning product. The sequence establishes durable local infrastructure and authorization first, then builds deterministic domain services, native wake support, memory and AI adapters, client experiences, and final release hardening. Each implementation task builds on earlier contracts and persistence boundaries, and the final tasks wire all components into tested desktop and web workflows.

## Tasks

- [ ] 1. Establish local infrastructure and persistence foundations
  - [x] 1.1 Add validated runtime settings, pinned service dependencies, and local service orchestration
    - Extend server-side configuration and `.env.example` without placing credentials in frontend bundles or version control.
    - Add pinned PostgreSQL 16/pgvector, Neo4j, and Redis-compatible services with health checks, private networking, localhost development ports, and named volumes.
    - Enforce production startup guards for local auth, insecure origins, default credentials, and missing secrets with typed safe errors.
    - _Requirements: 1.2, 1.3, 16.2, 19.3, 19.4, 25.1, 25.2, 25.3, 25.4_
  - [x] 1.2 Create Alembic migrations, canonical SQLAlchemy models, and idempotent seed commands
    - Implement the linear revision chain for identity, curriculum, work, learning, memory/vector, actions, outbox, lifecycle, operations, constraints, and owner-prefixed indexes.
    - Add model/schema consistency checks, revision compatibility checks, idempotent local persona seeds, and versioned curriculum seed entry points.
    - _Requirements: 2.2, 12.1, 16.1, 16.2, 16.12, 22.5, 25.7_
  - [-] 1.3 Implement unit-of-work, idempotency, audit, transactional outbox, and durable worker primitives
    - Add repository ports, SQLAlchemy implementations, transaction rollback behavior, operation-scoped idempotency records, audit records, outbox relay, leases, retry/backoff, and dead-letter states.
    - Keep job payloads ID-based where possible and retain committed intent across queue or worker failure.
    - _Requirements: 7.10, 7.11, 8.12, 8.13, 10.13, 10.14, 16.4, 16.14, 17.8, 17.9, 17.10, 19.16, 22.6_
  - [x] 1.4 Add cross-platform local startup, migration, seed, worker, and validation scripts
    - Implement `dev:local` and headless services-only orchestration with ordered readiness, safe remediation output, coordinated shutdown, and a separate destructive reset path.
    - Preserve existing lint, format, type, test, contract, conformance, build, and CI meanings while adding migration and service-backed jobs.
    - _Requirements: 1.1, 1.2, 1.3, 1.8, 25.5, 25.7, 25.8, 25.9_
  - [ ]* 1.5 Add migration, configuration, outbox-recovery, and local-orchestration automated tests
    - Test empty-to-head and prior-release migrations, seed idempotence, production config refusal, rollback, lease recovery, retry state, and safe missing-dependency reporting.
    - _Requirements: 1.8, 16.14, 22.5, 22.6, 23.3, 25.4, 25.8, 25.9_

- [ ] 2. Implement learner identity, authentication, and authorization boundaries
  - [-] 2.1 Build identity, profile, device, session, and observer relationship services
    - Implement the account-local learner, grade/timezone/preferences, device registrations, lifecycle-aware parent/teacher relationships, local development personas, and secure session contracts.
    - Resolve effective owner scope server-side and ignore untrusted owner identifiers supplied by clients.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.9, 2.10, 17.11_
  - [ ] 2.2 Implement centralized policy enforcement and owner-scoped repositories
    - Add learner ownership, active relationship scope intersection, observer read-only checks, revocation-on-request checks, scope-safe absence, and pseudonymous denial auditing.
    - Ensure authorization runs before body-driven lookups or service mutation and every data query carries an authenticated owner predicate.
    - _Requirements: 2.5, 2.6, 2.7, 2.8, 2.9, 6.14, 17.12, 17.15, 19.9, 19.10, 24.8_
  - [ ] 2.3 Add authenticated `/v1` identity endpoints and common API middleware
    - Implement request IDs, authentication dependencies, typed error envelopes, safe messages, cursor/time/UUID serialization, write idempotency enforcement, and compatible health routes.
    - _Requirements: 17.1, 17.2, 17.8, 17.9, 17.12, 17.13, 17.14, 17.15_
  - [ ]* 2.4 Add identity and authorization unit, invariant, integration, and security tests
    - Cover active, expired, absent, revoked, and out-of-scope relationships; observer mutation denial; owner isolation; foreign-ID indistinguishability; local-auth production refusal; and session revocation.
    - _Requirements: 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 19.9, 19.10, 23.1, 23.2, 23.5_

- [ ] 3. Checkpoint - Ensure foundation and authorization tests pass
  - Ensure all tests pass, ask the user if questions arise.
- [ ] 4. Implement subjects, assignments, goals, and deterministic planning
  - [ ] 4.1 Build subject, assignment, task, effort, goal, and source-brief application services
    - Implement lifecycle transitions, field validation, manual/pasted/uploaded intake metadata, editable extraction drafts, confirmation-only persistence, deletion audit preservation, and idempotent task creation.
    - Keep unconfirmed drafts outside canonical state and support school-managed, learner-created, and archived subjects.
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 7.9, 7.10, 7.11, 7.12, 7.13, 7.14, 24.2_
  - [ ] 4.2 Implement pure deterministic candidate scoring and schedule allocation
    - Add versioned fixed-point scoring, stable tie-breaking, availability clipping, non-overlapping 15-to-45-minute allocation, daily/requested limits, constraint explanations, and unscheduled-work results.
    - Persist complete reason inputs and preserve original reason history across block lifecycle changes.
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.11_
  - [ ] 4.3 Add planner persistence, conflict previews, and confirmed schedule mutations
    - Implement transactional plan creation, idempotent outcomes, planned/active/skipped/done state changes, manual overrides, conflict detection, and confirmation gates for AI-drafted changes.
    - Leave the existing schedule unchanged on conflicts, failed persistence, or unconfirmed proposals.
    - _Requirements: 8.7, 8.9, 8.10, 8.12, 8.13, 16.10, 22.11_
  - [ ]* 4.4 Add assignment lifecycle, planner rule, idempotency, conflict, and rollback tests
    - Cover validation errors, exact lifecycle transitions, equal-score stability, schedule bounds/non-overlap, no-capacity outcomes, duplicate keys, reason history, and atomic failure behavior.
    - _Requirements: 7.3, 7.4, 7.5, 7.10, 7.11, 7.12, 8.2, 8.3, 8.8, 8.9, 8.11, 8.12, 8.13, 23.1, 23.2_

- [ ] 5. Implement native desktop capabilities and local wake detection
  - [-] 5.1 Build the Tauri OS adapter registry and desktop lifecycle commands
    - Implement macOS tray, shortcut, permission, secure-store, notification, login-item, display, audio-device, close-to-tray, quit cleanup, capability reporting, and nonfatal recovery paths behind portable Rust traits.
    - Provide Windows/Linux adapter seams without changing domain or frontend contracts and enforce production CSP/secret boundaries.
    - _Requirements: 1.4, 1.5, 1.7, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 19.2, 19.3_
  - [x] 5.2 Implement the pure Rust double-clap detector and bounded audio capture adapter
    - Add adaptive noise-floor analysis over 10-to-30-millisecond frames, transient shape checks, 120-to-900-millisecond pairing, one-event emission, cooldown suppression, monotonic expiry, pause/unavailable states, and in-memory downmixing.
    - Ensure wake analysis has no network/AI dependency, persists no raw audio, and stores only opted-in aggregate diagnostics.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.8, 4.9, 4.10, 4.11, 20.12_
  - [ ] 5.3 Wire native wake, permission, synchronization, and overlay events to desktop React
    - Implement the sole Tauri bridge module, visible wake confirmation before separate speech capture, settings/pause/device-loss flows, keyboard-only fallback, focused overlay positioning, and explicit quit behavior.
    - _Requirements: 4.6, 4.7, 4.9, 4.11, 5.1, 5.2, 5.7, 5.9, 22.10, 22.12_
  - [ ]* 5.4 Add native adapter, wake fixture, privacy, and benchmark tests
    - Test detector state transitions, cooldown uniqueness, frame-window and interval boundaries, unavailable devices, tray failure recovery, zero network/raw-audio persistence, quiet-room detection reporting, and tray-mode CPU/RSS measurement.
    - _Requirements: 3.3, 3.7, 3.8, 3.9, 4.3, 4.5, 4.8, 4.11, 4.12, 20.12, 22.8, 23.1, 23.9_

- [ ] 6. Implement deliberate memory, retrieval stores, jobs, deletion, and export
  - [ ] 6.1 Build source, consent, auto-save, episode, upload-quarantine, and provenance services
    - Create explicit-save and named-rule capture transactions, field validation, subject/lifecycle metadata, file size/type scanning ports, untrusted-content boundaries, and failed graph-sync state.
    - Prevent ordinary chat from becoming long-term memory and make canonical episodes authoritative over graph augmentation.
    - _Requirements: 9.1, 9.2, 9.3, 9.5, 9.6, 9.10, 9.11, 19.11, 19.12, 19.13_
  - [ ] 6.2 Implement owner-filtered pgvector retrieval and bounded context ranking
    - Apply owner, permitted subject, date, and live-lifecycle filters before similarity ranking; combine canonical, source-chunk, and graph evidence; deduplicate; rank deterministically; and enforce record/token limits.
    - Return exact-filter empty results and explicit degraded supplementary-context status without broadening scope.
    - _Requirements: 9.7, 9.8, 10.2, 10.3, 10.4, 10.5, 10.6, 10.9, 10.10, 22.4_
  - [ ] 6.3 Implement Graphiti/Neo4j adapters and idempotent ingestion/retraction workers
    - Map authenticated owner/subject groups exactly, verify graph fact provenance against live canonical episodes, preserve canonical precedence, and support unavailable graph operation.
    - Add chunking, embedding, ingestion, retraction, cleanup, retry/dead-letter, and visible synchronization handlers through the durable outbox.
    - _Requirements: 9.4, 9.5, 9.6, 9.10, 16.3, 16.4, 16.7, 16.13, 20.4, 20.6, 20.7, 21.3_
  - [ ] 6.4 Implement forget, structured export, consent revocation, and account lifecycle flows
    - Exclude deleted episodes/chunks/facts immediately in the deletion transaction, enqueue cleanup once, retain retry state, and remove graph links after completion.
    - Generate versioned owner-scoped exports with every required category, empty arrays, provenance, checksums, short-lived authorization, and no secrets/raw audio; implement confirmed account deletion fan-out.
    - _Requirements: 20.1, 20.2, 20.3, 20.4, 20.5, 20.6, 20.7, 20.8, 20.9, 20.10, 20.11, 20.12_
  - [ ]* 6.5 Add memory, vector, graph, job, lifecycle, and export automated tests
    - Cover save/no-save rules, empty content, duplicate writes, strict filter preservation, canonical precedence, graph failure/recovery, immediate deletion exclusion, cleanup retries, complete export shape, upload rejection, and owner isolation.
    - _Requirements: 9.3, 9.8, 9.10, 9.11, 9.12, 10.3, 10.4, 20.2, 20.5, 20.6, 20.8, 20.9, 23.2, 23.3, 23.5_

- [ ] 7. Implement curriculum, validated practice, mastery, review, and next actions
  - [ ] 7.1 Build curriculum DAG, reviewed content lifecycle, and initial mathematics pack
    - Implement acyclic publication validation with cycle-edge reporting, immutable content/question versions, reviewer decisions, retired-version behavior, extensible packs, and the specified 15-concept prerequisite graph.
    - Seed at least one lesson and three varied questions with answers and explanations per concept using an idempotent manifest checksum.
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7, 12.8, 12.9, 12.10, 12.11, 12.12, 12.13, 12.14, 12.15, 12.16, 12.17, 12.18, 12.19, 12.20, 12.21, 12.22, 12.23, 12.24, 12.25_
  - [ ] 7.2 Implement generated-practice validation and academic-integrity policies
    - Validate answer specifications, rationales, near-duplicates, grade suitability, safety, relevance, provenance, and review state before service; prefer approved curated content and retain exact served versions.
    - Add conservative assessed-work behavior and typed unavailable outcomes without recording attempts or duplicate drafts.
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 13.8, 13.9, 24.5_
  - [ ] 7.3 Implement transactional BKT learning events and explainable recommendations
    - Add validated pure BKT formulas, versioned parameter sets, defensive bounds, served-context checks, row locking/versioning, snapshots, pacing reasons, recommendation bands, successor unlocks, first review, and atomic rollback.
    - Use correctness as the only mastery observation while preserving duration, hint, and retry evidence in reasons.
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7, 14.8, 14.9, 14.10, 14.11, 14.12, 14.13, 14.14, 14.15_
  - [ ] 7.4 Implement spaced review and deterministic adaptive next-action selection
    - Add versioned quality mapping, interval/ease/repetition/due state, learner-timezone display, low-quality shortening, pacing-sensitive quality, evidence-bearing priority selection, and safe missing-rule/content outcomes.
    - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7, 15.8, 15.9_
  - [ ]* 7.5 Add DAG, content, BKT, review, recommendation, and concurrency tests
    - Cover cycle detection, exact seed edges, review gates, retired content, generated validation failures, BKT equations/bounds/thresholds, duplicate event application, transaction rollback, review quality, due-date immutability, and deterministic next-action priority.
    - _Requirements: 12.2, 12.23, 13.2, 13.8, 14.3, 14.4, 14.5, 14.6, 14.7, 14.14, 15.2, 15.3, 15.8, 15.9, 23.1, 23.2_

- [ ] 8. Checkpoint - Ensure deterministic domain and persistence tests pass
  - Ensure all tests pass, ask the user if questions arise.
- [ ] 9. Implement AI provider abstraction and grounded assistant orchestration
  - [ ] 9.1 Build vendor-neutral AI ports and the configured OpenAI adapter
    - Implement generation, streaming, structured output, embeddings, and optional speech contracts with normalized usage/errors and server-side provider/model configuration.
    - Prevent unavailable or invalid provider output from mutating canonical state and keep deterministic domain services provider-independent.
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8, 11.9, 11.10_
  - [ ] 9.2 Implement intent routing, grounded response assembly, and bounded action proposals
    - Validate non-empty voice/text parity, classify the nine intents, read canonical state first, retrieve bounded permitted evidence, cite supported claims, and express uncertainty/no-record/degraded results.
    - Implement expiring owner-scoped proposals, confirm-once/reject state machines, allowed tools, prompt-injection isolation, academic-integrity responses, unsupported autonomy limits, non-diagnostic language, and evidence-bounded career exploration.
    - _Requirements: 5.4, 5.5, 5.6, 5.8, 10.1, 10.2, 10.7, 10.8, 10.9, 10.10, 10.11, 10.12, 10.13, 10.14, 10.15, 10.16, 10.17, 13.5, 24.3, 24.4, 24.6, 24.7, 24.9_
  - [ ] 9.3 Add assistant SSE streaming and action API resources
    - Emit ordered discriminated events, durable proposal references, citation events, warnings, completion/failure events, content-free heartbeats, bounded backpressure, and safe disconnect cancellation.
    - Expose confirm/reject routes through responsible domain services with authorization, audit, and idempotency.
    - _Requirements: 10.13, 10.14, 10.15, 10.16, 17.3, 17.8, 17.10, 19.16_
  - [ ]* 9.4 Add provider, retrieval, proposal, streaming, integrity, and autonomy tests
    - Cover provider failure/invalid output, context bounds, citation/uncertainty behavior, no-record scope preservation, voice/text intent parity, proposal substitution/expiry/replay, SSE ordering/backpressure, prompt injection, and unsupported external actions.
    - _Requirements: 5.4, 10.6, 10.8, 10.9, 10.10, 10.14, 10.16, 10.17, 11.5, 11.6, 13.5, 23.5, 24.3, 24.4, 24.9_

- [ ] 10. Implement analytics read models, pathway signals, and complete versioned API contracts
  - [ ] 10.1 Build dashboard aggregates, subject evidence, streaks, focused time, and pathway rules
    - Produce persisted/restart-safe home and subject read models, empty collections, deterministic evidence trends, and deletion-aware pathway signals with thresholds, bounded confidence, uncertainty, rule versions, and exploration actions.
    - Keep analytics query-only and scope outputs to learner or observer permissions.
    - _Requirements: 6.4, 6.5, 6.10, 6.11, 18.1, 18.2, 18.3, 18.4, 18.5, 18.6, 18.7, 18.8, 16.11_
  - [ ] 10.2 Implement all remaining `/v1` domain routers and regenerate shared contracts
    - Expose dashboard, subjects, assignments/tasks, planner/study blocks, memory/search/export/lifecycle, learning/practice/mastery/reviews, analytics/pathways, consent, jobs, and synchronization resources.
    - Regenerate OpenAPI TypeScript contracts and SSE unions, preserve compatibility, and fail validation on committed-schema drift.
    - _Requirements: 1.6, 6.3, 17.2, 17.4, 17.5, 17.6, 17.7, 17.13, 25.6_
  - [ ]* 10.3 Add analytics, API integration, contract, persistence, and performance tests
    - Cover empty states, observer-scoped aggregates, pathway thresholds/recalculation, typed errors, all write idempotency guards, contract generation drift, restart restoration, and the documented one-second dashboard benchmark.
    - _Requirements: 6.10, 6.11, 6.14, 17.8, 17.9, 17.10, 18.2, 18.3, 18.7, 18.8, 22.3, 22.5, 23.3_

- [ ] 11. Build shared accessible UI and the complete desktop learner experience
  - [ ] 11.1 Create shared accessible components and contract-to-view-model mappers
    - Implement app framing, navigation, tables, list/calendar/graph alternatives, overlays, evidence/provenance drawers, empty/unavailable/read-only states, confirmation panels, form controls, focus restoration, live regions, reduced motion, and non-color status cues.
    - _Requirements: 5.1, 5.5, 5.6, 6.3, 6.12, 6.13, 22.1, 22.2_
  - [ ] 11.2 Implement desktop home, subjects, assignments, memory, schedule, insights, pathway, and practice routes
    - Wire typed API clients, authoritative cache invalidation after confirmed actions, list/table/calendar views, forms, editable drafts, source displays, reasons, manual overrides, and labeled actionable empty states.
    - _Requirements: 5.3, 6.1, 6.4, 6.5, 6.6, 6.7, 6.8, 6.10, 6.12, 7.2, 22.10_
  - [ ] 11.3 Complete the companion overlay, offline recovery, secure-session bootstrap, and application lock
    - Implement all overlay states, keyboard-first focus, voice/text shared requests, source rendering, mutation previews, retry/text recovery, unavailable AI labeling, local deterministic operation, secure session storage, lock-gated rendering, and sensitive-cache clearing.
    - _Requirements: 5.1, 5.2, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 11.8, 19.3, 19.6, 22.10, 22.12_
  - [ ]* 11.4 Add desktop component, workflow, accessibility, and offline-state tests
    - Cover keyboard operation, focus, screen-reader names/live regions, visual alternatives, empty/error/offline states, mutation previews, lock behavior, API unavailability, and native bridge recovery.
    - _Requirements: 3.8, 5.2, 5.6, 5.7, 5.9, 6.12, 19.6, 22.1, 22.10, 22.12, 23.6_

- [ ] 12. Build the independently deployable read-only web dashboard
  - [ ] 12.1 Implement learner and observer web routes using shared contracts and UI
    - Add session bootstrap, dashboard/subject/assignment/memory/schedule/insight/pathway views, read-only labeling, scoped data rendering, and omission of mutation controls with no Tauri imports.
    - _Requirements: 6.2, 6.3, 6.4, 6.5, 6.9, 6.13, 18.6, 24.1, 24.8_
  - [ ] 12.2 Add independent deployment configuration and unavailable/stale-data behavior
    - Produce a separate web build/deployment target with secure origins/CSP and fetched-time-aware unavailable states that never present cached data as current.
    - _Requirements: 6.2, 6.15, 19.4, 22.2, 24.1_
  - [ ]* 12.3 Add web learner/observer contract, authorization, and accessibility tests
    - Cover separately built deployment, scoped parent/teacher reads, rejected writes, omitted controls, read-only empty/unavailable states, keyboard navigation, labels, and accessible visual alternatives.
    - _Requirements: 6.2, 6.3, 6.9, 6.13, 6.14, 6.15, 18.6, 22.2, 23.6, 23.10_

- [ ] 13. Harden security, privacy-minimal operations, resilience, and release portability
  - [ ] 13.1 Implement security middleware, redacted observability, rate limits, and consented diagnostics
    - Add TLS/origin/CORS/CSRF/session controls, AI/write rate limits before mutation, request/job/provider metrics, pseudonymous logs, deny-by-default content/secret redaction, safe logging fallback, telemetry separation, and diagnostic deletion.
    - _Requirements: 19.1, 19.4, 19.5, 19.14, 19.15, 19.17, 19.18, 19.19, 21.1, 21.2, 21.3, 21.4, 21.5, 21.6, 21.7, 21.8_
  - [ ] 13.2 Complete consent-dependent processing and cloud-sync-disabled boundaries
    - Gate future synchronization behind recorded opt-in, stop consent-dependent processing after revocation, expose lifecycle/synchronization status, and retain local deterministic operation during optional dependency outages.
    - _Requirements: 19.7, 19.8, 20.11, 22.10, 22.12_
  - [ ] 13.3 Add backup/restore automation and macOS-first signed release verification
    - Implement scripts and CI jobs for PostgreSQL/pgvector backup and restore ordering, Neo4j snapshots/reconciliation, migration compatibility, macOS signing, hardened runtime, notarization, stapling, update-manifest verification, and portable adapter compile checks.
    - _Requirements: 1.4, 1.5, 1.7, 22.7, 22.9_
  - [ ]* 13.4 Add security, observability, resilience, backup, and release pipeline tests
    - Cover secret/log scanning, rate limits with no write, upload attacks, local-mode exposure, audit records, logging failure independence, worker recovery, backup restoration, migration compatibility, signing/update verification, and platform adapter compilation.
    - _Requirements: 19.3, 19.11, 19.12, 19.13, 19.15, 19.16, 21.2, 21.7, 22.6, 22.7, 22.9, 23.5_

- [ ] 14. Integrate complete workflows and release acceptance automation
  - [ ] 14.1 Wire API, worker, repositories, adapters, feature health, desktop, and web composition roots
    - Connect all domain services through declared ports, enforce required versus degradable dependency health, and ensure shared versioned contracts drive both clients without bypassing authorization or confirmation.
    - Preserve baseline commands and expose recoverable feature availability across local and production profiles.
    - _Requirements: 1.1, 1.3, 1.6, 16.5, 16.6, 16.7, 16.8, 16.9, 16.10, 16.11, 16.13, 22.10, 24.1_
  - [ ]* 14.2 Add complete end-to-end and release-acceptance suites
    - Automate assignment creation through confirmed plan/focus completion; wake fixture through cited answer; deliberate save/retrieval/forget; practice/mastery/recommendation; observer reads/denied writes; export/restart; duplicate write; empty/uncertain response; and failed transaction rollback.
    - Verify explicit generated-mutation confirmation, no wake-triggered outbound request/raw audio, persisted canonical state, supported initial clients/intake, and bounded unsupported actions.
    - _Requirements: 23.4, 23.7, 23.8, 23.9, 23.10, 23.11, 23.12, 23.13, 24.1, 24.2, 24.9_

- [ ] 15. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP; core implementation tasks are required.
- Each task references granular acceptance criteria for traceability and assumes `requirements.md` and `design.md` remain the source of truth.
- The design has no named `Correctness Properties` section, so no property-based-test tasks were added; unit, invariant, integration, contract, end-to-end, security, accessibility, performance, and release tests cover the specified verification work.
- Checkpoints provide safe points for validating incremental progress before proceeding.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.4", "5.2"] },
    { "id": 2, "tasks": ["1.3", "2.1", "5.1"] },
    { "id": 3, "tasks": ["1.5", "2.2", "4.1", "5.3", "6.1", "7.1", "9.1"] },
    { "id": 4, "tasks": ["2.3", "2.4", "4.2", "6.2", "6.3", "7.2", "7.3"] },
    { "id": 5, "tasks": ["4.3", "6.4", "7.4", "9.2", "13.1"] },
    { "id": 6, "tasks": ["4.4", "5.4", "6.5", "7.5", "9.3", "10.1", "13.2"] },
    { "id": 7, "tasks": ["9.4", "10.2", "11.1", "13.3"] },
    { "id": 8, "tasks": ["10.3", "11.2", "12.1"] },
    { "id": 9, "tasks": ["11.3", "12.2"] },
    { "id": 10, "tasks": ["11.4", "12.3", "13.4"] },
    { "id": 11, "tasks": ["14.1"] },
    { "id": 12, "tasks": ["14.2"] }
  ]
}
```
