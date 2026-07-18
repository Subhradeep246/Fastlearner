# FastLearner — Complete Build Specification

## 1. Product Definition

FastLearner is an internet-connected, always-available personal study companion for students. It combines adaptive tutoring, assignment management, subject dashboards, persistent personal context, question generation, study planning, and evidence-based strength/pathway guidance in one desktop product.

Initial audience: students in grades 3–12. Initial adaptive curriculum: grades 3–5 fractions → decimals → percentages. Architecture must support any school subject and future curriculum packs.

The product is not a generic chatbot. It is a personal study system with durable structured state, source-backed memory, explainable recommendations, and a calm desktop presence.

## 2. Product Outcomes

Student can:

- Double-clap to wake FastLearner while it runs in desktop tray/background.
- Ask a question by voice or typing, get an answer grounded in saved notes, assignments, curriculum content, and approved sources.
- Save a note, lecture summary, assignment brief, goal, correction, resource, or preference into subject-scoped long-term memory.
- View all subjects, upcoming work, weak/strong concepts, review due dates, study streak, and next best action in one dashboard.
- Add assignments manually or paste/import a brief; receive task breakdown and study blocks.
- Practise concepts through generated or curated questions; record correctness, time, hints, and retries.
- Receive an explainable adaptive next lesson/review recommendation.
- Generate a daily/weekly study schedule based on deadlines, availability, mastery, spaced review, and stated goals.
- See suggested academic directions based on measured evidence, never deterministic career claims.

Parent/teacher can later receive a read-only learner dashboard. MVP focuses on one learner owning their data.

## 3. Product Principles and Hard Constraints

1. **Internet-connected AI; local ownership of context.** AI requests require connectivity. User data, event log, and source files remain local by default; cloud sync is opt-in later.
2. **Wake locally.** Double-clap detection processes microphone audio on device. It never uploads, records, or retains raw audio. Speech recording begins only after visible wake confirmation and explicit microphone permission.
3. **Memory requires intent.** Do not silently save every chat message. Student taps/says “save this” or enables a clear auto-save rule for a source/import.
4. **Provenance always.** Every memory, generated answer, schedule block, and pathway signal must expose source/evidence/reason.
5. **LLM is not source of truth.** Database state, assignment dates, curriculum graph, BKT mastery, and schedule rules are deterministic services. LLM handles language understanding, answer drafting, retrieval synthesis, restructuring content, and question generation.
6. **Age-appropriate defaults.** Minimal PII, no third-party behavioural trackers, clear deletion/export, conservative academic-integrity mode, and human review before AI curriculum content becomes published.
7. **Explainable adaptation.** Never return a black-box “study this.” Return rule, evidence, confidence, and next action.

## 4. MVP Scope

### Include

- macOS-first Tauri desktop application; architecture portable to Windows/Linux.
- Tray/background companion, double-clap wake, keyboard wake fallback, compact assistant overlay.
- React dashboard inside desktop app; optional separate web dashboard consumes same read-only API.
- Account-local student profile, subjects, assignments, notes/context, learning events, schedule, and dashboard.
- Graph-based RAG memory using **Graphiti** (likely intended project; if “Graphify” means another repository, isolate behind adapter and swap implementation).
- Initial math concept graph with 15 nodes: whole numbers through percentage of a quantity.
- BKT mastery engine, spaced review, explainable recommendation rules.
- AI provider abstraction supporting one configured provider first (OpenAI or Anthropic); no hard vendor coupling.
- Text input; voice input after wake; generated questions, hints, explanations, summaries, task breakdowns, and schedules.

### Exclude from MVP

- Mobile app.
- Fully automatic assignment/LMS/calendar import.
- Fully autonomous web browsing, emailing, submissions, or changes to calendar/tasks.
- Fully automated AI-generated curriculum publishing.
- Diagnosing learning disabilities or giving deterministic career decisions.
- Multi-user family/teacher permissions beyond data-model preparation.

## 5. Primary User Flows

### 5.1 Wake and ask

1. App runs in tray; background wake service is enabled only after permission.
2. Student claps twice within configured time window, or uses keyboard shortcut.
3. App shows compact overlay: “FastLearner is listening.”
4. Student speaks/types: “What do I need to finish for science?”
5. Backend loads assignments, current plan, and relevant memory from only that student/subject scope.
6. LLM returns concise answer with cited assignment/note context and proposed actions.
7. Any proposed write action needs explicit confirmation.

### 5.2 Capture context

1. Student says/types “Save this under history” or uses Save Context.
2. UI captures text, source, subject, type, optional file reference, and consent.
3. Raw source metadata is stored locally.
4. Memory worker ingests a Graphiti episode into that student’s subject group.
5. Graph extraction creates temporal entities/relationships with source provenance.
6. Future queries retrieve only relevant, permitted episodes and graph facts.

### 5.3 Assignment to plan

1. Student creates assignment: subject, title, due date, estimated effort, optional rubric/brief.
2. LLM may extract tasks from pasted brief using structured output.
3. Student reviews/edits extracted tasks before persistence.
4. Planner creates short, realistic blocks, prioritising deadline risk, mastery gaps, spaced review, and availability.
5. Dashboard explains each block: “Science report: due tomorrow; 40 minutes remaining.”

### 5.4 Adaptive lesson

1. Student opens a subject/next action.
2. App serves lesson then objective questions.
3. Every attempt records concept, question version, correctness, response duration, hints, retries, and timestamp.
4. BKT updates per-concept knowledge probability transactionally.
5. Recommendation service chooses repeat, alternate explanation, prerequisite review, due review, or next unlocked concept.
6. UI shows simple learner language plus optional “Why?” evidence.

## 6. Architecture

```text
                    ┌────────────────────────────────────────┐
                    │ Tauri + React desktop app               │
                    │ tray | clap wake | overlay | dashboard  │
                    └───────────────┬────────────────────────┘
                                    │ HTTPS / localhost API
                    ┌───────────────▼────────────────────────┐
                    │ FastAPI application                     │
                    │ auth | assistant | tasks | tutor | plan │
                    │ memory orchestration | analytics        │
                    └───────┬───────────────┬────────────────┘
                            │               │
              ┌─────────────▼───┐   ┌───────▼────────────────┐
              │ PostgreSQL       │   │ Graphiti + Neo4j        │
              │ pgvector         │   │ temporal context graph  │
              │ app state/events │   │ graph retrieval          │
              └─────────────────┘   └────────────────────────┘
                            │               │
                    ┌───────▼───────────────▼───────────────┐
                    │ AI provider adapter                     │
                    │ LLM | embeddings | STT/TTS optional     │
                    └────────────────────────────────────────┘
```

### 6.1 Technology choices

| Layer | Choice | Reason |
| --- | --- | --- |
| Desktop | Tauri v2 + React + TypeScript | Native tray/background integration; smaller footprint than Electron. |
| Native wake | Rust + `cpal` | Reliable microphone access outside visible web view. |
| API | Python 3.12+ FastAPI | Natural fit for BKT, Graphiti, AI SDKs, data services. |
| Relational store | PostgreSQL 16 + Alembic + SQLAlchemy | Durable transactional state and analytics. |
| Vector search | pgvector | Simple metadata-filtered document/source retrieval. |
| Temporal graph memory | Graphiti + Neo4j | Dated facts, source provenance, entity relations, graph/hybrid retrieval. |
| Cache/jobs | Redis + worker (RQ/Celery/Arq) | Background ingestion, embeddings, graph extraction, reminders. |
| Web dashboard | React/TypeScript; same component library | Shared UI and read-only dashboard deployment. |
| AI | Provider adapter | Permit OpenAI/Anthropic choice without domain logic rewrite. |

### 6.2 Service boundaries

| Service | Owns | Must not own |
| --- | --- | --- |
| Desktop shell | tray, permissions, clap detector, secure token storage, notifications | LLM decisions, data persistence rules |
| Assistant service | intent routing, tool selection, cited final answer | direct unrestricted database writes |
| Memory service | source ingestion, Graphiti episodes, retrieval, delete/export | scheduling/mastery rules |
| Curriculum service | concepts, prerequisites, approved lessons/questions | raw user memory |
| Learning service | BKT update, mastery, reviews, next recommendation | LLM-generated mastery verdicts |
| Planner service | task breakdown persistence, schedule optimisation, explanations | hidden autonomous calendar writes |
| Analytics service | read models, subject trends, dashboard aggregates | mutation endpoints |

## 7. Desktop Companion Requirements

### 7.1 Tray/background behaviour

- Start at login is opt-in.
- Tray menu: Open dashboard, Wake companion, microphone setting, pause wake listening, sync state, quit.
- App remains visible in system tray when main window closes; actual quit requires explicit Quit.
- Global shortcut default: `Cmd/Ctrl + Shift + Space`.
- Clear indicator when wake listening is active; pausing immediately stops microphone stream.
- Wake service must not call network or AI API.

### 7.2 Double-clap detector

Implement in Rust native layer; do not rely on browser Web Audio for production background mode.

Algorithm baseline:

1. Capture mono microphone frames using `cpal`; never write samples to disk.
2. Apply high-pass/band-pass filtering suitable for transient hand-clap energy.
3. Compute short-frame RMS/peak envelope (10–30 ms windows).
4. Detect transient where energy exceeds adaptive noise-floor threshold and has clap-like short duration.
5. Require two valid transients separated by 120–900 ms.
6. Apply 1.5–3 second cooldown after wake to avoid repeated triggers.
7. Emit `wake-detected` event to Tauri UI. UI opens overlay and begins opt-in speech capture.

Settings: sensitivity, double-clap interval, keyboard-only mode, wake pause, microphone device. Ship keyboard wake fallback. Collect false-positive/false-negative diagnostics locally only if user enables diagnostics.

Acceptance: no audio persistence/network calls; explicit permission; 90%+ double-clap detection target in quiet room is a performance benchmark, not a guarantee.

### 7.3 Companion overlay

- Compact, keyboard-first, appears near screen center/current display.
- States: idle, listening, thinking, answer, confirmation, error/offline.
- Quick actions: Ask, Save context, Add assignment, Start focus block, Generate questions, Show today.
- Show retrieved sources and planned writes before action confirmation.
- Voice and text always produce same structured intent.

## 8. Dashboard Requirements

### 8.1 Home

- Today’s next action and active focus block.
- Pending/overdue/soon assignments.
- Study streak and focused minutes.
- Subject strength cards with evidence count and trend.
- Due reviews and recommended practice.
- Pathway signals with confidence and “why this signal” drawer.

### 8.2 Subjects

For each subject: mastery heatmap/concept graph, current unit, recent attempts, strengths/gaps, pending assignments, saved context, review queue, and generate-practice action.

### 8.3 Assignments

List/table/calendar views. Fields: status, subject, deadline, estimated/actual effort, subtasks, source brief, risk level. Student can complete, reschedule, or edit. AI-generated task breakdown is always editable before saving.

### 8.4 Memory

Search saved notes/resources/goals by subject and date. Each result shows source, saved time, graph-derived links, and delete/export control. Provide “forget this” hard delete and graph retraction job.

### 8.5 Schedule

Daily/weekly blocks, available hours, completed blocks, conflicts, reschedule. Every block has a reason and manual override.

### 8.6 Insights and pathways

Show only measured signals: accuracy, persistence, response time trend, chosen subjects, completed work, and data volume. State uncertainty. Example: “Math pattern reasoning appears strong: 82% across 46 recent attempts. This is a signal to explore, not a career recommendation.”

## 9. Data Model

Use UUID primary keys, `created_at`, `updated_at`, and tenant/user scope on all user data. Store timestamps UTC.

### Identity and permissions

- `users(id, email?, display_name, birth_year?, created_at, deleted_at)`
- `profiles(user_id, grade_level, timezone, study_preferences_json)`
- `user_relationships(id, owner_user_id, learner_user_id, role[parent|teacher], permission_scope, status)` — deferred UI, create now for safe extension.
- `devices(id, user_id, name, public_key?, last_seen_at)`

### Curriculum and content

- `subjects(id, slug, title, owner_user_id nullable, archived_at)`
- `concepts(id, subject_id, key, title, description, difficulty, active)`
- `concept_edges(concept_id, prerequisite_concept_id)` — validate DAG in service/migration.
- `content_items(id, concept_id, kind[lesson|question|hint|explanation], body_json, difficulty, status[draft|reviewed|published|retired], source_id, version)`
- `question_versions(id, content_item_id, answer_spec_json, distractors_json, generator_metadata_json)`
- `content_reviews(id, content_item_id, reviewer_id, decision, notes, created_at)`

### Learning state

- `learning_events(id, idempotency_key, user_id, subject_id, concept_id, question_version_id, correct, response_time_ms, hint_used, retry_count, client_created_at, received_at)`
- `mastery_state(user_id, concept_id, p_learned, attempts, last_event_at, parameter_set_id, updated_at, version)`
- `bkt_parameter_sets(id, concept_id nullable, p_l0, p_t, p_s, p_g, effective_from, rationale)`
- `review_state(user_id, concept_id, due_at, interval_days, ease_factor, repetitions, last_review_at)`
- `mastery_snapshots(id, user_id, concept_id, p_learned, observed_at)`
- `recommendations(id, user_id, kind, concept_id, content_item_id, reason_json, status, created_at, acted_at)`

### Work and planning

- `assignments(id, user_id, subject_id, title, description, due_at, estimate_minutes, status[pending|in_progress|done|archived], source_memory_id nullable)`
- `assignment_tasks(id, assignment_id, title, estimate_minutes, status, due_at, position, generated_by)`
- `availability_windows(id, user_id, weekday, start_local_time, end_local_time)`
- `study_blocks(id, user_id, starts_at, ends_at, subject_id, assignment_task_id nullable, concept_id nullable, reason_json, status[planned|active|done|skipped])`
- `goals(id, user_id, subject_id nullable, title, target_date, status)`

### Memory and provenance

- `sources(id, user_id, type[note|upload|url|chat|assignment|generated], uri_or_path, title, checksum, captured_at, deleted_at)`
- `memory_episodes(id, user_id, subject_id nullable, group_id, kind, content, source_id nullable, graph_status, created_at, deleted_at)`
- `memory_retrieval_log(id, user_id, query_hash, episode_ids_json, graph_fact_ids_json, used_for)` — metadata only; no raw speech.
- `consents(id, user_id, kind, granted_at, revoked_at, device_id)`

### Graphiti mapping

- Use Graphiti group IDs scoped to owner: `user:{user_id}` and optionally `user:{user_id}:subject:{subject_id}`.
- Ingest an episode for saved note, assignment, corrected fact, goal, resource, or approved conversation summary.
- Episode metadata includes source ID, subject ID, visibility, created time, and user-provided confidence.
- Graph facts must retain episode/source reference. Deletion sends graph retraction/deletion job and marks DB source deleted before physical cleanup.
- Graphiti retrieval is augmentation, not canonical state. Canonical assignments/mastery/schedules always come from Postgres.

## 10. API Surface

Prefix all endpoints `/v1`. Use OAuth/session authentication before real multi-user release. All write endpoints accept `Idempotency-Key`.

### Core

- `GET /health`
- `GET /me`
- `GET /dashboard` — aggregated home read model.
- `GET /subjects`
- `GET /subjects/{subject_id}`
- `GET /subjects/{subject_id}/concepts`

### Assistant

- `POST /assistant/messages` — `{message, mode[text|voice], active_subject_id?}`; returns streamed cited response, tool proposals, and suggested actions.
- `POST /assistant/actions/{action_id}/confirm` — confirms proposed write/tool action.
- `POST /assistant/actions/{action_id}/reject`

### Memory

- `POST /memory/episodes`
- `GET /memory/search?q=&subject_id=&from=&to=`
- `GET /memory/episodes/{id}`
- `DELETE /memory/episodes/{id}`
- `GET /memory/export`

### Assignments and planning

- `GET|POST /assignments`
- `GET|PATCH|DELETE /assignments/{id}`
- `POST /assignments/{id}/extract-tasks` — draft only; no automatic persistence.
- `POST /study-plan/generate`
- `GET|PATCH /study-blocks`
- `POST /study-blocks/{id}/complete`

### Learning

- `POST /learning/events`
- `GET /learning/next`
- `GET /learning/mastery`
- `GET /learning/reviews/due`
- `POST /practice/generate` — returns validation-safe question draft/approved question.
- `POST /practice/{question_id}/answer`

### Analytics

- `GET /analytics/subjects`
- `GET /analytics/time-on-task?from=&to=`
- `GET /analytics/streaks`
- `GET /analytics/pathway-signals`

## 11. Assistant and AI Design

### 11.1 Intent router

Classify request into: answer question, save memory, create assignment, plan study, practise, explain concept, show dashboard, manage schedule, or general chat. Router returns strict structured JSON. It may propose but cannot directly mutate data.

### 11.2 Allowed tools

- Search subject memory
- Read assignments/tasks/schedule/mastery
- Draft assignment task breakdown
- Create question draft
- Retrieve approved curriculum content
- Draft schedule blocks
- Save memory/create assignment/create study block only after confirmation

### 11.3 RAG retrieval pipeline

1. Parse intent, user, subject, and time context.
2. Query canonical Postgres state first for assignments/mastery/schedule.
3. Query pgvector/source chunks with mandatory `user_id` filter.
4. Query Graphiti temporal graph in user/subject group for current and historical facts.
5. Deduplicate/rerank by source relevance, recency, graph relation, subject, and user confidence.
6. Build compact context containing source IDs, date, and permission scope.
7. LLM answers only with supplied context or explicitly states uncertainty.
8. Return citations to saved source/assignment/note; record retrieval metadata.

### 11.4 Generated content guardrails

- Questions require machine-checkable answer specification for objective practice.
- Generated question has difficulty, concept IDs, solution rationale, distractor rationale, and source/provenance metadata.
- Check duplicate similarity, answer validity, grade level, and harmful/irrelevant content before serving.
- Curriculum content is draft until adult/admin review marks it published.
- When student asks for homework completion, default to explanation, hints, examples, and review rather than presenting work as student-authored submission.

## 12. Learning and Recommendation Algorithms

### 12.1 BKT per concept

Store parameters per curriculum/concept: `P(L0)`, `P(T)`, `P(S)`, `P(G)`.

Given current `P(L)` and answer correctness:

- Correct: `posterior = P(L)(1-P(S)) / [P(L)(1-P(S)) + (1-P(L))P(G)]`
- Incorrect: `posterior = P(L)P(S) / [P(L)P(S) + (1-P(L))(1-P(G))]`
- Learning transition: `P(L_next) = posterior + (1-posterior)P(T)`

Use a database transaction: insert idempotent event → lock/version mastery row → calculate → update mastery + snapshot → create recommendation. Do not use LLM judgement as mastery state.

Initial thresholds:

- `P(L) < 0.60`: alternate explanation/question at same concept.
- `0.60 ≤ P(L) < 0.85`: continue concept; periodically insert prerequisite or due review.
- `P(L) ≥ 0.85`: mark temporarily mastered, unlock valid successor, schedule first review.

Response time, hints, and retries do not alter Bayes correctness update directly. Use them for pacing flags and recommendation explanation: repeated hints/slow answers trigger simpler explanation, smaller block, or prerequisite review.

### 12.2 Spaced review

Use SM-2-style state: interval, ease factor, repetitions, due date. A low-quality review shortens interval. A correct but slow/hint-heavy review receives lower quality than fast independent success. Store rules/version so changes are explainable.

### 12.3 Scheduler

Calculate candidate work blocks from:

1. Overdue and near-deadline assignment tasks.
2. Reviews due today.
3. Weak, prerequisite-blocking concepts.
4. Student goals and available windows.
5. Balance across subjects and maximum daily workload.

Score each candidate deterministically, e.g. deadline urgency + review urgency + mastery gap + goal value − fatigue/repetition penalty. Split into 15–45 minute blocks. Persist `reason_json`, such as `{"deadline_days":1,"remaining_minutes":80,"priority":"high"}`.

### 12.4 Pathway signals

Only derive after sufficient data (default: at least 20 attempts across 3 sessions per subject). Show subject-level strengths, persistence, and interest signals with low/medium/high confidence. Never infer immutable ability, diagnose, rank students, or claim a career outcome. Offer exploration actions instead.

## 13. Initial Curriculum Seed

Math subject, 15-node DAG:

1. Whole numbers and place value
2. Division as sharing — prerequisite 1
3. Fraction as part of a whole — prerequisite 2
4. Numerator and denominator — prerequisite 3
5. Equivalent fractions — prerequisite 4
6. Comparing fractions — prerequisite 5
7. Simplifying fractions — prerequisite 5
8. Add/subtract like denominators — prerequisite 4
9. Add/subtract unlike denominators — prerequisites 5, 8
10. Multiply fractions — prerequisite 4
11. Divide fractions — prerequisite 10
12. Decimal place value — prerequisite 1
13. Fractions as decimals — prerequisites 5, 12
14. Decimals as percentages — prerequisite 13
15. Percentage of a quantity — prerequisite 14

Seed at least one short lesson, three varied questions, answer keys, and explanations per node. The decimal place-value ordering intentionally precedes fractions-as-decimals.

## 14. Security, Privacy, and Data Lifecycle

- Require explicit microphone permission; app functions fully with keyboard/text when denied.
- No raw microphone audio persistence or upload for double-clap detection.
- Store API keys in OS keychain, never frontend bundle/database/plaintext config.
- Encrypt transport; encrypt cloud data at rest when sync ships. Protect local application data using OS account permissions and optional app lock.
- Scope every query by authenticated user ID; no user-controlled owner IDs accepted for access control.
- Validate upload size/type; malware scan before ingestion; do not execute imported files.
- Rate-limit AI and write endpoints; audit confirmed actions.
- Offer delete memory/source, delete account, and export structured data. Graph-delete jobs must be tracked/retryable.
- Do not use third-party behavioural analytics by default. Product telemetry must be opt-in, anonymous/minimal, and separate from study content.

## 15. Repository Layout

```text
fastlearner/
  apps/
    desktop/                 # Tauri v2 + React companion/dashboard
      src/
      src-tauri/
    web-dashboard/           # optional read-only React deployment
  services/
    api/
      app/
        api/ domain/ services/ repositories/ workers/
        learning/ memory/ assistant/ planner/
      alembic/
      tests/
  packages/
    ui/                      # shared React components
    contracts/               # generated OpenAPI/TypeScript contracts
  infra/
    docker-compose.yml       # Postgres, Neo4j, Redis local dev
  docs/
```

## 16. Configuration

Do not commit secrets. Provide `.env.example` with:

```text
DATABASE_URL=postgresql+psycopg://fastlearner:fastlearner@localhost:5432/fastlearner
REDIS_URL=redis://localhost:6379/0
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=change-me
AI_PROVIDER=openai
AI_MODEL=
AI_API_KEY=
EMBEDDING_MODEL=
APP_ENCRYPTION_KEY=
VITE_API_URL=http://localhost:8000/v1
```

## 17. Build Order and Acceptance Criteria

### Phase 0 — foundation

- Create monorepo, Docker local services, migrations, lint/format/test tooling, CI, `.env.example`.
- Implement auth-local development mode and typed API contracts.
- Acceptance: one command starts desktop/API/dependencies; CI runs unit tests and type checks.

### Phase 1 — core state and dashboard

- Implement subjects, assignments, tasks, events, dashboard read model, source storage.
- Build dashboard Home, Subjects, Assignments, Schedule skeleton.
- Acceptance: student can create/complete assignment; dashboard updates from persisted data after restart.

### Phase 2 — planner and explainability

- Implement availability, task breakdown draft/confirmation, deterministic scheduler, reason payloads.
- Acceptance: user creates 90-minute plan; every block displays exact reason; edits persist.

### Phase 3 — desktop companion

- Build Tauri tray, overlay, global shortcut, permission UI, keyboard flow.
- Implement native double-clap detector with local tests/audio fixtures and privacy controls.
- Acceptance: closed main window remains tray-resident; global shortcut works; double-clap opens overlay with no outbound request until user asks.

### Phase 4 — memory/RAG

- Implement source ingestion, subject scoping, pgvector retrieval, Graphiti adapter/Neo4j worker, delete/export.
- Acceptance: save three dated notes; assistant answers using relevant citation; deleting note removes it from future retrieval and queues graph deletion.

### Phase 5 — adaptive tutor

- Seed math DAG/content; implement BKT transaction, reviews, next recommendation, practice session UI.
- Acceptance: a simulated learner progresses/repeats according to thresholds; recommendation provides reason; duplicate event never updates mastery twice.

### Phase 6 — AI assistant and quality

- Add provider adapter, streaming assistant, tools/proposals, generated practice validation, citations, academic-integrity modes.
- Acceptance: assistant cannot mutate without confirmation; sourced answer cites stored assignment/note; unsupported claim is marked uncertain.

### Phase 7 — hardening

- Add auth, encrypted sync design, parent/teacher read-only roles, accessibility, performance, monitoring, backup/recovery, release signing.
- Acceptance: authorization tests cover every tenant boundary; accessibility keyboard/screen-reader pass; data export/delete flow works.

## 18. Testing Requirements

- Unit: BKT formulas, scheduler scoring, clap transient state machine, assignment/task lifecycle, permission guards.
- Property/invariant: concept graph acyclic; mastery remains `[0,1]`; idempotent event is applied once; user scope never leaks.
- Integration: Postgres + Neo4j + Redis workers, Graphiti ingestion/retrieval/delete, API contracts.
- End-to-end: add assignment → plan → complete focus block; wake → ask → cited answer; practise → mastery update → recommendation.
- Security: authz/tenant isolation, prompt-injection resistance from imported notes, upload validation, secret scan.
- Performance: dashboard under 1 second for normal student state; retrieval context bounded; wake detector CPU/memory baseline measured in tray mode.

## 19. Observability

Capture structured, privacy-minimal logs for request ID, user pseudonym, endpoint, latency, model/provider, token/cost bucket, retrieval count, graph-worker status, and recommendation rule. Do not log raw voice, full student notes, or secrets. Track failed graph ingestion/delete jobs with retry and visible sync status.

## 20. Definition of Done for MVP

MVP is done when one student can install/open desktop app, opt into local wake detection, capture subject context, add an assignment, receive and edit a study plan, practise seeded math concepts, see explainable mastery/dashboard updates, ask a cited question against saved context, search/delete/export memory, and restart app without losing state. All generated changes require confirmation; raw wake audio never leaves device.
