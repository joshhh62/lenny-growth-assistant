import { AlertTriangle, ExternalLink, FileText, Layout, Search, Sparkles } from "lucide-react";
import { useState } from "react";
import type { ArtifactSummary, Citation, Message } from "../types";
import { Markdown, fmtTs } from "./Markdown";

export interface Trace {
  text: string;
  done?: boolean;
  tool?: string;
}

export function MessageItem({
  msg,
  trace = [],
  artifacts = [],
  onOpenArtifact,
}: {
  msg: Message;
  trace?: Trace[];
  artifacts?: ArtifactSummary[];
  onOpenArtifact: (id: string) => void;
}) {
  const [hl, setHl] = useState<number | null>(null);
  if (msg.role === "user") {
    return (
      <article className="msg msg-user" aria-label="You">
        <div className="msg-body">{msg.content}</div>
      </article>
    );
  }
  const isError = Boolean(msg.error);
  const cites = msg.citations ?? [];
  const linked = artifacts.filter((a) => a.message_id === msg.id || msg.artifact_ids?.includes(a.id));

  return (
    <article className={"msg msg-assistant" + (isError ? " msg-error" : "")} aria-label="Assistant" aria-busy={msg.streaming || undefined}>
      <div className="msg-role">
        {isError ? <AlertTriangle size={13} /> : <Sparkles size={13} />}
        {isError ? "Couldn't answer" : "Assistant"}
        {msg.provider && !msg.streaming && (
          <span style={{ fontWeight: 400 }}>
            · {msg.provider === "ollama" ? "local" : "cloud"} · {msg.model}
            {msg.latency_ms != null && ` · ${(msg.latency_ms / 1000).toFixed(1)}s`}
          </span>
        )}
      </div>

      {trace.length > 0 && (
        <div className="trace" aria-live="polite">
          {trace.map((t, i) => (
            <div className="trace-item" key={i}>
              {t.done ? (t.tool ? <Search size={13} /> : <span style={{ width: 12 }} />) : <span className="spinner" aria-hidden="true" />}
              {t.tool ? (
                <span>
                  <code>{t.tool}</code> {t.text}
                </span>
              ) : (
                t.text
              )}
            </div>
          ))}
        </div>
      )}

      <div className="msg-body">
        {msg.content ? <Markdown text={msg.content} citations={cites} onCite={(n) => setHl(n)} /> : null}
        {msg.streaming && <span className="cursor" aria-hidden="true" />}
      </div>

      {linked.map((a) => (
        <button key={a.id} className="artifact-chip" onClick={() => onOpenArtifact(a.id)}>
          {a.kind === "html" ? <Layout size={15} /> : <FileText size={15} />}
          {a.title}
          <span className="kind">{a.kind}</span>
        </button>
      ))}

      {cites.length > 0 && !msg.streaming && <Sources cites={cites} hl={hl} />}
    </article>
  );
}

function Sources({ cites, hl }: { cites: Citation[]; hl: number | null }) {
  const [open, setOpen] = useState<Set<number>>(new Set());
  const toggle = (n: number) =>
    setOpen((s) => {
      const nx = new Set(s);
      nx.has(n) ? nx.delete(n) : nx.add(n);
      return nx;
    });
  return (
    <div className="sources">
      <div className="sources-head">Sources · {cites.length}</div>
      <div className="source-list">
        {cites.map((c) => (
          <div key={c.n} className={"source" + (hl === c.n ? " hl" : "")} id={`src-${c.n}`} aria-expanded={open.has(c.n)}>
            <span className="source-n">[{c.n}]</span>
            <div>
              <div className="source-title">{c.guest}</div>
              <div className="source-meta">
                <span>{c.title}</span>
                {c.timestamp_url && (
                  <a href={c.timestamp_url} target="_blank" rel="noopener noreferrer">
                    {c.start_seconds != null ? fmtTs(c.start_seconds) : "watch"} <ExternalLink size={11} style={{ verticalAlign: -1 }} />
                  </a>
                )}
              </div>
              <div className="source-quote">{c.text}</div>
              <button className="source-toggle" onClick={() => toggle(c.n)}>
                {open.has(c.n) ? "Show less" : "Show passage"}
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
