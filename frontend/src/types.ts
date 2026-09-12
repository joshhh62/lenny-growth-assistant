export type Provider = "anthropic" | "ollama";

export interface ProviderInfo {
  name: Provider;
  available: boolean;
  model: string;
  reason: string;
  latency_ms: number | null;
  runtime: string;
}

export interface AppConfig {
  app_name: string;
  default_provider: Provider;
  fallback_provider: Provider | null;
  providers: ProviderInfo[];
  retrieval: { episodes: number; chunks: number; embedded: number; last_ingested_at: string | null; top_k: number; mode: string };
}

export interface Session {
  id: string;
  user_id: string | null;
  title: string;
  provider: Provider;
  model: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  message_count?: number | null;
}

export interface Citation {
  n: number;
  id: number;
  episode_id: string;
  guest: string;
  title: string;
  speaker: string | null;
  start_seconds: number | null;
  end_seconds: number | null;
  youtube_url: string | null;
  timestamp_url: string | null;
  publish_date: string | null;
  text: string;
  score: number;
  sources: string[];
}

export interface ToolCall {
  name: string;
  input: Record<string, string>;
}

export interface Message {
  id: string;
  session_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  citations: Citation[];
  tool_calls: ToolCall[];
  provider: string | null;
  model: string | null;
  runtime: string | null;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  error: string | null;
  created_at: string;
  // client-only
  streaming?: boolean;
  artifact_ids?: string[];
}

export interface ArtifactSummary {
  id: string;
  session_id: string;
  message_id: string | null;
  kind: "markdown" | "html";
  title: string;
  created_at: string;
}

export interface Artifact extends ArtifactSummary {
  content: string;
}

export interface SessionDetail {
  session: Session;
  messages: Message[];
  artifacts: ArtifactSummary[];
}

export type StreamEvent =
  | { type: "provider"; provider: Provider; model: string; runtime: string; fallback_used: boolean; reason: string }
  | { type: "status"; text: string }
  | { type: "token"; text: string }
  | { type: "tool_call"; name: string; input: Record<string, string> }
  | { type: "tool_result"; name: string; summary: string }
  | { type: "citations"; items: Citation[]; final?: boolean }
  | { type: "artifact"; id: string; kind: "markdown" | "html"; title: string; sanitize_report?: Record<string, unknown> }
  | { type: "done"; message_id: string; latency_ms: number; usage: Record<string, number>; artifacts: string[] }
  | { type: "error"; code: string; message: string; retryable: boolean };

export interface ApiError {
  error: { code: string; message: string; details?: unknown; request_id?: string };
}
