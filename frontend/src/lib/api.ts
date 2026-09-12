import type { AppConfig, Artifact, Provider, Session, SessionDetail, StreamEvent } from "../types";

const BASE = import.meta.env.VITE_API_BASE || "";

export class HttpError extends Error {
  constructor(public status: number, public code: string, message: string, public requestId?: string) {
    super(message);
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + path, { headers: { "content-type": "application/json" }, ...init });
  } catch {
    throw new HttpError(0, "network", "Can't reach the API. Is the backend running?");
  }
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = body?.error ?? {};
    throw new HttpError(res.status, e.code ?? "http_error", e.message ?? res.statusText, e.request_id);
  }
  return body as T;
}

export const api = {
  config: () => req<AppConfig>("/api/config"),
  listSessions: (userId: string) => req<Session[]>(`/api/sessions?user_id=${encodeURIComponent(userId)}`),
  createSession: (userId: string, provider?: Provider) =>
    req<Session>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ user_id: userId, provider, metadata: { locale: navigator.language, tz: Intl.DateTimeFormat().resolvedOptions().timeZone } }),
    }),
  getSession: (id: string) => req<SessionDetail>(`/api/sessions/${id}`),
  updateSession: (id: string, patch: { title?: string; provider?: Provider }) =>
    req<Session>(`/api/sessions/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteSession: (id: string) => req<void>(`/api/sessions/${id}`, { method: "DELETE" }),
  getArtifact: (id: string) => req<Artifact>(`/api/artifacts/${id}`),
};

/** POST a message and stream Server-Sent Events back. Resolves when the stream ends. */
export async function streamMessage(
  sessionId: string,
  content: string,
  onEvent: (e: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${BASE}/api/sessions/${sessionId}/messages`, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "text/event-stream" },
      body: JSON.stringify({ content }),
      signal,
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") return;
    onEvent({ type: "error", code: "network", message: "Can't reach the API. Is the backend running?", retryable: true });
    return;
  }
  if (!res.ok || !res.body) {
    const body = await res.json().catch(() => ({}));
    const e = body?.error ?? {};
    onEvent({ type: "error", code: e.code ?? `http_${res.status}`, message: e.message ?? `Request failed (${res.status})`, retryable: res.status >= 500 });
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        for (const line of block.split("\n")) {
          if (line.startsWith("data: ")) {
            try {
              onEvent(JSON.parse(line.slice(6)) as StreamEvent);
            } catch {
              /* ignore malformed frame */
            }
          }
        }
      }
    }
  } catch (err) {
    if ((err as Error).name !== "AbortError") {
      onEvent({ type: "error", code: "stream", message: "The connection dropped mid-answer.", retryable: true });
    }
  }
}

export function getUserId(): string {
  try {
    const existing = localStorage.getItem("lga.user_id");
    if (existing) return existing;
    const id = "u_" + crypto.randomUUID().replace(/-/g, "").slice(0, 16);
    localStorage.setItem("lga.user_id", id);
    return id;
  } catch {
    return "u_anonymous";
  }
}
