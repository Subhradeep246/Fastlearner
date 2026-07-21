import { FormEvent, useMemo, useState } from "react";

export type LearnerPreferences = {
  schema_version?: number;
  onboarding_completed?: boolean;
  demo_profile?: boolean;
  name?: string;
  school?: string;
  subjects?: string[];
  goals?: string;
  interests?: string;
  learning_style?: string;
  answer_style?: string;
  session_minutes?: number;
  daily_limit_minutes?: number;
  voice_enabled?: boolean;
  double_clap?: boolean;
  remember_chats?: boolean;
  [key: string]: unknown;
};

export type LearnerProfile = {
  user_id: string;
  owner_user_id: string;
  grade_level: number;
  timezone: string;
  study_preferences: LearnerPreferences;
};

export type LearnerSetup = {
  name: string;
  gradeLevel: number;
  school: string;
  subjects: string[];
  goals: string;
  interests: string;
  learningStyle: string;
  answerStyle: string;
  sessionMinutes: number;
  dailyLimitMinutes: number;
  voiceEnabled: boolean;
  doubleClap: boolean;
  rememberChats: boolean;
};

const DEMO: LearnerSetup = {
  name: "Alex (Demo)",
  gradeLevel: 10,
  school: "My School",
  subjects: ["Mathematics", "Science", "English", "Computer Science"],
  goals: "Build strong fundamentals, stay consistent, and prepare confidently for exams.",
  interests: "AI, coding, science, and building useful products",
  learningStyle: "Step-by-step explanations, worked examples, then short quizzes",
  answerStyle: "Clear and concise",
  sessionMinutes: 25,
  dailyLimitMinutes: 90,
  voiceEnabled: true,
  doubleClap: true,
  rememberChats: false,
};

export function setupFromProfile(profile: LearnerProfile): LearnerSetup {
  const preferences = profile.study_preferences ?? {};
  return {
    ...DEMO,
    name: typeof preferences.name === "string" ? preferences.name : DEMO.name,
    gradeLevel: profile.grade_level || DEMO.gradeLevel,
    school: typeof preferences.school === "string" ? preferences.school : DEMO.school,
    subjects: Array.isArray(preferences.subjects) && preferences.subjects.length
      ? preferences.subjects.filter((item): item is string => typeof item === "string")
      : DEMO.subjects,
    goals: typeof preferences.goals === "string" ? preferences.goals : DEMO.goals,
    interests: typeof preferences.interests === "string" ? preferences.interests : DEMO.interests,
    learningStyle: typeof preferences.learning_style === "string" ? preferences.learning_style : DEMO.learningStyle,
    answerStyle: typeof preferences.answer_style === "string" ? preferences.answer_style : DEMO.answerStyle,
    sessionMinutes: typeof preferences.session_minutes === "number" ? preferences.session_minutes : DEMO.sessionMinutes,
    dailyLimitMinutes: typeof preferences.daily_limit_minutes === "number" ? preferences.daily_limit_minutes : DEMO.dailyLimitMinutes,
    voiceEnabled: typeof preferences.voice_enabled === "boolean" ? preferences.voice_enabled : true,
    doubleClap: typeof preferences.double_clap === "boolean" ? preferences.double_clap : true,
    rememberChats: typeof preferences.remember_chats === "boolean" ? preferences.remember_chats : false,
  };
}

export function Onboarding({ profile, editing, onSave, onClose }: {
  profile: LearnerProfile;
  editing: boolean;
  onSave: (setup: LearnerSetup) => Promise<void>;
  onClose?: () => void;
}) {
  const initial = useMemo(() => setupFromProfile(profile), [profile]);
  const [name, setName] = useState(initial.name);
  const [gradeLevel, setGradeLevel] = useState(initial.gradeLevel);
  const [school, setSchool] = useState(initial.school);
  const [subjects, setSubjects] = useState(initial.subjects.join(", "));
  const [goals, setGoals] = useState(initial.goals);
  const [interests, setInterests] = useState(initial.interests);
  const [learningStyle, setLearningStyle] = useState(initial.learningStyle);
  const [answerStyle, setAnswerStyle] = useState(initial.answerStyle);
  const [sessionMinutes, setSessionMinutes] = useState(initial.sessionMinutes);
  const [dailyLimitMinutes, setDailyLimitMinutes] = useState(initial.dailyLimitMinutes);
  const [voiceEnabled, setVoiceEnabled] = useState(initial.voiceEnabled);
  const [doubleClap, setDoubleClap] = useState(initial.doubleClap);
  const [rememberChats, setRememberChats] = useState(initial.rememberChats);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    const subjectList = [...new Set(subjects.split(",").map(item => item.trim()).filter(Boolean))];
    if (!name.trim() || !subjectList.length) {
      setError("Add your preferred name and at least one subject.");
      return;
    }
    setSaving(true); setError("");
    try {
      await onSave({
        name: name.trim(), gradeLevel, school: school.trim(), subjects: subjectList,
        goals: goals.trim(), interests: interests.trim(), learningStyle: learningStyle.trim(),
        answerStyle: answerStyle.trim(), sessionMinutes, dailyLimitMinutes,
        voiceEnabled, doubleClap, rememberChats,
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not save your setup");
      setSaving(false);
    }
  }

  return <div className="onboarding-backdrop" role="presentation">
    <form className="onboarding-card" onSubmit={submit} aria-labelledby="setup-title">
      <header className="onboarding-head">
        <div><span className="eyebrow">{editing ? "YOUR ZIPITY PROFILE" : "WELCOME TO ZIPITY"}</span><h2 id="setup-title">{editing ? "Edit your context" : "Make Zipity yours."}</h2><p>{editing ? "Changes shape lessons and assistant answers." : "Demo details are prefilled. Review them once; every answer then fits you."}</p></div>
        {editing && onClose && <button type="button" className="close" onClick={onClose} aria-label="Close profile">×</button>}
      </header>

      <div className="onboarding-grid">
        <section><span className="setup-step">01 · YOU</span>
          <label>Preferred name<input autoFocus required value={name} onChange={event => setName(event.target.value)} placeholder="What should Zipity call you?" /></label>
          <div className="setup-row"><label>Grade<select value={gradeLevel} onChange={event => setGradeLevel(Number(event.target.value))}>{Array.from({ length: 10 }, (_, index) => index + 3).map(grade => <option key={grade} value={grade}>Grade {grade}</option>)}</select></label><label>School <small>optional</small><input value={school} onChange={event => setSchool(event.target.value)} placeholder="Your school" /></label></div>
          <label>Subjects <small>comma separated</small><input required value={subjects} onChange={event => setSubjects(event.target.value)} /></label>
          <label>Main goal<textarea value={goals} onChange={event => setGoals(event.target.value)} /></label>
        </section>

        <section><span className="setup-step">02 · HOW YOU LEARN</span>
          <label>Interests<textarea value={interests} onChange={event => setInterests(event.target.value)} /></label>
          <label>Teaching style<select value={learningStyle} onChange={event => setLearningStyle(event.target.value)}><option>Step-by-step explanations, worked examples, then short quizzes</option><option>Short explanation, then lots of practice</option><option>Visual analogies and real-world examples</option><option>Challenge me and give hints only when needed</option></select></label>
          <label>Answer style<select value={answerStyle} onChange={event => setAnswerStyle(event.target.value)}><option>Clear and concise</option><option>Detailed and thorough</option><option>Friendly and conversational</option></select></label>
          <div className="setup-row"><label>Focus session<input type="number" min="10" max="120" value={sessionMinutes} onChange={event => setSessionMinutes(Number(event.target.value))} /></label><label>Daily target<input type="number" min="10" max="480" value={dailyLimitMinutes} onChange={event => setDailyLimitMinutes(Number(event.target.value))} /></label></div>
        </section>
      </div>

      <section className="zipity-options"><div><span className="setup-step">03 · ZIPITY</span><strong>Voice companion</strong><small>Double clap wakes Zipity. Profile stays inside your owner-scoped local workspace.</small></div><label><input type="checkbox" checked={voiceEnabled} onChange={event => setVoiceEnabled(event.target.checked)} /> Voice replies</label><label><input type="checkbox" checked={doubleClap} onChange={event => setDoubleClap(event.target.checked)} /> Double-clap wake</label><label><input type="checkbox" checked={rememberChats} onChange={event => setRememberChats(event.target.checked)} /> Remember chats by default</label></section>
      {error && <p className="form-error">{error}</p>}
      <footer className="onboarding-actions"><p>Nothing here is assumed real until you save it. Edit anytime from profile.</p><button className="primary" disabled={saving}>{saving ? "Saving your workspace…" : editing ? "Save profile" : "Start with Zipity"}</button></footer>
    </form>
  </div>;
}
