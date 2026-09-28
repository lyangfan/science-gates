"""SCI table and Git helper regressions, using only temporary local fixtures."""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import digest_table as table
import verify_digests as verify


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
            args = ["check", str(path)]
            if last_only:
                args.append("--last")
            for key, value in kwargs.items():
                args.extend(["--" + key.replace("_", "-"), str(value)])
            code = verify.main(args)
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
                self.assertIn(f"line={text.splitlines().index(bad_row) + 1} ", out)

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
                self.assertIn("line=3 ", out)

    def test_empty_digest_table_is_not_hidden_by_valid_table(self):
        code, out = self.check_text("| 路径 | sha256 |\n|---|---|\n\n" + self.good_table)
        self.assertEqual(verify.NOT_VERIFIABLE, code, out)
        self.assertIn("EMPTY_DIGEST_TABLE", out)
        self.assertIn("line=1 ", out)

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
                    self.assertIn("\"content_files\": 1", out)

    def test_explicit_third_digest_column_keeps_malformed_table(self):
        header = "| 文件 | 角色 | sha256 |\n|---|---|---|\n"
        text = (header + "| missing.csv | input | — |\n\n" + header
                + f"| {self.source} | input | {digest('frozen input')} |\n")
        code, out = self.check_text(text, digest_col=3)
        self.assertEqual(verify.NOT_VERIFIABLE, code, out)
        self.assertIn("line=3 missing.csv: BAD_VALUE_CELL", out)

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
            self.assertEqual(verify.PASS, verify.main(["write", str(path)]))
        code, out = self.check_text(path.read_text(encoding="utf-8"))
        self.assertEqual(verify.PASS, code, out)

    def test_fenced_digest_rows_are_counted_without_becoming_inputs(self):
        rows, skipped, fenced = table.parse_rows("```\n" + self.good_table + "```\n")
        self.assertEqual([], rows)
        self.assertEqual([], skipped)
        self.assertEqual(1, fenced)

    def test_cell_parser_preserves_empty_and_escaped_cells(self):
        self.assertEqual(["a.csv", "", r"note\|detail"],
                         table.split_cells(r"|a.csv||note\|detail|"))
        self.assertIsNone(table.split_cells("ordinary text"))


class GitHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="science-git-helpers-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "repo"
        self.root.mkdir()
        self.env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_AUTHOR_NAME": "SCI Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "SCI Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
        self.git("init", "-q")
        (self.root / "source.txt").write_text("input\n")
        (self.root / "reviews").mkdir()
        (self.root / "reviews/dispatch.md").write_text("dispatch\n")
        self.git("add", ".")
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], env=self.env,
                              text=True, capture_output=True, check=True).stdout.strip()

    def test_repository_and_state_do_not_depend_on_callers_directory(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root.parent)
            self.assertEqual(str(self.root), table.git_repository(str(self.root / "reviews/dispatch.md")))
            with mock.patch.dict(os.environ, self.env):
                self.assertEqual("clean", table.git_state(str(self.root / "source.txt")))
                (self.root / "source.txt").write_text("changed\n")
                self.assertEqual("dirty", table.git_state(str(self.root / "source.txt")))
                (self.root / "new.txt").write_text("untracked\n")
                self.assertEqual("untracked", table.git_state(str(self.root / "new.txt")))
        finally:
            os.chdir(previous)

    def test_mapping_rejects_noncanonical_directories_and_outside_paths(self):
        for path in ("", "../source.txt", "reviews/../source.txt", "./source.txt", "reviews/",
                     "reviews", ".git/config", str(self.root / "source.txt"), "with spaces.txt"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                table.repository_path(str(self.root), path)
        with self.assertRaises(ValueError):
            table.repository_path(str(self.root), str(self.root.parent / "outside.txt"), source=True)
        self.assertEqual("source.txt", table.repository_path(str(self.root), str(self.root / "source.txt"), source=True))
        self.assertEqual("future.txt", table.repository_path(str(self.root), "future.txt"))

    def test_mapping_rejects_symlink_escape(self):
        (self.root / "escape").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            table.repository_path(str(self.root), "escape/outside.txt")

    def test_remote_binding_requires_explicit_local_mapping_or_external_reason(self):
        self.assertEqual(("compute", "/data/input.txt"), table.split_remote("compute:/data/input.txt"))
        self.assertEqual("source.txt", verify.binding(str(self.root), "compute:/data/input.txt", "git:source.txt"))
        self.assertIsNone(verify.binding(str(self.root), "compute:/data/input.txt", "external:read-only input"))
        for declaration in ("git", "external:", "git:../source.txt", "git:reviews"):
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                verify.binding(str(self.root), "compute:/data/input.txt", declaration)


if __name__ == "__main__":
    unittest.main()
