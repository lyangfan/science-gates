#!/usr/bin/env python3
"""Create an exclusive, small synthetic example; never create a scientific PASS."""
from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True
import report

w = report.w


def create(root):
    root = Path(root).resolve()
    root.mkdir()  # Deliberately fail if the destination exists.
    (root / "reviews").mkdir()
    (root / "results").mkdir()
    def write(path, raw):
        if isinstance(raw, str):
            raw = raw.encode()
        w.write_new(root / path, raw)
    def save(path, obj):
        write(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    def ref(path):
        return {"path": path, "sha256": w.digest((root / path).read_bytes())}
    write("spec.md", "# 合成示例，非真实科学结果\n\n比较三个合成对象的方法 A/B；展示逐对象误差及组均值。\n"
          "所有数值仅演示接口，不用于方法优劣的科学判断。\n")
    save("input.json", {"synthetic": True, "A": ["0.3", "0.4", "0.5"], "B": ["0.1", "0.2", "0.3"]})
    write("generate.py", '''"""Generate this tiny synthetic fixture once, not a real experiment."""
import csv
from decimal import Decimal
import json
from pathlib import Path

data = json.loads(Path("input.json").read_text())
assert data["synthetic"] is True
with open("results/cases.csv", "x", newline="") as cases, open("results/summary.csv", "x", newline="") as summary:
    cw = csv.writer(cases, lineterminator="\\n")
    sw = csv.writer(summary, lineterminator="\\n")
    cw.writerow(["case", "method", "error"])
    sw.writerow(["method", "n", "mean_error"])
    for method in ("A", "B"):
        values = data[method]
        cw.writerows(("S%02d" % (i + 1), method, value) for i, value in enumerate(values))
        sw.writerow([method, len(values), sum(map(Decimal, values)) / len(values)])
print("Synthetic fixture: 6 case rows and 2 summary rows generated.")
''')
    argv = [sys.executable, "-B", "generate.py"]
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    generation = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=False)
    save("reviews/generation.json", {"synthetic": True, "argv": argv, "cwd": str(root),
        "started_utc": started, "finished_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "exit_code": generation.returncode, "stdout": generation.stdout, "stderr": generation.stderr,
        "code": ref("generate.py"), "input": ref("input.json"), "independent_reproduction": False})
    w.require(generation.returncode == 0, "synthetic fixture generation failed")
    report_contract = {"schema": report.CONTRACT, "spec": ref("spec.md"),
                       "analyses": [{"id": "EXP1", "title": "合成方法比较", "visualization": "required"}]}
    save("reviews/report-contract.json", report_contract)
    model = {
        "schema": report.REPORT, "title": "方法比较 · 合成数据报告示例",
        "overview": "展示一个科学 spec 如何连接实验目的、实际做法、结果图表与结论边界。全部数据均为人工构造。",
        "summary": "在三个人工构造对象中，方法 B 的平均误差为 0.2，方法 A 为 0.4。这仅演示展示流程，不能推出真实方法优劣。",
        "spec": ref("spec.md"),
        "sources": [
            {"id": "summary", **ref("results/summary.csv"), "format": "csv", "label": "合成汇总表", "keys": ["method"]},
            {"id": "cases", **ref("results/cases.csv"), "format": "csv", "label": "合成逐对象数据", "keys": ["case", "method"]},
            {"id": "generator", **ref("generate.py"), "format": "text", "label": "合成数据生成代码"},
            {"id": "fixture-input", **ref("input.json"), "format": "text", "label": "合成输入"},
            {"id": "generation-record", **ref("reviews/generation.json"), "format": "text", "label": "本次实际生成记录"}],
        "analyses": [{
            "id": "EXP1", "title": "合成方法比较", "status": "completed",
            "purpose": "说明同一批对象上两种方法的结果如何完整、可追溯地呈现。",
            "methods": ["为 S01–S03 各构造方法 A、B 的误差，共六条记录。", "按方法求三对象的算术均值；不做推断统计。"],
            "sample": "3 个合成对象，两种方法各 3 条记录；单位为示例误差单位。",
            "provenance": {"code_sources": ["generator"], "input_sources": ["fixture-input"],
                           "commands": [shlex.join(argv)], "record_sources": ["generation-record"]},
            "results": [
                {"id": "meanA", "label": "方法 A 平均误差", "unit": "示例单位", "source": "summary", "where": {"method": "A"}, "column": "mean_error"},
                {"id": "meanB", "label": "方法 B 平均误差", "unit": "示例单位", "source": "summary", "where": {"method": "B"}, "column": "mean_error"}],
            "figures": [
                {"id": "means", "kind": "bar", "source": "summary", "x": "method", "y": "mean_error",
                 "x_label": "方法", "y_label": "平均误差（示例单位）", "title": "两种方法的合成平均误差",
                 "caption": "纵轴从零开始。每组 n=3；这里的均值仅描述合成数据，不构成显著性或泛化结论。",
                 "alt": "方法 A 平均误差 0.4，方法 B 为 0.2。"},
                {"id": "allcases", "kind": "table", "source": "cases", "columns": ["case", "method", "error"],
                 "title": "全部逐对象结果", "caption": "完整列出六条合成记录，避免只报告均值而看不到对象组成。",
                 "alt": "三个对象和两种方法对应的六条误差记录。"}],
            "conclusions": [{"text": "这组构造数据中，方法 B 的平均误差低于方法 A。数据不足以支持真实方法性能结论。",
                             "result_ids": ["meanA", "meanB"], "source_ids": ["cases"]}],
            "limitations": ["人工构造的 3 个对象不代表任何真实研究总体。", "未进行统计推断，不能将描述性差异写成显著改进。"]
        }]
    }
    save("report-source.json", model)
    obj = report.Report(root, "report-source.json", ref("report-source.json")["sha256"],
                        "reviews/report-contract.json", ref("reviews/report-contract.json")["sha256"])
    write("demo.html", obj.render())
    save("reviews/report-check.json", obj.verify_html("demo.html"))
    policy = {"contract": ref("reviews/report-contract.json"), "source_object": "report-source",
              "html_object": "report-html", "verification_object": "report-check"}
    catalog = {"checks": [{"id": ident, "authority": "SCI report contract", "meaning": "须由独立 Reviewer 实质审查"}
                          for ident in report.CHECK_IDS], "report": policy}
    save("reviews/source-catalog.json", catalog)
    contract = {
        "schema": w.CONTRACT, "chain": "synthetic-example", "gate": "B", "authority": ref("spec.md"),
        "source_catalog": {**ref("reviews/source-catalog.json"), "collection": "checks", "id_key": "id"},
        "requirements": [{"id": ident, "stages": ["b1", "r1", "r2", "c1", "c2", "c3", "addendum"],
                          "na_stages": ["b1", "r1", "r2"], "deferred_stages": [],
                          "required_roles": ["product", "evidence"],
                          "required_objects": ["report-source", "report-html", "report-check"]}
                         for ident in report.CHECK_IDS],
        "allowed_hosts": ["local"], "write_roots": ["reviews"], "forbidden_paths": [],
        "actors": {"coordinator": "example-author", "implementer": "example-author", "reviewer": "unassigned-independent-reviewer"},
        "scope": "scoped_addendum"}
    save("reviews/workflow-contract.json", contract)
    mapping = [
        ("report-source", "report-source.json", "product"),
        ("report-html", "demo.html", "product"),
        ("report-check", "reviews/report-check.json", "evidence"),
        ("report-contract", "reviews/report-contract.json", "dependency"),
        ("summary", "results/summary.csv", "scientific_input"),
        ("cases", "results/cases.csv", "scientific_input"),
        ("generator", "generate.py", "dependency"),
        ("fixture-input", "input.json", "scientific_input"),
        ("generation-record", "reviews/generation.json", "evidence")]
    manifest = {
        "schema": w.MANIFEST, "chain": "synthetic-example", "gate": "B", "stage": "addendum",
        "contract": ref("reviews/workflow-contract.json"),
        "objects": [{"id": oid, **ref(path), "role": role, "state": "present", "preservation": "external:synthetic-fixture"}
                    for oid, path, role in mapping],
        "steps": [], "acceptance": [{"id": ident, "status": "evidenced", "evidence": ["report-source", "report-html", "report-check"]}
                                  for ident in report.CHECK_IDS]}
    save("reviews/stage.json", manifest)
    save("example-identities.json", {"synthetic": True, "scientific_pass": False,
                                    "manifest": ref("reviews/stage.json"), "contract": ref("reviews/workflow-contract.json"),
                                    "report_source": ref("report-source.json"), "report_contract": ref("reviews/report-contract.json")})
    return {"root": str(root), "html": str(root / "demo.html"), "identities": str(root / "example-identities.json"),
            "scientific_pass": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", required=True)
    a = p.parse_args()
    try:
        print(json.dumps(create(a.out), ensure_ascii=False))
        return 0
    except (OSError, w.WorkflowError) as error:
        print("EXAMPLE_ERROR: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
