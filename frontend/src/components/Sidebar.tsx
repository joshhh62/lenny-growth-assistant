import { Cloud, Cpu, Plus, Trash2, X } from "lucide-react";
import type { AppConfig, Provider, Session } from "../types";

export function Sidebar({
  sessions,
  activeId,
  config,
  provider,
  busy,
  onNew,
  onSelect,
  onDelete,
  onProvider,
  onClose,
}: {
  sessions: Session[];
  activeId: string | null;
  config: AppConfig | null;
  provider: Provider;
  busy: boolean;
  onNew: () => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onProvider: (p: Provider) => void;
  onClose: () => void;
}) {
  const info = (name: Provider) => config?.providers.find((p) => p.name === name);
  const local = info("ollama");
  const cloud = info("anthropic");
  const chosen = info(provider);

  return (
    <aside className="sidebar" aria-label="Conversations">
      <div className="sidebar-head">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">L</span>
          <span>Lenny Growth Assistant</span>
        </div>
        <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close sidebar" style={{ marginLeft: "auto" }}>
          <X size={16} />
        </button>
      </div>
      <div className="sidebar-actions">
        <button className="btn btn-primary btn-block" onClick={onNew} disabled={busy}>
          <Plus size={16} /> New chat
        </button>
      </div>

      <div className="session-list-label">Recent</div>
      <ul className="session-list">
        {sessions.length === 0 && <li className="empty-hint">No conversations yet.</li>}
        {sessions.map((s) => (
          <li key={s.id} className={"session-item" + (s.id === activeId ? " active" : "")}>
            <button className="session-btn" onClick={() => onSelect(s.id)} aria-current={s.id === activeId ? "page" : undefined}>
              <span className="session-title">{s.title}</span>
              <span className="session-meta">
                {s.provider === "ollama" ? "Local" : "Cloud"} · {relTime(s.updated_at)}
              </span>
            </button>
            <button className="btn btn-ghost btn-icon session-del" aria-label={`Delete "${s.title}"`} onClick={() => onDelete(s.id)}>
              <Trash2 size={14} />
            </button>
          </li>
        ))}
      </ul>

      <div className="sidebar-foot">
        <div className="provider-toggle" role="group" aria-label="Model provider">
          <span className="provider-toggle-label">Model</span>
          <div className="segmented">
            <button aria-pressed={provider === "ollama"} onClick={() => onProvider("ollama")} disabled={busy} title={local?.reason}>
              <span className={"dot " + (local ? (local.available ? "ok" : "bad") : "")} /> <Cpu size={14} /> Local
            </button>
            <button aria-pressed={provider === "anthropic"} onClick={() => onProvider("anthropic")} disabled={busy} title={cloud?.reason}>
              <span className={"dot " + (cloud ? (cloud.available ? "ok" : "bad") : "")} /> <Cloud size={14} /> Cloud
            </button>
          </div>
          {chosen && (
            <div className={"provider-note" + (chosen.available ? "" : " bad")}>
              {chosen.available ? (
                <>
                  <b>{chosen.model}</b> via {chosen.runtime === "agent_sdk" ? "Claude Agent SDK" : "Messages loop"}
                  {chosen.warning && <div className="provider-note bad" style={{ marginTop: 4 }}>⚠ {chosen.warning}</div>}
                </>
              ) : (
                <>
                  {chosen.reason}
                  {config?.fallback_provider && config.fallback_provider !== provider && info(config.fallback_provider)?.available
                    ? ` — will fall back to ${config.fallback_provider}.`
                    : ""}
                </>
              )}
            </div>
          )}
        </div>
        {config && (
          <div className="kv" title={config.retrieval.last_ingested_at ? `Indexed ${new Date(config.retrieval.last_ingested_at).toLocaleString()}` : ""}>
            <span>Knowledge base</span>
            <b>
              {config.retrieval.episodes} episodes
              {config.retrieval.chunks > 0 && ` · ${Math.round((100 * config.retrieval.embedded) / config.retrieval.chunks)}% embedded`}
            </b>
          </div>
        )}
      </div>
    </aside>
  );
}

function relTime(iso: string): string {
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  if (d < 60) return "now";
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}
