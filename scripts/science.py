#!/usr/bin/env python3
"""SCI entrypoint: workflow mechanics plus the mandatory scientific HTML contract."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
import report as reporting

w = reporting.w


def report_policy(ctx):
    ref = ctx.contract["source_catalog"]
    catalog = w.parse(w.stable_read(ctx.root / ref["path"], ref["sha256"]))
    policy = catalog.get("report")
    w.keys(policy, ("contract", "source_object", "html_object", "verification_object"))
    path = w.reference(ctx.root, policy["contract"], "report contract")
    ctx.forbidden(policy["contract"]["path"])
    reporting.load_contract(ctx.root, policy["contract"]["path"], policy["contract"]["sha256"])
    for key in ("source_object", "html_object", "verification_object"):
        reporting.identifier(policy[key], key)
    w.require(len({policy[key] for key in ("source_object", "html_object", "verification_object")}) == 3,
              "report objects must be distinct")
    matches = [obj for obj in ctx.objects.values() if obj["path"] == policy["contract"]["path"]]
    w.require(len(matches) == 1 and matches[0]["state"] == "present" and
              matches[0]["sha256"] == policy["contract"]["sha256"] and
              matches[0]["role"] in {"authority", "dependency"}, "report contract must be an authenticated dependency")
    if ctx.manifest["gate"] == "A":
        w.require({"A1", "A2", "A3", "A4", "A5", "A6"} <= set(ctx.requirements),
                  "Gate A requires A1-A6 coverage")
        return policy
    w.require(set(reporting.CHECK_IDS) <= set(ctx.requirements), "mandatory report checks missing from complete contract")
    required_objects = {policy["source_object"], policy["html_object"], policy["verification_object"]}
    for ident in reporting.CHECK_IDS:
        req = ctx.requirements[ident]
        w.require(w.RESULT <= set(req["stages"]), "report check missing final review stages")
        w.require(not w.RESULT.intersection(req["na_stages"] + req.get("deferred_stages", [])),
                  "final report checks cannot be NA or deferred")
        w.require(required_objects <= set(req["required_objects"]), "report artifacts missing from requirement")
        w.require({"product", "evidence"} <= set(req["required_roles"]), "report checks must require product and evidence")
    return policy


def verify_report(ctx, policy):
    final = ctx.manifest["gate"] == "B" and ctx.manifest["stage"] in w.RESULT
    supplied = any(policy[key] in ctx.objects and ctx.objects[policy[key]]["state"] == "present"
                   for key in ("source_object", "html_object", "verification_object"))
    if not final and not supplied:
        return None
    selected = {}
    for key in ("source_object", "html_object", "verification_object"):
        oid = policy[key]
        w.require(oid in ctx.objects and ctx.objects[oid]["state"] == "present", "complete report artifact set is missing")
        obj = ctx.objects[oid]
        w.require(":" not in obj["path"], "report controls must be local")
        w.require(obj["role"] == ("evidence" if key == "verification_object" else "product"), "report artifact has wrong role")
        selected[key] = obj
    source = selected["source_object"]
    model = w.parse(w.stable_read(ctx.root / source["path"], source["sha256"]))
    refs = [model.get("spec")] + model.get("sources", [])
    indexed = {obj["path"]: obj for obj in ctx.objects.values() if obj["state"] == "present"}
    indexed[ctx.contract["authority"]["path"]] = ctx.contract["authority"]
    # Check routing/authorization before the renderer opens any source table.
    for ref in refs:
        w.require(isinstance(ref, dict) and isinstance(ref.get("path"), str), "invalid report evidence reference")
        ctx.forbidden(ref["path"])
        w.require(ref["path"] in indexed and indexed[ref["path"]].get("sha256") == ref.get("sha256"),
                  "report source is outside authenticated manifest inputs", 1)
    report = reporting.Report(ctx.root, source["path"], source["sha256"],
                              policy["contract"]["path"], policy["contract"]["sha256"])
    result = report.verify_html(selected["html_object"]["path"])
    evidence = selected["verification_object"]
    saved = w.parse(w.stable_read(ctx.root / evidence["path"], evidence["sha256"]))
    for key in ("schema", "status", "scientific_pass", "source", "contract", "html", "template_sha256",
                "analyses", "required_independent_checks"):
        w.require(saved.get(key) == result[key], "report verification evidence mismatch: " + key, 1)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("render", "check", "run", "record"))
    p.add_argument("manifest")
    p.add_argument("--root", default=".")
    p.add_argument("--manifest-sha256", required=True)
    p.add_argument("--contract-sha256", required=True)
    for name in ("out", "commit", "step", "receipt-dir", "review", "review-sha256"):
        p.add_argument("--" + name)
    a = p.parse_args(argv)
    try:
        allowed = {"render": {"out"}, "check": {"commit"}, "run": {"step", "receipt_dir"},
                   "record": {"out", "review", "review_sha256"}}[a.command]
        supplied = {k for k in ("out", "commit", "step", "receipt_dir", "review", "review_sha256") if getattr(a, k) is not None}
        required = allowed if a.command != "check" else set()
        w.require(supplied <= allowed and required <= supplied, "invalid/missing command options", 2)
        ctx = w.Context(a.root, a.manifest, a.manifest_sha256, a.contract_sha256)
        policy = report_policy(ctx)
        if a.command in {"render", "record"}:
            ctx.output_path(a.out)
        if a.command == "render":
            w.write_new(ctx.root / a.out, w.render(ctx))
        elif a.command == "run":
            return w.run_step(ctx, a.step, a.receipt_dir)
        elif a.command == "check":
            result = verify_report(ctx, policy)
            print(json.dumps({"result": "MECHANICAL_OK", "scientific_pass": False,
                              "stage": ctx.manifest["stage"], "scope": ctx.contract["scope"],
                              "report": result, "stats": ctx.check(a.commit)}, ensure_ascii=False))
        else:
            verify_report(ctx, policy)
            w.write_new(ctx.root / a.out, w.record(ctx, a.review, a.review_sha256))
        return 0
    except w.WorkflowError as error:
        print("SCI_ERROR: " + str(error), file=sys.stderr)
        return error.code
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as error:
        print("SCI_ERROR: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
