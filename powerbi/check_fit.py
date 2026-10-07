"""Prove that no text in the report is cut off with an ellipsis.

Reads every visual in the PBIR report, collects each piece of text that renders
on a single line (titles, KPI labels and values, insight lines, slicer headers
and values, page tabs, table headers, matrix cells, bar-chart category labels)
and measures it with the report's real fonts. Dynamic text is evaluated in the
open Power BI window (open_pbip.ps1 -Keep) under every slicer combination that
can change it, and the widest result is the one checked.

A line fails if its measured width plus a 6% safety margin is wider than the
space its visual gives it. Exit code 1 on any failure.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "powerbi"))
import build_pbip as B  # noqa: E402

PAGES = B.RP / "definition" / "pages"
MARGIN = 1.06
SEGMENTS = [None, "SMB", "Mid-Market", "Enterprise"]
YEARS = [None, 2025, 2026, 2027]
FORMATS = {name: fmt for name, _, fmt, _ in B.MEASURES}


def dax_row(exprs: dict[str, str]) -> dict:
    query = "EVALUATE ROW(" + ", ".join(f'"{k}", {v}' for k, v in exprs.items()) + ")"
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(ROOT / "powerbi" / "query_desktop.ps1"), "-Dax", query], capture_output=True, text=True)
    if not r.stdout.strip():
        raise RuntimeError(r.stderr.strip()[:400])
    row = json.loads(r.stdout.strip())
    row = row[0] if isinstance(row, list) else row
    return {k.strip("[]"): v for k, v in row.items()}


def dax_table(query: str) -> list[dict]:
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(ROOT / "powerbi" / "query_desktop.ps1"), "-Dax", query], capture_output=True, text=True)
    if not r.stdout.strip():
        raise RuntimeError(r.stderr.strip()[:400])
    rows = json.loads(r.stdout.strip())
    return rows if isinstance(rows, list) else [rows]


def formatted(measure: str) -> str:
    """DAX expression returning the measure as the card shows it."""
    fmt = FORMATS.get(measure)
    if not fmt:
        return f"[{measure}]"
    return f'FORMAT([{measure}], "{fmt.replace(chr(34), chr(34) * 2)}")'


def contexts(page: str) -> list[str]:
    """Filter contexts a page's slicers can produce (as CALCULATE filter arguments)."""
    if page == "account":
        return []                       # handled per account below
    out = []
    years = YEARS if page in ("revenue", "cohorts") else [None]
    for sgm in SEGMENTS:
        for yr in years:
            f = []
            if sgm:
                f.append(f'Customer[segment] = "{sgm}"')
            if yr:
                f.append(f"'Date'[fiscal_year] = {yr}")
            out.append(", ".join(f))
    return out


def widest_values(page: str, measure: str, accounts: list[str]) -> list[str]:
    expr = formatted(measure)
    if page == "account":
        rows = dax_table(f"""EVALUATE TOPN(5, ADDCOLUMNS(VALUES(Customer[company_name]), "t",
                             CALCULATE({expr})), LEN([t]), DESC)""")
        return [str(r.get("[t]", "")) for r in rows if r.get("[t]") is not None]
    vals = []
    ctx = contexts(page)
    for i in range(0, len(ctx), 8):
        batch = {f"c{j}": (f"CALCULATE({expr}, {c})" if c else expr) for j, c in enumerate(ctx[i:i + 8])}
        vals += [str(v) for v in dax_row(batch).values() if v is not None]
    return vals


CELL_PAD = 14          # table and matrix cell padding, calibrated against the Desktop render
_cell_con = None


def cell_text(value, fmt) -> str:
    """A value as Power BI shows it with the column's format string."""
    if value is None:
        return ""
    if fmt == B.USD:
        return f"${value:,.0f}"
    if fmt == "0%":
        return f"{value:.0%}"
    if fmt in ("d mmm yyyy", "d mmm yy"):
        return f"{value.day} {value:%b %Y}"
    if fmt == "0.0":
        return f"{value:.1f}"
    return str(value)


def widest_cells(entity: str, col: str, high_only: bool) -> list[str]:
    """The 5 widest-looking values a table column can ever show (every value in the source, not just visible rows)."""
    global _cell_con
    _cell_con = _cell_con or duckdb.connect(str(ROOT / "warehouse" / "revenue.duckdb"), read_only=True)
    source = B.EXPORTS[B.TABLES[entity][0]]
    if entity == "Customer" and high_only:
        source = f"""(select c.* from {source} c join {B.EXPORTS['churn_risk_scores']} r using (customer_sk)
                      where r.risk_tier = 'High')"""
    elif high_only:
        source = f"(select * from {source} where risk_tier = 'High')"
    vals = [r[0] for r in _cell_con.sql(f"select distinct {col} from {source} where {col} is not null").fetchall()]
    texts = [cell_text(v, B.COLUMN_FORMATS.get(col)) for v in vals]
    return sorted(texts, key=len, reverse=True)[:5]


def prop(objs: dict, key: str, name: str, default=None):
    for entry in objs.get(key, []):
        v = entry.get("properties", {}).get(name)
        if v is not None:
            raw = v["expr"]["Literal"]["Value"]
            return raw.strip("'").rstrip("DL") if raw[0] != "'" else raw.strip("'")
    return default


layout: list[tuple] = []   # (page, what, needed px, available px) for whole-visual space checks


def collect() -> list[dict]:
    con = duckdb.connect(str(ROOT / "warehouse" / "revenue.duckdb"), read_only=True)
    names = [r[0] for r in con.sql("select distinct company_name from core.dim_customers where company_name is not null").fetchall()]
    ams = [r[0] for r in con.sql("select distinct account_manager from core.dim_customers").fetchall()]
    con.close()
    items: list[dict] = []

    def add(page, where, text, font, pt, room, bold=False):
        items.append({"id": len(items), "page": page, "where": where, "text": text, "font": font, "pt": float(pt),
                      "bold": bold, "room": room})

    order = json.loads((PAGES / "pages.json").read_text(encoding="utf-8"))["pageOrder"]
    for page in order:
        for vf in sorted((PAGES / page / "visuals").glob("*/visual.json")):
            v = json.loads(vf.read_text(encoding="utf-8"))
            name, w = vf.parent.name, v["position"]["width"]
            vis = v["visual"]
            vtype, objs = vis["visualType"], vis.get("objects", {})
            title = vis.get("visualContainerObjects", {}).get("title", [{}])[0].get("properties", {})
            if title.get("text"):
                add(page, f"{name} title", title["text"]["expr"]["Literal"]["Value"].strip("'").replace("''", "'"),
                    B.SANS, 11, w - 16)
            qs = vis.get("query", {}).get("queryState", {})
            if vtype == "cardVisual":
                projs = qs["Data"]["projections"]
                label_hidden = prop(objs, "label", "show") == "false"
                if label_hidden:                                   # insight / heading line
                    m = projs[0]["field"]["Measure"]["Property"]
                    font = prop(objs, "value", "fontFamily", B.SANS)
                    pt = float(prop(objs, "value", "fontSize", 11))
                    for text in widest_values(page, m, names):
                        add(page, f"{name} = [{m}]", text, font, pt, w - 20)
                else:                                              # KPI row
                    n = len(projs)
                    room = (w - 12 * (n - 1)) / n - 22            # accent bar + narrow padding
                    for pj in projs:
                        add(page, f"{name} label", pj.get("displayName", pj["nativeQueryRef"]), B.SANS, 10, room)
                        m = pj["field"]["Measure"]["Property"]
                        for text in widest_values(page, m, names):
                            add(page, f"{name} value [{m}]", text, B.SERIF, 22, room)
            elif vtype == "slicer":
                add(page, f"{name} header", prop(objs, "header", "text", ""), B.SANS, 10, w - 16)
                col = qs["Values"]["projections"][0]["field"]["Column"]["Property"]
                values = {"company_name": names, "account_manager": ams,
                          "segment": ["SMB", "Mid-Market", "Enterprise", "Unknown"],
                          "fiscal_year": ["2025", "2026", "2027"]}.get(col, [])
                widest = sorted(values, key=len, reverse=True)[:5]
                for text in widest:
                    add(page, f"{name} value", text, B.SANS, 11, w - 44)       # dropdown chevron
            elif vtype == "pageNavigator":
                room = w / len(order) - 16
                for pg in order:
                    disp = json.loads((PAGES / pg / "page.json").read_text(encoding="utf-8"))["displayName"]
                    add(page, f"{name} tab", disp, B.SERIF, 11, room)
            elif vtype == "tableEx":
                widths = {c["selector"]["metadata"]: float(c["properties"]["value"]["expr"]["Literal"]["Value"].rstrip("D"))
                          for c in objs.get("columnWidth", [])}
                high_only = any(f.get("name") == "highRisk" for f in v.get("filterConfig", {}).get("filters", []))
                # no horizontal scrollbar: columns must fit inside the visual
                total = sum(widths.values())
                layout.append((page, f"{name} column widths", total, w - 16))
                # no vertical scrollbar: title + header + the rows the top-N filter allows
                top = {"top8": 8, "newest4": 4, "latest6": 6, "newest6": 6}
                n_rows = next((top[f["name"]] for f in v.get("filterConfig", {}).get("filters", []) if f["name"] in top), None)
                if n_rows:
                    # row heights measured on the Desktop render: title ~30 px, header ~30 px, 9 pt rows ~24 px
                    layout.append((page, f"{name} {n_rows} rows", 30 + 30 + n_rows * 24, v["position"]["height"]))
                for pj in qs["Values"]["projections"]:
                    room = widths.get(pj["queryRef"], 100) - CELL_PAD
                    add(page, f"{name} header", pj.get("displayName", pj["nativeQueryRef"]), B.SANS, 9, room)
                    entity = pj["field"]["Column"]["Expression"]["SourceRef"]["Entity"]
                    col = pj["field"]["Column"]["Property"]
                    for text in widest_cells(entity, col, high_only):
                        add(page, f"{name} cell {col}", text, B.SANS, 9, room)
            elif vtype == "pivotTable":
                m = qs["Values"]["projections"][0]["field"]["Measure"]["Property"]
                rows_col = qs["Rows"]["projections"][0]["field"]["Column"]
                cols_col = qs["Columns"]["projections"][0]["field"]["Column"]
                cw = objs.get("columnWidth", [{}])[0].get("properties", {}).get("value")
                room = float(cw["expr"]["Literal"]["Value"].rstrip("D")) - CELL_PAD if cw else 60
                pt = float(prop(objs, "values", "fontSize", 9))
                cells = dax_table(f"""EVALUATE TOPN(5, SUMMARIZECOLUMNS('{rows_col['Expression']['SourceRef']['Entity']}'[{rows_col['Property']}],
                                     '{cols_col['Expression']['SourceRef']['Entity']}'[{cols_col['Property']}], "t", [{m}]), LEN([t]), DESC)""")
                for c in cells:
                    add(page, f"{name} cell", str(c.get("[t]", "")), B.SANS, pt, room)
                headers = dax_table(f"EVALUATE VALUES('{cols_col['Expression']['SourceRef']['Entity']}'[{cols_col['Property']}])")
                for h in headers:
                    add(page, f"{name} column header", str(list(h.values())[0]), B.SANS, float(prop(objs, "columnHeaders", "fontSize", 8)), room)
            elif vtype == "barChart":
                col = qs["Category"]["projections"][0]["field"]["Column"]["Property"]
                values = {"segment": ["Enterprise", "Mid-Market", "SMB"], "account_manager": ams,
                          "health_band": ["At Risk", "Watch", "Healthy"]}.get(col, [])
                for text in values:
                    add(page, f"{name} category label", text, B.SANS, 9, w * 0.45 - 10)
    return items


def main():
    items = collect()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump([{k: it[k] for k in ("id", "text", "font", "pt", "bold")} for it in items], f)
        tmp = f.name
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(ROOT / "powerbi" / "measure_text.ps1"), "-InFile", tmp], capture_output=True, text=True)
    Path(tmp).unlink(missing_ok=True)
    widths = {w["id"]: w["px"] for w in json.loads(r.stdout)}
    fails = 0
    tightest = []
    for it in items:
        need = widths[it["id"]] * MARGIN
        slack = it["room"] - need
        tightest.append((slack, it))
        if slack < 0:
            fails += 1
            print(f"  TOO WIDE  {it['page']:8s} {it['where']:42s} needs {need:6.0f}px, has {it['room']:6.0f}px: {it['text']}")
    for page, what, need, room in layout:
        if need > room:
            fails += 1
            print(f"  NO ROOM   {page:8s} {what:42s} needs {need:6.0f}px, has {room:6.0f}px")
    print(f"{len(layout) - sum(1 for *_, n, r in layout if n > r)} of {len(layout)} tables fit without scrollbars.")
    tightest.sort(key=lambda x: x[0])
    print(f"{len(items) - fails} of {len(items)} text lines fit (6% margin).")
    print("tightest fits:")
    for slack, it in tightest[:6]:
        if slack >= 0:
            print(f"  {slack:5.0f}px spare  {it['page']:8s} {it['where']:42s} {it['text'][:70]}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
