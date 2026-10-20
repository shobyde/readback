# Readback design decisions

This file pins the deliberate choices behind Readback's interface so that future
audits and redesign passes treat them as decisions, not accidents. It describes
the code as it is, records the 2026-10-19 audit batch and its resolutions, and
says what to do when a detector flags one of them again.

Sources: `src/readback/web/static/style.css` (single stylesheet),
`src/readback/web/static/fonts.css`, `src/readback/web/templates/`, and the
product constraints in `SPEC.md`. The audit that prompted this file is dated
2026-10-19 (its report was at `/tmp/ui-audit-readback.md` at the time of
writing).

**When a future audit flags something below:** cite the section. If the decision
actually changes, change it here in the same commit as the CSS. Do not let a
detector score silently override a decision, and do not let this file drift from
the stylesheet.

## Product constraints that shape the UI

- Server-rendered Jinja templates, one CSS file, no client-side JavaScript. State
  is expressed with CSS only (`:target`, `:has()`, `:focus-visible`, `<details>`).
- No CDN and no external font or icon requests. Icons are inline SVG or local SVG
  files; faces are local WOFF2.
- One warm light theme. `SPEC.md` rules out a dark neon treatment; Readback is a
  paper-and-ink product, not a dashboard.
- Two surfaces in one design system: a playful shelf for young readers and a
  calmer management surface for parents. Same palette, type, and components.
- Works down to 360px wide with no horizontal page scroll.

## 1. Palette and surfaces

All colour lives as custom properties on `:root` in `style.css`.

| Token | Value | RGB | Role |
| --- | --- | --- | --- |
| `--paper` | `#f6f1e6` | 246, 241, 230 | page surface |
| `--card` | `#fff8ea` | 255, 248, 234 | cards, inputs, table bodies |
| `--card-warm` | `#f8e8cc` | 248, 232, 204 | warm fills: avatars, meter track, draft badge, score-ring track |
| `--card-green` | `#e7ebd9` | 231, 235, 217 | secondary-button hover |
| `--card-blue` | `#e3edf2` | 227, 237, 242 | wash behind a missed result |
| `--ink` | `#2a2219` | 42, 34, 25 | body text |
| `--bark` | `#4b2f1f` | 75, 47, 31 | labels, strong small text |
| `--leather` | `#7a4a28` | 122, 74, 40 | links (7.0:1 on `--card`) |
| `--muted` | `#5f4e3b` | 95, 78, 59 | meta, hints, crumbs (7.53:1 on card, 7.06:1 on paper) |
| `--ember` | `#c9561f` | 201, 86, 31 | accent for borders, dots, marks; not for text on cream |
| `--ember-dark` | `#a84414` | 168, 68, 20 | link hover, state headings |
| `--ember-cta` | `#a8430f` | 168, 67, 15 | filled controls; `#fff8ea` text is 5.72:1 (AA) |
| `--ember-cta-hover` | `#8f370b` | 143, 55, 11 | filled-control hover |
| `--focus-ring` | `#a8430f` | 168, 67, 15 | focus outline on every surface |
| `--gold` | `#f2a63a` | 242, 166, 58 | points pill, "Up next" flag |
| `--moss` | `#5e6f3a` | 94, 111, 58 | secondary accent, pass state, section labels |
| `--sage` | `#8fa76a` | 143, 167, 106 | soft green accent, reserved (currently unused) |
| `--olive` | `#3e4a33` | 62, 74, 51 | small labels on paper |
| `--fail` / `--fail-soft` | `#8a3b2e` / `#f6ded4` | 138, 59, 46 / 246, 222, 212 | failed state (5.94:1) |
| `--pass` / `--pass-soft` | `#4e6633` / `#e5ecd8` | 78, 102, 51 / 229, 236, 216 | passed state (5.29:1) |

Structure tokens: `--border` `rgba(122, 74, 40, 0.18)`, `--border-strong`
`rgba(122, 74, 40, 0.34)`; radii `--r-sm` 10px, `--r-md` 14px, `--r-lg` 20px,
`--r-xl` 28px, `--r-pill` 999px; shadows `--shadow-sm`/`--shadow-md`/`--shadow-lg`
in warm ink; easing `--ease-cozy` `cubic-bezier(0.2, 0.8, 0.2, 1)`.

### The warm cream surface is a brand decision

`body` paints `--paper` (`rgb(246, 241, 230)`) under a 22px dot grid
(`radial-gradient(rgba(122, 74, 40, 0.07) 1px, transparent 1px)`), and cards sit
lighter on top with `--card`. This is the Hobnob-family palette: parchment, ember,
moss, gold, leather, bark. Readback is a reading product, so the page is literally
paper and the cards are the pages on it.

Accept the "default AI cream" advisory. The signal that this is a family and not
a generated default is the set, not the hex: a named ember/moss/gold/leather
ramp, warm fills for state, a paper-dot texture, and a display serif. Keep
`#f6f1e6`; do not drift toward `#faf7f2`, pure white, or a grey-neutral "clean"
surface, and do not introduce a second cream.

Surface rules:

- Page = `--paper`. Cards, inputs, tables = `--card`. Warm emphasis = `--card-warm`.
  The value step between page and card is the hierarchy; do not replace it with
  heavier shadows or borders.
- Body copy is `--ink`; secondary copy is `--muted`; labels are `--bark`.
- Accent fills that carry text must use `--ember-cta`, never `--ember` (ember is
  under 4.5:1 for text on cream).
- Known residue, not a licence: a few rules still hard-code warm whites —
  table row hover (`#fffcf3`); quiz choice cards and the reader-card avatar
  (`#fffdf6`); mini-cover ink and its page lines (`#fffdf6`,
  `rgba(255, 253, 246, …)`). When you touch those rules, move them onto
  `--card`/`--card-warm`. Do not add new hard-coded surfaces.

## 2. Typography

Real stacks, declared in `:root` in `style.css`:

```css
--font-head: "Fraunces", "Iowan Old Style", Georgia, serif;
--font-body: "Nunito", system-ui, -apple-system, "Segoe UI", sans-serif;
```

- **Fraunces** carries headings (`h1`–`h3`, bold by default), the wordmark
  (`.brand-name`, 700), book titles (`.book-title`, 600), draft titles
  (`.draft-title`, 600) and avatars (700). Headings use line-height 1.14 and
  letter-spacing -0.015em; the scale is `h1` 2.35rem, `h2` 1.45rem, `h3` 1.12rem.
- **Nunito** carries everything else: body 1rem/1.6, labels, buttons, tables,
  chips, metadata. It is the voice a child reads instructions in.
- **Locally hosted.** `style.css` starts with `@import url("fonts.css")`.
  `fonts.css` declares the faces with `font-display: swap`, Latin `unicode-range`,
  and local files: `static/fonts/fraunces-500.woff2`, `static/fonts/nunito-400.woff2`.
  No request leaves the machine for type.
- **Accepted advisory:** the detector calls Fraunces overused. It is the
  brand pairing, chosen for a bookish display serif against a rounded humanist
  sans; the correct response is this note, not a font swap.
- Implementation note: each family ships one weight file, and `fonts.css` maps
  500/600/700 (Fraunces) and 400/600/700/800 (Nunito) onto it, so the browser
  synthesises the heavier weights. Do not add `@font-face` weights without the
  matching WOFF2, and do not "fix" the repetition by dropping the declared
  weights; labels and numerals ask for 800.
- Small print sits at 0.82–0.92rem. Uppercase labels, where they survive, use
  0.07–0.12em tracking. See the 12px floor in section 5 before adding another.

## 3. Accent carriers

Accent colour is carried by **type, icons, state, and a subtle card carrier** —
never by a thick one-sided bar.

Where accent is allowed:

- **Type:** links (`--leather`, hover `--ember-dark`); active/current state text
  such as `.review-question:target h2`; small state labels; the `· Correct` /
  `· Worth another look` notes on a result.
- **Icons:** the local `bookmark.svg` and `nib.svg` illustrations, and inline
  SVG chevrons, the nav lock and the footer lock. Inline icons use a 24px
  viewBox with 2–2.4px round strokes, `currentColor` or moss.
- **State:** `.badge-pass`, `.badge-fail`, `.badge-draft`, `.badge-none`;
  `.review-question:target` (ring and "viewing" tag); the rail `:has()` highlight;
  the 3px `--focus-ring` outline at 2px offset; the conic-gradient `.score-ring`
  (moss for pass, ember for missed); the 7px `.meter` fill; `.stamp-finished`.
- **Subtle card carrier:** a reader's colour identity belongs in a small mark —
  a corner dot or medallion, a top-edge notch, a ≤2px inset rule, or the
  horizontal `.spine-strip` of finished books under a reader card. Those are
  identity marks, not decoration.

**Banned:** a one-sided 6–12px accent bar down a card edge. It is the most
recognisable AI-card tell, it competes with the content, and it makes every card
read as the same component in a different colour. Also banned: a one-sided bar on
a review question or a callout.

Allowed exception: `.quiz-bar`, the sticky quiz context bar, keeps a 4px
`--ember-cta` left edge. It is a utility strip, not a card, and the edge marks
the current context rather than decorating a card.

Mid-fix (2026-10-19): this tree still contains `.reader-card::before` (10px),
`.book-card::before` (12px), `.review-question` `border-left: 6px`, and
`.review-callout` `border-left: 8px`. The CSS ticket is replacing them with the
subtle carrier above while keeping the colour identity through `.spine-strip`,
`.meter`, and small marks. Do not reintroduce the bar in new work, and do not
"restore" it because a screenshot looked flat.

## 4. Hierarchy

Headings carry the hierarchy: `h1` for the page, `h2` for a section, `h3` for an
item inside a section. No skipped levels.

**No small uppercase kicker above a heading.** The audit found six
(`.eyebrow`: "The reading nook", "Parent area", "The shelf", "Reading quiz",
"Draft review", "All done"). A tracked uppercase row that restates the heading is
noise and a generated-UI tell. Put the context in the heading or in the first
sentence (`.lede`), which already carries it.

Uppercase marks that are allowed are **state attached to a thing**, not section
scaffolding: `.brand-sub` ("a cozy shelf", part of the wordmark), the `.badge`
family, `.card-flag` ("Up next"), `.stamp-finished` ("Finished"), `.quiz-count`.
Each names a state or a brand, and each sits with the element it describes.

Resolved 2026-10-19: no `.eyebrow` kicker remains in any template, and the
`.eyebrow` rules are deleted. Do not add a kicker in new work.

Anchors clear the 78px sticky header: `html` sets `scroll-padding-top: 96px`, and
in-page targets use `scroll-margin-top: 108px`.

## 5. Accessibility floors

These are floors, not preferences. A failure is a bug; fix it in the component,
not by shrinking the type or the target.

- **Functional labels ≥ 12px.** Anything a user must read to act — counts, state
  chips, hints, button labels, column headers, legends, control affordances —
  is at least 0.75rem. Below 12px is reserved for non-interactive legal text.
  All functional labels in this tree meet that floor as of 2026-10-19; the
  previous 10.5–11.8px labels were raised to 12px.
- **Hit areas ≥ 44×44 CSS px** for every control, regardless of visual size.
  Use padding, `min-height`, or a pseudo-element; inline text links are exempt.
  Applied today by `.site-nav a`, `.draft-review`, `.book-card .foot .button`,
  `.question-nav a`, the books table number input, and table action buttons.
  The remaining 38px circles and 39px number input were raised to 44px on
  2026-10-19.
- **Contrast AA** (4.5:1 body text, 3:1 large text and UI edges) on every surface,
  including placeholders. `--muted` is 7.06–7.53:1; `--ember-cta` with `--card`
  text is 5.72:1; `--bark` on `--card-warm` is 10.09:1. Placeholders are text:
  they must reach 4.5:1 too. An explicit `::placeholder` rule now uses
  `--placeholder` at ≥4.6:1. Never rely on the UA colour.
- **Names and roles.** Every control has a visible label or an accessible name.
  Reader quiz radios are named by their wrapping `<label>`. The 20
  correct-answer radios in `review.html` now sit in named `role="radiogroup"`
  groups and each carries an `aria-label` naming its choice.
- **Heading order.** No skipped levels. `result.html` uses `<h2>` for
  per-question headings under the page `<h1>`, with the status note in a span.
- **Keyboard.** The skip link is the first focusable element and lands on
  `#main`. Focus is always visible (3px ring, 2px offset). Destructive actions
  are behind `<details class="confirm-delete">`, not a hover or a bare button.
- **Motion.** `@media (prefers-reduced-motion: reduce)` sets `animation: none`
  and `transition-duration: 0.01ms`. Hover lifts and rotations are decorative
  only; nothing depends on them to be understood.
- **No-JS.** Every flow works with JavaScript disabled; if a new interaction
  cannot be expressed with links, forms, `:target`, `:has()`, or `<details>`, it
  does not belong in Readback.

## 6. Kid and parent surfaces

Same palette, type, and component vocabulary. The difference is **density, copy,
and action priority** — not a second theme. `base.html` sets
`body class="parent"` for `/parent*` and `"kids"` elsewhere, but almost no CSS
keys off it today; the split is carried by structure and copy.

| | Reader surface (`/`, `/reader/<id>`, quiz, result) | Parent surface (`/parent`, `/parent/books`, `/parent/quiz/<id>`) |
| --- | --- | --- |
| Density | Airy; one idea per screen; hero art with Nib (`.nook-hero`, `.reader-hero`) | Denser and scannable; rail beside editable questions; counts as plain numbers on rules (`.counts`), never clickable chips |
| Copy | Second person, playful: "Who's reading today?", "Take your time, …", "Ask a grown-up for a new book" | Imperative, explains consequences: "Approve publishes the whole quiz to every reader's shelf" |
| Action priority | Exactly one primary action per card or screen. Reader cards are whole-card links (`.reader-card`) with the focus ring on the card. The first unpassed book is `.book-card.primary`: 84×108 cover, 2px ember border, 48px full-width "Take quiz". Other cards keep a quiet button. | The review queue leads the page as `.review-callout` with one filled "Review" button per draft. Parent reader cards are static articles with one "View shelf" link. Destructive actions are behind `<details class="confirm-delete">`. Approve/reject live in their own `.decision-card`. |
| Identity | Avatar initial, gold points pill, `.spine-strip` shelf marks, `.stamp-finished` stickers, score ring | Same marks, calmer: moss secondary buttons, dashed borders, no decorative lift on reader cards |

Shared components keep one name and one behaviour on both surfaces. Do not fork
a button, badge, or card per surface; adjust copy and emphasis instead.

## 7. Audit batch (2026-10-19)

The UI audit that prompted this document landed in full: side-tab stripes and
kickers are gone, radios are named, result headings are ordered, placeholder
contrast and functional type meet the floors, tap targets are 44px, and the
favicon is served. Keep this section only as the record of what the batch
changed; new work inherits the rules above.

## 8. Changing this document

- A new component inherits these rules; it does not get an exemption because a
  detector suggested one.
- If a decision genuinely changes (new palette step, new face, new surface split),
  edit this file in the same change as the CSS and say why in one sentence.
- If a future audit flags an item covered here, link back to the section in the
  audit response instead of re-litigating it in code.
