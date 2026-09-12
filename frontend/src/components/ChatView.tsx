import { ArrowUp, Menu, PanelRight, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ArtifactSummary, Message, Provider, Session } from "../types";
import { MessageItem, type Trace } from "./MessageItem";

const SUGGESTIONS: { kind: string; text: string }[] = [
  { kind: "Ask", text: "What does Elena Verna say retention has to do with growth?" },
  { kind: "Ask", text: "How should an early-stage B2B team think about pricing?" },
  { kind: "Essay", text: "Write a Ship 30 for 30 essay on finding product-market fit" },
  { kind: "Artifact", text: "Create a markdown checklist for running a good user interview" },
  { kind: "Artifact", text: "Make an HTML one-pager summarising the best advice on activation" },
  { kind: "Ask", text: "What do guests say about when to hire your first PM?" },
];

export function ChatView({
  session,
  messages,
  streaming,
  trace,
  artifacts,
  liveProvider,
  onSend,
  onStop,
  onOpenArtifact,
  onToggleSidebar,
  onToggleArtifacts,
  onRename,
  artifactCount,
}: {
  session: Session | null;
  messages: Message[];
  streaming: boolean;
  trace: Trace[];
  artifacts: ArtifactSummary[];
  liveProvider: { provider: Provider; model: string; runtime: string; fallback_used: boolean; reason: string } | null;
  onSend: (text: string) => void;
  onStop: () => void;
  onOpenArtifact: (id: string) => void;
  onToggleSidebar: () => void;
  onToggleArtifacts: () => void;
  onRename: (title: string) => void;
  artifactCount: number;
}) {
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  // Keep the newest content in view while streaming, unless the user scrolled up.
  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
    if (nearBottom || !streaming) el.scrollTop = el.scrollHeight;
  }, [messages, trace, streaming]);

  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
  }, [draft]);

  const submit = () => {
    const t = draft.trim();
    if (!t || streaming) return;
    setDraft("");
    onSend(t);
    taRef.current?.focus();
  };

  const pill = liveProvider ?? (session ? { provider: session.provider, model: session.model, runtime: "", fallback_used: false, reason: "" } : null);

  return (
    <main className="chat">
      <header className="chat-head">
        <button className="btn btn-ghost btn-icon" onClick={onToggleSidebar} aria-label="Toggle conversations">
          <Menu size={18} />
        </button>
        <div className="chat-title">
          {session && editing ? (
            <input
              autoFocus
              defaultValue={session.title}
              aria-label="Conversation title"
              onBlur={(e) => {
                setEditing(false);
                if (e.target.value.trim() && e.target.value !== session.title) onRename(e.target.value.trim());
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                if (e.key === "Escape") setEditing(false);
              }}
            />
          ) : (
            <span onDoubleClick={() => session && setEditing(true)} title="Double-click to rename">
              {session?.title ?? "Lenny Growth Assistant"}
            </span>
          )}
        </div>
        {pill && (
          <span className={"pill " + (pill.fallback_used ? "warn" : "ok")} title={pill.fallback_used ? pill.reason : pill.runtime}>
            {pill.provider === "ollama" ? "Local" : "Cloud"} · <code>{pill.model}</code>
            {pill.fallback_used && " · fallback"}
          </span>
        )}
        <button className="btn btn-ghost btn-icon" onClick={onToggleArtifacts} aria-label="Toggle artifact panel" title="Artifacts">
          <PanelRight size={18} />
          {artifactCount > 0 && <span className="sr-only">{artifactCount} artifacts</span>}
        </button>
      </header>

      <div className="messages" ref={listRef} role="log" aria-live="polite" aria-relevant="additions">
        {messages.length === 0 ? (
          <div className="suggestions">
            <h1>Ask the podcast.</h1>
            <p className="lead">
              Grounded answers, essays and documents built only from Lenny's Podcast transcripts — every claim cites the episode and the minute.
            </p>
            <div className="suggestion-grid">
              {SUGGESTIONS.map((s) => (
                <button key={s.text} className="suggestion" onClick={() => onSend(s.text)} disabled={streaming}>
                  <span className="kind">{s.kind}</span>
                  <span className="text">{s.text}</span>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="messages-inner">
            {messages.map((m, i) => (
              <MessageItem
                key={m.id}
                msg={m}
                trace={i === messages.length - 1 && m.role === "assistant" && (m.streaming || trace.length > 0) ? trace : []}
                artifacts={artifacts}
                onOpenArtifact={onOpenArtifact}
              />
            ))}
          </div>
        )}
      </div>

      <div className="composer-wrap">
        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <textarea
            ref={taRef}
            rows={1}
            value={draft}
            placeholder="Ask a product or growth question…"
            aria-label="Message"
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
          />
          {streaming ? (
            <button type="button" className="btn btn-icon" onClick={onStop} aria-label="Stop generating" title="Stop">
              <Square size={16} />
            </button>
          ) : (
            <button type="submit" className="btn btn-accent btn-icon" disabled={!draft.trim()} aria-label="Send" title="Send (Enter)">
              <ArrowUp size={18} />
            </button>
          )}
        </form>
        <div className="composer-hint">
          <span>
            <kbd>Enter</kbd> send · <kbd>Shift</kbd>+<kbd>Enter</kbd> newline · <kbd>Esc</kbd> close panel
          </span>
          <span>Answers come only from the transcripts; check the sources.</span>
        </div>
      </div>
    </main>
  );
}
