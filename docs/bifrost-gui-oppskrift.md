# Recipe: a calm, clear dashboard (the Bifrost way)

What makes the Bifrost UI feel tidy is not a fancy framework but a handful of principles, applied
consistently. Here is the recipe; bring your own palette and content.

## Principles

- **Light theme, few colours.** White surfaces, one muted text colour, and *one* accent colour used
  sparingly (the active item, a thin line). Don't paint everything.
- **Whitespace.** 20–28 px around and between cards, and a centred column with a max width of 1100 px
  so lines don't get too long. Pages that need the room (maps) use a wide column (`main.wide`, 96 vw).
- **Few, reusable components.** A *card*, a *segmented control* and a *chart*. Everything else is
  built from these.
- **Thin, low-contrast lines.** Borders and grid lines are barely visible. No heavy boxes.
- **Numbers as numbers.** `font-variant-numeric: tabular-nums`, so digits line up.
- **Everything self-hosted.** The font (Inter), uPlot and HTMX are served locally, with no CDN.
- **Light interactivity.** HTMX for server-rendered fragments, no heavy SPA.
- **Responsive, not a separate layout.** Breakpoints at 760 px and 420 px tighten the spacing and fold
  the navigation into a menu button. The page never scrolls sideways.

## Design tokens

Everything is a CSS custom property, in two tiers: a **palette** (colour scales) and **role tokens**
that point into it. Components use the role tokens plus a few palette steps directly. Swap the
palette for your own; the *structure* is the point.

```css
:root{
  /* palette */
  --bl-900:#181c56; --bl-800:#3a14c6; --bl-600:#4e4feb; --bl-500:#8181f1;
  --bl-300:#b3b3f6; --bl-200:#dfe1fe; --bl-100:#ededfd;                     /* main scale */
  --gr-700:#14dd86; --gr-500:#64f7af; --gr-300:#a5face; --gr-100:#d5ffe9;  /* accent scale */
  --neu-100:#f8f8ee;                                                        /* warm neutral */
  /* roles */
  --bg:var(--neu-100); --surface:#ffffff; --surface-2:var(--bl-100);  /* page, cards, secondary */
  --border:var(--bl-200);                          /* almost invisible */
  --ink:var(--bl-900); --muted:#6e72a6; --faint:#9aa0d0;   /* text, labels/axes, small status */
  --primary:var(--bl-800); --primary-600:var(--bl-600);    /* action/active, focus */
  --accent:var(--gr-700);                          /* used sparingly */
  --danger:#d23a57;                                /* destructive actions only */
  --grid:#ececf8;                                  /* chart grid */
  --grad:linear-gradient(90deg,#3a14c6,#4e4feb 55%,#14dd86);   /* thin brand strip */
  --ink-rgb:24,28,86;                              /* --ink as r,g,b for rgba() shadows */
  --font:"Inter",Arial,sans-serif;
  --font-display:"Founders Grotesk","Inter",Arial,sans-serif;  /* headings */
  --r:14px;                                        /* corner radius */
}
```

## Components

```css
*{box-sizing:border-box}
body{margin:0; background:var(--bg); color:var(--ink); font-family:var(--font);
     font-size:14px; line-height:1.5; -webkit-font-smoothing:antialiased}
main{max-width:1100px; margin:28px auto; padding:0 26px;          /* the centred column */
     display:flex; flex-direction:column; gap:20px}
main.wide{max-width:96vw}                                          /* map pages */
.mono{font-variant-numeric:tabular-nums}

/* top bar: the small touch that lifts it is a 2 px gradient strip along the bottom edge */
.topbar{display:flex; align-items:center; justify-content:space-between; padding:14px 26px;
        background:var(--surface); border-bottom:1px solid var(--border);
        position:sticky; top:0; z-index:5}
.topbar::after{content:""; position:absolute; left:0; right:0; bottom:-1px; height:2px;
               background:var(--grad); opacity:.9}
nav a{color:var(--muted); text-decoration:none; margin-left:20px; font-weight:600; padding-bottom:4px}
nav a:hover{color:var(--ink)}
nav a.on{color:var(--primary); border-bottom:2px solid var(--accent)}

/* card: very light shadow and a thin border, nothing more. The heading is a flex row,
   so a subtitle or a small control can sit next to it. */
.card{background:var(--surface); border:1px solid var(--border); border-radius:var(--r);
      padding:22px; box-shadow:0 1px 2px rgba(var(--ink-rgb),.04)}
.card h2{margin:0 0 16px; font-family:var(--font-display); font-size:16px; font-weight:600;
         color:var(--ink); display:flex; align-items:center; gap:10px}
.card h2 .sub{font-size:12px; color:var(--muted); font-weight:400}

/* segmented control: one active choice (time range, direction, shortcuts ...) */
.controls{display:flex; gap:14px; flex-wrap:wrap}
.seg{display:inline-flex; background:var(--surface); border:1px solid var(--border);
     border-radius:10px; overflow:hidden}
.seg button{background:none; border:none; color:var(--muted); padding:9px 16px;
            cursor:pointer; font:inherit; font-weight:500}
.seg button:hover{color:var(--ink)}
.seg button.on{background:var(--bl-100); color:var(--primary)}

/* action button */
button[type=submit]{background:var(--primary); color:#fff; border:none; border-radius:9px;
                    padding:9px 18px; font:inherit; font-weight:600; cursor:pointer}
button[type=submit]:hover{background:var(--bl-600)}

/* small screens */
@media(max-width:760px){
  .topbar{padding:10px 14px; flex-wrap:wrap}   /* plus: nav folds behind a menu button */
  main{margin:16px auto; padding:0 14px; gap:14px}
  .card{padding:16px}
  .tbl{display:block; overflow-x:auto}          /* wide tables scroll inside their own box */
}
@media(max-width:420px){ .card{padding:13px} }
```

```html
<div class="controls">
  <div class="seg" id="range">
    <button class="on" data-v="36h">36 h</button><button data-v="7d">Week</button><button data-v="30d">Month</button>
  </div>
</div>
```

A few lines of JS move `.on` on click and pass `data-v` to a callback, so the control works the same
everywhere. Bifrost applies the same `.seg` rules to `<a>` elements for choices that are links.

## Charts (uPlot)

[uPlot](https://github.com/leeoniya/uPlot) is small and fast. The clean look comes from the settings:

- **Thin lines, limited palette.** Stroke width `2` and a repeating list of 10 colours, read from the
  CSS tokens when the chart is drawn, so a theme recolours the charts too.
- **Barely visible grid.** Axis text in `--muted`, grid and ticks in `--grid`.
- **The y-axis always starts at 0.** Honest traffic graphs, no exaggerated peaks.
- **Adaptive y-axis width.** Measure the widest label with the axis font. A fixed `size: 70` clipped
  the first digit when font, zoom or screen DPI made the labels wider.
- **Live legend** that follows the cursor.
- **Subtle area fill** on the first series, only when there are at most three series (the hex colour
  + `"18"` as alpha).
- **Responsive.** Width = container width, height = 40 % of the width clamped to 200–320 px, refit
  with a `ResizeObserver`. A matching `min-height` on the container keeps the page from jumping.
- **Formatted values.** bps in Kbps/Mbps/Gbps/Tbps; 24-hour time on the x-axis, with the label format
  chosen by the visible span (time, day and month, month name).

```js
const cvar = (n, fallback) =>
  getComputedStyle(document.documentElement).getPropertyValue(n).trim() || fallback;
const COLORS = ["--bl-800","--gr-700","--bl-500","--bl-600","--gr-500",
                "--bl-900","--bl-300","--gr-300","--muted","--danger"].map(n => cvar(n, "#6e72a6"));
const axc = cvar("--muted", "#6e72a6"), gridc = cvar("--grid", "#ececf8");

function yAxisSize(self, values, axisIdx, cycleNum) {     // uPlot's auto-size recipe
  const axis = self.axes[axisIdx];
  if (cycleNum > 1) return axis._size;                    // keep the first measurement
  let sz = (axis.ticks ? axis.ticks.size : 0) + (axis.gap || 0);
  const longest = (values || []).reduce((a, v) => (v && v.length > a.length ? v : a), "");
  if (longest) {
    self.ctx.font = axis.font[0];                         // the font uPlot draws with
    sz += self.ctx.measureText(longest).width / (window.devicePixelRatio || 1);
  }
  return Math.ceil(sz + 4);
}
const size = () => { const w = el.clientWidth || 320;
  return { width: w, height: Math.max(200, Math.min(320, Math.round(w * 0.4))) }; };

const u = new uPlot({
  ...size(),
  legend: { live: true },
  cursor: { points: { size: 5 } },
  scales: { y: { range: (_, min, max) => [0, max * 1.08 || 1] } },
  axes: [
    { stroke: axc, grid: { stroke: gridc }, ticks: { stroke: gridc } },              // x
    { stroke: axc, grid: { stroke: gridc }, ticks: { stroke: gridc },                // y
      values: (_, vals) => vals.map(fmtBps), size: yAxisSize },
  ],
  series: [{}, { label: "In+Out", stroke: COLORS[0], width: 2, fill: COLORS[0] + "18" }],
}, data, el);
new ResizeObserver(() => u.setSize(size())).observe(el);
```

Three components, one accent colour, plenty of whitespace, thin lines and a barely visible grid.
Stick to that and it stays tidy almost by itself, whatever palette or content you fill in.
