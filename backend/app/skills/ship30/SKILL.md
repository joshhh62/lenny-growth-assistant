# Skill: Ship 30 for 30 Essay

Turns grounded transcript material into a ~1,250-word "digital writing" essay in the style
taught by Ship 30 for 30 (Dickie Bush & Nicolas Cole). The principles below are distilled from
the Ship 30 for 30 Ultimate Guide and are the *contract* the generator is checked against.

## Inputs
- `topic` — the reader's question or subject, made specific (see Idea Generator).
- `angle` — one of the 4A paths (optional; default: Actionable).
- `passages` — numbered transcript excerpts. Every claim must trace back to one of them.

## Principles (encoded)

### 1. Specificity beats generality (The Endless Idea Generator)
Narrow the topic before writing: "growth" → "growth for a B2B SaaS with a sales-led motion".
Name the audience, the outcome, or the process in the headline whenever possible.
Pick ONE of the 4A angles and keep the whole essay on it:
- **Actionable** — here's how (steps, tactics)
- **Analytical** — here are the numbers / mechanics
- **Aspirational** — yes, you can (stories of people who did it)
- **Anthropological** — here's why (human behaviour behind the pattern)
Choose ONE organising pattern for the main points: How-To, Lessons Learned, Mistakes, or Frameworks.
All main points must follow the same pattern (parallel structure).

### 2. The headline answers WHO / WHAT / WHY, with a curiosity gap
Components: a number (container), the WHAT (subject), optionally WHO (audience), a FEEL word,
and the PROMISE (outcome). Give enough to understand what the piece is about, but not the answer.
Clear > clever. Preferred formats: "X Ways/Lessons/Mistakes…", "How <credible name> <did outcome>",
"Question? Try this…", "What <unexpected pair> have in common".

### 3. The hook (first 3–5 lines) earns the read
Open with the most surprising, concrete, or contrarian statement the transcripts support —
a stat, a named guest's counter-intuitive claim, or a vivid moment. Then state the promise:
what the reader will be able to do by the end. Establish credibility by *curating the experts*
("Here's what <guest>, <guest> and <guest> have learned…"). No throat-clearing, no "In today's world".

### 4. Skimmability = readability (Wheels & Spokes)
- Pre-writing skeleton: Headline → Intro (repeat promise + credibility) → Main points → Conclusion.
- **Wheels**: H2 headings that signal each main point; each is a mini-headline (a full claim, not a label).
- **Spokes**: short H3s or bold lead-ins inside a section when it has sub-ideas.
- Convert lists of ≥3 items into bullets. Bold ONE sentence per section — the sentence to remember.
- Paragraphs are 1–3 sentences. Never a wall of text.

### 5. Writing rhythm (sentence-level pacing)
Default to the **1/3/1 rhythm** per section: one-line opener, a 3-sentence body, a one-line closer.
Vary with 1/5/1 or 1/2/5/2/1 for the climactic section. Avoid 1/1/1/1 (jittery) and 5/5/5 (exhausting).
Alternate long and short. The first and last sentence of each section are bookends.

### 6. Rate of revelation
Every sentence must say something new. Cut repetition. Ask of each sentence: "Is this describing
something already said, or saying something new?"

### 7. Differentiation (The Tequila Test)
List what everyone says about the topic; don't say that. Lead with what the transcripts add that the
average blog post doesn't: the guest's specific mechanism, number, story, or contrarian take.

### 8. Grounding (non-negotiable for this product)
- Every factual claim, framework, or quote carries a bracketed citation [n] to a passage.
- Attribute ideas to the guest by name. Paraphrase; quote only short, striking phrases (≤ 25 words).
- If the passages are thin, narrow the essay to what they support rather than padding.
- Never invent guests, numbers, companies, or episodes.

### 9. Ending: one specific, useful takeaway
Close with a "Do this next" section: a single concrete action the reader can take this week,
plus a one-line TL;DR of the main points (bullets). End on a short, memorable sentence.

## Output format
Markdown only:
```
# <Headline>

<Hook: 3–5 short lines>

## <Main point 1 as a claim>
...
## <Main point 2 as a claim>
...
## <Main point 3 as a claim>   (3–5 main points total)
...
## Do this next
- ...
**TL;DR**
- ...
- ...

### Sources
[1] Guest — Episode title (timestamp)
```
Aim for 1,150–1,350 words (excluding the Sources list) — comfortably inside the
accepted band below, so a slightly long or short draft still passes.

## Quality checklist

Every line here is enforced by `check_essay()` in `essay.py`; a draft that fails
any of them is regenerated rather than shipped. The thresholds below are the
exact ones the code asserts.

- [ ] 1,000–1,500 words (body only — the Sources list is excluded)
- [ ] Exactly one H1; 3–7 H2 sections; a "Do this next" / "Next steps" / "Takeaways" section
- [ ] ≥ 5 citations, every one referencing a supplied passage number
- [ ] ≥ 4 bullets; ≥ 2 bold phrases
- [ ] Does not stop mid-sentence (a token-ceiling truncation is detected and retried)
- [ ] The first 400 characters contain no "In today's", "In this essay", "As we all know"
