import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import "./styles.css";

const API = "http://127.0.0.1:8001/v1";
type View = "Today" | "Work" | "Learn" | "Memory" | "Assistant";
type Subject = { id: string; title: string; slug: string; status: string };
type Assignment = {
  id: string; subject_id: string; title: string; status: string;
  due_at: string; estimated_minutes: number;
};
type Goal = { id: string; title: string; status: string; target_at?: string };

async function sessionToken(): Promise<string> {
  const cached = localStorage.getItem("fastlearner.token");
  if (cached) return cached;
  const response = await fetch(`${API}/local/session`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ persona: "learner" }),
  });
  if (!response.ok) throw new Error("Local API unavailable");
  const data = await response.json() as { token: string };
  localStorage.setItem("fastlearner.token", data.token);
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
    localStorage.removeItem("fastlearner.token");
    if (!init?.headers || !(init.headers as Record<string, string>)["X-Retry"]) {
      return api<T>(path, { ...init, headers: { ...(init?.headers ?? {}), "X-Retry": "1" } });
    }
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { error?: { message?: string } };
    throw new Error(body.error?.message ?? `Request failed (${response.status})`);
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
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [compose, setCompose] = useState<"assignment" | "subject" | "goal" | null>(null);

  const refresh = useCallback(async () => {
    try {
      setError("");
      const [nextSubjects, nextAssignments, nextGoals] = await Promise.all([
        api<Subject[]>("/subjects"), api<Assignment[]>("/assignments"), api<Goal[]>("/goals"),
      ]);
      setSubjects(nextSubjects); setAssignments(nextAssignments); setGoals(nextGoals);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not connect");
    } finally { setBusy(false); }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);
  const active = assignments.filter(item => item.status !== "done" && item.status !== "archived");
  const focusMinutes = active.reduce((sum, item) => sum + item.estimated_minutes, 0);

  async function assignmentAction(item: Assignment, action: "start" | "complete") {
    try {
      await api(`/assignments/${item.id}/actions`, { method: "POST", body: JSON.stringify({ action }) });
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Update failed"); }
  }

  return (
    <div className="fl-app">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">F</span><span>FastLearner</span></div>
        <nav aria-label="Main navigation">
          {(["Today", "Work", "Learn", "Memory", "Assistant"] as View[]).map((item, index) => (
            <button key={item} className={view === item ? "nav-item active" : "nav-item"} onClick={() => setView(item)}>
              <span aria-hidden="true">{["⌂", "□", "◫", "◇", "✦"][index]}</span>{item}
            </button>
          ))}
        </nav>
        <div className="side-bottom">
          <div className="profile-dot">A</div><div><strong>Alex</strong><small>Local workspace</small></div>
          <span className="online" title="API connected" />
        </div>
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
      {next.length ? <div className="task-list">{next.slice(0, 4).map(item => <TaskRow key={item.id} item={item} onAction={onAction} />)}</div> : <Empty icon="✓" title="Nothing urgent" copy="Add an assignment and FastLearner will organize your next steps." />}
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
  return <div className="stack"><div className="section-title"><div><span className="eyebrow">SCHOOLWORK</span><h2>Everything you’re working on</h2></div><button className="secondary" onClick={onAddSubject}>New subject</button></div>
    <div className="subject-pills">{subjects.map(s => <span key={s.id}>{s.title}</span>)}</div>
    <section className="panel"><div className="table-head"><span>Assignment</span><span>Subject</span><span>Due</span><span>Status</span><span /></div>{assignments.length ? assignments.map(item => <div className="work-row" key={item.id}><strong>{item.title}</strong><span>{names[item.subject_id] ?? "General"}</span><span>{new Date(item.due_at).toLocaleDateString()}</span><span className={`status ${item.status}`}>{item.status.replace("_", " ")}</span><button className="small-button" onClick={() => void onAction(item, item.status === "pending" ? "start" : "complete")}>{item.status === "pending" ? "Start" : "Done"}</button></div>) : <Empty icon="□" title="No assignments yet" copy="Add first assignment from top-right button." />}</section>
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
function Assistant() {
  const [message, setMessage] = useState(""); const [reply, setReply] = useState("");
  const [sending, setSending] = useState(false); const [chatError, setChatError] = useState("");
  async function send(event: FormEvent) {
    event.preventDefault(); if (!message.trim() || sending) return;
    setSending(true); setChatError(""); setReply("");
    try { const result = await api<{ message: string }>("/assistant/chat", { method: "POST", body: JSON.stringify({ message }) }); setReply(result.message); }
    catch (reason) { setChatError(reason instanceof Error ? reason.message : "Assistant unavailable"); }
    finally { setSending(false); }
  }
  async function speak() {
    try {
      const token = await sessionToken(); const response = await fetch(`${API}/assistant/speech`, { method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` }, body: JSON.stringify({ text: reply }) });
      if (!response.ok) throw new Error("Add ElevenLabs key to enable voice");
      const audio = new Audio(URL.createObjectURL(await response.blob())); await audio.play();
    } catch (reason) { setChatError(reason instanceof Error ? reason.message : "Speech unavailable"); }
  }
  return <div className="assistant-page"><div className="assistant-intro"><div className="assistant-orb">✦</div><span className="eyebrow">AI STUDY PARTNER</span><h2>What are we learning today?</h2><p>Ask for explanations, a study plan, quiz questions, or help breaking down work.</p></div>{reply && <article className="assistant-reply"><span>FASTLEARNER</span><p>{reply}</p><button className="small-button" onClick={() => void speak()}>▶ Read aloud</button></article>}{chatError && <p className="form-error assistant-error">{chatError}</p>}<form className="prompt-box" onSubmit={send}><textarea value={message} onChange={e => setMessage(e.target.value)} aria-label="Message FastLearner" placeholder="Ask anything about your work…" /><div><span>Baseten · GPT-OSS 120B</span><button className="send" disabled={!message.trim() || sending} aria-label="Send">{sending ? "…" : "↑"}</button></div></form></div>;
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
