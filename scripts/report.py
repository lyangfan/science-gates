#!/usr/bin/env python3
"""Render/check a bounded, offline scientific report; never decide scientific PASS."""
from __future__ import annotations

import argparse
import base64
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import html
import io
import json
from pathlib import Path
import re
import sys
import time

sys.dont_write_bytecode = True
import workflow as w

REPORT = "science-gates.report.v1"
CONTRACT = "science-gates.report-contract.v1"
CHECK_IDS = ("RPT-COVERAGE", "RPT-METHODS", "RPT-RESULTS", "RPT-VISUALS",
             "RPT-CONCLUSIONS", "RPT-PROVENANCE")
ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}\Z")
MAX_ROWS = 5000
MAX_POINTS = 200
STATES = {"completed": "已完成", "partial": "部分完成", "not_executed": "未执行"}
TEMPLATE = Path(__file__).resolve().parents[1] / "assets" / "report.html"


def identifier(value, label):
    w.require(isinstance(value, str) and bool(ID.fullmatch(value)), "invalid " + label)
    return value


def nonempty(value, label):
    w.require(isinstance(value, list) and bool(value), label + " must be a nonempty list")
    return value


def number(value):
    try:
        n = Decimal(str(value))
    except InvalidOperation as error:
        raise w.WorkflowError("plot value is not numeric: " + str(value)) from error
    w.require(n.is_finite() and abs(n) <= Decimal("1e100"), "plot value is not finite or is too large")
    return float(n)


def esc(value):
    return html.escape(str(value), quote=True)


def load_contract(root, path, sha):
    contract = w.parse(w.stable_read(w.safe_local(root, path), sha))
    w.keys(contract, ("schema", "spec", "analyses"))
    w.require(contract["schema"] == CONTRACT, "unsupported report contract")
    w.reference(root, contract["spec"], "report spec")
    analyses = nonempty(contract["analyses"], "contract analyses")
    ids = set()
    for analysis in analyses:
        w.keys(analysis, ("id", "title", "visualization"), ("reason",))
        aid = identifier(analysis["id"], "analysis ID")
        w.require(aid not in ids, "duplicate contract analysis")
        ids.add(aid)
        w.text(analysis["title"], "analysis title")
        w.require(analysis["visualization"] in {"required", "not_applicable"}, "invalid visualization applicability")
        if analysis["visualization"] == "not_applicable":
            w.text(analysis.get("reason"), "visualization exception reason")
    return contract


class Report:
    def __init__(self, root, source_path, source_sha, contract_path, contract_sha):
        started = time.monotonic()
        self.root = Path(root).resolve()
        self.source_path = source_path
        self.source_sha = source_sha
        self.contract_path = contract_path
        self.contract_sha = contract_sha
        self.contract = load_contract(self.root, contract_path, contract_sha)
        source_raw = w.stable_read(w.safe_local(self.root, source_path), source_sha)
        self.model = w.parse(source_raw)
        w.keys(self.model, ("schema", "title", "overview", "summary", "spec", "sources", "analyses"))
        m = self.model
        w.require(m["schema"] == REPORT, "unsupported report source schema")
        w.require(m["spec"] == self.contract["spec"], "report spec differs from frozen report contract", 1)
        for field in ("title", "overview", "summary"):
            w.text(m[field], "report " + field)
        self.sources = {}
        self.rows = {}
        self.images = {}
        self.headers = {}
        self.results = {}
        self.figure_rows = {}
        self.stats = {"source_files": 0, "source_bytes": 0, "rows": 0}
        source_paths = set()
        for source in nonempty(m["sources"], "report sources"):
            w.keys(source, ("id", "path", "sha256", "format", "label"), ("keys",))
            sid = identifier(source["id"], "source ID")
            w.require(sid not in self.sources, "duplicate source ID")
            path = w.safe_local(self.root, source["path"])
            w.require(path.resolve() not in source_paths, "duplicate source path or alias")
            source_paths.add(path.resolve())
            w.require(isinstance(source["sha256"], str) and w.HEX.fullmatch(source["sha256"]), "invalid source SHA256")
            w.text(source["label"], "source label")
            w.require(source["format"] in {"csv", "tsv", "text", "png", "jpeg"}, "unsupported source format")
            w.strings(source.get("keys", []), "source keys")
            if source["format"] in {"csv", "tsv"}:
                w.require(bool(source.get("keys")), "table source requires unique row keys")
            self.sources[sid] = source
        expected = [a["id"] for a in self.contract["analyses"]]
        actual = [a.get("id") for a in nonempty(m["analyses"], "report analyses") if isinstance(a, dict)]
        w.require(actual == expected, "report analysis coverage/order differs from frozen contract", 1)
        # Complete structural checks before opening any result-table content.
        for analysis, rule in zip(m["analyses"], self.contract["analyses"]):
            self.validate_analysis(analysis, rule)
        dom_ids = ["source-" + sid for sid in self.sources]
        for analysis in m["analyses"]:
            aid = analysis["id"]
            dom_ids.append(aid)
            dom_ids.extend(aid + "-" + r["id"] for r in analysis["results"])
            dom_ids.extend(aid + "-" + f["id"] + "-title" for f in analysis["figures"] if f["kind"] in {"bar", "scatter"})
        w.require(len(dom_ids) == len(set(dom_ids)), "generated HTML anchors collide")
        spec_raw = w.stable_read(w.reference(self.root, m["spec"], "spec"), m["spec"]["sha256"])
        self.stats["control_bytes"] = len(source_raw) + len(spec_raw)
        for sid, source in self.sources.items():
            raw = w.stable_read(w.safe_local(self.root, source["path"]), source["sha256"])
            self.stats["source_files"] += 1
            self.stats["source_bytes"] += len(raw)
            if source["format"] in {"png", "jpeg"}:
                is_png = raw.startswith(b"\x89PNG\r\n\x1a\n") and raw.endswith(b"IEND\xaeB\x60\x82")
                is_jpeg = raw.startswith(b"\xff\xd8") and raw.endswith(b"\xff\xd9")
                w.require(is_png if source["format"] == "png" else is_jpeg, "image format/signature mismatch")
                self.images[sid] = "data:image/" + source["format"] + ";base64," + base64.b64encode(raw).decode("ascii")
                continue
            if source["format"] == "text":
                w.require(bool(raw.strip()), "empty evidence source")
                raw.decode("utf-8")
                continue
            reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")),
                                    delimiter="\t" if source["format"] == "tsv" else ",", strict=True)
            headers = reader.fieldnames
            w.require(headers and all(headers) and len(headers) == len(set(headers)), "missing or duplicate table headers")
            w.require(set(source["keys"]) <= set(headers), "source keys not in table headers")
            rows, seen = [], set()
            for row in reader:
                w.require(None not in row and all(value is not None for value in row.values()), "ragged source table")
                key = tuple(row[k] for k in source["keys"])
                w.require(all(key) and key not in seen, "empty or duplicate source row key", 1)
                seen.add(key)
                rows.append(row)
                w.require(len(rows) <= MAX_ROWS, "report table exceeds 5000 rows; supply a reviewed summary table")
            w.require(rows, "empty result table is not report evidence")
            self.rows[sid], self.headers[sid] = rows, headers
            self.stats["rows"] += len(rows)
        for analysis in m["analyses"]:
            for result in analysis["results"]:
                sid = result["source"]
                rows = self.select(sid, result["where"])
                w.require(len(rows) == 1, "result locator must select exactly one row", 1)
                w.require(result["column"] in self.headers[sid], "result column missing")
                value = rows[0][result["column"]]
                w.require(bool(value.strip()), "result cell is empty")
                self.results[(analysis["id"], result["id"])] = value
            for figure in analysis["figures"]:
                sid = figure["source"]
                if figure["kind"] == "image":
                    continue
                rows = self.select(sid, figure.get("where", {}))
                fields = figure["columns"] if figure["kind"] == "table" else [figure["x"], figure["y"]]
                w.require(set(fields) <= set(self.headers[sid]), "figure columns missing")
                if figure["kind"] != "table":
                    w.require(len(rows) <= MAX_POINTS, "plot exceeds 200 points; supply a reviewed summary")
                    for row in rows:
                        number(row[figure["y"]])
                        if figure["kind"] == "scatter":
                            number(row[figure["x"]])
                    if figure["kind"] == "bar":
                        labels = [row[figure["x"]] for row in rows]
                        w.require(len(rows) <= 40 and len(labels) == len(set(labels)), "bar plot needs <=40 unique categories")
                self.figure_rows[(analysis["id"], figure["id"])] = rows
        self.stats["elapsed_seconds"] = round(time.monotonic() - started, 6)

    def validate_analysis(self, a, rule):
        w.keys(a, ("id", "title", "status", "purpose", "methods", "sample", "results", "figures", "conclusions", "limitations"),
               ("reason",))
        w.require(a["title"] == rule["title"], "analysis title differs from contract")
        w.require(a["status"] in STATES, "unknown analysis state")
        for field in ("purpose", "sample"):
            w.text(a[field], field)
        for field in ("methods", "limitations"):
            w.strings(a[field], field, nonempty=True)
        for field in ("results", "figures", "conclusions"):
            w.require(isinstance(a[field], list), field + " must be a list")
        if a["status"] != "completed":
            w.text(a.get("reason"), "incomplete analysis reason")
        if a["status"] == "completed":
            nonempty(a["results"], "completed results")
            nonempty(a["conclusions"], "completed conclusions")
        if a["status"] == "not_executed":
            w.require(not a["results"] and not a["figures"] and not a["conclusions"], "not-executed analysis cannot claim observed results")
        if a["status"] != "not_executed" and rule["visualization"] == "required":
            nonempty(a["figures"], "required figures")
        result_ids, figure_ids = set(), set()
        for r in a["results"]:
            w.keys(r, ("id", "label", "unit", "source", "where", "column"))
            rid = identifier(r["id"], "result ID")
            w.require(rid not in result_ids, "duplicate result ID")
            result_ids.add(rid)
            w.text(r["label"], "result label")
            w.require(isinstance(r["unit"], str), "result unit must be a string")
            self.table_source(r["source"])
            self.where(r["where"])
            w.require(set(self.sources[r["source"]]["keys"]) <= set(r["where"]), "result locator must include all unique row keys")
            w.text(r["column"], "result column")
        for f in a["figures"]:
            common = ("id", "kind", "source", "title", "caption", "alt")
            kind = f.get("kind") if isinstance(f, dict) else None
            w.require(kind in {"bar", "scatter", "table", "image"}, "unsupported figure type")
            extra = () if kind == "image" else ("columns",) if kind == "table" else ("x", "y", "x_label", "y_label")
            w.keys(f, common + extra, () if kind == "image" else ("where",))
            fid = identifier(f["id"], "figure ID")
            w.require(fid not in figure_ids, "duplicate figure ID")
            figure_ids.add(fid)
            if kind == "image":
                w.require(f["source"] in self.sources and self.sources[f["source"]]["format"] in {"png", "jpeg"},
                          "image figure needs a declared PNG/JPEG source")
            else:
                self.table_source(f["source"])
            self.where(f.get("where", {}))
            for field in ("title", "caption", "alt"):
                w.text(f[field], "figure " + field)
            if kind == "table":
                w.strings(f["columns"], "figure columns", nonempty=True)
            elif kind != "image":
                for field in ("x", "y", "x_label", "y_label"):
                    w.text(f[field], "figure " + field)
        for c in a["conclusions"]:
            w.keys(c, ("text", "result_ids", "source_ids"))
            w.text(c["text"], "conclusion")
            w.strings(c["result_ids"], "conclusion result IDs")
            w.strings(c["source_ids"], "conclusion source IDs")
            w.require(c["result_ids"] or c["source_ids"], "conclusion must link to evidence")
            w.require(set(c["result_ids"]) <= result_ids and set(c["source_ids"]) <= set(self.sources),
                      "conclusion has unknown evidence")

    def table_source(self, sid):
        w.require(sid in self.sources and self.sources[sid]["format"] in {"csv", "tsv"}, "unknown or non-table result source")

    @staticmethod
    def where(value):
        w.require(isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()),
                  "where must map column names to exact string values")

    def select(self, sid, where):
        w.require(set(where) <= set(self.headers[sid]), "filter column missing")
        rows = [r for r in self.rows[sid] if all(r[k] == v for k, v in where.items())]
        w.require(rows, "result/figure filter selects no data", 1)
        return rows

    def table(self, rows, fields):
        header = "".join("<th scope=\"col\">" + esc(f) + "</th>" for f in fields)
        body = "".join("<tr>" + "".join("<td>" + esc(row[f]) + "</td>" for f in fields) + "</tr>" for row in rows)
        return '<div class="table-scroll"><table><thead><tr>' + header + "</tr></thead><tbody>" + body + "</tbody></table></div>"

    def plot(self, aid, f, rows):
        width, height, left, top, plot_w, plot_h = 840, 410, 90, 26, 714, 280
        ys = [number(r[f["y"]]) for r in rows]
        ymin, ymax = min(0.0, min(ys)), max(0.0, max(ys))
        if ymin == ymax:
            ymax = ymin + 1
        span = ymax - ymin
        def py(v):
            return top + plot_h * (ymax - v) / span
        title_id = esc(aid + "-" + f["id"] + "-title")
        parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-labelledby="{title_id}">',
                 f'<title id="{title_id}">{esc(f["title"])}: {esc(f["alt"])}</title>']
        for i in range(5):
            v = ymin + span * i / 4
            y = py(v)
            parts += [f'<line class="grid" x1="{left}" x2="{left+plot_w}" y1="{y:.3f}" y2="{y:.3f}"/>',
                      f'<text class="tick" text-anchor="end" x="{left-10}" y="{y+5:.3f}">{v:.4g}</text>']
        parts.append(f'<line class="axis" x1="{left}" x2="{left+plot_w}" y1="{py(0):.3f}" y2="{py(0):.3f}"/>')
        if f["kind"] == "bar":
            slot = plot_w / len(rows)
            for i, (row, y) in enumerate(zip(rows, ys)):
                x = left + slot * i + slot * 0.2
                parts.append(f'<rect class="bar" x="{x:.3f}" y="{min(py(y),py(0)):.3f}" width="{slot*0.6:.3f}" height="{abs(py(y)-py(0)):.3f}"><title>{esc(row[f["x"]])}: {esc(row[f["y"]])}</title></rect>')
                parts.append(f'<text class="tick" x="{left+slot*(i+0.5):.3f}" y="{top+plot_h+22}" text-anchor="end" transform="rotate(-25 {left+slot*(i+0.5):.3f} {top+plot_h+22})">{esc(row[f["x"]])}</text>')
        else:
            xs = [number(row[f["x"]]) for row in rows]
            xmin, xmax = min(xs), max(xs)
            if xmin == xmax:
                xmin, xmax = xmin - 0.5, xmax + 0.5
            def px(v):
                return left + plot_w * (v - xmin) / (xmax - xmin)
            for i in range(5):
                v = xmin + (xmax-xmin) * i / 4
                parts.append(f'<text class="tick" x="{px(v):.3f}" y="{top+plot_h+25}" text-anchor="middle">{v:.4g}</text>')
            for row, x, y in zip(rows, xs, ys):
                parts.append(f'<circle class="point" cx="{px(x):.3f}" cy="{py(y):.3f}" r="5"><title>{esc(row[f["x"]])}, {esc(row[f["y"]])}</title></circle>')
        parts += [f'<text class="axis-label" x="{left+plot_w/2}" y="397" text-anchor="middle">{esc(f["x_label"])}</text>',
                  f'<text class="axis-label" transform="translate(22 {top+plot_h/2}) rotate(-90)" text-anchor="middle">{esc(f["y_label"])}</text>', "</svg>"]
        return "".join(parts)

    def render(self):
        m = self.model
        nav = "".join(f'<a href="#{esc(a["id"])}">{esc(a["id"])} · {esc(a["title"])}</a>' for a in m["analyses"])
        blocks = []
        for a in m["analyses"]:
            aid = a["id"]
            parts = [f'<section class="analysis" id="{esc(aid)}"><div class="section-head"><span class="eyebrow">{esc(aid)}</span><span class="state">{STATES[a["status"]]}</span></div>',
                     f'<h2>{esc(a["title"])}</h2><h3>实验目的</h3><p>{esc(a["purpose"])}</p>',
                     '<h3>实际方法与样本</h3><p>' + esc(a["sample"]) + '</p><ol>' +
                     "".join("<li>" + esc(v) + "</li>" for v in a["methods"]) + "</ol>"]
            if a.get("reason"):
                parts.append('<p class="notice">' + esc(a["reason"]) + "</p>")
            parts.append("<h3>实验结果</h3>")
            if a["results"]:
                parts.append('<div class="metrics">')
                for r in a["results"]:
                    value = self.results[(aid, r["id"])]
                    locator = ", ".join(k + "=" + v for k, v in r["where"].items()) + "; " + r["column"]
                    parts.append(f'<article class="metric" id="{esc(aid+"-"+r["id"])}"><span>{esc(r["label"])}</span><strong>{esc(value)} <small>{esc(r["unit"])}</small></strong><a href="#source-{esc(r["source"])}">来源：{esc(locator)}</a></article>')
                parts.append("</div>")
            else:
                parts.append("<p>本章节尚无可报告的实测结果；原因及证据边界见本节。</p>")
            for f in a["figures"]:
                rows = self.figure_rows.get((aid, f["id"]), [])
                parts.append(f'<figure tabindex="0" aria-label="{esc(f["title"])}"><h4>{esc(f["title"])}</h4>')
                if f["kind"] == "image":
                    parts.append(f'<img src="{self.images[f["source"]]}" alt="{esc(f["alt"])}" loading="lazy">')
                elif f["kind"] == "table":
                    parts.append(self.table(rows, f["columns"]))
                else:
                    parts.append('<p class="scroll-hint">图表可横向滚动；下方可展开完整数据。</p>')
                    parts.append(self.plot(aid, f, rows))
                    parts.append("<details><summary>查看图中完整数据</summary>" + self.table(rows, self.headers[f["source"]]) + "</details>")
                parts.append(f'<figcaption>{esc(f["caption"])} <a href="#source-{esc(f["source"])}">数据来源</a></figcaption></figure>')
            parts.append("<h3>科学结论</h3>")
            for c in a["conclusions"]:
                links = [f'<a href="#{esc(aid+"-"+rid)}">{esc(rid)}</a>' for rid in c["result_ids"]]
                links += [f'<a href="#source-{esc(sid)}">{esc(sid)}</a>' for sid in c["source_ids"]]
                parts.append("<p>" + esc(c["text"]) + ' <span class="evidence">依据：' + " · ".join(links) + "</span></p>")
            if not a["conclusions"]:
                parts.append("<p>尚不能从现有材料建立本分析的科学结论。</p>")
            parts.append('<h3>局限与适用边界</h3><ul>' + "".join("<li>" + esc(v) + "</li>" for v in a["limitations"]) + "</ul></section>")
            blocks.append("".join(parts))
        provenance = "<p>以下定位用于追溯，原始数据按项目权限在原位置保留。</p>"
        for sid, source in self.sources.items():
            provenance += f'<article id="source-{esc(sid)}"><h3>{esc(source["label"])}</h3><code>{esc(source["path"])}</code><p class="digest">SHA256 {esc(source["sha256"])}</p></article>'
        provenance += "<p>Spec: <code>" + esc(m["spec"]["path"]) + "</code></p><p class=\"digest\">" + esc(m["spec"]["sha256"]) + "</p>"
        provenance += "<p class=\"digest\">报告合同 " + esc(self.contract_sha) + "<br>报告源 " + esc(self.source_sha) + "</p>"
        template = w.stable_read(TEMPLATE).decode("utf-8")
        values = {"TITLE": esc(m["title"]), "OVERVIEW": esc(m["overview"]), "SUMMARY": esc(m["summary"]),
                  "NAV": nav, "ANALYSES": "".join(blocks), "PROVENANCE": provenance}
        for key in values:
            w.require(template.count("@@" + key + "@@") >= 1, "report template token missing")
        w.require(set(re.findall(r"@@([A-Z]+)@@", template)) == set(values), "unknown template token")
        template = re.sub(r"@@([A-Z]+)@@", lambda match: values[match.group(1)], template)
        return template.encode("utf-8")

    def verify_html(self, path):
        actual = w.stable_read(w.safe_local(self.root, path))
        expected = self.render()
        w.require(actual == expected, "HTML differs from authenticated report source/template", 1)
        return {"schema": "science-gates.report-check.v1", "status": "MECHANICAL_OK",
                "scientific_pass": False, "source": {"path": self.source_path, "sha256": self.source_sha},
                "contract": {"path": self.contract_path, "sha256": self.contract_sha},
                "html": {"path": path, "sha256": w.digest(actual)},
                "template_sha256": w.digest(w.stable_read(TEMPLATE)),
                "analyses": [{"id": a["id"], "status": a["status"]} for a in self.model["analyses"]],
                "required_independent_checks": list(CHECK_IDS), "stats": self.stats}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["render", "check"])
    p.add_argument("source")
    p.add_argument("--root", default=".")
    p.add_argument("--source-sha256", required=True)
    p.add_argument("--contract", required=True)
    p.add_argument("--contract-sha256", required=True)
    p.add_argument("--html", required=True)
    p.add_argument("--out")
    a = p.parse_args(argv)
    try:
        root = Path(a.root).resolve()
        target = w.safe_local(root, a.html)
        if a.command == "render":
            w.require(not target.exists() and not target.is_symlink(), "HTML output already exists", 1)
        if a.out:
            out = w.safe_local(root, a.out)
            w.require(not out.exists() and not out.is_symlink() and out != target, "verification output conflict", 1)
        report = Report(root, a.source, a.source_sha256, a.contract, a.contract_sha256)
        if a.command == "render":
            w.write_new(target, report.render())
        result = report.verify_html(a.html)
        if a.out:
            w.write_new(out, (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except w.WorkflowError as error:
        print("REPORT_ERROR: " + str(error), file=sys.stderr)
        return error.code
    except (OSError, ValueError, TypeError, KeyError, csv.Error) as error:
        print("REPORT_ERROR: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
