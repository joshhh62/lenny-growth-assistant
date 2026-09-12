import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Citation } from "../types";

/**
 * Renders assistant Markdown. Raw HTML is never rendered (react-markdown skips
 * it by default) — this is the Markdown half of the artifact security story.
 * Inline "[n]" citations become chips that link to the YouTube timestamp.
 */
export function Markdown({
  text,
  citations = [],
  onCite,
}: {
  text: string;
  citations?: Citation[];
  onCite?: (n: number) => void;
}) {
  const byN = new Map(citations.map((c) => [c.n, c]));
  // Turn [3] into a link we can intercept, but leave real markdown links alone.
  const prepared = text.replace(/\[(\d{1,2})\](?!\()/g, (_m, n) => `[${n}](#cite-${n})`);

  return (
    <div className="prose">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children, ...rest }) => {
            const m = href?.match(/^#cite-(\d+)$/);
            if (m) {
              const n = Number(m[1]);
              const c = byN.get(n);
              const label = c ? `${c.guest} — ${c.title}${c.start_seconds != null ? " @ " + fmtTs(c.start_seconds) : ""}` : `Source ${n}`;
              return (
                <a
                  className="cite"
                  href={c?.timestamp_url ?? "#"}
                  title={label}
                  aria-label={`Citation ${n}: ${label}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={(e) => {
                    if (onCite) {
                      e.preventDefault();
                      onCite(n);
                    }
                  }}
                >
                  {n}
                </a>
              );
            }
            return (
              <a href={href} target="_blank" rel="noopener noreferrer" {...rest}>
                {children}
              </a>
            );
          },
        }}
      >
        {prepared}
      </ReactMarkdown>
    </div>
  );
}

export function fmtTs(seconds: number | null): string {
  if (seconds == null) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}
