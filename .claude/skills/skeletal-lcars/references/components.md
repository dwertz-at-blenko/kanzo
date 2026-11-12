# Skeletal LCARS components, the CSS

Distilled from the Stitch export (`stitch-code.html`) and Kanzo's `src/skeletal.css`. Copy the block you need; keep the tokens.

## Shell

```css
body { background: #000; color: var(--sk-text); font: 15px/1.5 var(--sk-font); letter-spacing: .04em; }
body::before { /* telemetry grid */
  content: ""; position: fixed; inset: 0; z-index: -1; pointer-events: none;
  background-image:
    radial-gradient(circle at 10% 20%, rgba(59,130,246,.04) 0%, transparent 40%),
    radial-gradient(circle at 90% 80%, rgba(244,63,94,.025) 0%, transparent 40%),
    linear-gradient(to right, rgba(255,255,255,.015) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(255,255,255,.015) 1px, transparent 1px);
  background-size: 100% 100%, 100% 100%, 48px 48px, 48px 48px;
}
```

## Brand capsule and bridge line

```css
.corner { width: 64px; height: 40px; display: grid; place-items: center; background: var(--sk-blue-lead); color: #000;
          border-top-left-radius: var(--sk-elbow); font: 700 15px var(--sk-font); letter-spacing: .1em; }
.title  { font: 700 22px/1 var(--sk-font); letter-spacing: .06em; text-transform: uppercase; color: var(--sk-ice); }
.meta   { font: 11px var(--sk-mono); letter-spacing: .12em; text-transform: uppercase; color: var(--sk-lavender); }
.bridge { flex: 1; height: 8px;
  background:
    radial-gradient(circle at calc(100% - 4px) 50%, var(--sk-lavender) 3.5px, transparent 4.2px),
    linear-gradient(90deg, var(--sk-blue-lead) 0%, var(--sk-lavender) 60%, var(--sk-hairline) 100%) center / 100% 2px no-repeat; }
```

## Spine rail

```css
.rail { width: 260px; background: var(--sk-rail); border-right: 1px solid var(--sk-hairline); padding: 14px; }
.rail-header { padding: 7px 12px; background: var(--sk-lavender); color: #000; border-radius: 0 12px 0 0;
               font: 700 15px var(--sk-font); letter-spacing: .1em; text-transform: uppercase; }
.rail a { display: flex; justify-content: space-between; padding: 7px 10px; border-left: 2px solid transparent;
          font: 14px var(--sk-font); letter-spacing: .06em; text-transform: uppercase; color: var(--sk-muted); }
.rail a:hover, .rail a.active { color: var(--sk-cyan); border-left-color: var(--sk-cyan); background: rgba(56,189,248,.04); }
.rail a .count { font: 11.5px var(--sk-mono); color: var(--sk-lavender); }
.rail-foot { border-top: 1px dashed var(--sk-hairline); padding-top: 10px; }
```

## Buttons, pills, switcher

```css
.sys-btn { background: transparent; border: 1px solid var(--sk-hairline); border-radius: 2px; padding: 6px 14px; color: var(--sk-text);
           font: 700 13px var(--sk-font); letter-spacing: .08em; text-transform: uppercase; transition: all .15s; }
.sys-btn:hover { background: rgba(56,189,248,.12); border-color: var(--sk-cyan); color: var(--sk-cyan); }
.sys-btn.gold { border-color: rgba(250,204,21,.45); color: var(--sk-gold); }
.sys-btn.gold:hover { background: var(--sk-gold); color: #000; }
.pill { display: inline-flex; align-items: center; gap: 6px; height: 28px; padding: 0 10px; border-radius: 9999px;
        background: rgba(255,255,255,.03); border: 1px solid var(--sk-hairline); color: var(--sk-cyan); font: 10.5px var(--sk-mono); letter-spacing: .06em; }
.pill .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--sk-emerald); box-shadow: 0 0 6px var(--sk-emerald); animation: blink 2s ease-in-out infinite; }
.tabs { display: flex; gap: 2px; padding: 2px; border: 1px solid var(--sk-hairline); border-radius: 9999px; background: rgba(0,0,0,.6); }
.tab  { flex: 1; padding: 4px 8px; border: 0; border-radius: 9999px; background: none; color: var(--sk-muted);
        font: 700 11.5px var(--sk-font); letter-spacing: .08em; text-transform: uppercase; }
.tab[aria-pressed="true"] { background: var(--sk-blue-lead); color: #000; }
@keyframes blink { 50% { opacity: .35; } }
```

## Column header

```css
.column { --col-accent: var(--sk-cyan); background: transparent; }
.column[data-status="backlog"] { --col-accent: var(--sk-muted); }
.column[data-status="doing"]   { --col-accent: var(--sk-lavender); }
.column[data-status="review"]  { --col-accent: var(--sk-amber); }
.column[data-status="done"]    { --col-accent: var(--sk-emerald); }
.column h2 { display: flex; align-items: baseline; gap: 8px; margin: 0 0 10px; padding: 6px 8px 7px;
             border-bottom: 2px solid var(--col-accent); background: linear-gradient(180deg, rgba(255,255,255,.02), transparent);
             font: 700 17px var(--sk-font); letter-spacing: .08em; text-transform: uppercase; }
.column h2::before { content: "SEC-0" counter(sec); font: 10.5px var(--sk-mono); letter-spacing: .1em; color: var(--col-accent); }
.column h2 .count { margin-left: auto; font: 700 12.5px var(--sk-mono); color: var(--col-accent); }
```

## Razor card and the alert hierarchy

```css
.card { position: relative; padding: 10px 12px; border-radius: 4px; background: rgba(255,255,255,.015);
        border: 1px solid var(--sk-hairline-card); border-left: 3px solid var(--card-alert, var(--col-accent));
        transition: transform .2s cubic-bezier(.16,1,.3,1), box-shadow .2s, background .2s, border-color .2s; cursor: grab; }
.card:hover { background: rgba(255,255,255,.04); transform: translateY(-1px); border-color: rgba(255,255,255,.2); box-shadow: 0 4px 12px rgba(0,0,0,.8); }
.card.dragging { opacity: .45; transform: scale(.97); border: 1px dashed var(--sk-cyan); border-left: 3px solid var(--sk-gold); }
.card .meta  { display: flex; justify-content: space-between; margin-bottom: 4px; font: 700 10.5px var(--sk-mono); letter-spacing: .05em; color: var(--card-alert, var(--col-accent)); }
.card .meta .date { font-weight: 400; color: var(--sk-muted); }
.card .title { font: 600 16px/1.25 var(--sk-font); letter-spacing: .04em; }
.card .divider { height: 1px; margin: 8px 0; background: var(--sk-hairline-card); }
.card .foot  { display: flex; justify-content: space-between; align-items: center; font: 10.5px var(--sk-mono); }
.card[data-human="true"] { --card-alert: var(--sk-gold); }
.card[data-alert="amber"] { --card-alert: var(--sk-amber); border-color: rgba(251,146,60,.3); background: linear-gradient(90deg, rgba(251,146,60,.05), transparent 80%); }
.card[data-alert="red"]   { --card-alert: var(--sk-crimson); border-color: rgba(239,68,68,.3); background: linear-gradient(90deg, rgba(239,68,68,.06), transparent 80%); animation: alert 2.4s ease-in-out infinite; }
@keyframes alert { 50% { border-left-color: var(--sk-coral); box-shadow: -8px 0 18px -8px var(--sk-crimson); } }
.drop-slot { height: 52px; border: 1px dashed var(--sk-cyan); background: rgba(56,189,248,.06); border-radius: 4px; display: grid; place-items: center;
             font: 10.5px var(--sk-mono); letter-spacing: .15em; color: var(--sk-cyan); animation: blink 1.2s ease-in-out infinite; }
```

## Tags

```css
.tag { padding: 1px 6px; border-radius: 2px; background: rgba(255,255,255,.03); border: 1px solid rgba(255,255,255,.08);
       color: var(--sk-ice); font: 10px var(--sk-mono); letter-spacing: .06em; text-transform: uppercase; }
.tag.agent { border-color: rgba(167,139,250,.4); color: var(--sk-lavender); }
.tag[data-tag="p1"], .tag[data-tag="bug"] { border-color: rgba(251,146,60,.5); color: var(--sk-amber); }
.tag[data-tag="blocked"], .tag[data-tag="urgent"], .tag[data-tag="p0"] { border-color: rgba(239,68,68,.55); color: var(--sk-crimson); }
```

## Fields, panels, dialogs

```css
.field { background: rgba(255,255,255,.04); border: 1px solid var(--sk-hairline); border-radius: 2px; padding: 8px 12px; color: var(--sk-text); }
.field:focus { border-color: var(--sk-cyan); outline: none; }
.switch span { background: var(--sk-ghost); }  .switch input:checked + span { background: var(--sk-cyan); }
.panel  { background: #04070c; border-left: 1px solid var(--sk-hairline); box-shadow: 0 10px 34px rgba(0,0,0,.85); }
.dialog { background: #060a12; border: 1px solid var(--sk-hairline); border-radius: 4px; }
.section-label { padding-bottom: 5px; border-bottom: 1px solid var(--sk-hairline); font: 10.5px var(--sk-mono); letter-spacing: .12em; text-transform: uppercase; color: var(--sk-cyan); }
.toast { background: #0b1020; border: 1px solid var(--sk-cyan); border-radius: 2px; font: 12px var(--sk-mono); }
```

## Reduced motion

```css
@media (prefers-reduced-motion: reduce) { .card, .pill .dot, .drop-slot { animation: none; transition: none; transform: none; } }
```
