import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { LearnerProfile, LearnerSetup, Onboarding } from "./Onboarding";
import "./styles.css";
import "./onboarding.css";

const API = "http://127.0.0.1:8001/v1";
type View = "Today" | "Work" | "Learn" | "Memory" | "Assistant";
type Subject = { id: string; title: string; slug: string; status: string };
type Assignment = {
  id: string; subject_id: string; title: string; status: string;
  due_at: string; estimated_minutes: number;
};
type Goal = { id: string; title: string; status: string; target_at?: string };
type Me = { actor_id: string; owner_id: string; is_owner: boolean; profile: LearnerProfile | null };
let localSessionToken: string | null = null;

async function sessionToken(): Promise<string> {
  if (localSessionToken) return localSessionToken;
  localStorage.removeItem("fastlearner.token");
  const response = await fetch(`${API}/local/session`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ persona: "learner" }),
  });
  if (!response.ok) throw new Error("Local API unavailable");
  const data = await response.json() as { token: string };
  localSessionToken = data.token;
  return data.token;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const token = await sessionToken();
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json", Authorization: `Bearer ${token}`,
      ...(init?.method && init.method !== "GET" ? { "Idempotency-Key": crypto.randomUUID() } : {}),
      ...init?.headers,
    },
  });
  if (response.status === 401) {
    localSessionToken = null;
    if (!init?.headers || !(init.headers as Record<string, string>)["X-Retry"]) {
      return api<T>(path, { ...init, headers: { ...(init?.headers ?? {}), "X-Retry": "1" } });
    }
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { error?: { message?: string }; detail?: string };
    throw new Error(body.error?.message ?? body.detail ?? `Request failed (${response.status})`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export interface AppShellProps { readonly title: string; }

export function AppShell({ title }: AppShellProps) {
  const [view, setView] = useState<View>("Today");
  const [subjects, setSubjects] = useState<Subject[]>([]);
  const [assignments, setAssignments] = useState<Assignment[]>([]);
  const [goals, setGoals] = useState<Goal[]>([]);
  const [me, setMe] = useState<Me | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [compose, setCompose] = useState<"assignment" | "subject" | "goal" | null>(null);
  const [profileOpen, setProfileOpen] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setError("");
      const [nextSubjects, nextAssignments, nextGoals, nextMe] = await Promise.all([
        api<Subject[]>("/subjects"), api<Assignment[]>("/assignments"), api<Goal[]>("/goals"), api<Me>("/me"),
      ]);
      setSubjects(nextSubjects); setAssignments(nextAssignments); setGoals(nextGoals); setMe(nextMe);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not connect");
    } finally { setBusy(false); }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    const openAssistant = () => setView("Assistant");
    window.addEventListener("fastlearner:open-assistant", openAssistant);
    return () => window.removeEventListener("fastlearner:open-assistant", openAssistant);
  }, []);
  const active = assignments.filter(item => item.status !== "done" && item.status !== "archived");
  const focusMinutes = active.reduce((sum, item) => sum + item.estimated_minutes, 0);
  const preferences = me?.profile?.study_preferences ?? {};
  const learnerName = typeof preferences.name === "string" ? preferences.name : "Learner";
  const learnerInitial = learnerName.trim().charAt(0).toUpperCase() || "L";
  const needsOnboarding = !!me?.profile && preferences.onboarding_completed !== true;

  async function saveProfile(setup: LearnerSetup) {
    if (!me?.profile) throw new Error("Profile is unavailable");
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || me.profile.timezone || "UTC";
    const studyPreferences = {
      ...me.profile.study_preferences,
      schema_version: 1,
      onboarding_completed: true,
      demo_profile: setup.name.includes("(Demo)"),
      name: setup.name,
      school: setup.school,
      subjects: setup.subjects,
      goals: setup.goals,
      interests: setup.interests,
      learning_style: setup.learningStyle,
      answer_style: setup.answerStyle,
      session_minutes: setup.sessionMinutes,
      daily_limit_minutes: setup.dailyLimitMinutes,
      voice_enabled: setup.voiceEnabled,
      double_clap: setup.doubleClap,
      remember_chats: setup.rememberChats,
      assistant_name: "Zipity",
    };
    await api("/me/profile", { method: "PATCH", body: JSON.stringify({ grade_level: setup.gradeLevel, timezone, study_preferences: studyPreferences }) });
    const knownTitles = new Set(subjects.map(item => item.title.toLocaleLowerCase()));
    for (const subjectTitle of setup.subjects) {
      const slug = subjectTitle.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
      if (slug && !knownTitles.has(subjectTitle.toLocaleLowerCase())) {
        await api("/subjects", { method: "POST", body: JSON.stringify({ title: subjectTitle, slug }) });
        knownTitles.add(subjectTitle.toLocaleLowerCase());
      }
    }
    if (setup.goals && !goals.some(goal => goal.title === setup.goals)) {
      await api("/goals", { method: "POST", body: JSON.stringify({ title: setup.goals, subject_id: null, target_at: null }) });
    }
    const devices = await api<{ items: Array<{ name: string; platform: string; status: string }> }>("/me/devices");
    if (!devices.items.some(device => device.platform === "windows" && device.status === "active")) {
      await api("/me/devices", { method: "POST", body: JSON.stringify({ name: "This PC", platform: "windows" }) });
    }
    setProfileOpen(false);
    await refresh();
  }

  async function assignmentAction(item: Assignment, action: "start" | "complete") {
    try {
      await api(`/assignments/${item.id}/actions`, { method: "POST", body: JSON.stringify({ action }) });
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Update failed"); }
  }

  return (
    <div className="fl-app">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark"><img src="/zipity-mark.png" alt="" /></span><span>Zipity</span></div>
        <nav aria-label="Main navigation">
          {(["Today", "Work", "Learn", "Memory", "Assistant"] as View[]).map((item, index) => (
            <button key={item} className={view === item ? "nav-item active" : "nav-item"} onClick={() => setView(item)}>
              <span aria-hidden="true">{["⌂", "□", "◫", "◇", "✦"][index]}</span>{item}
            </button>
          ))}
        </nav>
        <button className="side-bottom profile-button" onClick={() => setProfileOpen(true)} title="Edit your Zipity profile">
          <div className="profile-dot">{learnerInitial}</div><div><strong>{learnerName}</strong><small>{me?.profile ? `Grade ${me.profile.grade_level} · Edit profile` : "Local workspace"}</small></div>
          <span className="online" title="API connected" />
        </button>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div><h1>{title}</h1><span className="page-title" aria-hidden="true">{view}</span></div>
          <div className="top-actions">
            <button className="icon-button" aria-label="Notifications">◌</button>
            <button className="primary" onClick={() => setCompose("assignment")}><span>＋</span> Add work</button>
          </div>
        </header>

        {error && <div className="notice"><span>!</span><div><strong>Connection needs attention</strong><small>{error}. Start local API on port 8001.</small></div><button onClick={() => void refresh()}>Retry</button></div>}
        {busy ? <Loading /> : view === "Today" ? (
          <Today active={active} goals={goals} subjects={subjects} focusMinutes={focusMinutes} onAction={assignmentAction} onAdd={() => setCompose("assignment")} />
        ) : view === "Work" ? (
          <Work assignments={assignments} subjects={subjects} onAction={assignmentAction} onAddSubject={() => setCompose("subject")} />
        ) : view === "Learn" ? <Learn /> : view === "Memory" ? <Memory /> : <Assistant />}
      </main>
      {compose && <Composer kind={compose} subjects={subjects} onClose={() => setCompose(null)} onSaved={async () => { setCompose(null); await refresh(); }} />}
      {me?.profile && (needsOnboarding || profileOpen) && <Onboarding profile={me.profile} editing={!needsOnboarding} onSave={saveProfile} onClose={needsOnboarding ? undefined : () => setProfileOpen(false)} />}
    </div>
  );
}

function Loading() { return <div className="loading"><span /><span /><span /></div>; }

function Today({ active, goals, subjects, focusMinutes, onAction, onAdd }: {
  active: Assignment[]; goals: Goal[]; subjects: Subject[]; focusMinutes: number;
  onAction: (a: Assignment, action: "start" | "complete") => void; onAdd: () => void;
}) {
  const next = [...active].sort((a, b) => a.due_at.localeCompare(b.due_at));
  return <div className="page-grid">
    <section className="hero-card">
      <div><span className="eyebrow">YOUR DAY</span><h2>Build momentum,<br />one clear step at a time.</h2><p>{active.length ? `${active.length} active item${active.length === 1 ? "" : "s"} · ${focusMinutes} min planned` : "Your day is clear. Add work when ready."}</p></div>
      <div className="progress-orbit"><div><strong>{Math.min(100, active.length ? 36 : 100)}%</strong><span>daily focus</span></div></div>
    </section>
    <section className="metric-row">
      <Metric label="Open work" value={String(active.length)} note="in your queue" />
      <Metric label="Study time" value={`${Math.floor(focusMinutes / 60)}h ${focusMinutes % 60}m`} note="estimated" />
      <Metric label="Subjects" value={String(subjects.filter(s => s.status === "active").length)} note="active" />
    </section>
    <section className="panel task-panel">
      <div className="panel-head"><div><span className="eyebrow">NEXT UP</span><h3>Priority work</h3></div><button className="text-button" onClick={onAdd}>Add assignment</button></div>
      {next.length ? <div className="task-list">{next.slice(0, 4).map(item => <TaskRow key={item.id} item={item} onAction={onAction} />)}</div> : <Empty icon="✓" title="Nothing urgent" copy="Add an assignment and Zipity will organize your next steps." />}
    </section>
    <section className="panel goals-panel"><div className="panel-head"><div><span className="eyebrow">DIRECTION</span><h3>Active goals</h3></div></div>
      {goals.filter(g => g.status === "active").slice(0, 3).map(goal => <div className="goal" key={goal.id}><span /><div><strong>{goal.title}</strong><small>{goal.target_at ? `Target ${new Date(goal.target_at).toLocaleDateString()}` : "Keep moving"}</small></div></div>)}
      {!goals.length && <Empty icon="↗" title="Set a direction" copy="Goals connect daily work to bigger outcomes." />}
    </section>
  </div>;
}

function Metric({ label, value, note }: { label: string; value: string; note: string }) { return <div className="metric"><span>{label}</span><strong>{value}</strong><small>{note}</small></div>; }

function TaskRow({ item, onAction }: { item: Assignment; onAction: (a: Assignment, action: "start" | "complete") => void }) {
  const soon = new Date(item.due_at); const due = soon.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return <article className="task-row"><button className="check" aria-label={`Complete ${item.title}`} onClick={() => void onAction(item, "complete")}>✓</button><div className="task-main"><strong>{item.title}</strong><small>Due {due} · {item.estimated_minutes} min</small></div><span className={`status ${item.status}`}>{item.status.replace("_", " ")}</span>{item.status === "pending" && <button className="small-button" onClick={() => void onAction(item, "start")}>Start</button>}</article>;
}

function Work({ assignments, subjects, onAction, onAddSubject }: { assignments: Assignment[]; subjects: Subject[]; onAction: (a: Assignment, action: "start" | "complete") => void; onAddSubject: () => void }) {
  const names = useMemo(() => Object.fromEntries(subjects.map(s => [s.id, s.title])), [subjects]);
  const [query, setQuery] = useState(""); const [subject, setSubject] = useState(""); const [status, setStatus] = useState(""); const [page, setPage] = useState(0);
  const pageSize = 25;
  const filtered = useMemo(() => assignments.filter(item => {
    const haystack = `${item.title} ${names[item.subject_id] ?? "General"}`.toLowerCase();
    return (!query || haystack.includes(query.toLowerCase())) && (!subject || item.subject_id === subject) && (!status || item.status === status);
  }), [assignments, names, query, subject, status]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const visible = filtered.slice(page * pageSize, (page + 1) * pageSize);
  useEffect(() => { setPage(0); }, [query, subject, status]);
  return <div className="stack"><div className="section-title"><div><span className="eyebrow">SCHOOLWORK</span><h2>Everything you’re working on</h2></div><button className="secondary" onClick={onAddSubject}>New subject</button></div>
    <div className="subject-pills"><button className={!subject ? "selected" : ""} onClick={() => setSubject("")}>All · {assignments.length}</button>{subjects.map(s => <button className={subject === s.id ? "selected" : ""} onClick={() => setSubject(s.id)} key={s.id}>{s.title}</button>)}</div>
    <section className="panel"><div className="work-toolbar"><input aria-label="Search assignments" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search assignments or subjects…" /><select aria-label="Filter status" value={status} onChange={event => setStatus(event.target.value)}><option value="">All statuses</option><option value="pending">Pending</option><option value="in_progress">In progress</option><option value="done">Done</option><option value="archived">Archived</option></select><small>{filtered.length} result{filtered.length === 1 ? "" : "s"}</small></div><div className="table-head"><span>Assignment</span><span>Subject</span><span>Due</span><span>Status</span><span /></div>{visible.length ? visible.map(item => <div className="work-row" key={item.id}><strong>{item.title}</strong><span>{names[item.subject_id] ?? "General"}</span><span>{new Date(item.due_at).toLocaleDateString()}</span><span className={`status ${item.status}`}>{item.status.replace("_", " ")}</span><button className="small-button" onClick={() => void onAction(item, item.status === "pending" ? "start" : "complete")}>{item.status === "pending" ? "Start" : "Done"}</button></div>) : <Empty icon="□" title="Nothing matches" copy="Try another subject, status, or search." />}{pageCount > 1 && <div className="pager"><button disabled={page === 0} onClick={() => setPage(value => value - 1)}>Previous</button><span>{page + 1} / {pageCount}</span><button disabled={page + 1 >= pageCount} onClick={() => setPage(value => value + 1)}>Next</button></div>}</section>
  </div>;
}

function Learn() {
  type Concept = { id: string; title: string; lesson_body?: string; mastery: number };
  type Question = { id: string; prompt: string; choices: string[] };
  const [concepts, setConcepts] = useState<Concept[]>([]); const [selected, setSelected] = useState<Concept | null>(null);
  const [questions, setQuestions] = useState<Question[]>([]); const [questionIndex, setQuestionIndex] = useState(0); const [feedback, setFeedback] = useState("");
  useEffect(() => { void api<Concept[]>("/learning/concepts").then(data => { setConcepts(data); setSelected(data[0] ?? null); }); }, []);
  useEffect(() => { if (selected) void api<Question[]>(`/learning/concepts/${selected.id}/questions`).then(data => { setQuestions(data); setQuestionIndex(0); setFeedback(""); }); }, [selected]);
  const question = questions[questionIndex];
  async function answer(answerIndex: number) {
    if (!question) return; const result = await api<{ correct: boolean; explanation: string; mastery: number }>("/learning/attempts", { method: "POST", body: JSON.stringify({ question_id: question.id, answer_index: answerIndex, duration_ms: 0 }) });
    setFeedback(`${result.correct ? "Correct" : "Not quite"}. ${result.explanation} Mastery: ${Math.round(result.mastery * 100)}%`);
    setConcepts(items => items.map(item => item.id === selected?.id ? { ...item, mastery: result.mastery } : item));
  }
  return <div className="learn-page"><div className="section-title"><div><span className="eyebrow">ADAPTIVE LEARNING</span><h2>Learn at your pace</h2><p>Lessons and practice adapt using explainable mastery signals.</p></div></div><div className="learn-layout"><aside className="concept-list">{concepts.map(concept => <button className={selected?.id === concept.id ? "selected" : ""} onClick={() => setSelected(concept)} key={concept.id}><span>{Math.round(concept.mastery * 100)}%</span><strong>{concept.title}</strong><i><b style={{ width: `${Math.round(concept.mastery * 100)}%` }} /></i></button>)}</aside><section className="lesson-card">{selected ? <><span className="eyebrow">LESSON</span><h3>{selected.title}</h3><p>{selected.lesson_body}</p>{question && <div className="quiz"><small>CHECK YOUR UNDERSTANDING · {questionIndex + 1}/{questions.length}</small><strong>{question.prompt}</strong><div>{question.choices.map((choice, index) => <button key={choice} disabled={!!feedback} onClick={() => void answer(index)}>{choice}</button>)}</div>{feedback && <p className="quiz-feedback">{feedback}</p>}{feedback && questionIndex < questions.length - 1 && <button className="primary" onClick={() => { setQuestionIndex(value => value + 1); setFeedback(""); }}>Next question</button>}</div>}</> : <Empty icon="◫" title="No lesson available" copy="Curriculum will appear after local seed runs." />}</section></div></div>;
}
function Memory() {
  const [items, setItems] = useState<Array<{ id: string; kind: string; content: string }>>([]);
  const [note, setNote] = useState(""); const [query, setQuery] = useState(""); const [memoryError, setMemoryError] = useState("");
  const load = useCallback(async (search = "") => { try { setItems(await api<Array<{ id: string; kind: string; content: string }>>(`/memory${search ? `?q=${encodeURIComponent(search)}` : ""}`)); } catch (reason) { setMemoryError(reason instanceof Error ? reason.message : "Memory unavailable"); } }, []);
  useEffect(() => { void load(); }, [load]);
  async function save(event: FormEvent) { event.preventDefault(); if (!note.trim()) return; try { await api("/memory", { method: "POST", body: JSON.stringify({ content: note, kind: "note" }) }); setNote(""); await load(query); } catch (reason) { setMemoryError(reason instanceof Error ? reason.message : "Could not save"); } }
  return <div className="memory-page"><div className="section-title"><div><span className="eyebrow">PERSONAL MEMORY</span><h2>Your knowledge, connected</h2><p>Save only what matters. Notes stay private in your local workspace.</p></div></div><div className="memory-grid"><form className="capture-card" onSubmit={save}><span className="eyebrow">QUICK CAPTURE</span><textarea value={note} onChange={e => setNote(e.target.value)} placeholder="Write a useful note, correction, or resource…" aria-label="Memory note" /><button className="primary" disabled={!note.trim()}>Save to memory</button></form><section className="panel memory-list"><div className="memory-search"><input value={query} onChange={e => { setQuery(e.target.value); void load(e.target.value); }} placeholder="Search your memory" aria-label="Search memory" /></div>{memoryError && <p className="form-error">{memoryError}</p>}{items.length ? items.map(item => <article key={item.id}><span>{item.kind}</span><p>{item.content}</p></article>) : <Empty icon="◇" title="Memory is empty" copy="Capture first note. Ordinary chat is never saved automatically." />}</section></div></div>;
}

function InlineMarkup({ text }: { text: string }) {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean);
  return <>{parts.map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={index}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={index}>{part.slice(1, -1)}</code>;
    return <span key={index}>{part}</span>;
  })}</>;
}

function splitTableRow(line: string) {
  return line.trim().replace(/^\||\|$/g, "").split("|").map(cell => cell.trim());
}

function isTableDivider(line: string) {
  const cells = splitTableRow(line);
  return cells.length > 1 && cells.every(cell => /^:?-{3,}:?$/.test(cell));
}

export function StructuredAnswer({ text, streaming = false }: { text: string; streaming?: boolean }) {
  if (!text) return <div className="answer-loading" aria-label="Zipity is thinking"><i /><i /><i /></div>;
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index] ?? "";
    if (!line.trim()) { index += 1; continue; }

    const fence = line.match(/^```([\w-]*)\s*$/);
    if (fence) {
      const code: string[] = []; index += 1;
      while (index < lines.length && !(lines[index] ?? "").startsWith("```")) { code.push(lines[index] ?? ""); index += 1; }
      if (index < lines.length) index += 1;
      blocks.push(<pre className="answer-code" key={`code-${index}`}><span>{fence[1] || "code"}</span><code>{code.join("\n")}</code></pre>);
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      const content = <InlineMarkup text={heading[2] ?? ""} />;
      blocks.push((heading[1] ?? "").length === 1 ? <h3 key={`h-${index}`}>{content}</h3> : <h4 key={`h-${index}`}>{content}</h4>);
      index += 1; continue;
    }

    if (line.includes("|") && index + 1 < lines.length && isTableDivider(lines[index + 1] ?? "")) {
      const headers = splitTableRow(line); const rows: string[][] = []; index += 2;
      while (index < lines.length && (lines[index] ?? "").includes("|") && (lines[index] ?? "").trim()) { rows.push(splitTableRow(lines[index] ?? "")); index += 1; }
      blocks.push(<div className="answer-table-wrap" key={`table-${index}`}><table><thead><tr>{headers.map((cell, cellIndex) => <th key={cellIndex}><InlineMarkup text={cell} /></th>)}</tr></thead><tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{headers.map((_, cellIndex) => <td key={cellIndex}><InlineMarkup text={row[cellIndex] ?? ""} /></td>)}</tr>)}</tbody></table></div>);
      continue;
    }

    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (ordered) {
      const items: string[] = []; const matcher = /^\s*\d+[.)]\s+(.+)$/;
      while (index < lines.length) {
        const item = (lines[index] ?? "").match(matcher); if (!item) break;
        let content = item[1] ?? ""; index += 1; const detail: string[] = [];
        while (index < lines.length && /^\s{2,}\S/.test(lines[index] ?? "") && !(lines[index] ?? "").match(matcher)) {
          detail.push((lines[index] ?? "").replace(/^\s*[-*]?\s*/, "")); index += 1;
        }
        if (detail.length) content += ` — ${detail.join(" ")}`;
        items.push(content);
        let nextItem = index; while (nextItem < lines.length && !(lines[nextItem] ?? "").trim()) nextItem += 1;
        if ((lines[nextItem] ?? "").match(matcher)) index = nextItem;
      }
      const children = items.map((item, itemIndex) => <li key={itemIndex}><InlineMarkup text={item} /></li>);
      blocks.push(<ol key={`list-${index}`}>{children}</ol>);
      continue;
    }

    const unordered = line.match(/^\s*[-*]\s+(.+)$/);
    if (unordered) {
      const items: string[] = []; const matcher = /^\s*[-*]\s+(.+)$/;
      while (index < lines.length) { const item = (lines[index] ?? "").match(matcher); if (!item) break; items.push(item[1] ?? ""); index += 1; }
      blocks.push(<ul key={`list-${index}`}>{items.map((item, itemIndex) => <li key={itemIndex}><InlineMarkup text={item} /></li>)}</ul>);
      continue;
    }

    if (line.startsWith("> ")) {
      const quote: string[] = [];
      while (index < lines.length && (lines[index] ?? "").startsWith("> ")) { quote.push((lines[index] ?? "").slice(2)); index += 1; }
      blocks.push(<blockquote key={`quote-${index}`}><InlineMarkup text={quote.join(" ")} /></blockquote>);
      continue;
    }

    if (/^-{3,}$/.test(line.trim())) { blocks.push(<hr key={`rule-${index}`} />); index += 1; continue; }

    const paragraph: string[] = [];
    while (index < lines.length && (lines[index] ?? "").trim() && !/^(#{1,3})\s|^```|^\s*[-*]\s+|^\s*\d+[.)]\s+|^> /.test(lines[index] ?? "")) {
      if (index + 1 < lines.length && (lines[index] ?? "").includes("|") && isTableDivider(lines[index + 1] ?? "")) break;
      paragraph.push((lines[index] ?? "").trim()); index += 1;
    }
    if (paragraph.length) blocks.push(<p key={`p-${index}`}><InlineMarkup text={paragraph.join(" ")} /></p>);
    else index += 1;
  }
  return <div className="answer-body">{blocks}{streaming && <span className="stream-caret" aria-hidden="true" />}</div>;
}

function plainSpeech(text: string) {
  return text.replace(/```[\s\S]*?```/g, match => match.replace(/```[\w-]*\n?|```/g, ""))
    .replace(/^#{1,3}\s+/gm, "").replace(/^\s*[-*]\s+/gm, "").replace(/^\s*\d+[.)]\s+/gm, "")
    .replace(/\*\*|`|^>\s?/gm, "").replace(/\|/g, ", ").trim();
}

function Assistant() {
  type Turn = { role: "user" | "assistant"; content: string };
  const [message, setMessage] = useState(""); const [turns, setTurns] = useState<Turn[]>([]);
  const [sending, setSending] = useState(false); const [chatError, setChatError] = useState("");
  const [remember, setRemember] = useState(false); const [contextCount, setContextCount] = useState(0);

  async function sendPrompt(rawPrompt: string, readReplyAloud = false) {
    const prompt = rawPrompt.trim(); if (!prompt || sending) return;
    const history = turns.slice(-20); const assistantIndex = history.length + 1;
    setTurns([...history, { role: "user", content: prompt }, { role: "assistant", content: "" }]);
    setMessage(""); setSending(true); setChatError(""); setContextCount(0);
    try {
      const token = await sessionToken();
      const response = await fetch(`${API}/assistant/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ message: prompt, history, use_memory: true, remember }),
      });
      if (!response.ok || !response.body) throw new Error("Assistant stream unavailable");
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ""; let spokenAnswer = ""; let pendingDelta = ""; let updateFrame = 0;
      const flushDelta = () => { if (!pendingDelta) return; const delta = pendingDelta; pendingDelta = ""; setTurns(items => items.map((item, index) => index === assistantIndex ? { ...item, content: item.content + delta } : item)); };
      while (true) {
        const { value, done } = await reader.read(); if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split("\n\n"); buffer = frames.pop() ?? "";
        for (const frame of frames) {
          const line = frame.split("\n").find(item => item.startsWith("data:")); if (!line) continue;
          const payload = JSON.parse(line.slice(5).trim()) as { type: string; text?: string; count?: number; message?: string };
          if (payload.type === "context") setContextCount(payload.count ?? 0);
          if (payload.type === "error") throw new Error(payload.message ?? "Assistant failed");
          if (payload.type === "delta" && payload.text) { spokenAnswer += payload.text; pendingDelta += payload.text; if (!updateFrame) updateFrame = window.requestAnimationFrame(() => { updateFrame = 0; flushDelta(); }); }
        }
      }
      if (updateFrame) window.cancelAnimationFrame(updateFrame); flushDelta();
      if (readReplyAloud && spokenAnswer.trim()) await speak(plainSpeech(spokenAnswer));
    } catch (reason) { setChatError(reason instanceof Error ? reason.message : "Assistant unavailable"); }
    finally { setSending(false); }
  }

  async function send(event: FormEvent) { event.preventDefault(); await sendPrompt(message); }

  useEffect(() => {
    const voiceQuery = (event: Event) => {
      const text = (event as CustomEvent<{ text?: string }>).detail?.text?.trim();
      if (text) { setMessage(text); void sendPrompt(text, true); }
    };
    window.addEventListener("fastlearner:voice-query", voiceQuery);
    return () => window.removeEventListener("fastlearner:voice-query", voiceQuery);
  });

  async function speak(text: string) {
    try {
      const token = await sessionToken(); const response = await fetch(`${API}/assistant/speech/stream`, { method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` }, body: JSON.stringify({ text }) });
      if (!response.ok) throw new Error("Voice unavailable");
      const audio = new Audio(URL.createObjectURL(await response.blob())); await audio.play();
    } catch (reason) { setChatError(reason instanceof Error ? reason.message : "Speech unavailable"); }
  }

  return <div className="assistant-page"><div className="assistant-intro"><div className={`assistant-orb ${sending ? "thinking" : ""}`}><img src="/zipity-mark.png" alt="" /></div><span className="eyebrow">ZIPITY · AI STUDY PARTNER</span><h2>{turns.length ? "Learning together" : "What are we learning today?"}</h2><p>Clear answers, useful structure, and next steps grounded in your saved context.</p></div><div className="conversation">{turns.map((turn, index) => <article className={`chat-turn ${turn.role}`} key={index}><header className="turn-header">{turn.role === "assistant" ? <img src="/zipity-mark.png" alt="" /> : <span>Y</span>}<div><strong>{turn.role === "assistant" ? "Zipity" : "You"}</strong><small>{turn.role === "assistant" && sending && index === turns.length - 1 ? "Building your answer…" : turn.role === "assistant" ? "Structured response" : "Question"}</small></div></header>{turn.role === "assistant" ? <StructuredAnswer text={turn.content} streaming={sending && index === turns.length - 1} /> : <p className="user-message">{turn.content}</p>}{turn.role === "assistant" && turn.content && <footer className="turn-actions"><button className="small-button" onClick={() => void speak(plainSpeech(turn.content))}>▶ Read aloud</button></footer>}</article>)}</div>{contextCount > 0 && <p className="grounding-note">◇ Grounded with {contextCount} relevant memor{contextCount === 1 ? "y" : "ies"}</p>}{chatError && <p className="form-error assistant-error">{chatError}</p>}<form className="prompt-box" onSubmit={send}><textarea value={message} onChange={e => setMessage(e.target.value)} onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.currentTarget.form?.requestSubmit(); } }} aria-label="Message Zipity" placeholder="Ask Zipity anything about your work…" /><div><label className="remember-toggle"><input type="checkbox" checked={remember} onChange={e => setRemember(e.target.checked)} /> Remember this conversation</label><span>Baseten streaming · ElevenLabs Flash</span><button className="send" disabled={!message.trim() || sending} aria-label="Send">{sending ? "…" : "↑"}</button></div></form></div>;
}

function Empty({ icon, title, copy }: { icon: string; title: string; copy: string }) { return <div className="empty"><span>{icon}</span><strong>{title}</strong><small>{copy}</small></div>; }

function Composer({ kind, subjects, onClose, onSaved }: { kind: "assignment" | "subject" | "goal"; subjects: Subject[]; onClose: () => void; onSaved: () => Promise<void> }) {
  const [saving, setSaving] = useState(false); const [formError, setFormError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setSaving(true); setFormError(""); const data = new FormData(event.currentTarget);
    try {
      if (kind === "subject") await api("/subjects", { method: "POST", body: JSON.stringify({ title: data.get("title"), slug: String(data.get("title")).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") }) });
      else if (kind === "goal") await api("/goals", { method: "POST", body: JSON.stringify({ title: data.get("title"), subject_id: data.get("subject_id") || null, target_at: data.get("target_at") ? new Date(String(data.get("target_at"))).toISOString() : null }) });
      else await api("/assignments", { method: "POST", body: JSON.stringify({ title: data.get("title"), subject_id: data.get("subject_id"), due_at: new Date(String(data.get("due_at"))).toISOString(), estimated_minutes: Number(data.get("estimated_minutes")) }) });
      await onSaved();
    } catch (reason) { setFormError(reason instanceof Error ? reason.message : "Could not save"); setSaving(false); }
  }
  return <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><form className="composer" onSubmit={submit}><div className="composer-head"><div><span className="eyebrow">NEW {kind.toUpperCase()}</span><h2>{kind === "assignment" ? "Add work" : kind === "subject" ? "Create subject" : "Set a goal"}</h2></div><button type="button" className="close" onClick={onClose}>×</button></div><label>Title<input name="title" required autoFocus placeholder={kind === "assignment" ? "e.g. Science lab report" : `Name your ${kind}`} /></label>{kind !== "subject" && <label>Subject<select name="subject_id" required={kind === "assignment"}><option value="">{kind === "goal" ? "All subjects" : "Choose subject"}</option>{subjects.filter(s => s.status === "active").map(s => <option key={s.id} value={s.id}>{s.title}</option>)}</select></label>}{kind === "assignment" && <div className="form-row"><label>Due date<input name="due_at" type="datetime-local" required /></label><label>Estimate<input name="estimated_minutes" type="number" min="5" step="5" defaultValue="45" required /></label></div>}{kind === "goal" && <label>Target date<input name="target_at" type="date" /></label>}{formError && <p className="form-error">{formError}</p>}<button className="primary full" disabled={saving}>{saving ? "Saving…" : `Create ${kind}`}</button></form></div>;
}
