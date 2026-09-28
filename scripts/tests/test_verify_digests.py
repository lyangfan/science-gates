"""SCI 摘要核验的 CLI 合同；所有 fixture 和 Git 写操作均限 /tmp。

运行：python3 -B -m unittest discover -s scripts/tests \
    -p 'test_verify_digests.py' -v

假 ssh 只记录调用并失败，测试从不连接网络。该记录用于证明先决检查失败后
不会读取旧快照的 target，比仅断言最终退出码更严格。
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
CHECKER = Path(__file__).resolve().parents[1] / "verify_digests.py"
_CORE_SPEC = importlib.util.spec_from_file_location("digest_checker_under_test", CHECKER)
CORE = importlib.util.module_from_spec(_CORE_SPEC)
_CORE_SPEC.loader.exec_module(CORE)


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


class VerifyDigestsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="science-digests-check-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.dispatch = "reviews/dispatch-b1.md"
        self.plan_path = "reviews/snapshot-plan.json"
        self.snapshot_path = "reviews/snapshot.tsv"
        self.ssh_log = self.root / "ssh-invoked.log"
        self.env = dict(
            os.environ,
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
            GIT_AUTHOR_NAME="SCI Digest Test", GIT_AUTHOR_EMAIL="gate@example.invalid",
            GIT_COMMITTER_NAME="SCI Digest Test", GIT_COMMITTER_EMAIL="gate@example.invalid",
            PYTHONDONTWRITEBYTECODE="1", SCI_TEST_SSH_LOG=str(self.ssh_log),
        )
        self.write("bin/ssh", '#!/bin/sh\nprintf "%s\\n" "$*" >> "$SCI_TEST_SSH_LOG"\nexit 81\n')
        (self.root / "bin/ssh").chmod(0o755)
        self.env["PATH"] = str(self.root / "bin") + os.pathsep + self.env.get("PATH", "")
        self.git("init", "-q")
        self.write("code/model.py", "candidate v1\n")

    def write(self, path: str, value: str | bytes) -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value.encode("utf-8") if isinstance(value, str) else value)
        return target

    def git(self, *args) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *args], env=self.env,
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def commit(self) -> str:
        self.git("add", "--all")
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def row(self, path: str, value: str | None = None, preservation="git"):
        if value is None:
            value = digest((self.root / path).read_bytes())
        return path, value, preservation

    def table(self, rows=None, *, path=None, prefix="# 派发\n\n", suffix="") -> Path:
        if rows is None:
            rows = [self.row("code/model.py")]
        text = prefix + "| 文件 | sha256 | 保全 |\n|---|---|---|\n"
        text += "".join("| " + " | ".join(row) + " |\n" for row in rows)
        return self.write(path or self.dispatch, text + suffix)

    def cli(self, *args, cwd: Path | None = None):
        result = subprocess.run(
            [sys.executable, "-B", str(CHECKER), *map(str, args)],
            cwd=cwd or self.root, env=self.env, capture_output=True, text=True,
            timeout=20,
        )
        return result.returncode, result.stdout + result.stderr

    def check(self, *args):
        return self.cli("check", self.dispatch, *args)

    def inprocess_check(self, *args):
        """竞争时序测试使用；cwd/env 始终还原，文件仍仅在 /tmp。"""
        previous = Path.cwd()
        output = io.StringIO()
        try:
            os.chdir(self.root)
            with patch.dict(os.environ, self.env), contextlib.redirect_stdout(output):
                code = CORE.main(["check", self.dispatch, *args])
        finally:
            os.chdir(previous)
        return code, output.getvalue()

    def assert_pass(self, result):
        code, output = result
        self.assertEqual(0, code, output)
        return output

    def assert_rejected(self, result, fragment=None):
        code, output = result
        self.assertNotEqual(0, code, output)
        self.assertNotIn("Traceback (most recent call last)", output)
        if fragment:
            self.assertIn(fragment, output)
        return output

    def assert_mismatch(self, result):
        output = self.assert_rejected(result, "MISMATCH")
        self.assertEqual(1, result[0], "字节不符须为 FAIL，不能退化为 NOT_VERIFIABLE。\n" + output)
        return output

    def assert_no_target_read(self):
        self.assertFalse(self.ssh_log.exists(),
                         self.ssh_log.read_text() if self.ssh_log.exists() else "")

    def legacy_snapshot(self, *, path=None, target="probe.invalid:/forbidden/", content=None):
        if content is None:
            content = digest("frozen\n") + "\tinput.txt\n"
        path = path or self.snapshot_path
        self.write(path, f"# snapshot {target}\n# created fixture files=1\n# exclude \n{content}")
        return path

    def plan(self, entries=None):
        if entries is None:
            entries = [{"baseline": self.snapshot_path, "action": "verify"}]
        self.write(self.plan_path, json.dumps({
            "schema": "agent-gates.snapshot-plan.v2", "entries": entries,
        }, ensure_ascii=False, indent=2) + "\n")
        return self.plan_path

    def planned_table(self, *, entries=None, extra_rows=None):
        self.legacy_snapshot()
        self.plan(entries)
        self.table([self.row(self.snapshot_path), self.row(self.plan_path), *(extra_rows or [])])

    def test_write_without_placeholders_preserves_bytes_mtime_and_avoids_old_paths(self):
        target = self.table([
            self.row("probe.invalid:/unavailable/old.txt", "a" * 64, "external:历史证据"),
            self.row("missing/old.txt", "b" * 64),
        ])
        # 固定为已知旧时间，避免依赖文件系统 mtime 分辨率。
        os.utime(target, ns=(1_600_000_000_123_456_789, 1_600_000_000_123_456_789))
        before = target.read_bytes(), target.stat().st_mtime_ns
        self.assert_pass(self.cli("write", self.dispatch))
        self.assertEqual(before, (target.read_bytes(), target.stat().st_mtime_ns))
        self.assert_no_target_read()

    def test_write_computes_only_placeholder_rows_and_keeps_old_bytes(self):
        old_row = self.row("probe.invalid:/unavailable/old.txt", "a" * 64, "external:历史证据")
        target = self.table([old_row, self.row("code/model.py", "<脚本填>")])
        original = target.read_bytes()
        self.assert_pass(self.cli("write", self.dispatch))
        self.assertEqual(original.replace("<脚本填>".encode(), digest("candidate v1\n").encode()),
                         target.read_bytes())
        self.assert_no_target_read()

    def test_write_missing_placeholder_target_fails_without_partial_rewrite(self):
        target = self.table([self.row("code/model.py", "<脚本填>"),
                             self.row("missing/new.txt", "<脚本填>")])
        before = target.read_bytes(), target.stat().st_mtime_ns
        self.assert_rejected(self.cli("write", self.dispatch))
        self.assertEqual(before, (target.read_bytes(), target.stat().st_mtime_ns))

    def test_malformed_digest_stops_before_snapshot_target(self):
        self.planned_table(extra_rows=[self.row("code/model.py", "not-a-sha256")])
        self.assert_rejected(self.check("--precommit", "--snapshot-plan", self.plan_path))
        self.assert_no_target_read()

    def test_required_row_missing_stops_before_snapshot_target(self):
        self.planned_table()
        self.assert_rejected(self.check("--precommit", "--snapshot-plan", self.plan_path,
                                       "--require", "code/not-listed.py"), "MISSING_REQUIRED_ROW")
        self.assert_no_target_read()

    def test_claimed_change_absent_from_diff_stops_before_snapshot_target(self):
        self.write("reviews/diff.patch", "diff --git a/code/other.py b/code/other.py\n"
                   "--- a/code/other.py\n+++ b/code/other.py\n@@ -1 +1 @@\n-old\n+new\n")
        self.planned_table(extra_rows=[self.row("reviews/diff.patch")])
        self.assert_rejected(self.check("--precommit", "--snapshot-plan", self.plan_path,
                                       "--claimed-changed", "code/model.py",
                                       "--diff", "reviews/diff.patch"), "CLAIMED_BUT_NOT_IN_DIFF")
        self.assert_no_target_read()

    def test_unbound_diff_is_rejected_before_reading_snapshot_target(self):
        self.write("reviews/diff.patch", "diff --git a/code/model.py b/code/model.py\n")
        self.planned_table()
        self.assert_rejected(self.check("--precommit", "--snapshot-plan", self.plan_path,
                                       "--claimed-changed", "code/model.py",
                                       "--diff", "reviews/diff.patch"))
        self.assert_no_target_read()

    def test_wrong_last_heading_stops_before_snapshot_target(self):
        self.legacy_snapshot()
        self.plan()
        self.table([self.row(self.snapshot_path), self.row(self.plan_path)],
                   prefix="## NON_CONVERGENT · fixture\n\n")
        self.assert_rejected(self.check("--precommit", "--snapshot-plan", self.plan_path,
                                       "--last", "--last-heading", "^## PASS "),
                             "LAST_TABLE_NOT_EXPECTED_SECTION")
        self.assert_no_target_read()

    def test_fixed_commit_verifies_dispatch_and_candidate_blob(self):
        self.table()
        fixed = self.commit()
        output = self.assert_pass(self.check("--commit", fixed))
        self.assertIn("DISPATCH_COMMIT_OK", output)
        self.assertIn("COMMIT_BLOB_OK", output)

    def test_dispatch_tampering_fails_before_new_input_is_read(self):
        self.table()
        fixed = self.commit()
        self.table([self.row("code/model.py"),
                    self.row("probe.invalid:/unauthorized/input.txt", "b" * 64, "external:测试")])
        self.assert_rejected(self.check("--commit", fixed), "DISPATCH_COMMIT_MISMATCH")
        self.assert_no_target_read()

    def test_worktree_content_change_is_detected_despite_correct_fixed_blob(self):
        self.table()
        fixed = self.commit()
        self.write("code/model.py", "changed after freeze\n")
        self.assert_rejected(self.check("--commit", fixed), "MISMATCH")

    def test_current_head_cannot_replace_the_named_fixed_commit(self):
        self.table([self.row("code/model.py", digest("candidate v2\n"))])
        old = self.commit()
        self.write("code/model.py", "candidate v2\n")
        current = self.commit()
        self.assert_rejected(self.check("--commit", old), "MISMATCH")
        self.assert_pass(self.check("--commit", current))

    def test_unrelated_later_commit_and_dirty_file_do_not_invalidate_fixed_input(self):
        self.table()
        fixed = self.commit()
        self.write("unrelated.txt", "later commit\n")
        self.commit()
        self.write("unrelated.txt", "unrelated dirty bytes\n")
        self.assert_pass(self.check("--commit", fixed))

    def test_commit_rejects_symbolic_short_and_noncommit_objects(self):
        self.table()
        fixed = self.commit()
        for value in ("HEAD", fixed[:12], "a" * 40, self.git("rev-parse", "HEAD^{tree}")):
            with self.subTest(value=value):
                self.assert_rejected(self.check("--commit", value))

    def test_legacy_snapshot_requires_explicit_plan_and_never_implicitly_scans(self):
        self.legacy_snapshot()
        self.table([self.row(self.snapshot_path)])
        self.assert_rejected(self.check("--precommit"))
        self.assert_no_target_read()

    def test_historical_snapshot_verifies_its_bytes_without_reading_target(self):
        self.planned_table(entries=[{"baseline": self.snapshot_path, "action": "historical",
                                     "reason": "保留旧轮次证据，本轮不巡检该目录"}])
        output = self.assert_pass(self.check("--precommit", "--snapshot-plan", self.plan_path))
        self.assertIn("HISTORICAL", output)
        self.assert_no_target_read()

    def test_historical_snapshot_still_rejects_changed_baseline_bytes(self):
        self.planned_table(entries=[{"baseline": self.snapshot_path, "action": "historical",
                                     "reason": "保留旧证据"}])
        with (self.root / self.snapshot_path).open("a", encoding="utf-8") as target:
            target.write("# changed after digest freeze\n")
        self.assert_mismatch(self.check("--precommit", "--snapshot-plan", self.plan_path))
        self.assert_no_target_read()

    def test_historical_requires_nonempty_reason(self):
        for reason in (None, "", "   "):
            with self.subTest(reason=reason):
                entry = {"baseline": self.snapshot_path, "action": "historical"}
                if reason is not None:
                    entry["reason"] = reason
                self.planned_table(entries=[entry])
                self.assert_rejected(self.check("--precommit", "--snapshot-plan", self.plan_path))
                self.assert_no_target_read()

    def test_plan_must_itself_be_listed_in_digest_table(self):
        self.legacy_snapshot()
        self.plan()
        self.table([self.row(self.snapshot_path)])
        self.assert_rejected(self.check("--precommit", "--snapshot-plan", self.plan_path))
        self.assert_no_target_read()

    def test_plan_baseline_must_be_listed_in_digest_table(self):
        self.legacy_snapshot()
        self.plan()
        self.table([self.row(self.plan_path)])
        self.assert_rejected(self.check("--precommit", "--snapshot-plan", self.plan_path))
        self.assert_no_target_read()

    def test_plan_omission_does_not_hide_a_digest_listed_snapshot(self):
        self.planned_table(entries=[])
        self.assert_rejected(self.check("--precommit", "--snapshot-plan", self.plan_path))
        self.assert_no_target_read()

    def test_duplicate_plan_entry_is_rejected_before_scanning(self):
        entry = {"baseline": self.snapshot_path, "action": "verify"}
        self.planned_table(entries=[entry, dict(entry)])
        self.assert_rejected(self.check("--precommit", "--snapshot-plan", self.plan_path))
        self.assert_no_target_read()

    def test_changed_plan_bytes_are_rejected_before_interpreting_its_target(self):
        self.planned_table()
        self.plan([{"baseline": self.snapshot_path, "action": "historical", "reason": "tampered"}])
        self.assert_mismatch(self.check("--precommit", "--snapshot-plan", self.plan_path))
        self.assert_no_target_read()

    def test_snapshot_sha_mismatch_is_rejected_before_scanning_its_target(self):
        self.planned_table()
        self.legacy_snapshot(content=digest("different\n") + "\tinput.txt\n")
        self.assert_mismatch(self.check("--precommit", "--snapshot-plan", self.plan_path))
        self.assert_no_target_read()

    def test_content_snapshot_detects_changed_live_content(self):
        self.write("protected/input.txt", "frozen\n")
        self.legacy_snapshot(target=str(self.root / "protected") + "/")
        self.plan()
        self.table([self.row(self.snapshot_path), self.row(self.plan_path)])
        self.assert_pass(self.check("--precommit", "--snapshot-plan", self.plan_path))
        self.write("protected/input.txt", "modified\n")
        self.assert_rejected(self.check("--precommit", "--snapshot-plan", self.plan_path))

    def test_metadata_snapshot_sha_mismatch_stops_before_reading_target(self):
        baseline = "reviews/metadata-snapshot.json"
        document = {
            "schema": "agent-gates.snapshot.v2", "created_utc": "2026-01-01T00:00:00+00:00",
            "target": "probe.invalid:/forbidden/", "mode": "metadata", "exclude": [],
            "records": {".": {"type": "directory", "size": 4096, "mtime_ns": 10,
                               "ctime_ns": 10, "mode": 493}},
        }
        self.write(baseline, json.dumps(document) + "\n")
        self.plan([{"baseline": baseline, "action": "verify"}])
        self.table([self.row(baseline), self.row(self.plan_path)])
        document["records"]["."]["mtime_ns"] = 11
        self.write(baseline, json.dumps(document) + "\n")
        self.assert_mismatch(self.check("--precommit", "--snapshot-plan", self.plan_path))
        self.assert_no_target_read()

    def test_metadata_snapshot_created_by_cli_is_consumed_by_check(self):
        self.write("protected/input.txt", "frozen\n")
        baseline = "reviews/metadata-snapshot.json"
        (self.root / "reviews").mkdir(exist_ok=True)
        self.assert_pass(self.cli("snapshot", str(self.root / "protected"), "-o", baseline,
                                  "--mode", "metadata"))
        self.plan([{"baseline": baseline, "action": "verify"}])
        self.table([self.row(baseline), self.row(self.plan_path)])
        output = self.assert_pass(self.check("--precommit", "--snapshot-plan", self.plan_path))
        self.assertIn("METADATA", output.upper())
        # 明确改变大小，无需依赖时钟或文件系统时间戳精度。
        self.write("protected/input.txt", "longer live content\n")
        self.assert_rejected(self.check("--precommit", "--snapshot-plan", self.plan_path))

    def test_metadata_snapshot_with_many_records_still_requires_explicit_plan(self):
        for index in range(10):
            self.write(f"protected/input-{index}.txt", f"frozen {index}\n")
        baseline = "reviews/metadata-snapshot.json"
        (self.root / "reviews").mkdir(exist_ok=True)
        self.assert_pass(self.cli("snapshot", str(self.root / "protected"), "-o", baseline,
                                  "--mode", "metadata"))
        # JSON 键顺序不属于 schema，schema 标记可能位于较长 records 之后。
        document = json.loads((self.root / baseline).read_text(encoding="utf-8"))
        self.write(baseline, json.dumps(document, sort_keys=True, indent=2) + "\n")
        self.table([self.row(baseline)])
        self.assert_rejected(self.check("--precommit"))

    def test_remote_content_failure_is_tool_error_without_partial_write(self):
        target = self.table([self.row("probe.invalid:/new/input.txt", "<脚本填>", "external:测试")])
        before = target.read_bytes(), target.stat().st_mtime_ns
        code, output = self.cli("write", self.dispatch)
        self.assertEqual(2, code, output)
        self.assertNotIn("Traceback (most recent call last)", output)
        self.assertTrue(self.ssh_log.exists())
        self.assertEqual(before, (target.read_bytes(), target.stat().st_mtime_ns))

    def test_plan_parsed_before_digest_cannot_be_replaced_by_differently_authenticated_bytes(self):
        target = str(self.root / "protected") + "/"
        self.write("protected/input.txt", "frozen\n")
        self.legacy_snapshot(target=target)
        self.plan([{"baseline": self.snapshot_path, "action": "verify"}])
        authenticated_plan = (self.root / self.plan_path).read_bytes()
        self.table([self.row(self.snapshot_path), self.row(self.plan_path)])
        # 表列身份要求 verify；预先放入另一份 historical 内容，实算开始时再替换。
        self.plan([{"baseline": self.snapshot_path, "action": "historical", "reason": "stale plan"}])
        consumed_targets = []
        compute = CORE.ContentCache.compute
        collect = CORE.snapshots.collect

        def replace_before_compute(cache, paths):
            self.write(self.plan_path, authenticated_plan)
            return compute(cache, paths)

        def record_collect(live_target, *args, **kwargs):
            consumed_targets.append(live_target)
            return collect(live_target, *args, **kwargs)

        with patch.object(CORE.ContentCache, "compute", replace_before_compute), \
                patch.object(CORE.snapshots, "collect", record_collect):
            code, output = self.inprocess_check("--precommit", "--snapshot-plan", self.plan_path)
        self.assertNotIn("Traceback (most recent call last)", output)
        if code == 0:
            self.assertEqual([target], consumed_targets,
                             "认证的是 verify 计划，成功时必须实际消费该计划，而非旧 historical 计划。\n" + output)
            self.assertNotIn("HISTORICAL_SNAPSHOT", output)

    def test_snapshot_path_replacement_cannot_change_the_authenticated_target(self):
        authenticated_target = str(self.root / "protected") + "/"
        replacement_target = str(self.root / "replacement") + "/"
        self.write("protected/input.txt", "frozen\n")
        self.write("replacement/input.txt", "frozen\n")
        self.legacy_snapshot(target=authenticated_target)
        self.plan()
        self.table([self.row(self.snapshot_path), self.row(self.plan_path)])
        consumed_targets = []
        compare_entered = []
        compare = CORE.snapshots.compare_snapshot
        collect = CORE.snapshots.collect

        def replace_before_compare(path, *args, **kwargs):
            # 两个目录内容相同，错误实现仍会报 OK；必须额外观察访问了哪个 target。
            compare_entered.append(True)
            self.legacy_snapshot(target=replacement_target)
            return compare(path, *args, **kwargs)

        def record_collect(live_target, *args, **kwargs):
            consumed_targets.append(live_target)
            return collect(live_target, *args, **kwargs)

        with patch.object(CORE.snapshots, "compare_snapshot", replace_before_compare), \
                patch.object(CORE.snapshots, "collect", record_collect):
            code, output = self.inprocess_check("--precommit", "--snapshot-plan", self.plan_path)
        self.assertNotIn("Traceback (most recent call last)", output)
        self.assertTrue(compare_entered, "有效fixture必须走到比较入口，确保替换时序真正发生。\n" + output)
        self.assertNotIn(replacement_target, consumed_targets,
                         "不得访问替换后的、未经摘要认证的快照 target。\n" + output)
        if code == 0:
            self.assertEqual([authenticated_target], consumed_targets, output)

    def test_historical_reason_must_be_a_nonempty_string(self):
        for reason in (None, False, True, 0, 7, 2.5):
            with self.subTest(reason=reason):
                self.planned_table(entries=[{"baseline": self.snapshot_path,
                                             "action": "historical", "reason": reason}])
                self.assert_rejected(self.check("--precommit", "--snapshot-plan", self.plan_path))
                self.assert_no_target_read()

    def test_stale_non_snapshot_classification_cannot_bypass_plan_requirement(self):
        self.legacy_snapshot()
        authenticated_snapshot = (self.root / self.snapshot_path).read_bytes()
        self.table([self.row(self.snapshot_path)])
        early_views = []
        authenticated_views = []
        classify = CORE.snapshot_bytes
        compute = CORE.ContentCache.compute

        def disguise_during_early_classification(path):
            if os.path.abspath(path) == str(self.root / self.snapshot_path):
                self.write(self.snapshot_path, "temporarily ordinary text\n")
                result = classify(path)
                early_views.append(result)
                return result
            return classify(path)

        def restore_before_real_hash(cache, paths):
            self.write(self.snapshot_path, authenticated_snapshot)
            values = compute(cache, paths)
            # 使用真实 ContentCache，证明摘要期间已保存可用快照观察，而非属性缺失假拒绝。
            authenticated_views.append(cache.snapshot_view(self.snapshot_path))
            return values

        with patch.object(CORE, "snapshot_bytes", disguise_during_early_classification), \
                patch.object(CORE.ContentCache, "compute", restore_before_real_hash):
            code, output = self.inprocess_check("--precommit")
        self.assertEqual([None], early_views, output)
        self.assertEqual(1, len(authenticated_views), output)
        self.assertIsInstance(authenticated_views[0], dict, output)
        self.assertEqual("probe.invalid:/forbidden/", authenticated_views[0]["target"], output)
        self.assertEqual(3, code, output)
        self.assertIn("authenticated snapshot omitted from plan", output)
        self.assertNotIn("TOOL_ERROR", output)
        self.assertNotIn("AttributeError", output)
        self.assert_no_target_read()


    def test_missing_fixed_blob_cannot_pass_via_live_input(self):
        self.table([self.row("code/model.py", preservation="git:code/missing.py")])
        fixed = self.commit()
        self.assert_mismatch(self.check("--commit", fixed))

    def test_missing_or_empty_preservation_is_rejected(self):
        for declaration in ("", "external:", "unrecognized"):
            with self.subTest(declaration=declaration):
                self.table([self.row("code/model.py", preservation=declaration)])
                self.assert_rejected(self.check("--precommit"), "BAD_PRESERVATION")

    def test_ignored_file_requires_explicit_external_basis_or_fixed_blob(self):
        self.write(".gitignore", "external.txt\n")
        self.write("external.txt", "external input\n")
        self.table([self.row("external.txt")])
        fixed = self.commit()
        self.assert_mismatch(self.check("--commit", fixed))
        self.table([self.row("external.txt", preservation="external:authorized input")])
        fixed = self.commit()
        self.assert_pass(self.check("--commit", fixed))

    def test_remote_mapping_checks_live_bytes_and_local_canonical(self):
        remote = "mirror:/srv/model.py"
        value = digest("candidate v1\n")
        self.table([(remote, value, "git:code/model.py")])
        fixed = self.commit()
        values = [{"path": "/srv/model.py", "sha256": value, "size": len("candidate v1\n")}]
        with patch.object(CORE, "remote_content", return_value=values) as fetch:
            self.assert_pass(self.inprocess_check("--precommit"))
            self.assert_pass(self.inprocess_check("--commit", fixed))
            self.assertEqual(("mirror", ["/srv/model.py"]), fetch.call_args.args)
            self.write("code/model.py", "changed local canonical\n")
            self.assert_mismatch(self.inprocess_check("--precommit"))
        changed = [{"path": "/srv/model.py", "sha256": digest("remote changed"), "size": 14}]
        with patch.object(CORE, "remote_content", return_value=changed):
            self.assert_mismatch(self.inprocess_check("--commit", fixed))
        self.assert_no_target_read()

    def test_git_patch_tracks_add_delete_rename_binary_and_unicode(self):
        self.write("old.txt", "rename content\n")
        self.write("deleted.txt", "delete content\n")
        self.write("binary.dat", b"\x00before")
        first = self.commit()
        (self.root / "old.txt").rename(self.root / "renamed.txt")
        (self.root / "deleted.txt").unlink()
        self.write("新增.txt", "new content\n")
        self.write("binary.dat", b"\x00after")
        second = self.commit()
        expected = {"old.txt", "renamed.txt", "deleted.txt", "新增.txt", "binary.dat"}
        for mode in ("--no-renames", "--find-renames"):
            with self.subTest(mode=mode):
                raw = (self.git("diff", mode, "--binary", "--full-index", first, second) + "\n").encode()
                self.assertEqual(expected, CORE.patch_touched_bytes(raw))
        patch_path = "reviews/changes.patch"
        self.write(patch_path, raw)
        self.table([self.row("code/model.py"), self.row(patch_path)])
        self.assert_pass(self.check("--precommit", "--diff", patch_path, "--claimed-changed", ",".join(sorted(expected))))
        self.assert_rejected(self.check("--precommit", "--diff", patch_path, "--claimed-changed", "code/model.py"),
                             "CLAIMED_BUT_NOT_IN_DIFF")

    def test_patch_hunk_cannot_invent_a_claimed_path(self):
        raw = (b"diff --git a/real.txt b/real.txt\n--- a/real.txt\n+++ b/real.txt\n@@ -1 +1 @@\n"
               b"--- a/not-actually-changed.txt\n+++ b/not-actually-changed.txt\n")
        self.assertEqual({"real.txt"}, CORE.patch_touched_bytes(raw))

    def test_placeholder_write_preserves_binding_column(self):
        self.table([("code/model.py", "<脚本填>", "git")])
        self.assert_pass(self.cli("write", self.dispatch))
        self.assertIn("| git |", (self.root / self.dispatch).read_text())
        self.assert_pass(self.check("--precommit"))


class RemoteBatchTests(unittest.TestCase):
    def paths(self):
        return [f"/data/个体'quoted-{index}-" + "数" * 70 + ".csv" for index in range(220)]

    def test_long_unicode_paths_preserve_every_input_and_bound_requests(self):
        paths = self.paths()
        batches = list(CORE.remote_batches(paths))
        self.assertGreater(len(batches), 1)
        self.assertEqual(paths, [path for batch in batches for path in batch])
        self.assertTrue(all(len(json.dumps(batch).encode()) <= 65536 for batch in batches))
        with self.assertRaises(RuntimeError):
            list(CORE.remote_batches(["/" + "数" * 20000]))

    def test_missing_file_does_not_drop_later_batches(self):
        paths = self.paths()
        def fetch(host, batch):
            self.assertEqual("probe.invalid", host)
            return [{"path": path, "sha256": None} if path == paths[0]
                    else {"path": path, "sha256": digest(path), "size": 1} for path in batch]
        with patch.object(CORE, "remote_content", side_effect=fetch) as fetch_mock:
            result = CORE.ContentCache().compute(["probe.invalid:" + path for path in paths])
        self.assertGreater(fetch_mock.call_count, 1)
        self.assertEqual(len(paths), len(result))
        self.assertIsNone(result["probe.invalid:" + paths[0]])
        self.assertEqual(digest(paths[-1]), result["probe.invalid:" + paths[-1]])

    def test_later_batch_error_cannot_return_partial_success(self):
        paths = self.paths()
        first = list(CORE.remote_batches(paths))[0]
        values = [{"path": path, "sha256": digest(path), "size": 1} for path in first]
        with patch.object(CORE, "remote_content", side_effect=[values, RuntimeError("remote failed")]), \
                self.assertRaisesRegex(RuntimeError, "remote failed"):
            CORE.ContentCache().compute(["probe.invalid:" + path for path in paths])


class ReviewBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="science-review-budget-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_oversized_file_fails_before_open(self):
        target = self.root / "large.bin"
        target.write_bytes(b"123456789")
        with patch("builtins.open", side_effect=AssertionError("content opened")):
            with self.assertRaisesRegex(RuntimeError, "content budget"):
                CORE.ContentCache(max_file_bytes=8).local(str(target))

    def test_total_budget_counts_unique_reads(self):
        a, b = self.root / "a.txt", self.root / "b.txt"
        a.write_bytes(b"1234")
        b.write_bytes(b"56789")
        cache = CORE.ContentCache(max_file_bytes=8, max_total_bytes=8)
        self.assertEqual(cache.local(str(a)), cache.local(str(a)))
        self.assertEqual(cache.read_bytes, 4)
        with patch("builtins.open", side_effect=AssertionError("second file opened")):
            with self.assertRaisesRegex(RuntimeError, "total byte budget"):
                cache.local(str(b))

    def test_file_growth_stops_at_budget_plus_one(self):
        target = self.root / "growing.txt"
        target.write_bytes(b"1234")
        real_open = open
        class GrowingReader:
            def __enter__(self):
                self.handle = real_open(target, "rb")
                return self
            def __exit__(self, *args):
                self.handle.close()
            def fileno(self):
                return self.handle.fileno()
            def read(self, amount):
                with real_open(target, "ab") as writer:
                    writer.write(b"x" * 100)
                return self.handle.read(amount)
        cache = CORE.ContentCache(max_file_bytes=8, max_total_bytes=8)
        with patch("builtins.open", return_value=GrowingReader()):
            with self.assertRaisesRegex(RuntimeError, "grew beyond"):
                cache.local(str(target))
        self.assertEqual(cache.read_bytes, 9)

    def test_remote_ref_fails_before_local_content_or_ssh(self):
        cache = CORE.ContentCache(max_file_bytes=8)
        with patch.object(cache, "local", side_effect=AssertionError("local read")), \
                patch.object(CORE, "remote_content", side_effect=AssertionError("SSH")):
            with self.assertRaisesRegex(RuntimeError, "remote content"):
                cache.compute(["small.txt", "fixture_host:/huge.bin"])

    def test_git_blob_size_checked_before_content_with_either_budget(self):
        for options in ({"max_file_bytes": 8}, {"max_total_bytes": 8}):
            calls = []
            def git(args, **kwargs):
                calls.append(args)
                self.assertEqual(args[3:5], ["cat-file", "-s"])
                return subprocess.CompletedProcess(args, 0, "9\n", "")
            with self.subTest(options=options), patch.object(CORE.subprocess, "run", side_effect=git):
                with self.assertRaisesRegex(RuntimeError, "Git blob exceeds"):
                    CORE.ContentCache(**options).blob(str(self.root), "a" * 40, "old.bin")
                self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
