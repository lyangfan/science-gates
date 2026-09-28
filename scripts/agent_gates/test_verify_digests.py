"""摘要表解析、Git 保全与旧 ENG 流程的契约回归；全部写操作仅发生在 /tmp fixture。

运行：python3 -m unittest discover -s tools/agent_gates -p 'test_verify_digests.py' -v
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "verify_digests", Path(__file__).with_name("verify_digests.py"))
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


class DigestTableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="digest-tables-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "input.txt"
        self.source.write_text("frozen input", encoding="utf-8")
        self.good_row = f"| {self.source} | {digest('frozen input')} |"
        self.good_table = "| 路径 | sha256 |\n|---|---|\n" + self.good_row + "\n"

    def check_text(self, text, *, last_only=False, **kwargs):
        path = self.root / "inputs.md"
        path.write_text(text, encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = verify.cmd_check([str(path)], last_only, **kwargs)
        return code, out.getvalue()

    def test_bad_digest_table_cannot_hide_beside_valid_table(self):
        bad_row = "| missing.csv | — |"
        bad_table = "| 路径 | sha256 |\n|---|---|\n" + bad_row + "\n"
        for tables in ((bad_table, self.good_table), (self.good_table, bad_table)):
            with self.subTest(bad_table_first=tables[0] == bad_table):
                text = "\n".join(tables)
                code, out = self.check_text(text)
                self.assertEqual(verify.NOT_VERIFIABLE, code, out)
                self.assertIn("BAD_VALUE_CELL", out)
                self.assertIn(f"行 {text.splitlines().index(bad_row) + 1}", out)

    def test_explicit_digest_table_reports_all_malformed_input_rows(self):
        cases = (
            ("| missing.csv | |", "BAD_VALUE_CELL"),
            ("|missing.csv||", "BAD_VALUE_CELL"),
            ("| missing.csv |", "ROW_TOO_FEW_COLUMNS"),
            ("| path with spaces.csv | — |", "PATH_HAS_WHITESPACE"),
            ("| package | — |", "NON_PATH_FIRST_COLUMN"),
            ("| | " + digest("frozen input") + " |", "EMPTY_PATH"),
            ("||" + digest("frozen input") + "|", "EMPTY_PATH"),
            ("| <尚未展开的路径> | — |", "UNEXPANDED_TEMPLATE_ROW"),
        )
        for row, reason in cases:
            with self.subTest(row=row):
                text = "| 文件 | sha256 |\n|---|---|\n" + row + "\n\n" + self.good_table
                code, out = self.check_text(text)
                self.assertEqual(verify.NOT_VERIFIABLE, code, out)
                self.assertIn(reason, out)
                self.assertIn("行 3", out)

    def test_empty_digest_table_is_not_hidden_by_valid_table(self):
        code, out = self.check_text("| 路径 | sha256 |\n|---|---|\n\n" + self.good_table)
        self.assertEqual(verify.NOT_VERIFIABLE, code, out)
        self.assertIn("EMPTY_DIGEST_TABLE", out)
        self.assertIn("行 1", out)

    def test_last_does_not_fall_back_from_malformed_latest_table(self):
        text = ("## PASS old\n\n" + self.good_table + "\n## PASS latest\n\n"
                "| 对象 | sha256 |\n|---|---|\n| missing.csv | — |\n")
        code, out = self.check_text(text, last_only=True, last_heading=r"^## PASS ")
        self.assertEqual(verify.NOT_VERIFIABLE, code, out)
        self.assertIn("BAD_VALUE_CELL", out)
        self.assertNotIn(self.good_row, out)

    def test_last_ignores_malformed_historical_digest_table(self):
        text = ("## PASS old\n\n| 对象 | sha256 |\n|---|---|\n| missing.csv | — |\n"
                "\n## PASS latest\n\n" + self.good_table)
        code, out = self.check_text(text, last_only=True, last_heading=r"^## PASS ")
        self.assertEqual(verify.PASS, code, out)
        self.assertNotIn("missing.csv", out)

    def test_non_digest_parameter_and_status_tables_are_ignored(self):
        other_tables = ("| 参数 | 值 |\n|---|---|\n| cache/path | — |\n| retries | 3 |\n\n"
                "| 轮 | dispatch commit | 报告 sha256 | verdict |\n|---|---|---|---|\n"
                "| b1 | " + "a" * 40 + " | " + "b" * 64 + " | PASS |\n")
        for tables in ((other_tables, self.good_table), (self.good_table, other_tables)):
            for last_only in (False, True):
                with self.subTest(digest_table_last=tables[-1] == self.good_table, last_only=last_only):
                    code, out = self.check_text("\n".join(tables), last_only=last_only)
                    self.assertEqual(verify.PASS, code, out)
                    self.assertIn("表内候选行 1 / 实核 1", out)

    def test_explicit_third_digest_column_keeps_malformed_table(self):
        header = "| 文件 | 角色 | sha256 |\n|---|---|---|\n"
        text = (header + "| missing.csv | input | — |\n\n" + header
                + f"| {self.source} | input | {digest('frozen input')} |\n")
        code, out = self.check_text(text, digest_col=3)
        self.assertEqual(verify.NOT_VERIFIABLE, code, out)
        self.assertIn("BAD_VALUE_CELL (行 3)", out)

    def test_digest_column_must_match_explicit_header(self):
        text = ("| 路径 | sha256 | 说明 |\n|---|---|---|\n"
                + f"| {self.source} | — | {digest('frozen input')} |\n")
        code, out = self.check_text(text, digest_col=3)
        self.assertEqual(verify.NOT_VERIFIABLE, code, out)
        self.assertIn("DIGEST_COLUMN_MISMATCH", out)

    def test_headerless_legacy_rows_and_placeholder_write_still_work(self):
        code, out = self.check_text(self.good_row + "\n")
        self.assertEqual(verify.PASS, code, out)
        path = self.root / "write.md"
        path.write_text("| 路径 | sha256 |\n|---|---|\n"
                        + f"| {self.source} | <脚本填> |\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify.PASS, verify.cmd_write(str(path)))
        code, out = self.check_text(path.read_text(encoding="utf-8"))
        self.assertEqual(verify.PASS, code, out)


class GitPreservationTests(unittest.TestCase):
    def setUp(self):
        self.previous_cwd = Path.cwd()
        self.temp = tempfile.TemporaryDirectory(prefix="verify-digests-", dir="/tmp")
        self.root = Path(self.temp.name).resolve()
        os.chdir(self.root)
        self.addCleanup(self.cleanup)
        self.git("init", "-q")
        self.write("code/model.py", "candidate v1\n")
        self.dispatch = "reviews/dispatch-b1.md"

    def cleanup(self):
        os.chdir(self.previous_cwd)
        self.temp.cleanup()

    def git(self, *args) -> str:
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_AUTHOR_NAME="Gate Test", GIT_AUTHOR_EMAIL="gate@example.invalid",
                   GIT_COMMITTER_NAME="Gate Test", GIT_COMMITTER_EMAIL="gate@example.invalid")
        return subprocess.run(["git", "-C", str(self.root), *args], env=env,
                              check=True, capture_output=True, text=True).stdout.strip()

    def write(self, path: str, content: str | bytes):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")

    def table(self, rows=None, *, path=None, extra=""):
        rows = rows if rows is not None else [
            ("code/model.py", digest("candidate v1\n"), "git")]
        body = "# 派发\n\n| 文件 | sha256 | 保全 |\n|---|---|---|\n"
        body += "".join("| " + " | ".join(row) + " |\n" for row in rows)
        self.write(path or self.dispatch, body + extra)

    def commit(self) -> str:
        self.git("add", "--all")
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def check(self, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = verify.cmd_check([self.dispatch], False, **kwargs)
        return code, out.getvalue()

    def assert_check(self, expected, fragment=None, **kwargs):
        code, out = self.check(**kwargs)
        self.assertEqual(expected, code, out)
        if fragment:
            self.assertIn(fragment, out)
        return out

    def test_precommit_accepts_uncommitted_candidates_without_subject_copy(self):
        self.table(extra="\n旧文档曾使用 /subject-b1/，新模式不因此触发副本检查。\n")
        out = self.assert_check(verify.PASS, "PRECOMMIT_TARGET_OK", precommit=True)
        self.assertNotIn("MISSING_SUBJECT_COPY", out)
        self.assertNotIn("ROUND_UNRECOGNIZED", out)

    def test_atomic_candidate_and_dispatch_commit(self):
        self.table()
        fixed = self.commit()
        out = self.assert_check(verify.PASS, "DISPATCH_COMMIT_OK", commit=fixed)
        self.assertIn("COMMIT_BLOB_OK", out)

    def test_unrelated_head_and_dirty_files_do_not_change_fixed_commit(self):
        self.table()
        fixed = self.commit()
        self.write("unrelated.txt", "later commit")
        self.commit()
        self.write("unrelated.txt", "unrelated dirty work")
        self.assert_check(verify.PASS, commit=fixed)

    def test_clean_current_head_cannot_substitute_for_fixed_commit(self):
        # 派发在两个提交中相同，但第一个提交尚未保存其声称的 v2 候选字节。
        self.table([("code/model.py", digest("candidate v2\n"), "git")])
        first = self.commit()
        self.write("code/model.py", "candidate v2\n")
        second = self.commit()
        self.assertEqual("clean", verify.git_state("code/model.py"))
        self.assert_check(verify.FAIL, "COMMIT_BLOB_MISMATCH", commit=first)
        self.assert_check(verify.PASS, commit=second)

    def test_rewritten_disk_fails_even_with_correct_fixed_blob(self):
        self.table()
        fixed = self.commit()
        self.write("code/model.py", "rewritten after freeze")
        self.assert_check(verify.FAIL, "MISMATCH", commit=fixed)

    def test_dispatch_must_itself_be_saved_in_the_fixed_commit(self):
        self.table()
        fixed = self.commit()
        self.table(extra="\n后加一句没有改动任何 digest。\n")
        with patch.object(verify, "compute") as compute_mock, patch.object(verify, "ssh") as ssh_mock:
            self.assert_check(verify.FAIL, "DISPATCH_COMMIT_MISMATCH", commit=fixed)
            compute_mock.assert_not_called()
            ssh_mock.assert_not_called()

    def test_missing_blob_in_fixed_commit_fails(self):
        self.write(".gitignore", "code/model.py\n")
        self.table()
        fixed = self.commit()
        self.assert_check(verify.FAIL, "MISSING_COMMIT_BLOB", commit=fixed)

    def test_commit_requires_full_commit_object_sha(self):
        self.table()
        fixed = self.commit()
        for value in ("HEAD", fixed[:12], "main", "a" * 40, self.git("rev-parse", "HEAD^{tree}")):
            with self.subTest(value=value):
                self.assert_check(verify.TOOL_ERROR, commit=value)

    def test_explicit_external_does_not_need_a_git_blob(self):
        self.write(".gitignore", "data/\n")
        self.write("data/raw.tsv", "individual-level data")
        self.table([("data/raw.tsv", digest("individual-level data"), "external:敏感个体数据政策")])
        fixed = self.commit()
        self.assert_check(verify.PASS, "EXTERNAL", commit=fixed)

    def test_missing_and_empty_declarations_never_pass(self):
        for tail in ((), ("",), ("external:",), ("external:   ",), ("ignored",), ("git:",)):
            with self.subTest(tail=tail):
                self.table([("code/model.py", digest("candidate v1\n"), *tail)])
                self.assert_check(verify.NOT_VERIFIABLE, "BAD_PRESERVATION", precommit=True)

    def test_ignore_status_never_implies_external_authorization(self):
        self.write(".gitignore", "code/model.py\n")
        self.table([("code/model.py", digest("candidate v1\n"))])
        self.assert_check(verify.NOT_VERIFIABLE, "BAD_PRESERVATION", precommit=True)

    def test_mapping_rejects_absolute_escape_and_invalid_targets(self):
        for mapping in ("../outside.py", "/tmp/outside.py", "code/../code/model.py",
                        "code/", ".", ".git/config", "code//model.py"):
            with self.subTest(mapping=mapping):
                self.table([("code/model.py", digest("candidate v1\n"), f"git:{mapping}")])
                self.assert_check(verify.NOT_VERIFIABLE, "BAD_PRESERVATION", precommit=True)

    def test_plain_git_rejects_source_outside_dispatch_repository(self):
        with tempfile.TemporaryDirectory(prefix="gate-outside-", dir="/tmp") as folder:
            outside = Path(folder) / "outside.py"
            outside.write_text("outside")
            self.table([(str(outside), digest("outside"), "git")])
            self.assert_check(verify.NOT_VERIFIABLE, "BAD_PRESERVATION", precommit=True)

    def test_mapping_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory(prefix="gate-outside-", dir="/tmp") as folder:
            outside = Path(folder) / "outside.py"
            outside.write_text("candidate v1\n")
            (self.root / "escaped.py").symlink_to(outside)
            self.table([("code/model.py", digest("candidate v1\n"), "git:escaped.py")])
            self.assert_check(verify.NOT_VERIFIABLE, "BAD_PRESERVATION", precommit=True)

    def test_remote_mapping_checks_live_remote_and_local_canonical(self):
        remote_path = "mirror:/srv/model.py"
        self.table([(remote_path, digest("candidate v1\n"), "git:code/model.py")])
        fixed = self.commit()
        remote = subprocess.CompletedProcess([], 0, f"{digest('candidate v1' + chr(10))}  /srv/model.py\n", "")
        with patch.object(verify, "ssh", return_value=remote) as ssh_mock:
            self.assert_check(verify.PASS, precommit=True)
            self.assert_check(verify.PASS, commit=fixed)
            self.assertEqual("mirror", ssh_mock.call_args.args[0])
            self.write("code/model.py", "different local canonical")
            self.assert_check(verify.FAIL, "PRECOMMIT_TARGET_MISMATCH", precommit=True)
        changed = subprocess.CompletedProcess([], 0, f"{digest('remote changed')}  /srv/model.py\n", "")
        with patch.object(verify, "ssh", return_value=changed):
            self.assert_check(verify.FAIL, "MISMATCH", commit=fixed)

    def test_remote_plain_git_is_rejected(self):
        self.table([("mirror:/srv/model.py", digest("candidate v1\n"), "git")])
        with patch.object(verify, "remote_sha256", return_value={"/srv/model.py": digest("candidate v1\n")}):
            self.assert_check(verify.NOT_VERIFIABLE, "BAD_PRESERVATION", precommit=True)

    def test_new_modes_reject_legacy_option_mixing(self):
        self.table()
        fixed = self.commit()
        for mode in ({"precommit": True}, {"commit": fixed}):
            for legacy in ({"review_root": "docs/reviews/"}, {"large_suffix": []}, {"large_suffix": [".pt"]}):
                with self.subTest(mode=mode, legacy=legacy):
                    self.assert_check(verify.TOOL_ERROR, **mode, **legacy)
        self.assert_check(verify.TOOL_ERROR, precommit=True, commit=fixed)

    def test_cli_distinguishes_omitted_and_explicit_legacy_defaults(self):
        self.table()
        for tail, expected in ((["--precommit"], verify.PASS),
                               (["--precommit", "--review-root", "docs/reviews/"], verify.TOOL_ERROR),
                               (["--precommit", "--large-suffix", ""], verify.TOOL_ERROR)):
            with self.subTest(tail=tail), patch("sys.argv", ["verify_digests.py", "check", self.dispatch, *tail]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(expected, verify.main())

    def test_legacy_eng_still_requires_and_accepts_subject_copy(self):
        self.table([("code/model.py", digest("candidate v1\n"))])
        self.assert_check(verify.NOT_VERIFIABLE, "MISSING_SUBJECT_COPY")
        subject = "reviews/subject-b1/code/model.py"
        self.write(subject, "candidate v1\n")
        self.table([("code/model.py", digest("candidate v1\n")),
                    (subject, digest("candidate v1\n"))])
        self.assert_check(verify.PASS)
        self.write(subject, "different copy")
        self.table([("code/model.py", digest("candidate v1\n")),
                    (subject, digest("different copy"))])
        self.assert_check(verify.FAIL, "SUBJECT_LIVE_MISMATCH")

    def test_legacy_clean_file_and_large_suffix_exemption_unchanged(self):
        self.table([("code/model.py", digest("candidate v1\n"))])
        self.commit()
        self.assert_check(verify.PASS)
        self.write("weights/model.pt", b"weights")
        self.table([("weights/model.pt", digest(b"weights"))])
        self.assert_check(verify.PASS, "LARGE_NO_COPY", large_suffix=[".pt"])

    def test_snapshot_relisting_is_preserved_in_new_mode(self):
        self.write("protected/input.txt", "frozen input")
        snap = "reviews/snapshot.tsv"
        (self.root / "reviews").mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify.PASS, verify.write_snapshot("protected/", snap))
        self.table([(snap, verify.sha256_file(snap), "git")])
        fixed = self.commit()
        self.assert_check(verify.PASS, "SNAPSHOT_OK", commit=fixed)
        self.write("protected/input.txt", "modified input")
        self.assert_check(verify.FAIL, "SNAPSHOT_DIFF", commit=fixed)

    def test_digest_failure_takes_priority_over_missing_rows(self):
        self.table([("code/model.py", digest("wrong digest"), "git"),
                    ("absent/file.txt", digest("absent"), "external:上游声明")])
        self.assert_check(verify.FAIL, "MISSING", precommit=True, require=["also/missing.txt"])

    def test_required_and_unparsed_rows_remain_not_verifiable(self):
        self.table()
        self.assert_check(verify.NOT_VERIFIABLE, "MISSING_REQUIRED_ROW", precommit=True,
                          require=["expected/file.txt"])
        self.table(extra="| missing/value.txt | — | git |\n")
        self.assert_check(verify.NOT_VERIFIABLE, "BAD_VALUE_CELL", precommit=True)

    def test_fenced_output_does_not_become_an_input_table(self):
        self.table(extra="\n```\n| stale/file.txt | " + "a" * 64 + " |\n```\n")
        self.assert_check(verify.PASS, "围栏内另有 1 行", precommit=True)

    def test_last_heading_and_digest_column_still_apply(self):
        self.dispatch = "ledger.md"
        self.write(self.dispatch, "## NON_CONVERGENT\n\n| code/model.py | role | " + digest("candidate v1\n") + " |\n")
        self.commit()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verify.cmd_check([self.dispatch], True, digest_col=3, last_heading=r"^## PASS ")
        self.assertEqual(verify.NOT_VERIFIABLE, code)
        self.assertIn("LAST_TABLE_NOT_EXPECTED_SECTION", output.getvalue())

    def test_git_diff_claims_cover_add_delete_rename_binary_and_unicode(self):
        self.write("old.txt", "rename content\n")
        self.write("deleted.txt", "delete content\n")
        self.write("binary.dat", b"\x00before")
        first = self.commit()
        (self.root / "old.txt").rename(self.root / "renamed.txt")
        (self.root / "deleted.txt").unlink()
        self.write("新增.txt", "new content\n")
        self.write("binary.dat", b"\x00after")
        second = self.commit()
        self.write("reviews/diff-c1.patch", self.git(
            "diff", "--no-renames", "--binary", "--full-index", "--no-ext-diff",
            "--no-textconv", "--no-color", "--src-prefix=a/", "--dst-prefix=b/",
            "--unified=3", "--diff-algorithm=myers", first, second) + "\n")
        touched = verify.patch_touched("reviews/diff-c1.patch")
        expected = {"old.txt", "renamed.txt", "deleted.txt", "新增.txt", "binary.dat"}
        self.assertEqual(expected, touched)
        # 兼容已有启用 rename detection 的 Git patch，两端均可用作 claim。
        self.write("reviews/rename.patch", self.git("diff", "--find-renames", first, second) + "\n")
        self.assertEqual(expected, verify.patch_touched("reviews/rename.patch"))
        self.table()
        self.assert_check(verify.PASS, "CLAIM_OK", precommit=True,
                          claimed_changed=sorted(expected), diff_path="reviews/diff-c1.patch")
        self.assert_check(verify.FAIL, "CLAIMED_BUT_NOT_IN_DIFF", precommit=True,
                          claimed_changed=["code/model.py"], diff_path="reviews/diff-c1.patch")

    def test_patch_content_cannot_invent_a_claimed_path(self):
        self.write("reviews/content.patch", "diff --git a/real.txt b/real.txt\n"
                   "--- a/real.txt\n+++ b/real.txt\n@@ -1 +1 @@\n"
                   "--- a/not-actually-changed.txt\n+++ b/not-actually-changed.txt\n")
        self.assertEqual({"real.txt"}, verify.patch_touched("reviews/content.patch"))

    def test_write_fills_digest_without_changing_preservation_column(self):
        self.table([("code/model.py", "<脚本填>", "git")])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify.PASS, verify.cmd_write(self.dispatch))
        self.assert_check(verify.PASS, precommit=True)
        self.assertIn("| git |", Path(self.dispatch).read_text())
        self.commit()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify.FAIL, verify.cmd_write(self.dispatch))



class RemoteBatchTests(unittest.TestCase):
    def test_long_quoted_unicode_paths_preserve_every_file_and_bound_commands(self):
        paths = [f"/srv/输入/O'Brien {i:05d}.csv" for i in range(7000)]
        seen = []
        def fake_ssh(host, command):
            import shlex
            self.assertEqual(host, "mirror")
            self.assertLessEqual(len(command.encode("utf-8")), 64 * 1024)
            argv = shlex.split(command)
            self.assertEqual(argv[:2], ["sha256sum", "--"])
            seen.extend(argv[2:])
            return subprocess.CompletedProcess([], 0, "".join(
                digest(path) + "  " + path + "\n" for path in argv[2:]), "")
        with patch.object(verify, "ssh", side_effect=fake_ssh) as call:
            result = verify.remote_sha256("mirror", paths)
        self.assertGreater(call.call_count, 1)
        self.assertEqual(seen, paths)
        self.assertEqual(result, {path: digest(path) for path in paths})

    def test_missing_file_does_not_discard_later_batches(self):
        paths = [f"/srv/{i:05d}_" + "x" * 200 for i in range(1000)]
        missing = paths[10]
        def fake_ssh(host, command):
            import shlex
            subset = shlex.split(command)[2:]
            out = "".join(digest(path) + "  " + path + "\n"
                          for path in subset if path != missing)
            return subprocess.CompletedProcess([], int(missing in subset), out, "missing")
        with patch.object(verify, "ssh", side_effect=fake_ssh):
            result = verify.remote_sha256("mirror", paths)
        self.assertEqual(result, {path: digest(path) for path in paths if path != missing})

    def test_later_batch_tool_error_is_not_partial_success(self):
        paths = [f"/srv/{i:05d}_" + "x" * 200 for i in range(1000)]
        with patch.object(verify, "ssh", side_effect=[
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 255, "", "disconnected")]):
            with self.assertRaisesRegex(RuntimeError, "exit 255"):
                verify.remote_sha256("mirror", paths)

if __name__ == "__main__":
    unittest.main()
