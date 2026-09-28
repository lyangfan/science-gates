"""Small local fixtures; no real remote calls, science reruns or repository writes."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

try:
    from . import workflow as w
except ImportError:
    import workflow as w


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow-v3-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "out").mkdir()
        self.write("authority.md", b"Scoped addendum authorized; no scientific rerun.\n")
        self.save("source.json", {"checks": [{"id": "V72"}]})
        self.write("evidence.txt", b"raw evidence\n")
        self.contract = {
            "schema": w.CONTRACT, "chain": "fixture", "gate": "B", "authority": self.ref("authority.md"),
            "source_catalog": {**self.ref("source.json"), "collection": "checks", "id_key": "id"},
            "requirements": [{"id": "V72", "stages": ["addendum"], "na_stages": [],
                              "required_roles": ["evidence"], "required_objects": ["raw"]}],
            "allowed_hosts": ["local"], "write_roots": ["out"], "forbidden_paths": ["excluded"],
            "actors": {"coordinator": "coordinator", "implementer": "implementer", "reviewer": "independent-reviewer"},
            "scope": "scoped_addendum",
        }
        self.save("contract.json", self.contract)
        self.contract_sha = self.sha("contract.json")
        self.manifest = {
            "schema": w.MANIFEST, "chain": "fixture", "gate": "B", "stage": "addendum",
            "contract": self.ref("contract.json"),
            "objects": [{"id": "raw", "path": "evidence.txt", "role": "evidence", "state": "present",
                         "sha256": self.sha("evidence.txt"), "preservation": "external:fixture"}],
            "steps": [], "acceptance": [{"id": "V72", "status": "evidenced", "evidence": ["raw"]}],
        }

    def write(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def save(self, path, obj):
        self.write(path, (json.dumps(obj, indent=2) + "\n").encode())

    def sha(self, path):
        return hashlib.sha256((self.root / path).read_bytes()).hexdigest()

    def ref(self, path):
        return {"path": path, "sha256": self.sha(path)}

    def freeze_contract(self):
        self.save("contract.json", self.contract)
        self.contract_sha = self.sha("contract.json")
        self.manifest["contract"] = self.ref("contract.json")

    def context(self):
        self.save("manifest.json", self.manifest)
        return w.Context(self.root, "manifest.json", self.sha("manifest.json"), self.contract_sha)

    def report(self, ctx, **changes):
        self.write("out/dispatch.md", w.render(ctx))
        report = {"schema": w.REVIEW, "chain": "fixture", "gate": ctx.manifest["gate"], "stage": ctx.manifest["stage"],
                  "manifest_path": ctx.manifest_path,
                  "manifest_sha256": ctx.manifest_sha256, "contract_sha256": ctx.contract_sha256,
                  "reviewer_id": "independent-reviewer", "verdict": "PASS",
                  "checks": [{"id": "V72", "verdict": "PASS", "evidence": ["raw"]}],
                  "findings": [], "dispatch": self.ref("out/dispatch.md")}
        report.update(changes)
        self.save("review.json", report)
        return report

    def finding(self, **changes):
        finding = {"id": "F1", "check": "B1", "severity": "MAJOR", "state": "CLOSED", "owner": "implementer",
                   "location": "evidence.txt:1", "excerpt": "raw evidence", "authority": "SCI §3",
                   "counterexample": "Missing required evidence must fail.", "closure": "Original counterexample fails; affected invariant passes."}
        finding.update(changes)
        return finding

    def gate(self, gate, stage, stages=None):
        self.contract.update(gate=gate, scope="full_chain")
        self.contract["requirements"][0]["stages"] = stages or [stage]
        self.manifest.update(gate=gate, stage=stage)
        self.freeze_contract()

    def history(self, stage, *, gate=None, verdict="FAIL"):
        gate = gate or self.manifest["gate"]
        self.save("previous-" + stage + ".json", {"schema": w.REVIEW, "chain": "fixture", "gate": gate,
            "stage": stage, "reviewer_id": "independent-reviewer", "contract_sha256": self.contract_sha, "verdict": verdict})
        self.manifest.setdefault("history", []).append({"gate": gate, "stage": stage, "report": self.ref("previous-" + stage + ".json")})

    def snapshot(self, target, *, mode="content", exclude=None, action="verify", suffix=""):
        baseline = "baseline" + suffix + ".json"
        root_record = {"type": "directory"}
        if mode == "metadata":
            root_record.update(size=0, mode=493, mtime_ns=0, ctime_ns=0)
        self.save(baseline, {"schema": "agent-gates.snapshot.v2", "created_utc": "2026-09-28T00:00:00Z",
            "target": target, "mode": mode, "exclude": exclude or [], "records": {".": root_record}})
        self.manifest["objects"].append({"id": "baseline" + suffix, "path": baseline, "role": "evidence", "state": "present",
            "sha256": self.sha(baseline), "preservation": "external:fixture"})
        entries = json.loads((self.root / "plan.json").read_text())["entries"] if (self.root / "plan.json").exists() else []
        entries.append({"baseline": baseline, "action": action, **({"reason": "frozen history only"} if action == "historical" else {})})
        self.save("plan.json", {"schema": "agent-gates.snapshot-plan.v2", "entries": entries})
        self.manifest["objects"] = [obj for obj in self.manifest["objects"] if obj["id"] != "plan"]
        self.manifest["objects"].append({"id": "plan", "path": "plan.json", "role": "evidence", "state": "present",
            "sha256": self.sha("plan.json"), "preservation": "external:fixture"})
        self.manifest["snapshot_plan"] = "plan"

    def step(self, code="pass", outputs=False, kind="acceptance"):
        self.manifest["steps"] = [{"id": "validate", "kind": kind,
            "argv": [sys.executable, "-B", "-c", code], "cwd": ".", "host": "local",
            "inputs": ["raw"], "outputs": ["result"] if outputs else []}]
        if outputs:
            self.manifest["objects"].append({"id": "result", "path": "out/result.txt", "role": "product",
                "state": "planned", "preservation": "external:fixture", "producer": "validate"})

    def test_valid_manifest_reuses_v2_for_explicit_content(self):
        ctx = self.context()
        stats = ctx.check()
        self.assertEqual(stats["content_files"], 1)
        self.assertEqual(stats["content_bytes"], len(b"raw evidence\n"))

    def test_jsonl_scientific_evidence_is_not_a_snapshot(self):
        raw = b'{"id": "one", "value": 1}\n{"id": "two", "value": 2}\n'
        self.write("records.jsonl", raw)
        self.manifest["objects"][0].update(path="records.jsonl", sha256=self.sha("records.jsonl"))
        stats = self.context().check()
        self.assertEqual(stats["content_files"], 1)
        self.assertEqual(stats["content_bytes"], len(raw))

    def test_jsonl_cannot_hide_snapshot_in_any_row(self):
        backend = w.load_v2()
        for schema in (backend.snapshots.SCHEMA, backend.snapshots.LEGACY_SCHEMA):
            for rows in ([{"schema": schema}, {"id": "data"}], [{"id": "data"}, {"schema": schema}]):
                raw = b"\n".join(json.dumps(row).encode() for row in rows)
                with self.subTest(schema=schema, rows=rows), self.assertRaisesRegex(w.WorkflowError, "snapshot document"):
                    backend.identify_snapshot(raw, "records.jsonl")

    def test_jsonl_invalid_or_duplicate_fields_fail_closed(self):
        backend = w.load_v2()
        for raw in (b'', b'{"id":"a"}\nnot JSON\n', b'{"id":"a","id":"b"}\n', b'{"id":"a"}\n[]\n', b'# snapshot /tmp/\n'):
            with self.subTest(raw=raw), self.assertRaises(w.WorkflowError):
                backend.identify_snapshot(raw, "records.ndjson")

    def test_jsonl_over_budget_is_rejected(self):
        backend = w.load_v2()
        with mock.patch.object(backend, "CONTROL_LIMIT", 10), self.assertRaisesRegex(w.WorkflowError, "exceeds"):
            backend.identify_snapshot(b'{"long":"value"}\n', "records.jsonl")

    def test_manifest_cannot_remove_required_id(self):
        self.manifest["acceptance"] = []
        with self.assertRaisesRegex(w.WorkflowError, "coverage"):
            self.context()

    def test_catalog_cannot_remove_source_required_id(self):
        self.contract["requirements"] = []
        self.freeze_contract()
        with self.assertRaises(w.WorkflowError):
            self.context()

    def test_source_and_generic_catalog_ids_must_match(self):
        self.contract["requirements"][0]["id"] = "different"
        self.freeze_contract()
        with self.assertRaisesRegex(w.WorkflowError, "frozen source"):
            self.context()

    def test_empty_source_collection_fails(self):
        self.save("source.json", {"checks": []})
        self.contract["source_catalog"].update(self.ref("source.json"))
        self.freeze_contract()
        with self.assertRaisesRegex(w.WorkflowError, "empty"):
            self.context()

    def test_empty_object_table_fails(self):
        self.manifest["objects"] = []
        with self.assertRaisesRegex(w.WorkflowError, "empty object"):
            self.context()

    def test_empty_evidence_is_not_na_or_pass(self):
        self.manifest["acceptance"][0]["evidence"] = []
        with self.assertRaisesRegex(w.WorkflowError, "empty evidence"):
            self.context()

    def test_na_requires_contract_permission(self):
        self.manifest["acceptance"][0].update(status="na", evidence=[], reason="not applicable")
        with self.assertRaisesRegex(w.WorkflowError, "not authorized"):
            self.context()
        self.contract["requirements"][0]["na_stages"] = ["addendum"]
        self.freeze_contract()
        self.context()

    def test_missing_present_evidence_is_not_na(self):
        (self.root / "evidence.txt").unlink()
        self.manifest["acceptance"][0].update(status="na", evidence=[], reason="missing")
        with self.assertRaisesRegex(w.WorkflowError, "missing present"):
            self.context()

    def test_externally_pinned_contract_cannot_be_replaced(self):
        approved = self.contract_sha
        self.contract["requirements"][0]["na_stages"] = ["addendum"]
        self.freeze_contract()
        self.save("manifest.json", self.manifest)
        with self.assertRaisesRegex(w.WorkflowError, "another contract"):
            w.Context(self.root, "manifest.json", self.sha("manifest.json"), approved)

    def test_unauthenticated_source_reference_fails(self):
        self.write("source.json", b'{"checks":[{"id":"forged"}]}')
        with self.assertRaisesRegex(w.WorkflowError, "SHA256 mismatch"):
            self.context()

    def test_object_modified_after_manifest_is_rejected(self):
        ctx = self.context()
        self.write("evidence.txt", b"changed")
        with self.assertRaisesRegex(w.WorkflowError, "object SHA256"):
            ctx.check()

    def test_control_modified_after_context_is_rejected(self):
        ctx = self.context()
        self.write("manifest.json", b"{}")
        with self.assertRaisesRegex(w.WorkflowError, "SHA256 mismatch"):
            ctx.check()

    def test_path_escape_and_forbidden_symlink_are_rejected(self):
        for path in ("../escape", "/tmp/escape", "a/../evidence.txt"):
            with self.subTest(path=path):
                self.manifest["objects"][0]["path"] = path
                with self.assertRaises(w.WorkflowError):
                    self.context()
        self.write("excluded/private.dat", b"must not read")
        (self.root / "alias").symlink_to(self.root / "excluded/private.dat")
        self.manifest["objects"][0]["path"] = "alias"
        with self.assertRaisesRegex(w.WorkflowError, "forbidden"):
            self.context()

    def test_control_symlink_is_rejected(self):
        target = self.root / "control-link"
        target.symlink_to(self.root / "source.json")
        with self.assertRaisesRegex(w.WorkflowError, "regular file"):
            w.stable_read(target)

    def test_unknown_host_rejected_before_backend(self):
        self.manifest["objects"][0]["path"] = "unapproved:/file.txt"
        with mock.patch.object(w, "load_v2", side_effect=AssertionError("expensive backend")):
            with self.assertRaisesRegex(w.WorkflowError, "undeclared"):
                self.context()

    def test_unknown_keys_and_duplicate_json_keys_fail(self):
        self.manifest["unexpected"] = True
        with self.assertRaisesRegex(w.WorkflowError, "unknown keys"):
            self.context()
        with self.assertRaisesRegex(w.WorkflowError, "duplicate JSON"):
            w.parse(b'{"schema":1,"schema":2}')

    def test_declared_stage_budget_is_bounded(self):
        for stage in ("r3", "c4"):
            self.manifest["stage"] = stage
            with self.subTest(stage=stage), self.assertRaisesRegex(w.WorkflowError, "budget"):
                self.context()

    def test_planned_output_has_one_creator(self):
        self.step(outputs=True)
        self.manifest["objects"][-1]["producer"] = "other"
        with self.assertRaisesRegex(w.WorkflowError, "ownership"):
            self.context()

    def test_planned_output_without_step_fails(self):
        self.step(outputs=True)
        self.manifest["steps"] = []
        with self.assertRaisesRegex(w.WorkflowError, "no creator"):
            self.context()

    def test_existing_output_stops_before_child_or_hashes(self):
        self.step(outputs=True)
        self.write("out/result.txt", b"preserve")
        with mock.patch.object(w, "load_v2", side_effect=AssertionError("backend called")), \
                mock.patch.object(w.subprocess, "run", side_effect=AssertionError("child called")):
            with self.assertRaisesRegex(w.WorkflowError, "already exists"):
                self.context()
        self.assertEqual((self.root / "out/result.txt").read_bytes(), b"preserve")

    def test_failed_command_preserves_raw_logs_and_return_code(self):
        self.step("import sys; print('raw stdout'); print('raw stderr',file=sys.stderr); sys.exit(7)")
        ctx = self.context()
        self.assertEqual(w.run_step(ctx, "validate", "out/run1"), 7)
        receipt = json.loads((self.root / "out/run1/receipt.json").read_text())
        self.assertEqual(receipt["command_rc"], 7)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual((self.root / "out/run1/stdout.bin").read_bytes(), b"raw stdout\n")
        self.assertEqual((self.root / "out/run1/stderr.bin").read_bytes(), b"raw stderr\n")
        with self.assertRaisesRegex(w.WorkflowError, "already exists"):
            w.run_step(ctx, "validate", "out/run1")

    def test_successful_command_missing_output_is_not_complete(self):
        self.step(outputs=True)
        self.assertEqual(w.run_step(self.context(), "validate", "out/run1"), 3)
        receipt = json.loads((self.root / "out/run1/receipt.json").read_text())
        self.assertEqual(receipt["command_rc"], 0)
        self.assertEqual(receipt["status"], "failed")

    def test_run_creates_declared_output_and_receipt(self):
        self.step("from pathlib import Path; Path('out/result.txt').write_text('result')", outputs=True)
        self.assertEqual(w.run_step(self.context(), "validate", "out/run1"), 0)
        receipt = json.loads((self.root / "out/run1/receipt.json").read_text())
        self.assertEqual(receipt["outputs"], [{"id": "result", "path": "out/result.txt", "sha256": self.sha("out/result.txt")}])

    def test_addendum_forbids_scientific_rerun(self):
        self.step(kind="scientific")
        with self.assertRaisesRegex(w.WorkflowError, "forbidden"):
                w.run_step(self.context(), "validate", "out/run1")

    def test_run_cannot_succeed_after_changing_an_input(self):
        self.step("from pathlib import Path; Path('evidence.txt').write_text('changed')")
        self.assertEqual(w.run_step(self.context(), "validate", "out/run1"), 1)
        receipt = json.loads((self.root / "out/run1/receipt.json").read_text())
        self.assertEqual(receipt["command_rc"], 0)
        self.assertEqual(receipt["status"], "failed")

    def test_receipt_requires_raw_logs_and_origin_manifest(self):
        self.step()
        self.assertEqual(w.run_step(self.context(), "validate", "out/run1"), 0)
        for ident, path, role in (("origin", "manifest.json", "authority"), ("stdout", "out/run1/stdout.bin", "evidence"),
                                  ("stderr", "out/run1/stderr.bin", "evidence"), ("receipt", "out/run1/receipt.json", "evidence")):
            self.manifest["objects"].append({"id": ident, "path": path, "role": role, "state": "present",
                "sha256": self.sha(path), "preservation": "external:fixture"})
        self.manifest["acceptance"][0]["receipts"] = ["receipt"]
        self.save("followup.json", self.manifest)
        ctx = w.Context(self.root, "followup.json", self.sha("followup.json"), self.contract_sha)
        ctx.check()
        self.manifest["objects"] = [obj for obj in self.manifest["objects"] if obj["id"] != "stderr"]
        self.save("followup.json", self.manifest)
        ctx = w.Context(self.root, "followup.json", self.sha("followup.json"), self.contract_sha)
        with self.assertRaisesRegex(w.WorkflowError, "not an authenticated input"):
            ctx.check()

    def test_record_requires_distinct_independent_reviewer(self):
        ctx = self.context()
        self.report(ctx, reviewer_id="implementer")
        with self.assertRaisesRegex(w.WorkflowError, "independent Reviewer"):
            w.record(ctx, "review.json", self.sha("review.json"))

    def test_record_rejects_incomplete_review_and_open_blocker(self):
        ctx = self.context()
        self.report(ctx, checks=[])
        with self.assertRaisesRegex(w.WorkflowError, "empty review"):
            w.record(ctx, "review.json", self.sha("review.json"))
        self.report(ctx, findings=[self.finding(state="OPEN")])
        with self.assertRaisesRegex(w.WorkflowError, "blocking"):
            w.record(ctx, "review.json", self.sha("review.json"))

    def test_record_rejects_report_or_dispatch_byte_drift(self):
        ctx = self.context()
        self.report(ctx)
        approved = self.sha("review.json")
        self.write("review.json", b"{}")
        with self.assertRaisesRegex(w.WorkflowError, "SHA256 mismatch"):
            w.record(ctx, "review.json", approved)
        self.report(ctx)
        self.write("out/dispatch.md", b"readable rewrite")
        with self.assertRaisesRegex(w.WorkflowError, "SHA256 mismatch"):
            w.record(ctx, "review.json", self.sha("review.json"))

    def test_addendum_record_never_claims_whole_chain_pass(self):
        ctx = self.context()
        self.report(ctx)
        result = w.record(ctx, "review.json", self.sha("review.json")).decode()
        self.assertIn("SCOPED_ADDENDUM", result)
        self.assertNotIn("## PASS", result)
        self.assertNotIn("CANDIDATE_PASS", result)

    def test_pre_execution_record_is_only_execution_permission(self):
        self.contract["scope"] = "full_chain"
        self.contract["requirements"][0]["stages"] = ["b1"]
        self.manifest["stage"] = "b1"
        self.freeze_contract()
        ctx = self.context()
        self.report(ctx, stage="b1")
        result = w.record(ctx, "review.json", self.sha("review.json")).decode()
        self.assertIn("CANDIDATE_EXECUTION_PERMISSION", result)
        self.assertNotIn("CANDIDATE_PASS", result)

    def test_result_record_refuses_deferred_required_check(self):
        self.contract["scope"] = "full_chain"
        self.contract["requirements"][0].update(stages=["c1"], deferred_stages=["c1"])
        self.manifest["stage"] = "c1"
        self.manifest["acceptance"][0].update(status="deferred", evidence=[], reason="owner decision")
        self.freeze_contract()
        ctx = self.context()
        self.report(ctx, stage="c1", checks=[{"id": "V72", "verdict": "DEFERRED_BY_OWNER", "evidence": [], "reason": "owner decision"}])
        with self.assertRaisesRegex(w.WorkflowError, "whole-chain PASS"):
            w.record(ctx, "review.json", self.sha("review.json"))

    def test_snapshot_requires_explicit_complete_plan(self):
        self.write("protected/input.txt", b"tiny")
        self.write("baseline.tsv", (f"# snapshot {self.root}/protected/\n" + self.sha("protected/input.txt") + "\tinput.txt\n").encode())
        self.manifest["objects"].append({"id": "baseline", "path": "baseline.tsv", "role": "evidence", "state": "present",
            "sha256": self.sha("baseline.tsv"), "preservation": "external:fixture"})
        with self.assertRaisesRegex(w.WorkflowError, "explicit plan"):
            self.context().check()
        self.save("plan.json", {"schema": "agent-gates.snapshot-plan.v2", "entries": [{"baseline": "baseline.tsv", "action": "verify"}]})
        self.manifest["objects"].append({"id": "plan", "path": "plan.json", "role": "evidence", "state": "present",
            "sha256": self.sha("plan.json"), "preservation": "external:fixture"})
        self.manifest["snapshot_plan"] = "plan"
        self.context().check()

    def test_protection_anchor_cannot_be_refreshed(self):
        self.contract["protected"] = [{"id": "history", "path": "evidence.txt", "policy": "frozen", "evidence_refs": [self.ref("evidence.txt")]}]
        self.manifest["protection"] = [{"id": "history", "policy": "frozen", "evidence": ["raw"]}]
        self.freeze_contract()
        self.context()
        self.write("evidence.txt", b"new baseline")
        self.manifest["objects"][0]["sha256"] = self.sha("evidence.txt")
        with self.assertRaisesRegex(w.WorkflowError, "cannot be replaced"):
            self.context()

    def test_record_output_cannot_overwrite_existing_file(self):
        target = self.root / "out/record.md"
        w.write_new(target, b"first")
        with self.assertRaises(w.WorkflowError):
            w.write_new(target, b"second")
        self.assertEqual(target.read_bytes(), b"first")

    def test_control_limit_is_enforced(self):
        self.write("too-large.json", b" " * 65)
        with mock.patch.object(w, "CONTROL_LIMIT", 64):
            with self.assertRaisesRegex(w.WorkflowError, "8 MiB"):
                w.stable_read(self.root / "too-large.json")

    def test_closed_finding_requires_every_protocol_field(self):
        ctx = self.context()
        finding = self.finding()
        self.report(ctx, findings=[finding])
        self.assertIn(b"F1", w.record(ctx, "review.json", self.sha("review.json")))
        for field in finding:
            with self.subTest(field=field, case="missing"):
                incomplete = {key: value for key, value in finding.items() if key != field}
                self.report(ctx, findings=[incomplete])
                with self.assertRaisesRegex(w.WorkflowError, "missing keys"):
                    w.record(ctx, "review.json", self.sha("review.json"))
            with self.subTest(field=field, case="empty"):
                self.report(ctx, findings=[{**finding, field: ""}])
                with self.assertRaises(w.WorkflowError):
                    w.record(ctx, "review.json", self.sha("review.json"))

    def test_refuted_finding_needs_separate_authenticated_decision(self):
        ctx = self.context()
        self.report(ctx, findings=[self.finding(state="REFUTED")])
        with self.assertRaisesRegex(w.WorkflowError, "Decision Owner"):
            w.record(ctx, "review.json", self.sha("review.json"))
        self.report(ctx, findings=[self.finding(state="REFUTED", decision_ref=self.ref("authority.md"))])
        w.record(ctx, "review.json", self.sha("review.json"))

    def test_snapshot_forbidden_descendant_stops_before_hash_or_walk(self):
        self.contract["forbidden_paths"] = ["parent/excluded"]
        self.freeze_contract()
        self.write("parent/excluded/private.dat", b"must not read")
        for mode in ("content", "metadata"):
            for excludes in ([], ["excluded", "excluded/*"]):
                with self.subTest(mode=mode, excludes=excludes):
                    self.manifest["objects"] = [self.manifest["objects"][0]]
                    if (self.root / "plan.json").exists():
                        (self.root / "plan.json").unlink()
                    self.snapshot(str(self.root / "parent"), mode=mode, exclude=excludes)
                    ctx = self.context()
                    backend = w.load_v2()
                    with mock.patch.object(w, "load_v2", return_value=backend), \
                            mock.patch.object(backend.ContentCache, "compute", side_effect=AssertionError("content hashing started")), \
                            mock.patch.object(backend.snapshots, "compare_snapshot", side_effect=AssertionError("directory traversal started")):
                        with self.assertRaisesRegex(w.WorkflowError, "overlaps forbidden subtree"):
                            ctx.check()

    def test_all_snapshot_scopes_preflight_before_first_collection(self):
        (self.root / "safe").mkdir()
        self.snapshot(str(self.root / "safe"), suffix="safe")
        self.snapshot(str(self.root), suffix="root")
        backend = w.load_v2()
        with mock.patch.object(w, "load_v2", return_value=backend), \
                mock.patch.object(backend.snapshots, "compare_snapshot", side_effect=AssertionError("early scan")):
            with self.assertRaisesRegex(w.WorkflowError, "overlaps forbidden subtree"):
                self.context().check()

    def test_snapshot_parent_alias_cannot_bypass_forbidden_boundary(self):
        self.contract["forbidden_paths"] = ["parent/excluded"]
        self.freeze_contract()
        (self.root / "parent").mkdir()
        (self.root / "alias").symlink_to(self.root / "parent", target_is_directory=True)
        self.snapshot(str(self.root / "alias"))
        with self.assertRaisesRegex(w.WorkflowError, "overlaps forbidden subtree"):
            self.context().check()

    def test_historical_snapshot_parent_does_not_trigger_collection(self):
        self.snapshot(str(self.root), action="historical")
        backend = w.load_v2()
        with mock.patch.object(w, "load_v2", return_value=backend), \
                mock.patch.object(backend.snapshots, "compare_snapshot", side_effect=AssertionError("historical scan")):
            self.context().check()

    def test_relative_snapshot_target_cannot_use_another_process_cwd(self):
        self.snapshot("safe")
        with self.assertRaisesRegex(w.WorkflowError, "must be absolute"):
            self.context().check()

    def test_remote_forbidden_tree_stops_before_any_ssh_or_hash(self):
        self.contract["allowed_hosts"].append("fixture_host")
        self.contract["forbidden_paths"].append("fixture_host:/parent/excluded")
        self.freeze_contract()
        self.snapshot("fixture_host:/parent")
        backend = w.load_v2()
        with mock.patch.object(w, "load_v2", return_value=backend), \
                mock.patch.object(backend.ContentCache, "compute", side_effect=AssertionError("content hashing started")), \
                mock.patch.object(w.subprocess, "run", side_effect=AssertionError("remote call")):
            with self.assertRaisesRegex(w.WorkflowError, "overlaps forbidden subtree"):
                self.context().check()

    def test_remote_alias_uncertainty_is_not_silently_accepted(self):
        self.contract["allowed_hosts"].append("fixture_host")
        self.contract["forbidden_paths"].append("fixture_host:/parent/excluded")
        self.freeze_contract()
        self.snapshot("fixture_host:/alias/scan")
        with mock.patch.object(w.subprocess, "run", side_effect=AssertionError("remote call")):
            with self.assertRaisesRegex(w.WorkflowError, "ancestor aliases"):
                self.context().check()

    def test_gate_a_first_review_can_generate_candidate_without_permit(self):
        self.gate("A", "b1")
        ctx = self.context()
        self.report(ctx)
        result = w.record(ctx, "review.json", self.sha("review.json")).decode()
        self.assertIn("CANDIDATE_PASS", result)
        self.assertIn("门 A", result)
        self.assertNotIn("EXECUTION_PERMISSION", result)

    def test_gate_a_single_closure_requires_failed_first_review(self):
        self.gate("A", "c1", ["b1", "c1"])
        with self.assertRaisesRegex(w.WorkflowError, "history"):
            self.context()
        self.history("b1")
        ctx = self.context()
        self.report(ctx)
        self.assertIn(b"CANDIDATE_PASS", w.record(ctx, "review.json", self.sha("review.json")))
        self.manifest["history"] = []
        self.history("b1", verdict="PASS")
        with self.assertRaisesRegex(w.WorkflowError, "non-PASS"):
            self.context()

    def test_gate_a_rejects_b_stages_and_permit(self):
        for stage in ("prepare", "r1", "r2", "execute", "c2", "c3", "addendum"):
            with self.subTest(stage=stage):
                self.gate("A", stage)
                with self.assertRaisesRegex(w.WorkflowError, "gate stage"):
                    self.context()
        self.gate("A", "b1")
        self.manifest["execution_permit"] = self.ref("authority.md")
        with self.assertRaisesRegex(w.WorkflowError, "cannot consume"):
            self.context()

    def test_gate_is_required_and_mixed_contract_review_history_rejected(self):
        del self.manifest["gate"]
        with self.assertRaisesRegex(w.WorkflowError, "missing keys"):
            self.context()
        self.manifest["gate"] = "B"
        self.contract["gate"] = "A"
        self.freeze_contract()
        with self.assertRaisesRegex(w.WorkflowError, "gate mismatch"):
            self.context()
        self.gate("B", "b1")
        ctx = self.context()
        self.report(ctx, gate="A")
        with self.assertRaisesRegex(w.WorkflowError, "scope mismatch"):
            w.record(ctx, "review.json", self.sha("review.json"))
        self.gate("A", "c1", ["b1", "c1"])
        self.history("b1", gate="B")
        with self.assertRaisesRegex(w.WorkflowError, "historical gate"):
            self.context()

    def test_b1_cannot_reset_provided_failed_r2_history(self):
        self.gate("B", "b1", ["b1", "r1", "r2", "c1"])
        self.history("r2")
        with self.assertRaisesRegex(w.WorkflowError, "round budget"):
            self.context()

    def test_repair_requires_full_earlier_round_prefix(self):
        self.gate("B", "r2", ["b1", "r1", "r2"])
        self.history("r1")
        with self.assertRaisesRegex(w.WorkflowError, "history"):
            self.context()
        self.history("b1")
        self.context()

    def permit_fixture(self):
        self.gate("B", "b1", ["b1", "execute", "c1"])
        origin = self.context()
        self.report(origin, object_sha256={"raw": self.sha("evidence.txt")})
        self.save("origin.json", self.manifest)
        # Bind report to an immutable origin filename, then regenerate dispatch.
        origin = w.Context(self.root, "origin.json", self.sha("origin.json"), self.contract_sha)
        self.report(origin, object_sha256={"raw": self.sha("evidence.txt")})
        self.manifest["stage"] = "execute"
        self.manifest["execution_permit"] = self.ref("review.json")
        return self.context()

    def test_permit_requires_full_authenticated_origin_review(self):
        ctx = self.permit_fixture()
        ctx.permit(["raw"])
        original = json.loads((self.root / "review.json").read_text())
        for patch, expected in (({"checks": []}, "empty review"),
                ({"findings": [self.finding(state="OPEN", severity="BLOCKER")]}, "blocking"),
                ({"manifest_sha256": "0" * 64}, "SHA256 mismatch"),
                ({"gate": "A"}, "invalid pre-execution")):
            with self.subTest(patch=patch):
                self.save("review.json", {**original, **patch})
                self.manifest["execution_permit"] = self.ref("review.json")
                with self.assertRaisesRegex(w.WorkflowError, expected):
                    self.context().permit(["raw"])
        no_origin = {key: value for key, value in original.items() if key != "manifest_path"}
        self.save("review.json", no_origin)
        self.manifest["execution_permit"] = self.ref("review.json")
        with self.assertRaisesRegex(w.WorkflowError, "origin manifest"):
            self.context().permit(["raw"])

    def test_gate_b_result_can_use_complete_prior_permission(self):
        self.permit_fixture()
        self.manifest["stage"] = "c1"
        ctx = self.context()
        self.write("out/result-dispatch.md", w.render(ctx))
        report = json.loads((self.root / "review.json").read_text())
        report.update(stage="c1", manifest_path=ctx.manifest_path, manifest_sha256=ctx.manifest_sha256,
                      dispatch=self.ref("out/result-dispatch.md"))
        self.save("result-review.json", report)
        result = w.record(ctx, "result-review.json", self.sha("result-review.json")).decode()
        self.assertIn("CANDIDATE_PASS", result)
        self.assertIn("门 B", result)

    def test_permit_binds_scientific_argv_and_step_presence(self):
        self.step(code="print('approved')", kind="scientific")
        self.permit_fixture().permit(["raw"])
        approved = copy.deepcopy(self.manifest["steps"][0])
        for step in ({**approved, "argv": [sys.executable, "-c", "print('unreviewed')"]},
                     {**approved, "id": "different-step"}):
            self.manifest["steps"] = [step]
            with mock.patch.object(w.subprocess, "Popen", side_effect=AssertionError("must not execute")):
                with self.assertRaisesRegex(w.WorkflowError, "differs from reviewed step"):
                    self.context().permit(["raw"])

    def test_permit_binds_scientific_output_path(self):
        self.step(code="pass", kind="scientific", outputs=True)
        self.permit_fixture().permit(["raw"])
        output = self.manifest["steps"][0]["outputs"][0]
        next(obj for obj in self.manifest["objects"] if obj["id"] == output)["path"] = "out/unreviewed-result.txt"
        with self.assertRaisesRegex(w.WorkflowError, "output route differs"):
            self.context().permit(["raw"])

    def test_receipt_and_origin_must_belong_to_same_gate(self):
        self.step()
        w.run_step(self.context(), "validate", "out/run1")
        path = "out/run1/receipt.json"
        receipt = json.loads((self.root / path).read_text())
        self.assertEqual(receipt["gate"], "B")
        receipt["gate"] = "A"
        self.save(path, receipt)
        ctx = self.context()
        with self.assertRaisesRegex(w.WorkflowError, "unrelated execution receipt"):
            ctx.receipt({"path": path, "sha256": self.sha(path)})


if __name__ == "__main__":
    unittest.main()
