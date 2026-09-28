"""Small temporary fixtures only; never connects to a real remote host."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

try:
    from . import snapshots
except ImportError:
    import snapshots


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "data"
        self.root.mkdir()
        (self.root / "a.txt").write_bytes(b"abc")
        self.out = self.base / "baseline.json"

    def tearDown(self):
        self.temporary.cleanup()

    def collect(self, mode="content", exclude=None):
        return snapshots.collect(str(self.root), mode, exclude or [])

    def write(self, mode="content", exclude=None):
        return snapshots.write_snapshot(str(self.root), str(self.out), mode, exclude or [])

    def test_content_hashes_and_keeps_empty_directories(self):
        (self.root / "empty").mkdir()
        result = self.collect()
        self.assertEqual(result["records"]["a.txt"], {
            "type": "file", "sha256": hashlib.sha256(b"abc").hexdigest()})
        self.assertEqual(result["records"]["empty"], {"type": "directory"})
        self.assertEqual(result["stats"], {
            "content_file_count": 1, "content_bytes": 3, "metadata_entries": 0, "entry_count": 3})

    def test_metadata_never_opens_or_hashes_file_content(self):
        (self.root / "empty").mkdir()
        (self.root / "link").symlink_to("a.txt")
        with mock.patch("builtins.open", side_effect=AssertionError("content open")), \
                mock.patch("os.open", side_effect=AssertionError("descriptor open")), \
                mock.patch("hashlib.sha256", side_effect=AssertionError("content hash")):
            result = self.collect("metadata")
        record = result["records"]["a.txt"]
        info = (self.root / "a.txt").stat()
        self.assertEqual(record["size"], 3)
        self.assertEqual(record["mtime_ns"], info.st_mtime_ns)
        self.assertEqual(record["ctime_ns"], info.st_ctime_ns)
        self.assertIn("mode", record)
        self.assertNotIn("sha256", record)
        self.assertEqual(result["records"]["link"]["target"], "a.txt")
        self.assertEqual(result["records"]["empty"]["type"], "directory")
        self.assertEqual(result["stats"]["metadata_entries"], 4)
        self.assertEqual(result["stats"]["content_file_count"], 0)
        self.assertEqual(result["stats"]["content_bytes"], 0)

    def test_excluded_files_and_directories_are_never_opened(self):
        (self.root / "cache").mkdir()
        (self.root / "cache" / "blob.bin").write_bytes(b"not read")
        (self.root / "skip.bin").write_bytes(b"also not read")
        original_open = os.open
        opened = []

        def guarded_open(path, flags, *args, **kwargs):
            opened.append(os.fspath(path))
            if "cache" in os.fspath(path) or os.fspath(path).endswith("skip.bin"):
                self.fail("excluded content opened")
            return original_open(path, flags, *args, **kwargs)

        with mock.patch("os.open", side_effect=guarded_open):
            result = self.collect(exclude=["cache/*", "skip.bin"])
        self.assertEqual(set(result["records"]), {".", "a.txt"})
        self.assertEqual(opened, [str(self.root / "a.txt")])

    def test_symlink_targets_are_recorded_without_traversal(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"unread")
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        (self.root / "broken").symlink_to("absent")
        result = self.collect()
        self.assertEqual(result["records"]["link"], {"type": "symlink", "target": str(outside)})
        self.assertEqual(result["records"]["broken"], {"type": "symlink", "target": "absent"})
        self.assertFalse(any(key.startswith("link/") for key in result["records"]))
        self.assertEqual(result["stats"]["content_file_count"], 1)

    def test_symlink_and_at_suffix_file_do_not_collide(self):
        (self.root / "link").symlink_to("a.txt")
        (self.root / "link@").write_bytes(b"real file")
        result = self.collect()
        self.assertEqual(result["records"]["link"]["type"], "symlink")
        self.assertEqual(result["records"]["link@"]["type"], "file")

    def test_symlink_root_rejected_even_with_trailing_slash(self):
        link = self.base / "root-link"
        link.symlink_to(self.root, target_is_directory=True)
        for mode in ("content", "metadata"):
            with self.subTest(mode=mode), self.assertRaisesRegex(snapshots.SnapshotError, "root must not"):
                snapshots.collect(str(link) + "/", mode, [])

    def test_missing_and_nondirectory_root_fail(self):
        for target in (self.root / "absent", self.root / "a.txt"):
            with self.subTest(target=target), self.assertRaises(snapshots.SnapshotError):
                snapshots.collect(str(target), "metadata", [])

    def test_scandir_permission_error_is_not_partial_success(self):
        with mock.patch("os.scandir", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(snapshots.SnapshotError, "denied"):
                self.collect()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFO support")
    def test_special_node_fails_unless_explicitly_excluded(self):
        os.mkfifo(self.root / "pipe")
        with self.assertRaisesRegex(snapshots.SnapshotError, "unsupported"):
            self.collect("metadata")
        result = self.collect("metadata", ["pipe"])
        self.assertNotIn("pipe", result["records"])

    def test_file_mutation_during_content_read_fails(self):
        real_fstat = os.fstat
        calls = 0

        def mutate(descriptor):
            nonlocal calls
            original = real_fstat(descriptor)
            calls += 1
            if calls == 1:
                (self.root / "a.txt").write_bytes(b"changed")
            return original

        with mock.patch("os.fstat", side_effect=mutate):
            with self.assertRaisesRegex(snapshots.SnapshotError, "changed during"):
                self.collect()

    def test_metadata_changes_during_scan_fail(self):
        real_lstat = os.lstat
        file_calls = 0

        def mutate(path, *args, **kwargs):
            nonlocal file_calls
            if os.fspath(path) == str(self.root / "a.txt"):
                file_calls += 1
                if file_calls == 2:
                    (self.root / "a.txt").write_bytes(b"changed")
            return real_lstat(path, *args, **kwargs)

        with mock.patch("os.lstat", side_effect=mutate):
            with self.assertRaisesRegex(snapshots.SnapshotError, "changed during"):
                self.collect("metadata")

    def test_write_and_compare_content(self):
        written = self.write()
        self.assertEqual(written["schema"], snapshots.SCHEMA)
        self.assertIn("created_utc", written)
        status, details, stats = snapshots.compare_snapshot(str(self.out))
        self.assertEqual(status, "SNAPSHOT_CONTENT_OK")
        self.assertEqual(details, [])
        self.assertEqual(stats["content_bytes"], 3)
        (self.root / "a.txt").write_bytes(b"xyz")
        status, details, stats = snapshots.compare_snapshot(str(self.out))
        self.assertEqual(status, "SNAPSHOT_CONTENT_DIFF")
        self.assertEqual(details, ["~ a.txt"])
        self.assertEqual(stats["modified"], 1)

    def test_metadata_status_is_distinct(self):
        self.write("metadata")
        status, details, stats = snapshots.compare_snapshot(str(self.out))
        self.assertEqual(status, "SNAPSHOT_METADATA_OK")
        self.assertEqual(details, [])
        self.assertEqual(stats["content_bytes"], 0)
        os.chmod(self.root / "a.txt", 0o600)
        status, details, _stats = snapshots.compare_snapshot(str(self.out))
        self.assertEqual(status, "SNAPSHOT_METADATA_DIFF")
        self.assertIn("~ a.txt", details)

    def test_added_removed_and_empty_directory_changes(self):
        self.write()
        (self.root / "a.txt").unlink()
        (self.root / "empty").mkdir()
        status, details, stats = snapshots.compare_snapshot(str(self.out))
        self.assertEqual(status, "SNAPSHOT_CONTENT_DIFF")
        self.assertEqual(details, ["+ empty", "- a.txt"])
        self.assertEqual((stats["added"], stats["removed"]), (1, 1))

    def test_no_clobber_precedes_collection(self):
        self.out.write_bytes(b"preserved")
        with mock.patch.object(snapshots, "collect", side_effect=AssertionError("should not collect")):
            with self.assertRaisesRegex(snapshots.SnapshotError, "already exists"):
                self.write()
        self.assertEqual(self.out.read_bytes(), b"preserved")

    def test_no_clobber_detects_creation_race(self):
        original_collect = snapshots.collect

        def racing_collect(*args):
            result = original_collect(*args)
            self.out.write_bytes(b"other writer")
            return result

        with mock.patch.object(snapshots, "collect", side_effect=racing_collect):
            with self.assertRaises(snapshots.SnapshotError):
                self.write()
        self.assertEqual(self.out.read_bytes(), b"other writer")

    def test_failed_output_is_removed(self):
        with mock.patch("os.fsync", side_effect=OSError("write failed")):
            with self.assertRaisesRegex(snapshots.SnapshotError, "write failed"):
                self.write()
        self.assertFalse(self.out.exists())

    def test_json_roundtrip_supports_tabs_newlines_and_backslashes(self):
        name = "tab\tnewline\nback\\slash"
        (self.root / name).write_bytes(b"tiny")
        self.write()
        self.assertIn(name, snapshots.read_snapshot(str(self.out))["records"])
        self.assertEqual(snapshots.compare_snapshot(str(self.out))[0], "SNAPSHOT_CONTENT_OK")

    def test_parse_snapshot_uses_supplied_bytes_and_accepts_late_schema(self):
        for index in range(8):
            (self.root / f"extra-{index}.txt").write_bytes(b"tiny")
        document = self.write("metadata")
        document.pop("stats")
        raw = json.dumps(document, sort_keys=True).encode("utf-8")
        self.assertGreater(raw.index(b"agent-gates.snapshot.v2"), 512)
        with mock.patch("builtins.open", side_effect=AssertionError("parser opened a path")):
            parsed = snapshots.parse_snapshot(raw)
        self.assertEqual(parsed, document)

    def test_parse_snapshot_rejects_invalid_bytes(self):
        for raw in (b"\xff", b"not JSON", "not bytes"):
            with self.subTest(raw=raw), self.assertRaises(snapshots.SnapshotError):
                snapshots.parse_snapshot(raw)

    def test_authenticated_baseline_survives_path_replacement(self):
        self.write()
        authenticated = snapshots.parse_snapshot(self.out.read_bytes())
        other = self.base / "other-directory"
        other.mkdir()
        (other / "a.txt").write_bytes(b"abc")
        replacement = {**authenticated, "target": str(other)}
        self.out.write_text(json.dumps(replacement), encoding="utf-8")
        (self.root / "a.txt").write_bytes(b"xyz")
        with mock.patch.object(snapshots, "read_snapshot", side_effect=AssertionError("baseline reread")):
            status, details, stats = snapshots.compare_snapshot(str(self.out), baseline=authenticated)
        self.assertEqual(status, "SNAPSHOT_CONTENT_DIFF")
        self.assertEqual(details, ["~ a.txt"])
        self.assertEqual(stats["target"], str(self.root))

    def test_authenticated_baseline_is_structurally_validated(self):
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.compare_snapshot("unused", baseline={"schema": "unknown"})

    def test_legacy_tsv_is_content_only_and_ignores_directory_records(self):
        (self.root / "empty").mkdir()
        (self.root / "link").symlink_to("a.txt")
        digest = hashlib.sha256(b"abc").hexdigest()
        self.out.write_text(f"# snapshot {self.root}/\n# created historical files=2\n"
                            f"# exclude \n{digest}\ta.txt\nsymlink:a.txt\tlink@\n", encoding="utf-8")
        document = snapshots.read_snapshot(str(self.out))
        self.assertEqual(document["mode"], "content")
        self.assertEqual(document["schema"], snapshots.LEGACY_SCHEMA)
        self.assertEqual(snapshots.compare_snapshot(str(self.out))[0], "SNAPSHOT_CONTENT_OK")

    def test_legacy_invalid_or_duplicate_rows_fail(self):
        digest = hashlib.sha256(b"abc").hexdigest()
        for body in ("bad\ta.txt", f"{digest}\ta.txt\n{digest}\ta.txt", "malformed",
                     "symlink:a.txt\tlink@\n" + digest + "\tlink"):
            with self.subTest(body=body):
                self.out.write_text(f"# snapshot {self.root}/\n{body}\n", encoding="utf-8")
                with self.assertRaises(snapshots.SnapshotError):
                    snapshots.read_snapshot(str(self.out))

    def test_json_schema_mode_paths_and_duplicate_keys_fail(self):
        document = self.write()
        document.pop("stats")
        invalid_documents = []
        for change in ({"schema": "unknown"}, {"mode": "other"},
                       {"records": {"../escape": {"type": "directory"}}}):
            invalid_documents.append(json.dumps({**document, **change}))
        invalid_documents.append('{"schema":"one","schema":"two"}')
        invalid_documents.append(json.dumps({**document, "mode": "metadata"}))
        for text in invalid_documents:
            with self.subTest(text=text):
                self.out.write_text(text, encoding="utf-8")
                with self.assertRaises(snapshots.SnapshotError):
                    snapshots.read_snapshot(str(self.out))

    def test_remote_executes_same_collector_with_mocked_ssh(self):
        (self.root / "skip").write_bytes(b"unread")
        (self.root / "link").symlink_to("a.txt")
        real_run = subprocess.run

        def local_interpreter(argv, **kwargs):
            self.assertEqual(argv, ["ssh", "unit", "python3 -"])
            self.assertLessEqual(kwargs["timeout"], 900)
            return real_run([sys.executable, "-"], **kwargs)

        for mode in ("content", "metadata"):
            local = self.collect(mode, ["skip"])
            with self.subTest(mode=mode), mock.patch.object(snapshots.subprocess, "run", side_effect=local_interpreter):
                remote = snapshots.collect("unit:" + str(self.root), mode, ["skip"])
            self.assertEqual(remote["records"], local["records"])
            self.assertEqual(remote["stats"], local["stats"])
            self.assertEqual(remote["target"], "unit:" + str(self.root))

    def test_remote_nonzero_rejects_even_valid_partial_output(self):
        partial = json.dumps(self.collect())
        outcome = subprocess.CompletedProcess([], 2, stdout=partial, stderr="permission denied")
        with mock.patch.object(snapshots.subprocess, "run", return_value=outcome):
            with self.assertRaisesRegex(snapshots.SnapshotError, "permission denied"):
                snapshots.collect("unit:" + str(self.root), "content", [])

    def test_remote_timeout_missing_program_and_invalid_output_fail(self):
        for failure in (subprocess.TimeoutExpired("ssh", 900), FileNotFoundError("ssh")):
            with self.subTest(failure=failure), mock.patch.object(snapshots.subprocess, "run", side_effect=failure):
                with self.assertRaises(snapshots.SnapshotError):
                    snapshots.collect("unit:/data", "metadata", [])
        with mock.patch.object(snapshots.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "not JSON", "")):
            with self.assertRaises(snapshots.SnapshotError):
                snapshots.collect("unit:/data", "metadata", [])

    def test_remote_wrong_mode_or_reported_content_reads_fail(self):
        local = self.collect("metadata")
        for update in ({"mode": "content"},
                       {"stats": {**local["stats"], "content_file_count": 1}},
                       {"records": {}}, {"target": "/different"}):
            outcome = subprocess.CompletedProcess([], 0, json.dumps({**local, **update}), "")
            with self.subTest(update=update), mock.patch.object(snapshots.subprocess, "run", return_value=outcome):
                with self.assertRaises(snapshots.SnapshotError):
                    snapshots.collect("unit:" + str(self.root), "metadata", [])


if __name__ == "__main__":
    unittest.main()
