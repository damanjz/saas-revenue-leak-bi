"""Draw the case-study charts as SVG straight from the scorecard tables.

    python docs/charts.py
writes docs/img/detection.svg and docs/img/early-warning.svg. Numbers come
from the warehouse, never typed in, so a rerun of the pipeline redraws them.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT / "docs" / "img"
INK, RUST, TEAL, OCHRE = "#2f55b0", "#c0533a", "#1f8f7a", "#c27a1a"
PAPER, RAIL, RULE, TEXT, MUTED = "#fbfaf7", "#f2efe7", "#e3ded2", "#1d2330", "#5b6170"
FONT = "Segoe UI, system-ui, sans-serif"
START, END = date(2024, 10, 1), date(2026, 9, 30)
LABELS = {
    "INC-01": "Price rise on the Starter plan",
    "INC-02": "Release 5.2 breaks Reports",
    "INC-03": "Account manager neglects their book",
    "INC-04": "Stripe re-sends webhooks",
    "INC-05": "Partner launch brings weak customers",
}


def esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def detection(con, key):
    inc = con.sql("select * from scorecard.incidents order by incident").df()
    windows = {i["id"]: (date.fromisoformat(i["start"]), date.fromisoformat(i["end"])) for i in key["incidents"]}
    W, left, right, row = 820, 250, 24, 46
    H = 44 + row * len(inc) + 30
    span = (END - START).days

    def x(d):
        return left + (W - left - right) * (d - START).days / span

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="{FONT}">',
           f'<rect width="{W}" height="{H}" fill="{PAPER}"/>']
    for y_, m in ((2024, 10), (2025, 1), (2025, 4), (2025, 7), (2025, 10), (2026, 1), (2026, 4), (2026, 7)):
        d = date(y_, m, 1)
        out.append(f'<line x1="{x(d):.1f}" y1="34" x2="{x(d):.1f}" y2="{H - 26}" stroke="{RULE}"/>')
        out.append(f'<text x="{x(d):.1f}" y="24" font-size="11" fill="{MUTED}" text-anchor="middle">{d:%b %Y}</text>')
    for i, r in enumerate(inc.itertuples()):
        y = 44 + i * row
        s, e = windows[r.incident]
        out.append(f'<text x="0" y="{y + 14}" font-size="12" fill="{TEXT}">{esc(LABELS[r.incident])}</text>')
        out.append(f'<text x="0" y="{y + 30}" font-size="10.5" fill="{MUTED}">{r.incident} · {r.kind.replace("_", " ")}</text>')
        out.append(f'<rect x="{x(s):.1f}" y="{y + 6}" width="{max(x(e) - x(s), 3):.1f}" height="16" fill="{RAIL}" stroke="{RULE}"/>')
        out.append(f'<line x1="{x(s):.1f}" y1="{y + 2}" x2="{x(s):.1f}" y2="{y + 26}" stroke="{TEXT}" stroke-width="1.5"/>')
        if r.detected:
            a = date.fromisoformat(str(r.first_alert)[:10])
            out.append(f'<line x1="{x(s):.1f}" y1="{y + 14}" x2="{x(a):.1f}" y2="{y + 14}" stroke="{INK}" stroke-width="2"/>')
            out.append(f'<circle cx="{x(a):.1f}" cy="{y + 14}" r="5" fill="{INK}"/>')
            label = f"{int(r.days_to_detect)} days"
            anchor, dx = ("end", -9) if x(a) > W - 90 else ("start", 9)
            out.append(f'<text x="{x(a) + dx:.1f}" y="{y + 18}" font-size="11" fill="{INK}" text-anchor="{anchor}">{label}</text>')
        else:
            out.append(f'<text x="{x(e) + 8:.1f}" y="{y + 18}" font-size="11" fill="{RUST}">missed</text>')
    ly = H - 10
    out.append(f'<rect x="{left}" y="{ly - 9}" width="18" height="9" fill="{RAIL}" stroke="{RULE}"/>')
    out.append(f'<text x="{left + 24}" y="{ly}" font-size="10.5" fill="{MUTED}">incident active</text>')
    out.append(f'<circle cx="{left + 130}" cy="{ly - 4}" r="4" fill="{INK}"/>')
    out.append(f'<text x="{left + 140}" y="{ly}" font-size="10.5" fill="{MUTED}">first alert from the monitor, days after the incident started</text>')
    out.append("</svg>")
    (IMG / "detection.svg").write_text("\n".join(out), encoding="utf-8", newline="\n")


def early_warning(con):
    ew = con.sql("select * from scorecard.early_warning").df()
    order = ["ALL", "INC-01", "INC-02", "INC-03", "INC-05", "organic"]
    names = {"ALL": "All churns", "INC-01": "Price rise", "INC-02": "Bad release", "INC-03": "Neglected book",
             "INC-05": "Weak partner cohort", "organic": "Organic (no incident)"}
    ew = ew.set_index("true_cause").reindex([o for o in order if o in set(ew["true_cause"])])
    W, left, row, bar = 820, 170, 40, 12
    plot = W - left - 70
    H = 30 + row * len(ew) + 26
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="{FONT}">',
           f'<rect width="{W}" height="{H}" fill="{PAPER}"/>']
    for pct in (0, 25, 50, 75, 100):
        xx = left + plot * pct / 100
        out.append(f'<line x1="{xx:.1f}" y1="22" x2="{xx:.1f}" y2="{H - 24}" stroke="{RULE}"/>')
        out.append(f'<text x="{xx:.1f}" y="14" font-size="10.5" fill="{MUTED}" text-anchor="middle">{pct}%</text>')
    for i, (cause, r) in enumerate(ew.iterrows()):
        y = 30 + i * row
        weight = "600" if cause == "ALL" else "400"
        out.append(f'<text x="0" y="{y + 14}" font-size="12" font-weight="{weight}" fill="{TEXT}">{names[cause]}</text>')
        out.append(f'<text x="0" y="{y + 28}" font-size="10" fill="{MUTED}">{int(r.churns)} churns</text>')
        for j, (val, col) in enumerate(((r.model_caught, INK), (r.health_caught, OCHRE))):
            yy = y + 4 + j * (bar + 3)
            out.append(f'<rect x="{left}" y="{yy}" width="{plot * val:.1f}" height="{bar}" fill="{col}"/>')
            out.append(f'<text x="{left + plot * val + 6:.1f}" y="{yy + 10}" font-size="10.5" fill="{col}">{val:.0%}</text>')
    ly = H - 8
    out.append(f'<rect x="{left}" y="{ly - 9}" width="12" height="9" fill="{INK}"/>')
    out.append(f'<text x="{left + 18}" y="{ly}" font-size="10.5" fill="{MUTED}">churn model (top 10% risk that week)</text>')
    out.append(f'<rect x="{left + 250}" y="{ly - 9}" width="12" height="9" fill="{OCHRE}"/>')
    out.append(f'<text x="{left + 268}" y="{ly}" font-size="10.5" fill="{MUTED}">rules-based health score (At Risk band)</text>')
    out.append("</svg>")
    (IMG / "early-warning.svg").write_text("\n".join(out), encoding="utf-8", newline="\n")


def main():
    IMG.mkdir(parents=True, exist_ok=True)
    key = json.loads((ROOT / "data" / "answer_key" / "answer_key.json").read_text())
    con = duckdb.connect(str(ROOT / "warehouse" / "revenue.duckdb"), read_only=True)
    detection(con, key)
    early_warning(con)
    print("wrote docs/img/detection.svg, docs/img/early-warning.svg")


if __name__ == "__main__":
    main()
