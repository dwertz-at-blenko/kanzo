---
name: skeletal-lcars
description: The Skeletal LCARS design system (Dave's official look, designed in Google Stitch) for Kanzo themes, dashboards, status pages and any tool UI that should match kanzo.blenko.cloud. Use when styling or restyling a page in this look, reviewing a UI against it, extending src/skeletal.css, or when the user says Skeletal, LCARS, Stitch theme, tactical, hairline, razor cards, or asks for the board's look on something else.
---

# Skeletal LCARS

A dark, hairline, high-chroma terminal: black void, single-pixel dividers, one bright anchor per element, Antonio for words and a monospace for data. It reads as a 24th-century console without the heavy orange slabs: structure is implied by thin lines and colour, never by boxes.

The reference render is `../../../docs/themes/skeletal/stitch-screen.png`; the original Stitch markup and CSS is `references/stitch-code.html`. Kanzo's implementation is `src/skeletal.css`, applied to the stock DOM under `html[data-theme="skeletal"]`.

## Principles

1. **Void first.** Pure black background, a 48px telemetry grid at 1.5% white, two faint radial glows (blue top left, coral bottom right). Surfaces are at most 4% white; depth comes from hairlines, not fills.
2. **One anchor per element.** A card, a column, a nav row each get exactly one high-chroma accent (a left bar, an underline, a border). Colour means something; it is never decoration.
3. **Razor dividers.** 1px lines at 12 to 14% opacity. Never a 2px border except the column underline and the bridge line.
4. **Two voices.** Antonio, uppercase with tracking, for titles, labels and navigation. Monospace, small, for ids, dates, counts, tags and metadata. Body copy stays in Antonio at normal case.
5. **Square and pill, nothing between.** Radii are 2 to 4px for cards, buttons and fields, 9999px for pills and the switcher, 24px for the single LCARS elbow in the top left corner. No 10 to 18px rounded rectangles.
6. **Motion is telemetry.** A blinking status dot, a slow pulse on a red alert, a 200ms lift on hover with a `cubic-bezier(.16, 1, .3, 1)` ease. Everything stops under `prefers-reduced-motion`.
7. **Zero dependencies.** Plain CSS, system monospace, Antonio served by the app. No frameworks, no icon fonts, no external requests.

## Tokens

```css
--sk-void: #000000;        --sk-rail: #03060a;
--sk-blue-lead: #3b82f6;   --sk-blue-dim: #1e3a8a;   --sk-cyan: #38bdf8;
--sk-lavender: #a78bfa;    --sk-lavender-dim: #4c1d95; --sk-ice: #c7d2fe;
--sk-gold: #facc15;        --sk-amber: #fb923c;      --sk-coral: #f43f5e;
--sk-crimson: #ef4444;     --sk-emerald: #10b981;    --sk-teal: #14b8a6;
--sk-hairline: rgba(56, 189, 248, .14);   --sk-hairline-card: rgba(148, 163, 184, .12);
--sk-text: #f1f5f9;        --sk-muted: #64748b;      --sk-ghost: #334155;
--sk-font: "Antonio", -apple-system, "Segoe UI", Roboto, sans-serif;
--sk-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
--sk-elbow: 24px;
```

| Role | Token | Used for |
| --- | --- | --- |
| Structure | blue-lead | the LCARS corner block, the active switcher tab, the start of the bridge line |
| Section headers | lavender | the spine header block, counts in the nav, metadata, comment authors |
| Interactive, focus | cyan | hover and active states, links, the todo column, focus rings |
| Titles | ice | h1, brand, dialog titles |
| Operator action | gold | New task, primary buttons, human tasks |
| Attention | amber | the review column, p1 and bug tags, amber alert cards |
| Alert | crimson, coral | blocked, urgent, p0; danger buttons; the pulse colour |
| Resolved | emerald | the done column, the online dot, sharing on |
| Agents | teal | agent and audit tags |
| Quiet | muted, ghost | backlog, secondary text, switch tracks |

Readable text is never below `--sk-muted` (#64748b) on black; `--sk-ghost` is for lines and tracks only.

## Typography

| Use | Font | Size | Weight | Case and tracking |
| --- | --- | --- | --- | --- |
| Brand, page title | Antonio | 19 to 22px | 700 | uppercase, .06 to .08em |
| Column names, dialog titles | Antonio | 17px | 700 | uppercase, .08em |
| Card titles | Antonio | 16px | 600 | normal case, .04em, line-height 1.25 |
| Navigation, buttons | Antonio | 13 to 14px | 400 nav, 700 buttons | uppercase, .06 to .08em |
| Body | Antonio | 15px | 400 | normal case, .04em |
| Section labels, property names | mono | 10.5px | 400 | uppercase, .1 to .12em, cyan or muted |
| Ids, dates, counts | mono | 10.5 to 12.5px | 700 ids, 400 dates | .05em |
| Tags | mono | 10px | 400 | uppercase, .06em |

## Layout

- **Top rail:** brand capsule on the left (64 x 40 blue block reading LCARS with the 24px elbow on its top left, then the title), a 2px bridge line from blue to lavender ending in a lavender node, telemetry pills, then actions on the right. Height about 60px.
- **Spine rail:** 260px sidebar on `--sk-rail`, hairline on its right. Order: brand, New project (sys button), nav, lavender header block (PROJECTS or OPERATIONS, radius `0 12px 0 0`), project rows, then at the bottom Settings, the switcher and the version line with its blinking emerald dot.
- **Stage:** columns on the void, 16px apart, each 220px minimum, no column background. Phone: columns 78vw wide, the rail becomes a top row.
- **Bottom rail (optional, for pages that have one):** a hairline on top, mono stats `BACKLOG: 03  IN-FLIGHT: 05`, right-aligned system tag.

## Components

See `references/components.md` for the CSS of each. In short:

- **Column header:** `SEC-0n` mono prefix in the column's accent, uppercase name, mono count on the right, 2px underline in the accent, a faint top-to-bottom gradient. Accents: backlog muted, todo cyan, doing lavender, review amber, done emerald.
- **Razor card:** 1px hairline box at 1.5% white, 3px left bar in `--card-alert` (defaults to the column accent), radius 4px, padding 10 to 12px. Meta row (mono: id left, date right) above the title, a hairline divider, then tags left and status glyph right. Hover: lift 1 to 2px, border to 20% white, deep shadow. Dragging: dashed cyan border, gold left bar, 45% opacity. Drop slot: dashed cyan, pulsing.
- **Alert hierarchy on cards:** `data-alert="red"` crimson bar, 30% crimson border, a 6% crimson gradient from the left, slow pulse. `data-alert="amber"` the same in amber, no pulse. Human tasks get a gold bar. Everything else uses the column accent.
- **Tags (agent badges):** mono, 1px border at 8% white, 3% white fill, radius 2px, ice text. Agent tags lavender or teal border; alert tags take their colour.
- **Sys button:** transparent, 1px hairline, radius 2px, uppercase Antonio 700, hover fills 12% cyan and turns the text cyan. Gold variant for the one primary action: gold border and text, hover fills solid gold with black text.
- **Telemetry pill:** mono 10.5px, 1px hairline, radius 9999px, 3% white fill, cyan text; a 6px emerald dot with glow when it reports health.
- **Switcher:** pill group with a hairline border on 60% black; the active tab is a solid blue-lead pill with black text.
- **Fields:** 1px hairline, radius 2px, 4% white fill, cyan border on focus. Switches: ghost track, cyan when on, black knob.
- **Panels and dialogs:** `#04070c` or `#060a12` surfaces, hairline borders, radius 4px, 34px black shadow. Section labels in mono cyan with a hairline under them.

## Do and don't

- Do keep hairlines at 12 to 14% and fills under 4%; if a surface needs more contrast, add a line, not a fill.
- Do give every list its mono counter and every section its mono label.
- Don't introduce a new hue. Pick from the table; if nothing fits, the element is probably not important enough for colour.
- Don't round to 8 to 18px. Don't use drop shadows for resting elements. Don't use bold monospace except for ids and counts.
- Don't make text uppercase in editable fields, long descriptions or Markdown.
- Don't load fonts, icons or scripts from outside the app.

## Extending Kanzo's theme

Edit `src/skeletal.css` only under `html[data-theme="skeletal"]`, prefer Kanzo's own variables (`--bg`, `--hover`, `--border`) so menus and dialogs follow for free, and reach for the `--sk-` tokens for accents. Bump `v0.x` on the first line, `--theme-version` and `SKELETAL_VERSION` in `src/web.rs` together; `cargo test` checks they agree. Screenshot desktop, the task panel and a 390px phone into `docs/themes/skeletal/` and update `docs/themes/README.md`.

The DOM contract the theme relies on (data attributes on columns, cards, tags, the switcher) is listed in `docs/themes/README.md`; add attributes in `src/app.js` rather than selecting on text.
