import { Code2, Copy, Download, Eye, ShieldCheck, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import type { Artifact, ArtifactSummary } from "../types";
import { Markdown } from "./Markdown";

/**
 * Artifact viewer.
 *
 * HTML artifacts render in an <iframe sandbox=""> — the empty sandbox attribute
 * is the strictest setting: unique opaque origin, no scripts, no forms, no
 * popups, no top-navigation, no same-origin access. The document itself also
 * carries a server-injected CSP (see backend/app/artifacts/sanitize.py). The
 * server has already allow-listed the markup, so this is the second of two
 * independent layers.
 *
 * Markdown artifacts render through react-markdown with raw HTML disabled.
 */
export function ArtifactPanel({
  artifacts,
  activeId,
  onSelect,
  onClose,
}: {
  artifacts: ArtifactSummary[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onClose: () => void;
}) {
  const [cache, setCache] = useState<Record<string, Artifact>>({});
  const [view, setView] = useState<"preview" | "source">("preview");
  const [err, setErr] = useState<string | null>(null);
  const active = activeId ? cache[activeId] : undefined;

  useEffect(() => {
    if (!activeId || cache[activeId]) return;
    let cancelled = false;
    setErr(null);
    api
      .getArtifact(activeId)
      .then((a) => !cancelled && setCache((c) => ({ ...c, [a.id]: a })))
      .catch((e) => !cancelled && setErr(e.message));
    return () => {
      cancelled = true;
    };
  }, [activeId, cache]);

  useEffect(() => setView("preview"), [activeId]);

  const copy = async () => {
    if (!active) return;
    try {
      await navigator.clipboard.writeText(active.content);
    } catch {
      /* clipboard unavailable */
    }
  };
  const download = () => {
    if (!active) return;
    const blob = new Blob([active.content], { type: active.kind === "html" ? "text/html" : "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${active.title.replace(/[^\w\- ]+/g, "").trim() || "artifact"}.${active.kind === "html" ? "html" : "md"}`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const bytes = useMemo(() => (active ? new Blob([active.content]).size : 0), [active]);

  return (
    <section className="artifact-panel" aria-label="Artifact viewer">
      <div className="artifact-head">
        <span className="artifact-title">{active?.title ?? "Artifacts"}</span>
        <div className="segmented" style={{ gridTemplateColumns: "auto auto" }} role="group" aria-label="View mode">
          <button aria-pressed={view === "preview"} onClick={() => setView("preview")} disabled={!active}>
            <Eye size={14} /> Preview
          </button>
          <button aria-pressed={view === "source"} onClick={() => setView("source")} disabled={!active}>
            <Code2 size={14} /> Source
          </button>
        </div>
        <button className="btn btn-ghost btn-icon" onClick={copy} disabled={!active} aria-label="Copy source" title="Copy source">
          <Copy size={16} />
        </button>
        <button className="btn btn-ghost btn-icon" onClick={download} disabled={!active} aria-label="Download" title="Download">
          <Download size={16} />
        </button>
        <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close artifact panel" title="Close (Esc)">
          <X size={16} />
        </button>
      </div>

      {artifacts.length > 1 && (
        <div className="artifact-tabs" role="tablist">
          {artifacts.map((a) => (
            <button key={a.id} role="tab" className="artifact-tab" aria-selected={a.id === activeId} onClick={() => onSelect(a.id)} title={a.title}>
              {a.title}
            </button>
          ))}
        </div>
      )}

      <div className="artifact-body">
        {!activeId && <div className="artifact-empty">Artifacts you ask for — essays, documents, HTML pages — open here beside the chat.</div>}
        {activeId && err && <div className="artifact-empty">Couldn't load this artifact: {err}</div>}
        {activeId && !active && !err && <div className="artifact-empty">Loading…</div>}
        {active && view === "source" && <pre className="artifact-code">{active.content}</pre>}
        {active && view === "preview" && active.kind === "html" && (
          <iframe
            title={active.title}
            sandbox=""
            referrerPolicy="no-referrer"
            srcDoc={active.content}
          />
        )}
        {active && view === "preview" && active.kind === "markdown" && (
          <div className="artifact-md">
            <Markdown text={active.content} />
          </div>
        )}
      </div>

      <div className="artifact-foot">
        <ShieldCheck size={14} />
        <span>
          {active?.kind === "html"
            ? "Sandboxed: server-sanitized HTML, CSP-locked, rendered in an isolated iframe (no scripts, forms, or network)."
            : "Rendered from Markdown with raw HTML disabled."}
        </span>
        <span className="grow" />
        {active && (
          <span>
            {active.kind} · {(bytes / 1024).toFixed(1)} KB
          </span>
        )}
      </div>
    </section>
  );
}
