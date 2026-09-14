import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ArtifactPanel } from "./ArtifactPanel";
import type { Artifact, ArtifactSummary } from "../types";

vi.mock("../lib/api", () => ({ api: { getArtifact: vi.fn() } }));
import { api } from "../lib/api";

const summary = (id: string, kind: "html" | "markdown"): ArtifactSummary => ({
  id,
  session_id: "s1",
  message_id: "m1",
  kind,
  title: `${kind} artifact`,
  created_at: new Date().toISOString(),
});

const artifact = (id: string, kind: "html" | "markdown", content: string, citations: unknown[] = []): Artifact =>
  ({ ...summary(id, kind), content, citations }) as Artifact;

const HOSTILE_HTML =
  '<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'"></head>' +
  "<body><h1>Report</h1><script>window.__pwned = 1</script></body></html>";

beforeEach(() => vi.mocked(api.getArtifact).mockReset());

describe("ArtifactPanel — rendering boundary", () => {
  it("renders an HTML artifact inside a fully-locked sandboxed iframe", async () => {
    // The second of the two security layers (the first is the server-side
    // allow-list). sandbox="" is the strictest value there is: opaque origin,
    // no scripts, no forms, no popups, no top-level navigation.
    vi.mocked(api.getArtifact).mockResolvedValue(artifact("a1", "html", HOSTILE_HTML));
    render(<ArtifactPanel artifacts={[summary("a1", "html")]} activeId="a1" onSelect={() => {}} onClose={() => {}} />);

    const frame = await waitFor(() => {
      const f = document.querySelector("iframe");
      expect(f).not.toBeNull();
      return f!;
    });
    expect(frame.getAttribute("sandbox")).toBe("");
    expect(frame.getAttribute("referrerPolicy")).toBe("no-referrer");
    expect(frame.getAttribute("srcdoc")).toContain("<h1>Report</h1>");
    // The markup is carried as srcDoc, never injected into this document.
    expect((window as unknown as Record<string, unknown>).__pwned).toBeUndefined();
    expect(document.querySelector("script")).toBeNull();
  });

  it("renders a Markdown artifact as markdown, not as raw HTML", async () => {
    vi.mocked(api.getArtifact).mockResolvedValue(
      artifact("a2", "markdown", "# Heading\n\n<script>window.__pwned = 1</script>\n\n- point"),
    );
    render(<ArtifactPanel artifacts={[summary("a2", "markdown")]} activeId="a2" onSelect={() => {}} onClose={() => {}} />);

    expect(await screen.findByRole("heading", { name: "Heading" })).toBeInTheDocument();
    expect(screen.getByRole("listitem")).toHaveTextContent("point");
    expect(document.querySelector("script")).toBeNull();
    expect((window as unknown as Record<string, unknown>).__pwned).toBeUndefined();
  });

  it("gives the viewer its citations so inline [n] chips link to the episode", async () => {
    // Regression: the panel rendered chips with no citation data behind them,
    // so every citation inside an essay linked to "#".
    vi.mocked(api.getArtifact).mockResolvedValue(
      artifact("a3", "markdown", "Teams that form a habit retain [1].", [
        {
          n: 1,
          guest: "Lauryn Isford",
          title: "Airtable onboarding",
          start_seconds: 90,
          timestamp_url: "https://www.youtube.com/watch?v=VID1&t=90s",
        },
      ]),
    );
    render(<ArtifactPanel artifacts={[summary("a3", "markdown")]} activeId="a3" onSelect={() => {}} onClose={() => {}} />);

    const chip = await screen.findByRole("link", { name: /Citation 1/i });
    expect(chip).toHaveAttribute("href", "https://www.youtube.com/watch?v=VID1&t=90s");
  });

  it("shows the raw source on demand, so the evaluator can read what was generated", async () => {
    vi.mocked(api.getArtifact).mockResolvedValue(artifact("a1", "html", HOSTILE_HTML));
    render(<ArtifactPanel artifacts={[summary("a1", "html")]} activeId="a1" onSelect={() => {}} onClose={() => {}} />);
    await waitFor(() => expect(document.querySelector("iframe")).not.toBeNull());

    await userEvent.click(screen.getByRole("button", { name: /Source/ }));
    expect(document.querySelector("iframe")).toBeNull();
    expect(screen.getByText(/Content-Security-Policy/)).toBeInTheDocument();
  });

  it("explains the isolation in the footer rather than hiding it", async () => {
    vi.mocked(api.getArtifact).mockResolvedValue(artifact("a1", "html", HOSTILE_HTML));
    render(<ArtifactPanel artifacts={[summary("a1", "html")]} activeId="a1" onSelect={() => {}} onClose={() => {}} />);
    expect(await screen.findByText(/Sandboxed/i)).toBeInTheDocument();
  });

});
