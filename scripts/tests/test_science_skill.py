"""Portable synthetic cases; no real data, remote hosts, or independent PASS."""
from __future__ import annotations

import contextlib
import base64
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import make_example
import prepare
import report
import science

w = report.w


class ReportFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="science-skill-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "project with spaces"
        make_example.create(self.root)
        self.model = self.read("report-source.json")
        self.rule = self.read("reviews/report-contract.json")

    def read(self, path):
        return json.loads((self.root / path).read_text())

    def save(self, path, obj):
        (self.root / path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")

    def sha(self, path):
        return w.digest((self.root / path).read_bytes())

    def instance(self):
        self.save("report-source.json", self.model)
        self.save("reviews/report-contract.json", self.rule)
        return report.Report(self.root, "report-source.json", self.sha("report-source.json"),
                             "reviews/report-contract.json", self.sha("reviews/report-contract.json"))

    def source_table(self, sid, content):
        source = next(s for s in self.model["sources"] if s["id"] == sid)
        (self.root / source["path"]).write_text(content)
        source["sha256"] = self.sha(source["path"])


class ReportTests(ReportFixture):
    def test_offline_html_contains_actual_numbers_and_all_data(self):
        rendered = self.instance().render().decode()
        self.assertIn("<strong>0.4 ", rendered)
        self.assertIn("<strong>0.2 ", rendered)
        self.assertIn("S03", rendered)
        self.assertIn('aria-labelledby="EXP1-means-title"', rendered)
        self.assertNotIn("<script", rendered)
        self.assertNotIn('src="http', rendered)
        self.assertNotIn("@@ANALYSES@@", rendered)

    def test_exact_html_check_detects_edit(self):
        instance = self.instance()
        (self.root / "candidate.html").write_bytes(instance.render().replace(b"0.4", b"0.9", 1))
        with self.assertRaisesRegex(w.WorkflowError, "HTML differs"):
            instance.verify_html("candidate.html")

    def test_source_digest_cannot_be_replaced_silently(self):
        (self.root / "results/summary.csv").write_text("method,n,mean_error\nA,3,99\nB,3,88\n")
        with self.assertRaisesRegex(w.WorkflowError, "SHA256 mismatch"):
            self.instance()

    def test_wrong_spec_identity_fails(self):
        self.model["spec"]["sha256"] = "a" * 64
        with self.assertRaisesRegex(w.WorkflowError, "spec differs"):
            self.instance()

    def test_missing_analysis_fails_before_table_reads(self):
        self.model["analyses"] = []
        original = w.stable_read
        def guarded(path, *args):
            if "/results/" in str(path):
                raise AssertionError("result content read before coverage preflight")
            return original(path, *args)
        with mock.patch.object(w, "stable_read", side_effect=guarded):
            with self.assertRaises(w.WorkflowError):
                self.instance()

    def test_duplicate_analysis_does_not_satisfy_count(self):
        self.model["analyses"] *= 2
        with self.assertRaisesRegex(w.WorkflowError, "coverage"):
            self.instance()

    def test_duplicate_rows_fail_even_when_count_matches(self):
        self.source_table("summary", "method,n,mean_error\nA,3,0.4\nA,3,0.2\n")
        with self.assertRaisesRegex(w.WorkflowError, "duplicate source row key"):
            self.instance()

    def test_empty_table_fails(self):
        self.source_table("summary", "method,n,mean_error\n")
        with self.assertRaisesRegex(w.WorkflowError, "empty result table"):
            self.instance()

    def test_duplicate_headers_fail(self):
        self.source_table("summary", "method,n,mean_error,mean_error\nA,3,0.4,0.9\n")
        with self.assertRaisesRegex(w.WorkflowError, "duplicate table headers"):
            self.instance()

    def test_ragged_rows_fail(self):
        self.source_table("summary", "method,n,mean_error\nA,3\n")
        with self.assertRaisesRegex(w.WorkflowError, "ragged"):
            self.instance()

    def test_missing_source_keys_fail(self):
        self.model["sources"][0]["keys"] = []
        with self.assertRaisesRegex(w.WorkflowError, "unique row keys"):
            self.instance()

    def test_metric_must_bind_all_unique_keys(self):
        self.model["analyses"][0]["results"][0]["where"] = {}
        with self.assertRaisesRegex(w.WorkflowError, "all unique row keys"):
            self.instance()

    def test_empty_selection_fails(self):
        self.model["analyses"][0]["results"][0]["where"] = {"method": "missing"}
        with self.assertRaisesRegex(w.WorkflowError, "selects no data"):
            self.instance()

    def test_unknown_metric_column_fails(self):
        self.model["analyses"][0]["results"][0]["column"] = "invented"
        with self.assertRaisesRegex(w.WorkflowError, "column missing"):
            self.instance()

    def test_handwritten_metric_value_rejected(self):
        self.model["analyses"][0]["results"][0]["value"] = 42
        with self.assertRaisesRegex(w.WorkflowError, "unknown keys"):
            self.instance()

    def test_unsupported_conclusion_reference_fails(self):
        self.model["analyses"][0]["conclusions"][0]["result_ids"] = ["invented"]
        with self.assertRaisesRegex(w.WorkflowError, "unknown evidence"):
            self.instance()

    def test_conclusion_without_evidence_fails(self):
        self.model["analyses"][0]["conclusions"][0].update(result_ids=[], source_ids=[])
        with self.assertRaisesRegex(w.WorkflowError, "link to evidence"):
            self.instance()

    def test_unexecuted_analysis_cannot_claim_observed_results(self):
        self.model["analyses"][0].update(status="not_executed", reason="尚未运行")
        with self.assertRaisesRegex(w.WorkflowError, "cannot claim"):
            self.instance()

    def test_unexecuted_analysis_is_visible_but_not_scientific_pass(self):
        self.model["analyses"][0].update(status="not_executed", reason="尚未运行", results=[], figures=[], conclusions=[])
        instance = self.instance()
        (self.root / "candidate.html").write_bytes(instance.render())
        result = instance.verify_html("candidate.html")
        self.assertFalse(result["scientific_pass"])
        self.assertEqual(result["analyses"][0]["status"], "not_executed")
        self.assertIn("尚不能", instance.render().decode())

    def test_missing_required_visualization_fails(self):
        self.model["analyses"][0]["figures"] = []
        with self.assertRaisesRegex(w.WorkflowError, "required figures"):
            self.instance()

    def test_nonfinite_plot_fails(self):
        self.source_table("summary", "method,n,mean_error\nA,3,NaN\nB,3,0.2\n")
        with self.assertRaisesRegex(w.WorkflowError, "not finite"):
            self.instance()

    def test_negative_values_are_drawn_around_zero(self):
        self.source_table("summary", "method,n,mean_error\nA,3,-0.4\nB,3,0.2\n")
        output = self.instance().render().decode()
        self.assertIn("-0.4", output)
        self.assertIn('class="axis"', output)

    def test_scatter_uses_numeric_axes(self):
        f = self.model["analyses"][0]["figures"][0]
        f.update(kind="scatter", x="n", x_label="样本数")
        self.assertIn('class="point"', self.instance().render().decode())

    def test_html_in_text_is_escaped(self):
        self.model["analyses"][0]["purpose"] = '<script>alert("bad")</script>'
        output = self.instance().render().decode()
        self.assertNotIn("<script", output)
        self.assertIn("&lt;script&gt;", output)

    def test_template_tokens_in_user_text_are_not_expanded(self):
        self.model["title"] = "Literal @@ANALYSES@@"
        output = self.instance().render().decode()
        self.assertIn("<title>Literal @@ANALYSES@@</title>", output)
        self.assertEqual(output.count('class="analysis"'), 1)

    def test_generated_anchor_collision_rejected(self):
        self.rule["analyses"][0]["id"] = "source-summary"
        self.model["analyses"][0]["id"] = "source-summary"
        with self.assertRaisesRegex(w.WorkflowError, "anchors collide"):
            self.instance()

    def add_image(self, raw):
        path = "results/picture.png"
        (self.root / path).write_bytes(raw)
        self.model["sources"].append({"id": "picture", "path": path, "sha256": self.sha(path),
                                     "format": "png", "label": "合成图片"})
        self.model["analyses"][0]["figures"].append({"id": "picture", "kind": "image", "source": "picture",
            "title": "合成示意", "caption": "仅验证离线嵌图接口。", "alt": "一个合成像素"})

    def test_approved_image_is_embedded_without_network(self):
        self.add_image(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aO1sAAAAASUVORK5CYII="))
        self.assertIn('src="data:image/png;base64,', self.instance().render().decode())

    def test_image_signature_cannot_be_html(self):
        self.add_image(b"<html><script>malicious()</script></html>")
        with self.assertRaisesRegex(w.WorkflowError, "signature mismatch"):
            self.instance()

    def test_reference_cannot_escape_project(self):
        self.model["sources"][0]["path"] = "../outside.csv"
        with self.assertRaises(w.WorkflowError):
            self.instance()

    def test_large_file_rejected_without_content_read(self):
        target = self.root / "results/summary.csv"
        with open(target, "wb") as handle:
            handle.truncate(w.CONTROL_LIMIT + 1)
        with mock.patch.object(w.os, "open", side_effect=AssertionError("opened oversized file")):
            with self.assertRaisesRegex(w.WorkflowError, "exceeds"):
                w.stable_read(target)

    def test_render_refuses_to_overwrite_html(self):
        before = (self.root / "demo.html").read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            rc = report.main(["render", "report-source.json", "--root", str(self.root),
                "--source-sha256", self.sha("report-source.json"), "--contract", "reviews/report-contract.json",
                "--contract-sha256", self.sha("reviews/report-contract.json"), "--html", "demo.html"])
        self.assertNotEqual(rc, 0)
        self.assertEqual((self.root / "demo.html").read_bytes(), before)

    def test_example_refuses_existing_directory(self):
        with self.assertRaises(FileExistsError):
            make_example.create(self.root)


class WorkflowIntegrationTests(ReportFixture):
    def context(self, mutate=None):
        contract = self.read("reviews/workflow-contract.json")
        manifest = self.read("reviews/stage.json")
        if mutate:
            mutate(contract, manifest)
        self.save("reviews/workflow-contract.json", contract)
        manifest["contract"]["sha256"] = self.sha("reviews/workflow-contract.json")
        self.save("reviews/stage.json", manifest)
        return w.Context(self.root, "reviews/stage.json", self.sha("reviews/stage.json"), self.sha("reviews/workflow-contract.json"))

    def test_valid_report_policy_and_content(self):
        ctx = self.context()
        result = science.verify_report(ctx, science.report_policy(ctx))
        self.assertEqual(result["status"], "MECHANICAL_OK")
        self.assertFalse(result["scientific_pass"])

    def test_gate_a_contract_has_report_policy_without_granting_science_pass(self):
        def mutate(c, m):
            catalog = self.read("reviews/source-catalog.json")
            ids = ["A1", "A2", "A3", "A4", "A5", "A6"]
            catalog["checks"] = [{"id": ident} for ident in ids]
            self.save("reviews/source-catalog.json", catalog)
            c["source_catalog"]["sha256"] = self.sha("reviews/source-catalog.json")
            c.update(gate="A", scope="full_chain", requirements=[
                {"id": ident, "stages": ["b1", "c1"], "na_stages": [], "required_roles": ["evidence"],
                 "required_objects": ["report-check"]} for ident in ids])
            m.update(gate="A", stage="b1", acceptance=[
                {"id": ident, "status": "evidenced", "evidence": ["report-check"]} for ident in ids])
        self.assertEqual(science.report_policy(self.context(mutate))["html_object"], "report-html")

    def test_missing_report_requirements_rejected(self):
        def mutate(c, m):
            c["requirements"][0]["required_objects"] = []
        with self.assertRaisesRegex(w.WorkflowError, "artifacts missing"):
            science.report_policy(self.context(mutate))

    def test_unauthorized_final_na_rejected(self):
        def mutate(c, m):
            c["requirements"][0]["na_stages"].append("c1")
        with self.assertRaisesRegex(w.WorkflowError, "cannot be NA"):
            science.report_policy(self.context(mutate))

    def test_final_report_checks_cannot_disappear_by_stage(self):
        def mutate(c, m):
            c["requirements"][0]["stages"].remove("c3")
        with self.assertRaisesRegex(w.WorkflowError, "final review stages"):
            science.report_policy(self.context(mutate))

    def test_report_source_must_be_in_manifest_before_read(self):
        def mutate(c, m):
            m["objects"] = [o for o in m["objects"] if o["id"] != "cases"]
        ctx = self.context(mutate)
        policy = science.report_policy(ctx)
        with mock.patch.object(report, "Report", side_effect=AssertionError("renderer read unauthorized source")):
            with self.assertRaisesRegex(w.WorkflowError, "outside authenticated"):
                science.verify_report(ctx, policy)

    def test_editing_page_and_updating_manifest_hash_does_not_pass(self):
        page = self.root / "demo.html"
        page.write_bytes(page.read_bytes().replace(b"0.4", b"0.9", 1))
        def mutate(c, m):
            next(o for o in m["objects"] if o["id"] == "report-html")["sha256"] = self.sha("demo.html")
        ctx = self.context(mutate)
        with self.assertRaisesRegex(w.WorkflowError, "HTML differs"):
            science.verify_report(ctx, science.report_policy(ctx))

    def test_fake_verification_record_fails(self):
        evidence = self.read("reviews/report-check.json")
        evidence["scientific_pass"] = True
        self.save("reviews/report-check.json", evidence)
        def mutate(c, m):
            next(o for o in m["objects"] if o["id"] == "report-check")["sha256"] = self.sha("reviews/report-check.json")
        ctx = self.context(mutate)
        with self.assertRaisesRegex(w.WorkflowError, "evidence mismatch"):
            science.verify_report(ctx, science.report_policy(ctx))

    def test_no_independent_report_cannot_record(self):
        ctx = self.context()
        with contextlib.redirect_stderr(io.StringIO()):
            rc = science.main(["record", "reviews/stage.json", "--root", str(self.root),
                "--manifest-sha256", ctx.manifest_sha256, "--contract-sha256", ctx.contract_sha256,
                "--review", "reviews/absent-review.json", "--review-sha256", "a"*64, "--out", "reviews/ledger-candidate.md"])
        self.assertNotEqual(rc, 0)
        self.assertFalse((self.root / "reviews/ledger-candidate.md").exists())

    def test_real_cli_from_unrelated_cwd_and_space_path(self):
        ctx = self.context()
        result = subprocess.run([sys.executable, "-B", str(Path(science.__file__).resolve()), "check",
            "reviews/stage.json", "--root", str(self.root), "--manifest-sha256", ctx.manifest_sha256,
            "--contract-sha256", ctx.contract_sha256], cwd="/tmp", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["scientific_pass"])


class PrepareTests(unittest.TestCase):
    def test_fills_only_null_and_deduplicates_actual_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x.txt").write_text("x")
            model = [{"path": "x.txt", "sha256": None}, {"path": "x.txt", "sha256": None},
                     {"path": "x.txt", "sha256": "a"*64}]
            updated, stats = prepare.fill(root, model)
            self.assertEqual(stats["hashed_files"], 1)
            self.assertEqual(stats["existing_values_unchanged"], 1)
            self.assertEqual(updated[0]["sha256"], w.digest(b"x"))
            self.assertEqual(updated[2]["sha256"], "a"*64)

    def test_existing_digests_do_not_read_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x.txt").write_text("x")
            with mock.patch.object(w, "stable_read", side_effect=AssertionError("existing digest re-read")):
                _, stats = prepare.fill(root, [{"path": "x.txt", "sha256": "a"*64}])
            self.assertEqual(stats["hashed_files"], 0)

    def test_placeholder_strings_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x.txt").write_text("x")
            with self.assertRaises(w.WorkflowError):
                prepare.fill(root, [{"path": "x.txt", "sha256": "<sha256>"}])


if __name__ == "__main__":
    unittest.main()
