import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Markdown } from "./Markdown";
import type { Citation } from "../types";

/**
 * Grounding is the product's core promise, and it is delivered by these chips:
 * an inline [n] must become a link to that passage's exact second. These tests
 * pin that contract — including the case that shipped broken, where the artifact
 * viewer rendered chips with no citations behind them and every link went to "#".
 */

const cite = (n: number, over: Partial<Citation> = {}): Citation =>
  ({
    n,
    id: n,
    episode_id: `ep${n}`,
    guest: `Guest ${n}`,
    title: `Episode ${n}`,
    speaker: null,
    start_seconds: 3723,
    end_seconds: 3800,
    youtube_url: null,
    timestamp_url: `https://www.youtube.com/watch?v=VID${n}&t=3723s`,
    publish_date: null,
    text: "…",
    score: 1,
    sources: ["lexical"],
    ...over,
  }) as Citation;

describe("Markdown citations", () => {
  it("turns an inline [n] into a link to that passage's timestamp", () => {
    render(<Markdown text="Retention compounds [1]." citations={[cite(1)]} />);
    const chip = screen.getByRole("link", { name: /Citation 1/i });
    expect(chip).toHaveAttribute("href", "https://www.youtube.com/watch?v=VID1&t=3723s");
    expect(chip).toHaveTextContent("1");
  });

  it("labels the chip with the guest, episode and timestamp for screen readers", () => {
    render(<Markdown text="Claim [2]." citations={[cite(2)]} />);
    expect(screen.getByRole("link", { name: /Guest 2 — Episode 2 @ 1:02:03/ })).toBeInTheDocument();
  });

  it("opens citations in a new tab without leaking the referrer", () => {
    render(<Markdown text="Claim [1]." citations={[cite(1)]} />);
    const chip = screen.getByRole("link", { name: /Citation 1/i });
    expect(chip).toHaveAttribute("target", "_blank");
    expect(chip).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });

  it("calls onCite instead of navigating when the chat supplies a handler", async () => {
    const onCite = vi.fn();
    const { default: userEvent } = await import("@testing-library/user-event");
    render(<Markdown text="Claim [3]." citations={[cite(3)]} onCite={onCite} />);
    await userEvent.click(screen.getByRole("link", { name: /Citation 3/i }));
    expect(onCite).toHaveBeenCalledWith(3);
  });

  it("does not turn a real markdown link into a citation chip", () => {
    render(<Markdown text="See [the post](https://example.com/x)." citations={[cite(1)]} />);
    const link = screen.getByRole("link", { name: "the post" });
    expect(link).toHaveAttribute("href", "https://example.com/x");
    expect(link).not.toHaveClass("cite");
  });

  it("renders a chip harmlessly when the citation is missing", () => {
    // Regression: the artifact viewer passed no citations, so every chip fell
    // back to href="#" and clicking one did nothing.
    render(<Markdown text="Claim [9]." citations={[cite(1)]} />);
    const chip = screen.getByRole("link", { name: /Citation 9/i });
    expect(chip).toHaveAttribute("href", "#");
  });

  it("never renders raw HTML embedded in the markdown", () => {
    render(<Markdown text={'# Title\n<script>window.__pwned = 1</script>\n<b>bold</b>'} />);
    expect(document.querySelector("script")).toBeNull();
    expect(document.querySelector("b")).toBeNull();
    expect(screen.getByText(/Title/)).toBeInTheDocument();
  });
});
