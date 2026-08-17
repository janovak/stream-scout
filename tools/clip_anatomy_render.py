"""Render the payload from clip_anatomy.py as a standalone HTML page.

Kept separate from the reconstruction so the numbers can be dumped as JSON and
checked without going near the drawing code.

Two charts, not one with two y-axes: chat volume and intensity are different
scales, and overlaying them on a shared axis invents a crossing point that does
not mean anything. The colours are validated for colour-blind separation in both
themes; the marker badges are numbered so identity never rests on colour alone.
"""

CLIP_CAPTURE_SECONDS = 30  # Twitch always grabs the 30s before the request

# Light and dark steps are chosen per theme, not flipped, so both sit inside
# their own mode's lightness band.
_CSS_TOKENS = """
:root{--bg:#FCFCFB;--surf:#fff;--ink:#191A17;--mut:#63665E;--line:#E3E4DE;
--vol:#2F6FD0;--int:#B4690E;--ok:#00806A;--bad:#C0392B;
--sh:0 1px 2px rgba(20,22,18,.06),0 10px 28px rgba(20,22,18,.07)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--bg:#141513;--surf:#1D1F1C;--ink:#ECEDE8;--mut:#9A9D93;--line:#2C2F2A;
--vol:#4A86D8;--int:#B57F2E;--ok:#00997F;--bad:#D14A44;
--sh:0 1px 2px rgba(0,0,0,.45),0 10px 28px rgba(0,0,0,.35)}}
:root[data-theme="dark"]{
--bg:#141513;--surf:#1D1F1C;--ink:#ECEDE8;--mut:#9A9D93;--line:#2C2F2A;
--vol:#4A86D8;--int:#B57F2E;--ok:#00997F;--bad:#D14A44;
--sh:0 1px 2px rgba(0,0,0,.45),0 10px 28px rgba(0,0,0,.35)}
"""

_CSS = _CSS_TOKENS + """
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-size:16px;line-height:1.55;
font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:1060px;margin:0 auto;padding:52px 22px 90px;display:flex;flex-direction:column;gap:36px}
h1{margin:0;font-size:clamp(1.7rem,4vw,2.3rem);letter-spacing:-.025em;line-height:1.12;
text-wrap:balance;font-weight:680}
.eyebrow{margin:0 0 10px;font-size:.71rem;text-transform:uppercase;letter-spacing:.14em;
color:var(--mut);font-weight:660}
.lede{margin:12px 0 0;color:var(--mut);max-width:68ch}
.lede b{color:var(--ink);font-weight:620}
.card{background:var(--surf);border:1px solid var(--line);border-radius:13px;
padding:24px 26px;box-shadow:var(--sh)}
h2{margin:0 0 4px;font-size:1.14rem;letter-spacing:-.012em}
.sub{margin:0 0 18px;color:var(--mut);font-size:.9rem}
.scroll{overflow-x:auto}
svg{display:block;min-width:700px}
.gl{stroke:var(--line);stroke-width:1}
.tk{fill:var(--mut);font-size:11px;text-anchor:middle;font-variant-numeric:tabular-nums}
.tk.ax{text-anchor:end}
.mk{stroke:var(--ink);stroke-width:1.5;stroke-dasharray:3 3;opacity:.5}
.clipband{fill:var(--ok);opacity:.13;stroke:var(--ok);stroke-width:1;
stroke-dasharray:4 3;stroke-opacity:.55}
.bandlbl{fill:var(--ok);font-size:11px;font-weight:680;letter-spacing:.04em;text-transform:uppercase}
.bdg{fill:var(--ink)}
.bdgt{fill:var(--bg);font-size:11px;font-weight:700;text-anchor:middle}
.volL{fill:none;stroke:var(--vol);stroke-width:2;stroke-linejoin:round}
.volA{fill:var(--vol);opacity:.13}
.intL{fill:none;stroke:var(--int);stroke-width:2;stroke-linejoin:round}
.intA{fill:var(--int);opacity:.13}
.kline{stroke:var(--bad);stroke-width:1.5;stroke-dasharray:5 3}
.klbl{fill:var(--bad);font-size:11px;text-anchor:end;font-weight:640}
.alab{fill:var(--mut);font-size:11px;font-weight:620;letter-spacing:.04em;text-transform:uppercase}
.legend{display:flex;gap:18px;list-style:none;padding:0;margin:0 0 14px;font-size:.85rem;
color:var(--mut);flex-wrap:wrap}
.legend li{display:flex;align-items:center;gap:7px}
.sw{width:11px;height:11px;border-radius:3px;display:inline-block}
.steps{list-style:none;margin:22px 0 0;padding:0;display:grid;gap:12px}
.steps li{display:grid;grid-template-columns:92px 1fr;gap:14px;align-items:start}
.stepn{font-variant-numeric:tabular-nums;font-weight:700;display:flex;align-items:center;
justify-content:flex-end;gap:7px;padding-top:1px;border-right:2px solid var(--line);padding-right:12px}
.num{background:var(--ink);color:var(--bg);width:18px;height:18px;border-radius:50%;
display:grid;place-items:center;font-size:.72rem;flex:none}
.stepb h4{margin:0;font-size:.95rem}
.stepb p{margin:2px 0 0;color:var(--mut);font-size:.88rem}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:18px;margin:0}
.facts div{display:flex;flex-direction:column;gap:1px}
.facts dt{font-size:.74rem;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);font-weight:640}
.facts dd{margin:0;font-size:1.16rem;font-weight:680;font-variant-numeric:tabular-nums}
.verdict{display:inline-flex;align-items:center;gap:8px;font-size:.9rem;font-weight:660;
padding:8px 14px;border-radius:9px}
.verdict.ok{background:color-mix(in srgb,var(--ok) 14%,transparent);color:var(--ok)}
.verdict.edge{background:color-mix(in srgb,var(--int) 17%,transparent);color:var(--int)}
.verdict.bad{background:color-mix(in srgb,var(--bad) 14%,transparent);color:var(--bad)}
.warn{margin:16px 0 0;padding:13px 16px;border-radius:9px;font-size:.9rem;
background:color-mix(in srgb,var(--bad) 10%,transparent);color:var(--ink);
border:1px solid color-mix(in srgb,var(--bad) 35%,transparent)}
.foot{color:var(--mut);font-size:.84rem;margin:0}
a{color:var(--vol)}
"""


def render(p):
    W, PADL, PADR = 980, 58, 20
    PW = W - PADL - PADR
    HA, HB = 190, 150
    series = [s for s in p["series"] if s["i"] is not None]
    if not series:
        raise SystemExit("no measurable seconds in the plotted window")
    xmin = min(s["o"] for s in series)
    xmax = max(s["o"] for s in series)

    def x(o):
        return PADL + (o - xmin) / max(1, (xmax - xmin)) * PW

    wmax = max(10, max(s["w"] for s in series)) * 1.06
    imax = max(6.0, max(s["i"] for s in series)) * 1.08

    def y(v, mx, h):
        return h - (min(v, mx) / mx) * h

    def line(key, mx, h, cls):
        d = " ".join(("M" if n == 0 else "L") + f"{x(s['o']):.1f},{y(s[key], mx, h):.1f}"
                     for n, s in enumerate(series))
        a = (f"M{x(series[0]['o']):.1f},{h:.1f} "
             + " ".join(f"L{x(s['o']):.1f},{y(s[key], mx, h):.1f}" for s in series)
             + f" L{x(series[-1]['o']):.1f},{h:.1f} Z")
        return f'<path class="{cls}A" d="{a}"/><path class="{cls}L" d="{d}"/>'

    step = 10 if (xmax - xmin) <= 140 else 20
    lo_tick = int(xmin // step) * step
    ticks = [o for o in range(lo_tick, xmax + 1, step) if xmin <= o <= xmax]

    marks = []
    if p["hold_open_offset"] is not None:
        marks.append((p["hold_open_offset"], "Hold opens",
                      f"chat crosses k={p['config']['k']}; the detector opens an episode "
                      "and remembers the highest point"))
    marks.append((0, "Peak",
                  f"intensity {p['peak_intensity']} &mdash; {p['peak_count']} messages in "
                  f"{p['config']['window_seconds']}s against a {p['baseline_mean']}/s baseline"))
    if p["emit_offset"] is not None:
        marks.append((p["emit_offset"], "Episode ends &rarr; emit",
                      "chat falls back under k, so the detector reports, carrying the peak"))
    if p["request_offset"] is not None:
        marks.append((p["request_offset"], "Clip requested",
                      f"+{p['config']['watermark_seconds']}s watermark. Twitch grabs the "
                      f"previous {CLIP_CAPTURE_SECONDS}s"))
    marks = [m for m in marks if xmin <= m[0] <= xmax]

    def vlines(h, badge=False):
        out = "".join(f'<line class="mk" x1="{x(o):.1f}" y1="0" x2="{x(o):.1f}" y2="{h}"/>'
                      for o, _, _ in marks)
        if badge:
            for n, (o, _, _) in enumerate(marks, 1):
                out += (f'<circle class="bdg" cx="{x(o):.1f}" cy="-9" r="9"/>'
                        f'<text class="bdgt" x="{x(o):.1f}" y="-5.5">{n}</text>')
        return out

    band = ""
    if p["request_offset"] is not None:
        b0 = max(xmin, p["request_offset"] - CLIP_CAPTURE_SECONDS)
        b1 = min(xmax, p["request_offset"])
        if b1 > b0:
            band = (f'<rect class="clipband" x="{x(b0):.1f}" y="0" '
                    f'width="{x(b1) - x(b0):.1f}" height="{HA}"/>'
                    f'<text class="bandlbl" x="{x((b0 + b1) / 2):.1f}" y="16" '
                    f'text-anchor="middle">the {CLIP_CAPTURE_SECONDS}s this clip covers</text>')

    yga = "".join(
        f'<g><line class="gl" x1="{PADL}" y1="{y(v, wmax, HA):.1f}" x2="{PADL + PW}" '
        f'y2="{y(v, wmax, HA):.1f}"/><text class="tk ax" x="{PADL - 8}" '
        f'y="{y(v, wmax, HA) + 4:.1f}">{int(v)}</text></g>'
        for v in [0, wmax / 3, 2 * wmax / 3, wmax])
    ygb = "".join(
        f'<g><line class="gl" x1="{PADL}" y1="{y(v, imax, HB):.1f}" x2="{PADL + PW}" '
        f'y2="{y(v, imax, HB):.1f}"/><text class="tk ax" x="{PADL - 8}" '
        f'y="{y(v, imax, HB) + 4:.1f}">{v:.0f}</text></g>'
        for v in [0, imax / 3, 2 * imax / 3, imax])
    tick_svg = "".join(f'<text class="tk" x="{x(o):.1f}" y="14">{o:+d}</text>' for o in ticks)
    kline = (f'<line class="kline" x1="{PADL}" y1="{y(p["config"]["k"], imax, HB):.1f}" '
             f'x2="{PADL + PW}" y2="{y(p["config"]["k"], imax, HB):.1f}"/>'
             f'<text class="klbl" x="{PADL + PW}" y="{y(p["config"]["k"], imax, HB) - 6:.1f}">'
             f'trigger k = {p["config"]["k"]}</text>')

    steps = "".join(
        f'<li><div class="stepn"><span class="num">{n}</span>{o:+d}s</div>'
        f'<div class="stepb"><h4>{t}</h4><p>{d}</p></div></li>'
        for n, (o, t, d) in enumerate(marks, 1))

    ro = p["request_offset"]
    if ro is None:
        vcls, vtxt = "edge", "The detector never reported this peak in the replay"
    elif ro > CLIP_CAPTURE_SECONDS:
        vcls, vtxt = "bad", (f"Peak missed &mdash; the clip opens {ro - CLIP_CAPTURE_SECONDS}s "
                             "after the moment had passed")
    elif ro > CLIP_CAPTURE_SECONDS - 5:
        vcls, vtxt = "edge", (f"On the edge &mdash; the peak sits {CLIP_CAPTURE_SECONDS - ro}s "
                              "from the start of the clip")
    else:
        vcls, vtxt = "ok", (f"Peak captured &mdash; {CLIP_CAPTURE_SECONDS - ro}s inside "
                            "the clip's window")

    clip_link = (f' &middot; <a href="https://clips.twitch.tv/{p["clip_id"]}" '
                 f'target="_blank" rel="noopener">watch the clip</a>') if p["clip_id"] else ""
    poll = ""
    if p["short_share"] >= 0.6:
        poll = (f'<p class="warn"><b>{p["short_share"]:.0%} of the peak window is one- or '
                "two-character messages.</b> That pattern is chat answering a poll rather than "
                "reacting to a moment. The spike is real; it may still not be worth clipping.</p>")

    facts = "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in [
        ("Intensity", p["peak_intensity"]),
        (f'Messages / {p["config"]["window_seconds"]}s', p["peak_count"]),
        ("Baseline", f'{p["baseline_mean"]}/s'),
        ("Peak &rarr; request", f"{ro}s" if ro is not None else "&mdash;"),
    ])

    return f"""<title>Clip Anatomy &mdash; {p['login']}</title>
<style>{_CSS}</style>
<div class="wrap">
<header>
<p class="eyebrow">Stream Scout &middot; {p['login']} &middot; {p['peak_time']}{clip_link}</p>
<h1>Anatomy of a clip</h1>
<p class="lede">Replayed second by second through the live detector at
<b>k&nbsp;=&nbsp;{p['config']['k']}</b>. Chat rests near {p['baseline_mean']} messages a second and
reaches <b>{p['peak_count']} in {p['config']['window_seconds']} seconds</b>. The markers show what
the system did, and when.</p>
</header>

<section class="card">
<h2>What happened</h2>
<p class="sub">Time in seconds, relative to the peak. Two measures on two scales, so they get
two charts.</p>
<dl class="facts">{facts}</dl>
<p style="margin:18px 0 0"><span class="verdict {vcls}">{vtxt}</span></p>
{poll}
</section>

<section class="card">
<h2>The spike</h2>
<ul class="legend">
<li><span class="sw" style="background:var(--vol)"></span>Chat volume,
{p['config']['window_seconds']}s window</li>
<li><span class="sw" style="background:var(--int)"></span>Intensity</li>
</ul>
<div class="scroll">
<svg viewBox="0 0 {W} {HA + HB + 108}" role="img"
     aria-label="Chat volume and intensity around the peak">
<g transform="translate(0,14)">
 <text class="alab" x="{PADL}" y="-2">Messages in the {p['config']['window_seconds']}-second window</text>
 <g transform="translate(0,20)">{yga}{band}{line('w', wmax, HA, 'vol')}{vlines(HA, True)}</g>
</g>
<g transform="translate(0,{HA + 62})">
 <text class="alab" x="{PADL}" y="-2">Intensity (standard deviations above baseline)</text>
 <g transform="translate(0,8)">{ygb}{line('i', imax, HB, 'int')}{kline}{vlines(HB)}</g>
 <g transform="translate(0,{HB + 12})">{tick_svg}
 <text class="alab" x="{PADL + PW / 2:.0f}" y="30" text-anchor="middle">seconds from peak</text></g>
</g>
</svg>
</div>
<ol class="steps">{steps}</ol>
</section>

<section class="card">
<h2>Reading this</h2>
<p class="sub" style="margin:0">Twitch always captures the
{CLIP_CAPTURE_SECONDS} seconds <em>before</em> the request, so whether the clip contains its own
peak comes down to one number: peak&nbsp;&rarr;&nbsp;request. Under
{CLIP_CAPTURE_SECONDS}s the moment is in the clip; over it, the clip starts after the moment
has already passed.</p>
<p class="foot" style="margin:14px 0 0">The intensity curve, the baseline, the hold and the emit
second come from the detector itself. The request time is modelled as emit + watermark + clip
delay &mdash; the true request time lives only in the taskmanager log.</p>
</section>
</div>"""
