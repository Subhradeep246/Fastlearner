# Adaptive Learning App — Build Specification

## 1. Objective

Build a desktop application that teaches any topic to a learner (initial focus: children, grades 3–5 math) by:
- Estimating the learner's per-concept mastery and pace ("grasping power") from their interactions
- Adapting content difficulty, review frequency, and sequencing in real time based on that estimate
- Sourcing and restructuring the best available material for a topic into consistent, bite-sized lessons
- Feeding all interaction data to a companion web dashboard so a parent/teacher/the learner can see progress analytics

MVP scope: elementary math, fractions → decimals → percentages, as one connected concept graph (~15 nodes). Prove the adaptive loop end-to-end on this narrow domain before generalizing to "any topic."

---

## 2. Functional Requirements

**Desktop app (learner-facing)**
- Present one concept at a time: short lesson content, then practice questions
- Record every interaction: correctness, response time, hint usage, retry count
- Request "what's next" from the backend after each interaction and render it
- Show the learner their own progress (streaks, mastery badges) at a simple level

**Backend**
- Serve concept DAG + lesson content + questions to the desktop app
- Log every interaction event durably
- Run the mastery/pacing update after each event and return the next recommended concept
- Expose read-only endpoints for the analytics dashboard (mastery over time, time-on-task, streaks, concept-level struggle points)
- Provide an admin/content pipeline path to ingest and restructure new source material into the lesson format

**Analytics dashboard (web)**
- Per-learner view: mastery heatmap across the concept DAG, time spent, trend over time
- Highlight concepts where the learner is stuck (repeated failures, long dwell time)
- No editing/write access — read-only consumer of backend data

---

## 3. Non-Functional Requirements
- Explainability: mastery estimates must be inspectable — no black-box "why did it recommend this" answers
- Works offline-first on the desktop app for the learning session itself; syncs events when connectivity is available
- Content pipeline output must be reviewable by a human before going live (no fully-automated unreviewed content to children)
- Privacy: learner data (esp. if under 13) stored with minimal PII, no third-party analytics trackers

---

## 4. Architecture

```
        Desktop app (Tauri + React)      Analytics dashboard (React web)
                    \                         /
                     \                       /
                   FastAPI backend (API + orchestration)
                    /            |            \
              Postgres      BKT engine      Claude API
        (state + concept   (mastery +     (content restructuring
              DAG)          pacing logic)   + question generation)
```

- **Desktop app** — Tauri + React. Renders lessons/questions, captures interaction events, calls backend. No mastery logic locally.
- **FastAPI backend** — central orchestrator. Three jobs: serve content, log events, call the BKT engine and return next-step recommendations.
- **Postgres** — stores the concept DAG (nodes + prerequisite edges), per-user mastery state (one row per concept per user), and the full raw event log (source of truth for analytics).
- **BKT engine** — Bayesian Knowledge Tracing. Deterministic, explainable, needs no training data. Updates a per-concept mastery probability after every event and decides: advance, repeat, or insert review.
- **Claude API** — used only for (a) turning raw source material into consistently formatted lesson content, and (b) generating practice questions at a target difficulty. Never used to decide mastery — keep that in the BKT engine so it stays auditable.
- **Analytics dashboard** — separate React app, read-only endpoints against the same Postgres/backend.

---

## 5. Learner Model — Method (Bayesian Knowledge Tracing)

Why BKT over a neural approach (e.g. Deep Knowledge Tracing): no training data exists on day one, BKT parameters can be reasonably hand-set, and its output is a simple explainable probability rather than a black-box score. Revisit DKT only once real interaction logs exist at scale.

Per concept, track four parameters:
- `P(L0)` — probability the learner already knows the concept before any practice
- `P(T)` — probability of transitioning from "not known" to "known" after one practice opportunity
- `P(S)` — probability of a "slip" (answering wrong despite knowing it)
- `P(G)` — probability of a "guess" (answering right despite not knowing it)

Update rule after each observed response (correct/incorrect):
1. Compute `P(L | evidence)` using Bayes' rule given the observed correctness and current `P(L)`
2. Apply the learning transition: `P(L_next) = P(L | evidence) + (1 − P(L | evidence)) × P(T)`
3. Use `P(L_next)` as the mastery estimate for that concept going forward

Pacing rules on top of the mastery estimate:
- `P(L) < 0.6` → repeat concept with a different question
- `0.6 ≤ P(L) < 0.85` → continue at same concept, mix in occasional review of prerequisite concepts
- `P(L) ≥ 0.85` → advance to next unlocked concept in the DAG

For the "memorize" dimension (not just "understand"), layer a spaced-repetition schedule (SM-2 style) on top of mastered concepts so they get revisited at increasing intervals instead of being dropped once `P(L)` crosses threshold.

---

## 6. Concept DAG — MVP Content (fractions → percentages)

15-node slice, prerequisites in parentheses:

1. Whole numbers & place value
2. Division as sharing (1)
3. Concept of a fraction — parts of a whole (2)
4. Numerator/denominator meaning (3)
5. Equivalent fractions (4)
6. Comparing fractions (5)
7. Simplifying fractions (5)
8. Adding/subtracting like denominators (4)
9. Adding/subtracting unlike denominators (5, 8)
10. Multiplying fractions (4)
11. Dividing fractions (10)
12. Fractions as decimals (5)
13. Decimal place value (12)
14. Decimals as percentages (13)
15. Percentage of a quantity (14)

Encode as a seed file (JSON or SQL) with `concept_id`, `title`, `prerequisite_ids[]`.

---

## 7. Data Model (Postgres, minimum viable schema)

- `concepts(id, title, prerequisite_ids[])`
- `content_items(id, concept_id, type[lesson|question], body, difficulty)`
- `users(id, role[learner|parent|teacher], ...)`
- `mastery(user_id, concept_id, p_learned, updated_at)`
- `events(id, user_id, concept_id, content_item_id, correct, response_time_ms, hint_used, retry_count, created_at)`

---

## 8. API Surface (FastAPI, minimum viable)

- `GET /concepts` — full DAG
- `GET /users/{id}/next` — next recommended concept + content item, based on current mastery
- `POST /events` — log an interaction event; triggers BKT update
- `GET /users/{id}/mastery` — current mastery state across all concepts (used by dashboard)
- `GET /users/{id}/events?range=` — raw event history (used by dashboard for time-on-task, trends)
- `POST /admin/content/ingest` — feed raw source material through Claude API restructuring, output held for human review before publishing

---

## 9. Build Order (recommended milestones)

1. Hand-seed the 15-node concept DAG + a handful of lesson/question content items directly in Postgres — skip content pipeline for now
2. Build FastAPI endpoints for `/concepts`, `/events`, `/users/{id}/next` with BKT engine wired in
3. Build the desktop app (Tauri + React) against these endpoints — get one learner able to go through the full DAG end-to-end
4. Build the analytics dashboard reading `/users/{id}/mastery` and `/users/{id}/events`
5. Only after the above works end-to-end: build the Claude API content-ingestion pipeline to generalize beyond the hand-seeded 15 concepts
6. Only after that: generalize the concept DAG structure to support arbitrary topics, not just this one math slice

---

## 10. Explicit Non-Goals for MVP
- No support for arbitrary/open-ended topics yet (fixed to the fractions→percentages slice)
- No essay/free-text grading (all content is objectively gradable — multiple choice, numeric answer)
- No mobile app (desktop + web dashboard only)
- No fully automated content publishing (human review gate stays in place)
