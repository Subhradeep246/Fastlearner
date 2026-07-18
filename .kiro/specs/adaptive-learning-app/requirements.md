# Requirements Document

## Introduction

FastLearner is an internet-connected adaptive learning system for students in grades 3–12. The complete product combines a macOS-first desktop companion, a separately deployable web dashboard, parent and teacher read-only access, durable local-first learner context, assignment planning, adaptive tutoring, explainable recommendations, and evidence-based pathway signals. The initial adaptive curriculum covers grades 3–5 fractions, decimals, and percentages while supporting future subjects and curriculum packs.

The existing foundation is an established baseline, not outstanding product work. The baseline substantially implements the npm, Python, and Rust workspaces and toolchains; React desktop and web shells; Tauri shell; FastAPI health service; shared UI, content, and generated-contract packages; conformance support; and continuous-integration checks. The requirements below preserve and extend that baseline to the complete product.

## Glossary

- **Adaptive_Learning_System**: The complete FastLearner product, including client applications, application services, data stores, workers, and shared contracts.
- **Student**: The learner who owns a FastLearner profile and associated study data.
- **Authorized_Observer**: A parent or teacher granted active, scoped, read-only access to a Student profile.
- **Desktop_Companion**: The Tauri and React desktop client that provides tray, wake, overlay, dashboard, permission, and notification experiences.
- **Web_Dashboard**: The separately deployable React client that consumes the same read-only API and shared user-interface contracts as the Desktop_Companion.
- **Application_API**: The authenticated, versioned FastAPI interface used by approved clients.
- **Wake_Service**: The on-device native component that detects a configured double clap from transient microphone energy.
- **Assistant_Service**: The service that routes intents, retrieves permitted context, proposes tools, and produces cited responses.
- **Memory_Service**: The service that ingests, retrieves, exports, retracts, and deletes subject-scoped learner context with provenance.
- **Curriculum_Service**: The service that owns concepts, prerequisite relationships, reviewed content, and versioned questions.
- **Learning_Service**: The deterministic service that records learning events, updates mastery and review state, and recommends learning actions.
- **Planner_Service**: The deterministic service that manages tasks, availability, goals, schedules, and reasoned study blocks.
- **Analytics_Service**: The read-only service that produces dashboard aggregates, trends, streaks, focused time, and pathway signals.
- **Authorization_Service**: The service that authenticates callers and enforces learner ownership and observer permission scopes.
- **AI_Provider_Adapter**: The vendor-neutral interface for language models, embeddings, and optional speech services, initially configured for OpenAI.
- **Canonical_State**: Authoritative relational data for identity, assignments, curriculum, learning state, schedules, consent, and provenance.
- **Memory_Episode**: A deliberately saved note, assignment, correction, goal, resource, or approved conversation summary.
- **Source_Record**: Metadata identifying the origin, capture time, checksum, ownership, and lifecycle state of saved or generated content.
- **Graph_Memory**: Subject- and owner-scoped temporal entities and relationships derived through the Graphiti adapter and stored separately from Canonical_State.
- **Retrieval_Context**: A bounded set of authorized canonical records, source chunks, Memory_Episodes, and Graph_Memory facts supplied for response generation.
- **Provenance**: Source, evidence, date, rule, confidence, and ownership information supporting an output.
- **BKT**: Bayesian Knowledge Tracing using versioned initial knowledge, transition, slip, and guess parameters.
- **Spaced_Review**: Versioned review scheduling based on interval, ease factor, repetitions, due date, and response quality.
- **Concept_DAG**: A directed acyclic graph of concepts and prerequisite relationships.
- **Study_Block**: A 15-to-45-minute scheduled unit of assignment, review, goal, or concept work with a persisted reason.
- **Pathway_Signal**: A non-deterministic, evidence-backed indication of a subject strength, persistence pattern, or interest for exploration.
- **Confirmed_Action**: A proposed mutation explicitly accepted by the Student before persistence.
- **Foundation_Baseline**: The substantially implemented workspaces, toolchains, shells, health service, shared packages, contracts, and continuous-integration capability described in the introduction.

## Requirements


### Requirement 1: Foundation continuity and release portability

**User Story:** As a developer, I want the established foundation retained and extended, so that product delivery builds on validated work rather than recreating the scaffold.

#### Acceptance Criteria

1. THE Adaptive_Learning_System SHALL treat the Foundation_Baseline as an existing implementation baseline.
2. THE Adaptive_Learning_System SHALL preserve compatible npm, Python, and Rust workspace toolchains for product extensions.
3. THE Adaptive_Learning_System SHALL preserve the existing React desktop shell, React web shell, Tauri shell, FastAPI health service, shared packages, generated contracts, conformance support, and continuous-integration checks.
4. THE Adaptive_Learning_System SHALL deliver the first signed desktop release for macOS.
5. THE Desktop_Companion SHALL isolate operating-system integrations behind interfaces that support Windows and Linux implementations.
6. WHEN a supported client consumes the Application_API, THE Adaptive_Learning_System SHALL use shared versioned contracts across desktop and web clients.
7. WHERE a Windows or Linux build is configured, THE Desktop_Companion SHALL provide platform-specific tray, shortcut, permission, secure-storage, and notification adapters without changing domain-service contracts.
8. IF a Foundation_Baseline check fails, THEN THE Adaptive_Learning_System SHALL report the affected workspace, command, and failure outcome through the existing validation tooling.

### Requirement 2: Learner identity, ownership, and observer access

**User Story:** As a Student, I want ownership and access controls around study data, so that personal learning information remains under my control.

#### Acceptance Criteria

1. THE Adaptive_Learning_System SHALL support one account-local Student as the initial data owner.
2. THE Adaptive_Learning_System SHALL associate every user-data record with a UUID owner scope, creation timestamp, update timestamp, and UTC time representation.
3. THE Adaptive_Learning_System SHALL maintain profile grade level, timezone, and study preferences for each Student.
4. THE Adaptive_Learning_System SHALL maintain device registrations and parent or teacher relationship records with role, permission scope, and lifecycle status.
5. WHEN an Authorized_Observer accesses a learner view, THE Authorization_Service SHALL restrict returned data to the active relationship permission scope.
6. WHILE an Authorized_Observer session is active, THE Authorization_Service SHALL permit read operations and return an authorization error for learner-data mutations.
7. IF a relationship is inactive, expired, absent, or outside the requested scope, THEN THE Authorization_Service SHALL return an authorization error without learner data.
8. IF authentication is absent or invalid, THEN THE Authorization_Service SHALL return an authentication error without learner data.
9. WHEN a Student revokes an observer relationship, THE Authorization_Service SHALL end subsequent observer access for the revoked relationship.
10. WHEN a caller supplies an owner identifier, THE Authorization_Service SHALL derive the effective owner scope from the authenticated identity and active relationship rather than the supplied identifier.

### Requirement 3: Desktop tray and background companion

**User Story:** As a Student, I want FastLearner available from the desktop background, so that study help is accessible without keeping a full window open.

#### Acceptance Criteria

1. THE Desktop_Companion SHALL provide tray actions for opening the dashboard, waking the companion, selecting microphone settings, pausing wake listening, viewing synchronization status, and quitting.
2. WHEN the Student closes the main window, THE Desktop_Companion SHALL remain available in the system tray.
3. WHEN the Student selects Quit, THE Desktop_Companion SHALL terminate the desktop process and active microphone stream.
4. THE Desktop_Companion SHALL register `Cmd/Ctrl + Shift + Space` as the default wake shortcut.
5. WHERE start-at-login is enabled by the Student, THE Desktop_Companion SHALL start in background mode at operating-system login.
6. WHILE wake listening is active, THE Desktop_Companion SHALL display a persistent local listening indicator.
7. WHEN the Student pauses wake listening, THE Desktop_Companion SHALL stop the microphone stream before reporting the paused state.
8. IF microphone permission is denied or unavailable, THEN THE Desktop_Companion SHALL provide the complete keyboard and text interaction flow.
9. IF tray initialization fails, THEN THE Desktop_Companion SHALL keep the main-window keyboard and text flow available and display a recoverable error.

### Requirement 4: Local double-clap wake and speech capture

**User Story:** As a Student, I want a privacy-preserving double-clap wake option, so that I can open the companion hands-free.

#### Acceptance Criteria

1. THE Wake_Service SHALL process mono microphone frames on the local device without persisting or transmitting raw wake audio.
2. THE Wake_Service SHALL evaluate transient energy in 10-to-30-millisecond analysis windows against an adaptive noise floor.
3. WHEN two valid clap-like transients occur 120 to 900 milliseconds apart, THE Wake_Service SHALL emit one wake event.
4. WHEN the Wake_Service emits a wake event, THE Wake_Service SHALL apply a configured cooldown from 1.5 to 3 seconds.
5. WHILE the cooldown is active, THE Wake_Service SHALL suppress additional wake events.
6. WHEN a local wake event or keyboard wake occurs, THE Desktop_Companion SHALL show visible wake confirmation before speech recording begins.
7. WHEN visible wake confirmation is shown and microphone permission is active, THE Desktop_Companion SHALL allow the Student to begin speech capture.
8. WHILE the Wake_Service evaluates wake audio, THE Wake_Service SHALL make zero network and AI-provider requests.
9. THE Wake_Service SHALL provide settings for sensitivity, double-clap interval, microphone device, keyboard-only mode, and wake pause.
10. WHERE local diagnostics are enabled by the Student, THE Wake_Service SHALL store false-positive and false-negative diagnostic metadata without raw audio.
11. IF microphone permission is revoked or the selected device becomes unavailable, THEN THE Wake_Service SHALL stop wake listening and report the unavailable state.
12. WHEN evaluated in the documented quiet-room benchmark setup, THE Wake_Service SHALL detect at least 90 percent of benchmark double-clap samples and label the measured result as a benchmark rather than a field guarantee.

### Requirement 5: Companion overlay and intent parity

**User Story:** As a Student, I want a compact assistant overlay for voice or text, so that common study actions require minimal interruption.

#### Acceptance Criteria

1. THE Desktop_Companion SHALL provide overlay states for idle, listening, thinking, answer, confirmation, offline, and error conditions.
2. WHEN the overlay opens, THE Desktop_Companion SHALL position the overlay near the center of the current display and focus the primary keyboard control.
3. THE Desktop_Companion SHALL offer quick actions for asking a question, saving context, adding an assignment, starting a focus block, generating questions, and showing today.
4. WHEN voice or text expresses equivalent content, THE Assistant_Service SHALL produce the same structured intent category and proposed action type.
5. WHEN a response uses Retrieval_Context, THE Desktop_Companion SHALL display the associated sources with the response.
6. WHEN the Assistant_Service proposes a mutation, THE Desktop_Companion SHALL display the proposed writes before requesting confirmation.
7. IF connectivity required for an AI request is unavailable, THEN THE Desktop_Companion SHALL show the offline state while preserving available local dashboard and editing functions.
8. IF submitted voice or text contains no usable content, THEN THE Desktop_Companion SHALL remain in the input state and request content without creating an assistant action.
9. IF intent processing fails, THEN THE Desktop_Companion SHALL enter the error state and provide retry or text-entry recovery.

### Requirement 6: Student and observer dashboards

**User Story:** As a Student or Authorized_Observer, I want an evidence-based dashboard, so that I can understand current work, progress, and recommended next actions.

#### Acceptance Criteria

1. THE Desktop_Companion SHALL provide home, subjects, assignments, memory, schedule, insights, and pathway views.
2. THE Web_Dashboard SHALL be deployable independently from the Desktop_Companion.
3. THE Web_Dashboard SHALL consume the same read-only Application_API contracts and shared user-interface components as the desktop dashboard.
4. WHEN a Student opens Home, THE Analytics_Service SHALL provide the next action, active focus block, assignment status, study streak, focused minutes, subject evidence trends, due reviews, recommended practice, and Pathway_Signals.
5. WHEN a Student opens a subject, THE Analytics_Service SHALL provide the mastery map, current unit, recent attempts, strengths, gaps, assignments, saved context, review queue, and practice action for the selected subject.
6. WHEN a Student opens assignments, THE Desktop_Companion SHALL provide list, table, and calendar presentations of assignment status, subject, deadline, effort, subtasks, source brief, and risk.
7. WHEN a Student opens memory, THE Desktop_Companion SHALL provide subject and date search with source, saved time, graph-derived links, delete, and export controls.
8. WHEN a Student opens schedule, THE Desktop_Companion SHALL provide daily and weekly blocks, availability, completion state, conflicts, rescheduling, reasons, and manual overrides.
9. WHILE an Authorized_Observer views a dashboard, THE Web_Dashboard SHALL label the experience as read-only and omit mutation controls.
10. WHEN dashboard data changes through a Confirmed_Action, THE Analytics_Service SHALL return updated persisted aggregates after refresh and application restart.
11. IF a dashboard collection contains zero records, THEN THE Analytics_Service SHALL return an empty collection for the authorized scope.
12. IF the Desktop_Companion receives an empty dashboard collection, THEN THE Desktop_Companion SHALL present a labeled empty state and an authorized next action.
13. IF the Web_Dashboard receives an empty dashboard collection, THEN THE Web_Dashboard SHALL present a labeled read-only empty state.
14. IF an Authorized_Observer requests dashboard data outside the active permission scope, THEN THE Authorization_Service SHALL return an authorization error without out-of-scope aggregates.
15. IF the Application_API is unavailable, THEN THE Web_Dashboard SHALL present an unavailable state without presenting cached data as current.

### Requirement 7: Subjects, assignments, tasks, and goals

**User Story:** As a Student, I want to manage subjects and schoolwork, so that deadlines and learning goals remain organized.

#### Acceptance Criteria

1. THE Adaptive_Learning_System SHALL support school-managed subjects, learner-created subjects, and archived subjects.
2. WHEN a Student creates an assignment, THE Adaptive_Learning_System SHALL capture subject, title, due date, estimated effort, pending status, and optional brief or rubric Source_Record.
3. WHEN a Student starts an assignment, THE Adaptive_Learning_System SHALL transition the assignment from pending to in-progress status.
4. WHEN a Student completes an assignment, THE Adaptive_Learning_System SHALL transition the assignment to done status.
5. WHEN a Student archives an assignment, THE Adaptive_Learning_System SHALL transition the assignment to archived status.
6. WHEN a Student edits or reschedules an assignment, THE Adaptive_Learning_System SHALL persist the changed fields and update time.
7. WHEN a Student records actual effort, THE Adaptive_Learning_System SHALL associate the effort with the assignment or completed Study_Block.
8. WHEN the Assistant_Service extracts tasks from an assignment brief, THE Assistant_Service SHALL return an editable draft containing task titles, effort estimates, due dates, ordering, and generation Provenance.
9. WHILE an extracted task breakdown remains unconfirmed, THE Adaptive_Learning_System SHALL exclude the draft from Canonical_State.
10. WHEN the Student confirms an edited task breakdown, THE Adaptive_Learning_System SHALL persist the Confirmed_Action once.
11. IF a confirmed task breakdown repeats the same idempotency key, THEN THE Adaptive_Learning_System SHALL return the original outcome without creating duplicate tasks.
12. IF required assignment data is absent or invalid, THEN THE Adaptive_Learning_System SHALL return field-specific validation errors without changing Canonical_State.
13. THE Adaptive_Learning_System SHALL support subject-scoped and cross-subject goals with target date and lifecycle status.
14. WHEN an assignment is deleted, THE Adaptive_Learning_System SHALL preserve applicable audit and Source_Record lifecycle information while excluding the assignment from active work views.

### Requirement 8: Deterministic study planning

**User Story:** As a Student, I want an editable study schedule with exact reasons, so that I can balance deadlines, review, mastery gaps, goals, and availability.

#### Acceptance Criteria

1. THE Planner_Service SHALL calculate candidate work from overdue tasks, near-deadline tasks, due reviews, prerequisite-blocking concepts, goals, availability, subject balance, and configured maximum daily workload.
2. THE Planner_Service SHALL score each candidate using versioned deterministic deadline-urgency, review-urgency, mastery-gap, goal-value, fatigue, and repetition rules.
3. THE Planner_Service SHALL resolve equal candidate scores using a versioned deterministic tie-break rule.
4. WHEN the Student requests a plan, THE Planner_Service SHALL split selected work into Study_Blocks of 15 to 45 minutes within available windows.
5. WHEN the Planner_Service creates a Study_Block, THE Planner_Service SHALL persist the exact scoring inputs and human-readable reason in the Study_Block reason data.
6. WHEN a requested workload exceeds available time, THE Planner_Service SHALL identify unscheduled work and explain the limiting availability or workload constraint.
7. WHEN the Student edits, reschedules, skips, starts, or completes a Study_Block, THE Planner_Service SHALL persist the selected planned, active, skipped, or done state and maintain the original reason history.
8. WHEN the Student requests a 90-minute plan with at least 90 available minutes, THE Planner_Service SHALL produce non-overlapping Study_Blocks totaling no more than 90 minutes.
9. WHEN schedule conflicts occur, THE Planner_Service SHALL identify conflicting blocks and request a Student decision before changing Canonical_State.
10. THE Planner_Service SHALL require a Confirmed_Action before writing AI-drafted schedule changes.
11. IF no availability window can contain a Study_Block, THEN THE Planner_Service SHALL return zero new Study_Blocks and identify unavailable scheduling capacity.
12. IF plan generation repeats the same idempotency key, THEN THE Planner_Service SHALL return the original plan outcome without creating duplicate Study_Blocks.
13. IF plan persistence fails, THEN THE Planner_Service SHALL leave the pre-request schedule state unchanged and return a typed failure.

### Requirement 9: Deliberate memory capture and provenance

**User Story:** As a Student, I want to save selected learning context with its origin, so that future help uses durable information without silently retaining every conversation.

#### Acceptance Criteria

1. WHEN the Student invokes save context, THE Memory_Service SHALL capture content, kind, subject, consent, Source_Record, capture time, and optional file reference.
2. WHERE a Student-configured auto-save rule applies to a source or import, THE Memory_Service SHALL capture only content covered by the named rule and consent record.
3. WHILE neither explicit save intent nor a matching auto-save rule exists, THE Memory_Service SHALL keep chat content outside long-term Memory_Episodes.
4. WHEN a Memory_Episode is accepted, THE Memory_Service SHALL assign the Graphiti group `user:{user_id}` or `user:{user_id}:subject:{subject_id}` from the authenticated owner scope.
5. WHEN graph extraction derives a fact, THE Memory_Service SHALL retain the supporting Memory_Episode, Source_Record, subject, visibility, creation time, and user-provided confidence.
6. THE Memory_Service SHALL treat Graph_Memory as retrieval augmentation and Canonical_State as authoritative for assignments, mastery, curriculum, and schedules.
7. WHEN the Student searches memory, THE Memory_Service SHALL filter results by authenticated owner, permitted subject, requested dates, and lifecycle state.
8. WHEN a memory search has no matching permitted records, THE Memory_Service SHALL return an empty result set without broadening owner, subject, date, or lifecycle filters.
9. WHEN the Adaptive_Learning_System generates an answer, schedule, recommendation, memory link, or Pathway_Signal, THE Adaptive_Learning_System SHALL expose applicable Provenance.
10. IF Graph_Memory ingestion fails, THEN THE Memory_Service SHALL retain the accepted local Memory_Episode, record failed synchronization state, and make the ingestion job eligible for retry.
11. IF explicit save content is empty, THEN THE Memory_Service SHALL return a validation error without creating a Memory_Episode or Source_Record.
12. IF a memory write repeats the same idempotency key, THEN THE Memory_Service SHALL return the original outcome without creating a duplicate Memory_Episode.

### Requirement 10: Grounded retrieval and assistant actions

**User Story:** As a Student, I want answers grounded in my authorized records and approved sources, so that study guidance is traceable and safe to act upon.

#### Acceptance Criteria

1. WHEN the Assistant_Service receives a non-empty message, THE Assistant_Service SHALL classify the message as question answering, memory saving, assignment creation, study planning, practice, concept explanation, dashboard display, schedule management, or general chat using structured output.
2. WHEN the Assistant_Service needs learner context, THE Assistant_Service SHALL retrieve Canonical_State before supplementary source chunks and Graph_Memory.
3. WHEN the Memory_Service retrieves source chunks, THE Memory_Service SHALL apply an authenticated owner filter before similarity ranking.
4. WHEN the Memory_Service retrieves Graph_Memory, THE Memory_Service SHALL restrict retrieval to permitted owner and subject groups.
5. THE Memory_Service SHALL deduplicate and rank Retrieval_Context by source relevance, recency, graph relationship, subject, and user confidence.
6. THE Assistant_Service SHALL bound Retrieval_Context by configured record and token limits.
7. WHEN supplied context supports an answer, THE Assistant_Service SHALL cite the relevant assignment, note, Source_Record, or approved curriculum item.
8. IF supplied context does not support a requested factual claim, THEN THE Assistant_Service SHALL state the uncertainty and identify missing evidence.
9. IF authorized retrieval returns no records, THEN THE Assistant_Service SHALL state that no supporting learner records were found without broadening the authorized scope.
10. IF supplementary vector or Graph_Memory retrieval fails, THEN THE Assistant_Service SHALL identify unavailable supplementary context and avoid presenting unsupported claims as grounded.
11. THE Assistant_Service SHALL permit tools for authorized searches, reads, drafts, approved-content retrieval, and proposed actions.
12. WHILE a proposed write lacks Student confirmation, THE Assistant_Service SHALL keep the proposal outside Canonical_State.
13. WHEN the Student confirms a proposed write, THE Assistant_Service SHALL submit one idempotent Confirmed_Action through the responsible domain service.
14. IF confirmation repeats the same idempotency key, THEN THE Assistant_Service SHALL return the original action outcome without repeating the mutation.
15. WHEN the Student rejects a proposed write, THE Assistant_Service SHALL mark the proposal rejected without changing learner Canonical_State.
16. IF a proposal is absent, expired, rejected, already completed, or outside the authenticated owner scope, THEN THE Assistant_Service SHALL return a typed action-state error without changing Canonical_State.
17. IF a message contains no usable content, THEN THE Assistant_Service SHALL return a validation error without invoking the AI_Provider_Adapter.

### Requirement 11: AI provider independence and connected operation

**User Story:** As a product operator, I want OpenAI used through a vendor-neutral abstraction, so that provider changes do not rewrite learning domain logic.

#### Acceptance Criteria

1. THE AI_Provider_Adapter SHALL expose vendor-neutral operations for language generation, embeddings, streaming, structured output, and optional speech services.
2. THE Adaptive_Learning_System SHALL configure OpenAI as the first production AI provider.
3. THE Adaptive_Learning_System SHALL select provider, language model, and embedding model through runtime configuration outside frontend bundles.
4. WHEN a provider response enters a domain workflow, THE AI_Provider_Adapter SHALL normalize content, usage metadata, errors, and structured-output validation into shared contracts.
5. IF the configured provider is unavailable, THEN THE AI_Provider_Adapter SHALL return a typed provider error without modifying Canonical_State.
6. IF provider structured output fails contract validation, THEN THE AI_Provider_Adapter SHALL return a typed validation error without submitting a domain mutation.
7. WHEN another provider implements the AI_Provider_Adapter contract, THE Adaptive_Learning_System SHALL support provider replacement without changing mastery, planning, authorization, or memory lifecycle rules.
8. WHILE connectivity is unavailable, THE Adaptive_Learning_System SHALL identify AI-dependent actions as unavailable and retain local deterministic functions.
9. WHEN connectivity resumes, THE Adaptive_Learning_System SHALL require the Student to retry or confirm any uncompleted AI-dependent action before persistence.
10. THE Adaptive_Learning_System SHALL keep language-model output outside the source-of-truth role for assignments, dates, curriculum relationships, mastery, reviews, permissions, and schedule rules.

### Requirement 12: Curriculum graph and reviewed content

**User Story:** As a Student, I want approved curriculum and prerequisite-aware lessons, so that practice follows a coherent learning progression.

#### Acceptance Criteria

1. THE Curriculum_Service SHALL maintain concepts, prerequisite edges, versioned content items, question versions, and adult or administrator reviews.
2. THE Curriculum_Service SHALL validate every Concept_DAG as acyclic before publication.
3. THE Curriculum_Service SHALL seed a mathematics Concept_DAG containing whole numbers and place value; division as sharing; fraction as part of a whole; numerator and denominator; equivalent fractions; comparing fractions; simplifying fractions; addition and subtraction with like denominators; addition and subtraction with unlike denominators; multiplication of fractions; division of fractions; decimal place value; fractions as decimals; decimals as percentages; and percentage of a quantity.
4. THE Curriculum_Service SHALL define whole numbers and place value as the initial root concept.
5. THE Curriculum_Service SHALL define division as sharing with whole numbers and place value as the prerequisite.
6. THE Curriculum_Service SHALL define fraction as part of a whole with division as sharing as the prerequisite.
7. THE Curriculum_Service SHALL define numerator and denominator with fraction as part of a whole as the prerequisite.
8. THE Curriculum_Service SHALL define equivalent fractions with numerator and denominator as the prerequisite.
9. THE Curriculum_Service SHALL define comparing fractions with equivalent fractions as the prerequisite.
10. THE Curriculum_Service SHALL define simplifying fractions with equivalent fractions as the prerequisite.
11. THE Curriculum_Service SHALL define addition and subtraction with like denominators with numerator and denominator as the prerequisite.
12. THE Curriculum_Service SHALL define addition and subtraction with unlike denominators with equivalent fractions and addition and subtraction with like denominators as prerequisites.
13. THE Curriculum_Service SHALL define multiplication of fractions with numerator and denominator as the prerequisite.
14. THE Curriculum_Service SHALL define division of fractions with multiplication of fractions as the prerequisite.
15. THE Curriculum_Service SHALL define decimal place value with whole numbers and place value as the prerequisite.
16. THE Curriculum_Service SHALL define fractions as decimals with equivalent fractions and decimal place value as prerequisites.
17. THE Curriculum_Service SHALL define decimals as percentages with fractions as decimals as the prerequisite.
18. THE Curriculum_Service SHALL define percentage of a quantity with decimals as percentages as the prerequisite.
19. THE Curriculum_Service SHALL seed at least one short lesson, three varied questions, answer keys, and explanations for each initial concept.
20. THE Curriculum_Service SHALL support lesson, question, hint, and explanation content states of draft, reviewed, published, and retired.
21. WHILE generated curriculum content lacks an approving review, THE Curriculum_Service SHALL restrict the content to draft or review contexts.
22. WHEN an authorized reviewer approves content, THE Curriculum_Service SHALL record reviewer, decision, notes, version, source, and decision time before publication.
23. IF prerequisite validation detects a cycle, THEN THE Curriculum_Service SHALL reject publication and identify the involved concept edges.
24. IF published content is retired, THEN THE Curriculum_Service SHALL exclude the retired version from new practice while retaining version references for historical attempts.
25. THE Curriculum_Service SHALL support additional school subjects and curriculum packs without changing learner ownership rules.

### Requirement 13: Generated practice and academic integrity

**User Story:** As a Student, I want validated practice, hints, and explanations, so that generated material supports learning without completing assessed work dishonestly.

#### Acceptance Criteria

1. WHEN the Assistant_Service generates an objective question, THE Curriculum_Service SHALL require concept identifiers, grade level, difficulty, machine-checkable answer specification, solution rationale, distractor rationale, and Provenance.
2. WHEN a generated question is prepared for service, THE Curriculum_Service SHALL validate answer correctness, duplicate similarity, grade suitability, harmful content, and subject relevance.
3. IF generated practice fails validation, THEN THE Curriculum_Service SHALL withhold the draft and return the failed validation checks.
4. WHEN approved curated content satisfies a practice request, THE Learning_Service SHALL prefer the approved content over an unreviewed generated draft.
5. WHEN a Student asks for assessed homework completion, THE Assistant_Service SHALL provide explanations, hints, analogous examples, and review guidance rather than student-authored submission text.
6. WHERE an academic-integrity mode is configured, THE Assistant_Service SHALL apply the configured conservative response policy and disclose the active policy.
7. WHEN a question version is served, THE Curriculum_Service SHALL retain the exact version and generation or source metadata used for the attempt.
8. IF no validated generated or approved question is available, THEN THE Learning_Service SHALL report the unavailable content condition without recording a practice attempt.
9. IF practice generation is retried with the same idempotency key, THEN THE Curriculum_Service SHALL return the original generation outcome without persisting a duplicate draft.

### Requirement 14: Learning events and Bayesian mastery

**User Story:** As a Student, I want practice attempts to update mastery consistently, so that recommendations reflect objective learning evidence.

#### Acceptance Criteria

1. WHEN the Student answers a question, THE Learning_Service SHALL record owner, subject, concept, question version, correctness, response duration, hint use, retry count, client time, receipt time, and idempotency key.
2. WHEN a unique learning event is received, THE Learning_Service SHALL insert the event, lock or version the mastery record, calculate BKT, update mastery, create a snapshot, and create a recommendation in one transaction.
3. IF a previously applied idempotency key is received, THEN THE Learning_Service SHALL return the prior outcome without a second mastery update, snapshot, or recommendation.
4. WHEN an answer is correct, THE Learning_Service SHALL calculate `posterior = P(L)(1-P(S)) / (P(L)(1-P(S)) + (1-P(L))P(G))`.
5. WHEN an answer is incorrect, THE Learning_Service SHALL calculate `posterior = P(L)P(S) / (P(L)P(S) + (1-P(L))(1-P(G)))`.
6. WHEN each answer posterior is calculated, THE Learning_Service SHALL calculate `P(L_next) = posterior + (1-posterior)P(T)` using the effective versioned BKT parameter set.
7. THE Learning_Service SHALL keep every mastery probability in the inclusive range from 0 to 1.
8. THE Learning_Service SHALL use correctness as the observation for the BKT update.
9. WHEN response duration, hint use, or retries indicate pacing difficulty under the active rule version, THE Learning_Service SHALL include a simpler explanation, smaller block, or prerequisite review in the recommendation reason without altering the correctness observation.
10. WHEN `P(L) < 0.60`, THE Learning_Service SHALL recommend an alternate explanation or question for the same concept.
11. WHEN `0.60 <= P(L) < 0.85`, THE Learning_Service SHALL continue the concept and insert prerequisite or due review according to the active rule version.
12. WHEN `P(L) >= 0.85`, THE Learning_Service SHALL mark the concept temporarily mastered, unlock each valid successor, and schedule the first review.
13. THE Learning_Service SHALL expose the recommendation rule, evidence, confidence, and next action in learner-readable form.
14. IF any transactional learning-event operation fails, THEN THE Learning_Service SHALL roll back the event, mastery update, snapshot, and recommendation as one unit.
15. IF a learning event references a question version or concept outside the authenticated owner and served-practice context, THEN THE Authorization_Service SHALL return an authorization or validation error without updating mastery.

### Requirement 15: Spaced review and adaptive next action

**User Story:** As a Student, I want review timing and next lessons adapted to performance, so that practice reinforces knowledge at useful intervals.

#### Acceptance Criteria

1. THE Learning_Service SHALL maintain Spaced_Review interval, ease factor, repetitions, due date, last-review time, and rule version per Student and concept.
2. WHEN a review receives a low response-quality rating, THE Learning_Service SHALL shorten the next interval according to the active Spaced_Review rule version.
3. WHEN a correct review is slow or hint-heavy, THE Learning_Service SHALL assign a lower response-quality rating than an independent correct response under the same rule version.
4. WHEN a Student requests the next learning action, THE Learning_Service SHALL choose among same-concept repetition, alternate explanation, prerequisite review, due review, and unlocked successor content.
5. WHEN multiple actions are eligible, THE Learning_Service SHALL select an action using deterministic versioned priority rules.
6. WHEN the Learning_Service returns an action, THE Learning_Service SHALL include contributing mastery, prerequisite, review, pacing, and content evidence.
7. IF no approved action is eligible, THEN THE Learning_Service SHALL report the unavailable prerequisite or content condition.
8. IF no review is due, THEN THE Learning_Service SHALL return an empty due-review collection without changing review dates.
9. IF the effective Spaced_Review rule version is unavailable, THEN THE Learning_Service SHALL return a typed configuration error without changing review state.

### Requirement 16: Canonical data and service boundaries

**User Story:** As a product operator, I want durable, separated domain ownership, so that learner state remains consistent and explainable.

#### Acceptance Criteria

1. THE Adaptive_Learning_System SHALL persist identity, profiles, relationships, devices, subjects, concepts, content, learning state, work, planning, memory metadata, Provenance, and consents as Canonical_State.
2. THE Adaptive_Learning_System SHALL maintain PostgreSQL-compatible relational storage for Canonical_State and metadata-filtered vector retrieval.
3. THE Memory_Service SHALL maintain Graph_Memory through a Graphiti adapter with a Neo4j-compatible graph store.
4. THE Adaptive_Learning_System SHALL process ingestion, embeddings, graph extraction, deletion, and reminders through retryable background jobs.
5. THE Desktop_Companion SHALL own tray integration, permissions, wake detection, secure token storage, and notifications without owning domain persistence rules.
6. THE Assistant_Service SHALL own intent routing, allowed-tool selection, response drafting, and action proposals without unrestricted direct writes.
7. THE Memory_Service SHALL own source ingestion, retrieval, graph episodes, export, retraction, and deletion without mastery or schedule decisions.
8. THE Curriculum_Service SHALL own approved concepts, prerequisites, lessons, questions, hints, explanations, and content reviews without raw learner memory.
9. THE Learning_Service SHALL own mastery, review, learning-event, and next-action rules without language-model mastery verdicts.
10. THE Planner_Service SHALL own tasks, availability, schedule optimization, and planning explanations without hidden calendar mutations.
11. THE Analytics_Service SHALL own read models and aggregates without learner-data mutation endpoints.
12. THE Adaptive_Learning_System SHALL validate the documented status values and relationships for identity, curriculum, learning, planning, memory, and consent records.
13. WHEN Canonical_State and Graph_Memory disagree, THE Adaptive_Learning_System SHALL use Canonical_State for assignments, mastery, curriculum, schedules, permissions, and lifecycle decisions.
14. IF a background job fails after durable enqueue, THEN THE Adaptive_Learning_System SHALL retain job state and make the job eligible for retry according to the worker policy.

### Requirement 17: Versioned application API

**User Story:** As an approved client developer, I want a typed and versioned API, so that desktop and web experiences interact with domain services consistently.

#### Acceptance Criteria

1. THE Application_API SHALL prefix product endpoints with `/v1` and expose an unversioned or deployment-compatible health check.
2. THE Application_API SHALL expose authenticated resources for the current user, dashboard, subjects, subject detail, and subject concepts.
3. THE Application_API SHALL expose assistant message streaming and explicit action confirmation and rejection resources.
4. THE Application_API SHALL expose memory creation, filtered search, episode detail, deletion, and structured export resources.
5. THE Application_API SHALL expose assignment collection, assignment detail, task-extraction draft, plan generation, study-block editing, and block completion resources.
6. THE Application_API SHALL expose learning-event recording, next action, mastery, due review, practice generation, and practice-answer resources.
7. THE Application_API SHALL expose subject analytics, time-on-task, streak, and Pathway_Signal resources.
8. WHEN a client sends a write request, THE Application_API SHALL require and apply an `Idempotency-Key`.
9. IF a write request omits the `Idempotency-Key`, THEN THE Application_API SHALL return a typed validation error without changing Canonical_State.
10. IF a completed write repeats the same `Idempotency-Key` in the same authenticated operation scope, THEN THE Application_API SHALL return the original outcome without repeating the mutation.
11. WHEN a resource identifier is requested, THE Authorization_Service SHALL derive owner scope from the authenticated identity rather than a client-provided owner identifier.
12. IF a resource is absent within the authorized scope, THEN THE Application_API SHALL return a typed not-found error without disclosing existence in another owner scope.
13. WHEN an API contract changes incompatibly, THE Application_API SHALL introduce a new contract version or preserve backward compatibility.
14. WHEN the Application_API returns an error, THE Application_API SHALL provide a typed error code, safe message, request identifier, and applicable field details.
15. IF authentication or authorization fails, THEN THE Application_API SHALL return the applicable typed error without protected response content.

### Requirement 18: Evidence-based insights and pathways

**User Story:** As a Student, I want cautious insight into strengths and interests, so that I can explore academic directions without deterministic labels.

#### Acceptance Criteria

1. THE Analytics_Service SHALL derive subject insights from accuracy, persistence, response-time trend, chosen subjects, completed work, and evidence volume.
2. WHEN a subject has fewer than 20 attempts or fewer than 3 sessions, THE Analytics_Service SHALL withhold a subject-level Pathway_Signal and identify the evidence threshold.
3. WHEN a subject has at least 20 attempts across at least 3 sessions, THE Analytics_Service SHALL classify Pathway_Signal confidence as low, medium, or high using versioned rules.
4. WHEN the Analytics_Service presents a Pathway_Signal, THE Analytics_Service SHALL provide evidence count, measured trend, uncertainty statement, rule version, and an exploration action.
5. THE Analytics_Service SHALL describe Pathway_Signals as opportunities for exploration rather than immutable ability, diagnosis, student ranking, or career outcome.
6. WHEN an Authorized_Observer views a Pathway_Signal, THE Web_Dashboard SHALL present the same evidence and uncertainty available within the Authorized_Observer permission scope.
7. IF eligible evidence is absent, THEN THE Analytics_Service SHALL present an insufficient-evidence state without producing confidence or outcome claims.
8. IF underlying evidence is deleted, THEN THE Analytics_Service SHALL recalculate the Pathway_Signal from the remaining eligible evidence.

### Requirement 19: Privacy, security, and age-appropriate defaults

**User Story:** As a Student or guardian, I want conservative privacy and security controls, so that learning assistance protects a minor's data and agency.

#### Acceptance Criteria

1. THE Adaptive_Learning_System SHALL collect only profile and identity information required for the enabled product functions.
2. THE Desktop_Companion SHALL require explicit operating-system microphone permission before opening a microphone stream.
3. THE Desktop_Companion SHALL store AI credentials and session secrets in operating-system secure storage rather than frontend bundles, relational records, or plaintext configuration.
4. THE Adaptive_Learning_System SHALL encrypt network transport for non-localhost production communication.
5. THE Adaptive_Learning_System SHALL protect local application data with operating-system account permissions.
6. WHERE the Student enables an application lock, THE Desktop_Companion SHALL require successful unlock before displaying learner data.
7. WHEN cloud synchronization becomes available, THE Adaptive_Learning_System SHALL keep synchronization disabled until the Student or guardian records opt-in consent.
8. WHERE cloud synchronization is enabled, THE Adaptive_Learning_System SHALL encrypt synchronized learner data at rest.
9. THE Authorization_Service SHALL scope each data query to the authenticated learner owner and active permission relationship.
10. IF an authorization check fails, THEN THE Authorization_Service SHALL return an authorization error without learner records or cross-owner existence details.
11. WHEN a Student uploads a file, THE Memory_Service SHALL validate configured size and type limits and complete malware screening before ingestion.
12. IF upload validation or malware screening fails, THEN THE Memory_Service SHALL reject ingestion and return a safe validation result without executing imported content.
13. THE Memory_Service SHALL treat imported files and notes as untrusted content and separate embedded instructions from authorized system and Student intent.
14. THE Adaptive_Learning_System SHALL rate-limit AI requests and write requests by authenticated account and configured time window.
15. IF a request exceeds an active rate limit, THEN THE Adaptive_Learning_System SHALL return a typed rate-limit error without applying a write.
16. WHEN a Confirmed_Action changes learner state, THE Adaptive_Learning_System SHALL create an audit record containing action type, actor, target, time, and request identifier.
17. THE Adaptive_Learning_System SHALL operate without third-party behavioral tracking by default.
18. WHERE product telemetry is enabled by consent, THE Adaptive_Learning_System SHALL keep anonymous or pseudonymous operational telemetry separate from study content.
19. THE Adaptive_Learning_System SHALL provide age-appropriate language and conservative academic-integrity defaults for grades 3–12.

### Requirement 20: Deletion, export, and data lifecycle

**User Story:** As a Student, I want to inspect, export, and delete personal learning data, so that local ownership includes practical control over the data lifecycle.

#### Acceptance Criteria

1. WHEN the Student requests a structured export, THE Memory_Service SHALL include owned profile, subjects, assignments, learning state, schedules, Memory_Episodes, Source_Records, consents, and available Provenance in a documented format.
2. WHEN the Student owns no records in an export category, THE Memory_Service SHALL include the empty category in the documented export format.
3. WHEN the Student invokes forget this for a Memory_Episode, THE Memory_Service SHALL mark the episode and Source_Record deleted before future retrieval.
4. WHEN a Memory_Episode enters deleted state, THE Memory_Service SHALL enqueue a tracked Graph_Memory retraction and physical-cleanup job.
5. WHILE a deletion job remains incomplete, THE Memory_Service SHALL exclude the deleted episode and associated graph facts from Retrieval_Context.
6. IF a graph deletion job fails, THEN THE Memory_Service SHALL retain retry state, schedule retry according to the worker policy, and display synchronization status without restoring retrieval eligibility.
7. WHEN graph deletion completes, THE Memory_Service SHALL record completion and remove applicable graph-derived links.
8. IF a deletion request repeats the same idempotency key, THEN THE Memory_Service SHALL return the current deletion outcome without enqueuing duplicate cleanup work.
9. IF a requested Memory_Episode is outside the authenticated owner scope, THEN THE Authorization_Service SHALL return an authorization or scope-safe not-found error without changing lifecycle state.
10. WHEN the Student requests account deletion and completes identity confirmation, THE Adaptive_Learning_System SHALL revoke sessions, mark owned records for deletion, enqueue external-store cleanup, and report lifecycle status.
11. WHEN consent is revoked, THE Adaptive_Learning_System SHALL stop the consent-dependent processing from the revocation time forward.
12. THE Adaptive_Learning_System SHALL retain zero raw speech in retrieval logs, wake diagnostics, or operational logs.

### Requirement 21: Observability and privacy-minimal operations

**User Story:** As a product operator, I want useful operational evidence without study-content logging, so that reliability can be maintained without exposing learner material.

#### Acceptance Criteria

1. THE Adaptive_Learning_System SHALL emit structured operational logs containing request identifier, user pseudonym, endpoint, latency, model and provider identifiers, token or cost bucket, retrieval count, graph-worker status, and recommendation rule when applicable.
2. THE Adaptive_Learning_System SHALL exclude raw voice, full Student notes, AI credentials, session secrets, and encryption keys from operational logs.
3. WHEN a graph ingestion or deletion job fails, THE Adaptive_Learning_System SHALL record retry count, safe error category, next retry time, and visible synchronization state.
4. WHEN an AI request completes, THE AI_Provider_Adapter SHALL record provider, model, latency, usage bucket, outcome category, and request identifier without prompt or response study content by default.
5. WHEN an authorization denial occurs, THE Authorization_Service SHALL record actor pseudonym, requested resource type, scope decision, and request identifier without returning protected resource data.
6. WHERE consented diagnostics are enabled, THE Adaptive_Learning_System SHALL provide a deletion control for locally stored diagnostic records.
7. IF operational logging fails, THEN THE Adaptive_Learning_System SHALL preserve domain transaction behavior and surface the logging failure through a safe operational channel.
8. WHEN a recommendation is produced, THE Learning_Service SHALL record the deterministic rule version used without recording full Student notes.

### Requirement 22: Accessibility, performance, resilience, and release quality

**User Story:** As a Student, I want a responsive and accessible product that preserves work, so that FastLearner remains dependable during everyday study.

#### Acceptance Criteria

1. THE Desktop_Companion SHALL support keyboard navigation and screen-reader labels for all Student workflows.
2. THE Web_Dashboard SHALL support keyboard navigation and screen-reader labels for all Student and Authorized_Observer views.
3. WHEN tested with a benchmark dataset representing normal Student state in the documented benchmark environment, THE Analytics_Service SHALL return the home dashboard read model within 1 second at the service boundary.
4. THE Assistant_Service SHALL enforce configured Retrieval_Context size limits before sending an AI request.
5. WHEN the Adaptive_Learning_System restarts after a committed mutation, THE Adaptive_Learning_System SHALL restore the committed Canonical_State without requiring re-entry.
6. IF a background job process stops after durable enqueue, THEN THE Adaptive_Learning_System SHALL make the job eligible for retry after worker recovery.
7. THE Adaptive_Learning_System SHALL provide backup and recovery procedures for relational, vector, and graph data.
8. THE Desktop_Companion SHALL measure and report wake-listening CPU and memory use against a documented tray-mode benchmark.
9. WHEN a production desktop release is created, THE Desktop_Companion SHALL use platform-appropriate signing and update verification.
10. THE Adaptive_Learning_System SHALL provide visible error recovery actions for offline AI, failed synchronization, validation failure, and authorization denial.
11. IF an uncommitted mutation fails, THEN THE Adaptive_Learning_System SHALL preserve the last committed Canonical_State and present a retry-safe error.
12. WHILE AI connectivity is unavailable, THE Desktop_Companion SHALL keep local navigation, Canonical_State viewing, and supported deterministic editing available.

### Requirement 23: Verification and complete-product acceptance

**User Story:** As a product owner, I want automated evidence for critical rules and complete workflows, so that release readiness is objectively verifiable.

#### Acceptance Criteria

1. THE Adaptive_Learning_System SHALL provide unit tests for BKT formulas, scheduler scoring, clap transient state, assignment lifecycle, task lifecycle, and permission guards.
2. THE Adaptive_Learning_System SHALL provide invariant tests proving Concept_DAG acyclicity, mastery bounds from 0 to 1, one-time idempotent-event application, and owner-scope isolation.
3. THE Adaptive_Learning_System SHALL provide integration tests for relational storage, graph storage, job processing, Graphiti ingestion, Graph_Memory retrieval, graph deletion, and shared API contracts.
4. THE Adaptive_Learning_System SHALL provide end-to-end tests for assignment creation through plan and focus completion; wake through cited answer; and practice through mastery update and recommendation.
5. THE Adaptive_Learning_System SHALL provide security tests for authorization boundaries, tenant isolation, imported-content prompt injection, upload validation, rate limits, and secret scanning.
6. THE Adaptive_Learning_System SHALL provide accessibility tests for keyboard and screen-reader operation across desktop Student, web Student, and web Authorized_Observer experiences.
7. WHEN release acceptance is executed, THE Adaptive_Learning_System SHALL demonstrate installation or opening, consented local wake, subject-context capture, assignment creation, editable planning, seeded-math practice, explainable mastery, cited questioning, memory search, memory deletion, structured export, and persisted restart state for one Student.
8. WHEN release acceptance exercises a generated mutation, THE Adaptive_Learning_System SHALL demonstrate explicit confirmation before Canonical_State changes.
9. WHEN release acceptance exercises wake detection, THE Adaptive_Learning_System SHALL demonstrate zero raw wake-audio persistence and zero wake-triggered outbound request before a Student question.
10. WHEN release acceptance exercises observer access, THE Adaptive_Learning_System SHALL demonstrate a separately deployed Web_Dashboard with active parent and teacher read-only scopes and rejected mutations.
11. WHEN release acceptance exercises a duplicate write, THE Adaptive_Learning_System SHALL demonstrate one persisted outcome for the repeated idempotency key.
12. WHEN release acceptance exercises an empty collection or unsupported claim, THE Adaptive_Learning_System SHALL demonstrate the specified empty or uncertainty response without fabricated data.
13. WHEN release acceptance exercises a failed transactional mutation, THE Adaptive_Learning_System SHALL demonstrate preservation of the last committed Canonical_State.

### Requirement 24: Initial release scope and autonomy limits

**User Story:** As a Student or guardian, I want bounded automation, so that the initial product assists learning without taking unreviewed external actions.

#### Acceptance Criteria

1. THE Adaptive_Learning_System SHALL provide the Desktop_Companion and Web_Dashboard as the supported initial-release clients.
2. THE Adaptive_Learning_System SHALL provide manual assignment entry and pasted or uploaded assignment-brief intake for the initial release.
3. WHEN a Student requests automated learning-management-system, calendar, or assignment import, THE Assistant_Service SHALL explain that the integration is unavailable and offer supported manual intake.
4. WHEN a Student requests autonomous web browsing, email transmission, schoolwork submission, or an external calendar change, THE Assistant_Service SHALL provide guidance or a reviewable proposal without performing the external action.
5. WHEN generated curriculum content is proposed for publication, THE Curriculum_Service SHALL require a recorded human review decision.
6. WHEN a Student asks for a learning-disability diagnosis, THE Assistant_Service SHALL provide a non-diagnostic limitation statement and encourage discussion with a qualified adult or professional.
7. WHEN a Student asks for a deterministic career decision, THE Assistant_Service SHALL present evidence-bounded Pathway_Signals and exploration options without a career verdict.
8. WHILE a parent or teacher uses the initial observer experience, THE Authorization_Service SHALL enforce read-only learner access.
9. IF an unsupported autonomous action is requested, THEN THE Assistant_Service SHALL leave external systems and Canonical_State unchanged unless the Student separately confirms a supported local mutation.

### Requirement 25: Configuration and development operations

**User Story:** As a developer, I want reproducible local configuration and validation, so that the complete product can be developed without committing secrets.

#### Acceptance Criteria

1. THE Adaptive_Learning_System SHALL provide a version-controlled `.env.example` containing placeholders for `DATABASE_URL`, `REDIS_URL`, `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `AI_PROVIDER`, `AI_MODEL`, `AI_API_KEY`, `EMBEDDING_MODEL`, `APP_ENCRYPTION_KEY`, and `VITE_API_URL`.
2. THE Adaptive_Learning_System SHALL keep production credential values outside version-controlled configuration.
3. THE Adaptive_Learning_System SHALL provide local development orchestration for PostgreSQL 16 with vector search, Neo4j, and Redis-compatible job storage.
4. WHEN a required runtime configuration value is absent, THE Adaptive_Learning_System SHALL return a typed startup or feature-availability error identifying the missing setting without exposing another secret.
5. THE Adaptive_Learning_System SHALL retain the Foundation_Baseline automated lint, format, type, unit-test, contract-generation, conformance, build, and continuous-integration commands for affected languages and packages.
6. WHEN generated API contracts differ from the committed Application_API schema, THE Adaptive_Learning_System SHALL fail the contract-consistency check.
7. WHEN a developer starts the documented local environment command, THE Adaptive_Learning_System SHALL start or connect the desktop client, Application_API, relational store, graph store, and job store required by the selected development profile.
8. IF a required local dependency is unavailable, THEN THE Adaptive_Learning_System SHALL report the unavailable dependency and affected feature without exposing configured credentials.
9. WHEN continuous integration runs, THE Adaptive_Learning_System SHALL execute unit tests and type checks for the affected Foundation_Baseline and product workspaces.
