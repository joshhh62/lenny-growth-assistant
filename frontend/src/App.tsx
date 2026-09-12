import { useCallback, useEffect, useRef, useState } from "react";
import { ArtifactPanel } from "./components/ArtifactPanel";
import { ChatView } from "./components/ChatView";
import type { Trace } from "./components/MessageItem";
import { Sidebar } from "./components/Sidebar";
import { HttpError, api, getUserId, streamMessage } from "./lib/api";
import type { AppConfig, ArtifactSummary, Citation, Message, Provider, Session, StreamEvent } from "./types";

type LiveProvider = { provider: Provider; model: string; runtime: string; fallback_used: boolean; reason: string };
type Toast = { id: number; kind: "err" | "warn" | "info"; text: string };

export default function App() {
  const userId = useRef(getUserId()).current;
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [apiDown, setApiDown] = useState(false);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [active, setActive] = useState<Session | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [artifacts, setArtifacts] = useState<ArtifactSummary[]>([]);
  const [activeArtifact, setActiveArtifact] = useState<string | null>(null);
  const [artifactOpen, setArtifactOpen] = useState(false);
  const isNarrow = useNarrow();
  const [sidebarOpen, setSidebarOpen] = useState(() => !window.matchMedia("(max-width: 900px)").matches);
  const [provider, setProvider] = useState<Provider>("ollama");
  const [streaming, setStreaming] = useState(false);
  const [trace, setTrace] = useState<Trace[]>([]);
  const [liveProvider, setLiveProvider] = useState<LiveProvider | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  const toast = useCallback((kind: Toast["kind"], text: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, kind, text }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 7000);
  }, []);

  // --- bootstrap: config + sessions, then poll config for provider health ---
  const loadConfig = useCallback(async () => {
    try {
      const c = await api.config();
      setConfig(c);
      setApiDown(false);
      return c;
    } catch (e) {
      setApiDown(true);
      if (e instanceof HttpError && e.code === "database_unavailable") toast("err", e.message);
      return null;
    }
  }, [toast]);

  const loadSessions = useCallback(async () => {
    try {
      setSessions(await api.listSessions(userId));
    } catch {
      /* apiDown banner covers it */
    }
  }, [userId]);

  useEffect(() => {
    (async () => {
      const c = await loadConfig();
      if (c) {
        // Prefer the configured default; if it's unavailable but the other is, start there.
        const def = c.providers.find((p) => p.name === c.default_provider);
        const other = c.providers.find((p) => p.name !== c.default_provider);
        setProvider(def?.available || !other?.available ? c.default_provider : other!.name);
      }
      await loadSessions();
    })();
    const t = setInterval(loadConfig, 20000);
    return () => clearInterval(t);
  }, [loadConfig, loadSessions]);

  // --- session selection ----------------------------------------------------
  const openSession = useCallback(async (id: string) => {
    try {
      const d = await api.getSession(id);
      setActive(d.session);
      setProvider(d.session.provider);
      setMessages(d.messages);
      setArtifacts(d.artifacts);
      setActiveArtifact(d.artifacts[0]?.id ?? null);
      setLiveProvider(null);
      setTrace([]);
      if (window.matchMedia("(max-width: 900px)").matches) setSidebarOpen(false);
    } catch (e) {
      toast("err", (e as Error).message);
    }
  }, [toast]);

  const newSession = useCallback(async (): Promise<Session | null> => {
    try {
      const s = await api.createSession(userId, provider);
      setSessions((l) => [s, ...l]);
      await openSession(s.id);
      return s;
    } catch (e) {
      toast("err", (e as Error).message);
      return null;
    }
  }, [userId, provider, openSession, toast]);

  const deleteSession = useCallback(async (id: string) => {
    if (!confirm("Delete this conversation? This cannot be undone.")) return;
    try {
      await api.deleteSession(id);
      setSessions((l) => l.filter((s) => s.id !== id));
      if (active?.id === id) {
        setActive(null);
        setMessages([]);
        setArtifacts([]);
        setActiveArtifact(null);
      }
    } catch (e) {
      toast("err", (e as Error).message);
    }
  }, [active, toast]);

  const changeProvider = useCallback(async (p: Provider) => {
    setProvider(p);
    if (!active) return;
    try {
      const s = await api.updateSession(active.id, { provider: p });
      setActive(s);
      setSessions((l) => l.map((x) => (x.id === s.id ? s : x)));
    } catch (e) {
      toast("err", (e as Error).message);
    }
  }, [active, toast]);

  const rename = useCallback(async (title: string) => {
    if (!active) return;
    const s = await api.updateSession(active.id, { title }).catch(() => null);
    if (s) {
      setActive(s);
      setSessions((l) => l.map((x) => (x.id === s.id ? s : x)));
    }
  }, [active]);

  // --- send & stream --------------------------------------------------------
  const send = useCallback(async (text: string, target?: Session) => {
    const sess = target ?? active;
    if (!sess || streaming) return;
    const sid = sess.id;
    const tempUser: Message = { id: "tmp-u-" + Date.now(), session_id: sid, role: "user", content: text, citations: [], tool_calls: [], provider: null, model: null, runtime: null, latency_ms: null, input_tokens: null, output_tokens: null, error: null, created_at: new Date().toISOString() };
    const tempId = "tmp-a-" + Date.now();
    const tempAsst: Message = { ...tempUser, id: tempId, role: "assistant", content: "", streaming: true, artifact_ids: [] };
    setMessages((m) => [...m, tempUser, tempAsst]);
    setStreaming(true);
    setTrace([]);
    setLiveProvider(null);
    if (messages.length === 0 || target) {
      const title = text.slice(0, 60);
      setSessions((l) => l.map((s) => (s.id === sid ? { ...s, title } : s)));
      setActive((s) => (s && s.id === sid && s.title === "New chat" ? { ...s, title } : s));
    }

    let cites: Citation[] = [];
    const ac = new AbortController();
    abortRef.current = ac;
    const patch = (fn: (m: Message) => Message) => setMessages((list) => list.map((m) => (m.id === tempId ? fn(m) : m)));

    const onEvent = (e: StreamEvent) => {
      switch (e.type) {
        case "provider":
          setLiveProvider(e);
          if (e.fallback_used) toast("warn", `${e.reason}; answered with ${e.provider === "ollama" ? "the local model" : "Claude"} instead.`);
          break;
        case "status":
          setTrace((t) => [...t.map((x) => ({ ...x, done: true })), { text: e.text }]);
          break;
        case "tool_call":
          setTrace((t) => [...t.map((x) => ({ ...x, done: true })), { text: summariseInput(e.input), tool: e.name }]);
          break;
        case "tool_result":
          setTrace((t) => t.map((x) => ({ ...x, done: true })));
          break;
        case "token":
          setTrace((t) => t.map((x) => ({ ...x, done: true })));
          patch((m) => ({ ...m, content: m.content + e.text }));
          break;
        case "citations":
          cites = e.items;
          patch((m) => ({ ...m, citations: e.items }));
          break;
        case "artifact": {
          const a: ArtifactSummary = { id: e.id, session_id: sid, message_id: null, kind: e.kind, title: e.title, created_at: new Date().toISOString() };
          setArtifacts((l) => [a, ...l]);
          setActiveArtifact(e.id);
          setArtifactOpen(true);
          patch((m) => ({ ...m, artifact_ids: [...(m.artifact_ids ?? []), e.id] }));
          break;
        }
        case "done":
          setTrace([]);
          patch((m) => ({ ...m, id: e.message_id, streaming: false, latency_ms: e.latency_ms, citations: cites, provider: liveProviderRef.current?.provider ?? m.provider, model: liveProviderRef.current?.model ?? m.model }));
          setArtifacts((l) => l.map((a) => (e.artifacts.includes(a.id) ? { ...a, message_id: e.message_id } : a)));
          break;
        case "error":
          setTrace([]);
          patch((m) => ({ ...m, streaming: false, error: e.code, content: m.content || `⚠️ ${e.message}` }));
          toast(e.retryable ? "warn" : "err", e.message);
          break;
      }
    };
    await streamMessage(sid, text, onEvent, ac.signal);
    setStreaming(false);
    setTrace([]);
    abortRef.current = null;
    patch((m) => ({ ...m, streaming: false }));
    loadSessions();
    loadConfig();
  }, [active, streaming, messages.length, toast, loadSessions, loadConfig]);

  const liveProviderRef = useRef<LiveProvider | null>(null);
  useEffect(() => {
    liveProviderRef.current = liveProvider;
  }, [liveProvider]);

  const stop = useCallback(() => abortRef.current?.abort(), []);

  // On narrow screens the sidebar is a drawer: close it when the layout narrows
  // and whenever an artifact opens (only one overlay at a time).
  useEffect(() => {
    if (isNarrow) setSidebarOpen(false);
  }, [isNarrow]);
  useEffect(() => {
    if (isNarrow && artifactOpen) setSidebarOpen(false);
  }, [isNarrow, artifactOpen]);

  // --- keyboard ---------------------------------------------------------------
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === "Escape") setArtifactOpen(false);
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  const cls = ["app", artifactOpen && activeArtifact ? "artifact-open" : "", sidebarOpen ? "sidebar-open" : "sidebar-collapsed"].join(" ");

  return (
    <div className={cls}>
      {sidebarOpen && isNarrow && <div className="scrim" onClick={() => setSidebarOpen(false)} aria-hidden="true" />}
      <Sidebar
        sessions={sessions}
        activeId={active?.id ?? null}
        config={config}
        provider={provider}
        busy={streaming}
        onNew={newSession}
        onSelect={openSession}
        onDelete={deleteSession}
        onProvider={changeProvider}
        onClose={() => setSidebarOpen(false)}
      />
      <div style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
        {apiDown && (
          <div className="banner err" role="alert" style={{ margin: 12, marginBottom: 0 }}>
            <span className="grow">
              <b>Can't reach the API.</b> Start the backend (see README → Run) — this page will reconnect automatically.
            </span>
          </div>
        )}
        {!apiDown && config && !config.providers.some((p) => p.available) && (
          <div className="banner warn" role="status" style={{ margin: 12, marginBottom: 0 }}>
            <span className="grow">
              <b>No model available.</b> Start Ollama (<code>ollama serve</code>, then <code>ollama pull {config.providers.find((p) => p.name === "ollama")?.model}</code>) or set <code>ANTHROPIC_API_KEY</code>.
            </span>
          </div>
        )}
        <ChatView
          session={active}
          messages={messages}
          streaming={streaming}
          trace={trace}
          artifacts={artifacts}
          liveProvider={liveProvider}
          onSend={(t) => (active ? send(t) : newSession().then((s) => s && send(t, s)))}
          onStop={stop}
          onOpenArtifact={(id) => {
            setActiveArtifact(id);
            setArtifactOpen(true);
          }}
          onToggleSidebar={() => setSidebarOpen((v) => !v)}
          onToggleArtifacts={() => setArtifactOpen((v) => !v)}
          onRename={rename}
          artifactCount={artifacts.length}
        />
      </div>
      <ArtifactPanel artifacts={artifacts} activeId={activeArtifact} onSelect={setActiveArtifact} onClose={() => setArtifactOpen(false)} />
      <div className="toasts" aria-live="assertive">
        {toasts.map((t) => (
          <div key={t.id} className={"banner " + t.kind} role={t.kind === "err" ? "alert" : "status"}>
            <span className="grow">{t.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function useNarrow(query = "(max-width: 900px)"): boolean {
  const [narrow, setNarrow] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const h = (e: MediaQueryListEvent) => setNarrow(e.matches);
    mq.addEventListener("change", h);
    return () => mq.removeEventListener("change", h);
  }, [query]);
  return narrow;
}

function summariseInput(input: Record<string, string>): string {
  const q = input.query ?? input.topic ?? input.title;
  return q ? `“${String(q).slice(0, 70)}”` : "";
}
