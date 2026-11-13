"""Build a self-contained HTML dashboard from the tenant query logs.

Reads logs/*.jsonl (written by tenant_ops_agent.py) and writes dashboard.html.

Run:  python build_dashboard.py [--logs DIR] [--kb FILE] [--out FILE]
"""
import argparse
import datetime as dt
import html
import json
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"
KB_PATH = HERE / "data" / "knowledge_base.json"
OUT_PATH = HERE / "dashboard.html"

# Stacking order. Each outcome has a status token and an icon, so color is never the only cue.
OUTCOMES = [
    ("answered", "Answered", "✓", "good"),
    ("no_evidence", "Not covered", "◐", "warning"),
    ("unsupported_figure", "Figure blocked", "▲", "serious"),
    ("cross_tenant_mention", "Cross-tenant blocked", "✕", "critical"),
]
# A record with several guardrail flags counts under the most severe one.
SEVERITY = ["cross_tenant_mention", "unsupported_figure", "no_evidence"]

CSS = """
:root { color-scheme: light; --page: #f9f9f7; --surface-1: #fcfcfb; --text-primary: #0b0b0b;
  --text-secondary: #52514e; --muted: #898781; --grid: #e1e0d9; --axis: #c3c2b7; --series-1: #2a78d6;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b; }
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) { color-scheme: dark; --page: #0d0d0d; --surface-1: #1a1a19;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --grid: #2c2c2a; --axis: #383835; --series-1: #3987e5; }
}
:root[data-theme="dark"] { color-scheme: dark; --page: #0d0d0d; --surface-1: #1a1a19;
  --text-primary: #f0efec; --text-secondary: #c3c2b7; --grid: #2c2c2a; --axis: #383835; --series-1: #3987e5; }
body { margin: 0; background: var(--page); color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 960px; margin: 0 auto; padding: 32px 24px 48px; }
h1 { font-size: 22px; font-weight: 600; margin: 0 0 4px; }
.sub { color: var(--text-secondary); font-size: 14px; margin: 0 0 24px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 16px; }
.tile, .card { background: var(--surface-1); border: 1px solid var(--grid); border-radius: 8px; padding: 16px; }
.tile .value { font-size: 30px; font-weight: 600; }
.tile .label { color: var(--text-secondary); font-size: 13px; margin-top: 2px; }
.card { margin-bottom: 16px; }
.card h2 { font-size: 15px; font-weight: 600; margin: 0 0 2px; }
.card p.note { color: var(--text-secondary); font-size: 13px; margin: 0 0 12px; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 13px; color: var(--text-secondary); margin-bottom: 10px; }
.swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; vertical-align: -1px; }
svg { width: 100%; height: auto; display: block; overflow: visible; }
svg .lbl { fill: var(--text-secondary); font-size: 13px; }
svg .val { fill: var(--text-primary); font-size: 12px; }
svg .axis { stroke: var(--axis); stroke-width: 1; }
svg .seg { stroke: var(--surface-1); stroke-width: 2; }
details { margin-top: 16px; font-size: 13px; color: var(--text-secondary); }
summary { cursor: pointer; }
table { border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 8px; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--grid); color: var(--text-primary); }
th { color: var(--text-secondary); font-weight: 500; }
.num { text-align: right; }
.empty { color: var(--text-secondary); }
"""


def outcome_of(record):
    flags = set(record.get("flags", []))
    for key in SEVERITY:
        if key in flags:
            return key
    return "answered"


def load_records(log_dir):
    records = []
    for path in sorted(log_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as f:
            records.extend(json.loads(line) for line in f if line.strip())
    return records


def bar_path(x, y, w, h, r=4):
    """Rectangle with a rounded data end only; the baseline end stays square."""
    r = min(r, w / 2, h / 2)
    return (f"M{x:.1f},{y:.1f} H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
            f"V{y + h - r:.1f} Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f} H{x:.1f} Z")


def hbar_svg(rows, title, label_w=260, width=760, row_h=28, bar_h=14):
    """rows: (label, value, tooltip). One series, so one color."""
    plot_w = width - label_w - 60
    peak = max(v for _, v, _ in rows)
    height = row_h * len(rows) + 4
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
           f'<line class="axis" x1="{label_w}" y1="0" x2="{label_w}" y2="{height}"/>']
    for i, (label, value, tip) in enumerate(rows):
        y = i * row_h + (row_h - bar_h) / 2
        cy = i * row_h + row_h / 2 + 4
        w = max(2.0, plot_w * value / peak)
        out.append(f'<text class="lbl" x="{label_w - 10}" y="{cy:.1f}" text-anchor="end">{html.escape(label)}</text>')
        out.append(f'<path d="{bar_path(label_w, y, w, bar_h)}" style="fill:var(--series-1)">'
                   f'<title>{html.escape(tip)}</title></path>')
        out.append(f'<text class="val" x="{label_w + w + 6:.1f}" y="{cy:.1f}">{value}</text>')
    out.append("</svg>")
    return "\n".join(out)


def stacked_svg(tenants, per_tenant, title, label_w=160, width=760, row_h=30, bar_h=16):
    """One row per tenant, one segment per outcome. The stroke on each segment is the 2px surface gap."""
    plot_w = width - label_w - 60
    totals = {t: sum(per_tenant[t].values()) for t in tenants}
    peak = max(totals.values())
    height = row_h * len(tenants) + 4
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
           f'<line class="axis" x1="{label_w}" y1="0" x2="{label_w}" y2="{height}"/>']
    for i, t in enumerate(tenants):
        y = i * row_h + (row_h - bar_h) / 2
        cy = i * row_h + row_h / 2 + 4
        out.append(f'<text class="lbl" x="{label_w - 10}" y="{cy:.1f}" text-anchor="end">{html.escape(t)}</text>')
        x = label_w
        for key, label, _, token in OUTCOMES:
            n = per_tenant[t].get(key, 0)
            if not n:
                continue
            w = plot_w * n / peak
            tip = f"{t} · {label}: {n} of {totals[t]}"
            out.append(f'<rect class="seg" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{bar_h}" '
                       f'style="fill:var(--{token})"><title>{html.escape(tip)}</title></rect>')
            x += w
        out.append(f'<text class="val" x="{x + 6:.1f}" y="{cy:.1f}">{totals[t]}</text>')
    out.append("</svg>")
    return "\n".join(out)


def legend_html():
    spans = "".join(
        f'<span><span class="swatch" style="background:var(--{token})"></span>{icon} {html.escape(label)}</span>'
        for _, label, icon, token in OUTCOMES
    )
    return f'<div class="legend">{spans}</div>'


def table_html(headers, rows):
    """headers: (name, numeric). rows: lists of cell values, escaped here."""
    head = "".join(f'<th class="{"num" if num else ""}">{html.escape(name)}</th>' for name, num in headers)
    body = "".join(
        "<tr>" + "".join(f'<td class="{"num" if num else ""}">{html.escape(str(cell))}</td>'
                         for (_, num), cell in zip(headers, row)) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def card(title, note, content):
    return (f'<section class="card"><h2>{html.escape(title)}</h2>'
            f'<p class="note">{html.escape(note)}</p>{content}</section>')


def tile(value, label):
    return f'<div class="tile"><div class="value">{value}</div><div class="label">{html.escape(label)}</div></div>'


def page(sub, body, generated):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tenant Operations Assistant: dashboard</title>
<style>{CSS}</style>
</head>
<body>
<main>
<h1>Tenant Operations Assistant</h1>
<p class="sub">{html.escape(sub)} · generated {generated}</p>
{body}
</main>
</body>
</html>
"""


def render(records, chunk_info, generated):
    if not records:
        body = '<div class="card empty">No queries logged yet. The agent appends one line per question to logs/&lt;tenant&gt;.jsonl.</div>'
        return page("No logged queries", body, generated)

    total = len(records)
    stamps = sorted(r["ts"] for r in records)
    by_tenant = Counter(r["tenant_id"] for r in records)
    tenants = [t for t, _ in by_tenant.most_common()]
    per_tenant = defaultdict(Counter)
    for r in records:
        per_tenant[r["tenant_id"]][outcome_of(r)] += 1
    overall = Counter(outcome_of(r) for r in records)
    draft_answers = sum("draft_source" in r.get("flags", []) for r in records)
    chunks = Counter(c for r in records for c in r.get("chunk_ids", []))

    tiles = [
        (total, "Logged queries"),
        (len(tenants), "Tenants"),
        (f"{100 * overall['answered'] / total:.0f}%", "Answered"),
        (overall["cross_tenant_mention"] + overall["unsupported_figure"], "Blocked by guardrails"),
        (overall["no_evidence"], "Not covered"),
        (draft_answers, "Answers citing draft docs"),
    ]
    body = '<div class="kpis">' + "".join(tile(v, label) for v, label in tiles) + "</div>"
    body += card(
        "Query volume by tenant",
        "Logged questions per tenant.",
        hbar_svg([(t, by_tenant[t], f"{t}: {by_tenant[t]} queries") for t in tenants], "Query volume by tenant"),
    )
    body += card(
        "Outcomes by tenant",
        "Answered versus guardrail outcomes. Hover a segment for its count.",
        legend_html() + stacked_svg(tenants, per_tenant, "Outcomes by tenant"),
    )
    if chunks:
        rows = []
        for cid, n in chunks.most_common(10):
            info = chunk_info.get(cid, {})
            title = info.get("title", cid)
            label = f"{cid} · {title}"
            if len(label) > 34:
                label = label[:33] + "…"
            rows.append((label, n, f"{cid} · {title} · {info.get('scope', 'unknown scope')} · {n} retrievals"))
        body += card(
            "Chunk usage",
            "Most-retrieved chunks. A tenant only retrieves its own chunks and global ones.",
            hbar_svg(rows, "Chunk usage"),
        )

    tenant_table = table_html(
        [("Tenant", False), ("Queries", True)] + [(label, True) for _, label, _, _ in OUTCOMES],
        [[t, by_tenant[t]] + [per_tenant[t][k] for k, _, _, _ in OUTCOMES] for t in tenants],
    )
    chunk_table = table_html(
        [("Chunk", False), ("Title", False), ("Scope", False), ("Retrievals", True)],
        [[cid, chunk_info.get(cid, {}).get("title", cid), chunk_info.get(cid, {}).get("scope", "unknown"), n]
         for cid, n in chunks.most_common()],
    )
    body += f"<details><summary>Table view</summary>{tenant_table}{chunk_table}</details>"

    stamps_sub = f"{total} logged queries, {stamps[0][:10]} to {stamps[-1][:10]}"
    return page(stamps_sub, body, generated)


def main():
    parser = argparse.ArgumentParser(description="Build the tenant operations dashboard.")
    parser.add_argument("--logs", type=Path, default=LOG_DIR)
    parser.add_argument("--kb", type=Path, default=KB_PATH)
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args()

    records = load_records(args.logs)
    chunk_info = {c["id"]: c for c in json.loads(args.kb.read_text(encoding="utf-8"))} if args.kb.exists() else {}
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    args.out.write_text(render(records, chunk_info, generated), encoding="utf-8")
    print(f"wrote {args.out} from {len(records)} logged queries")


if __name__ == "__main__":
    main()
