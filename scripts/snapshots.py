"""Explicit content or metadata snapshots; no implicit mode downgrade.

Metadata equality is not content equality.  Traversal checks for concurrent changes,
but cannot provide an atomic filesystem snapshot.  Remote collection executes the
same collector source as local collection and emits no successful partial result.
"""
from __future__ import annotations

import datetime as dt
import inspect
import json
import os
import re
import subprocess

SCHEMA = "agent-gates.snapshot.v2"
LEGACY_SCHEMA = "agent-gates.snapshot.legacy"
SSH_TIMEOUT = 900


class SnapshotError(RuntimeError):
    """A snapshot cannot be collected, interpreted, or written reliably."""


def _collect_local(target: str, mode: str, exclude: list[str]) -> dict:
    # Keep imports and traversal helpers inside this function: its exact source is
    # also executed by the remote Python interpreter.
    import fnmatch
    import hashlib
    import os
    import stat

    if mode not in ("content", "metadata"):
        raise SnapshotError("snapshot mode must be content or metadata")
    if not isinstance(target, str) or not target or "\x00" in target:
        raise SnapshotError("invalid snapshot target")
    if not isinstance(exclude, list) or any(
        not isinstance(pattern, str) or not pattern or "\x00" in pattern
        for pattern in exclude
    ):
        raise SnapshotError("exclude must be a list of nonempty glob strings")
    # Strip a trailing slash before lstat, which would otherwise dereference a
    # symlink root on POSIX systems.
    root = os.path.abspath(target.rstrip("/") or "/")
    records = {}
    observed = {}
    stats = {"content_file_count": 0, "content_bytes": 0, "metadata_entries": 0}

    def signature(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)

    def stable(path, before, after=None):
        after = os.lstat(path) if after is None else after
        if signature(before) != signature(after):
            raise SnapshotError("node changed during snapshot: " + path)

    def metadata(kind, info):
        return {"type": kind, "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                "ctime_ns": info.st_ctime_ns, "mode": stat.S_IMODE(info.st_mode)}

    def excluded(rel, info):
        candidates = [rel]
        if stat.S_ISDIR(info.st_mode):
            candidates.append(rel + "/")
        if stat.S_ISLNK(info.st_mode):
            candidates.append(rel + "@")  # legacy symlink exclusion spelling
        return any(fnmatch.fnmatchcase(value, pattern)
                   for pattern in exclude for value in candidates)

    def visit(path, rel, info):
        observed[path] = info
        if stat.S_ISLNK(info.st_mode):
            record = metadata("symlink", info) if mode == "metadata" else {"type": "symlink"}
            record["target"] = os.readlink(path)
            stable(path, info)
            records[rel] = record
        elif stat.S_ISREG(info.st_mode):
            if mode == "metadata":
                records[rel] = metadata("file", info)
                stable(path, info)
                return
            digest = hashlib.sha256()
            count = 0
            # Nonblocking prevents a race replacing a regular file with a FIFO
            # from hanging open(); nofollow prevents a replacement symlink read.
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    raise SnapshotError("file type changed during snapshot: " + path)
                stable(path, info, opened)
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    count += len(chunk)
                stable(path, info, os.fstat(handle.fileno()))
            stable(path, info)
            if count != info.st_size:
                raise SnapshotError("file length changed during snapshot: " + path)
            records[rel] = {"type": "file", "sha256": digest.hexdigest()}
            stats["content_file_count"] += 1
            stats["content_bytes"] += count
        elif stat.S_ISDIR(info.st_mode):
            records[rel] = metadata("directory", info) if mode == "metadata" else {"type": "directory"}
            # scandir errors must propagate.  os.walk's default error suppression
            # is inappropriate for a complete baseline.
            with os.scandir(path) as scan:
                names = sorted(entry.name for entry in scan)
            for name in names:
                child = os.path.join(path, name)
                child_rel = name if rel == "." else rel + "/" + name
                child_info = os.lstat(child)
                if excluded(child_rel, child_info):
                    continue
                visit(child, child_rel, child_info)
            stable(path, info)
        else:
            raise SnapshotError("unsupported filesystem node: " + path)

    try:
        root_info = os.lstat(root)
        if stat.S_ISLNK(root_info.st_mode):
            raise SnapshotError("snapshot root must not be a symlink: " + root)
        if not stat.S_ISDIR(root_info.st_mode):
            raise SnapshotError("snapshot target is not a directory: " + root)
        visit(root, ".", root_info)
        # Detect mutations to earlier entries while later entries were scanned.
        for path, before in observed.items():
            stable(path, before)
    except OSError as error:
        raise SnapshotError("snapshot collection failed: " + str(error)) from error
    stats["metadata_entries"] = len(records) if mode == "metadata" else 0
    stats["entry_count"] = len(records)
    return {"target": target, "mode": mode, "records": records, "stats": stats}


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotError("duplicate JSON key: " + key)
        result[key] = value
    return result


def _loads(text: str):
    try:
        return json.loads(text, object_pairs_hook=_unique_pairs)
    except (ValueError, TypeError) as error:
        raise SnapshotError("invalid snapshot JSON: " + str(error)) from error


def _validate_records(records, mode):
    if not isinstance(records, dict):
        raise SnapshotError("snapshot records must be an object")
    for rel, record in records.items():
        if (not isinstance(rel, str) or not rel or "\x00" in rel or rel.startswith("/")
                or (rel != "." and any(part in ("", ".", "..") for part in rel.split("/")))):
            raise SnapshotError("invalid relative snapshot path: " + repr(rel))
        if not isinstance(record, dict) or record.get("type") not in ("file", "directory", "symlink"):
            raise SnapshotError("invalid snapshot node: " + repr(rel))
        kind = record["type"]
        if rel == "." and kind != "directory":
            raise SnapshotError("snapshot root record must be a directory")
        fields = {"type"}
        if mode == "metadata":
            fields.update(("size", "mtime_ns", "ctime_ns", "mode"))
            if any(type(record.get(key)) is not int for key in ("size", "mtime_ns", "ctime_ns", "mode")):
                raise SnapshotError("invalid metadata fields: " + repr(rel))
            if record["size"] < 0 or not 0 <= record["mode"] <= 0o7777:
                raise SnapshotError("invalid metadata size or permissions: " + repr(rel))
        elif kind == "file":
            fields.add("sha256")
            if not isinstance(record.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]):
                raise SnapshotError("invalid content digest: " + repr(rel))
        if kind == "symlink":
            fields.add("target")
            if not isinstance(record.get("target"), str) or not record["target"] or "\x00" in record["target"]:
                raise SnapshotError("invalid symlink target: " + repr(rel))
        if set(record) != fields:
            raise SnapshotError("unexpected fields for snapshot mode: " + repr(rel))


def _validate_document(document, *, legacy=False):
    if not isinstance(document, dict):
        raise SnapshotError("snapshot must be an object")
    expected = LEGACY_SCHEMA if legacy else SCHEMA
    if document.get("schema") != expected:
        raise SnapshotError("unsupported snapshot schema")
    if document.get("mode") not in ("content", "metadata"):
        raise SnapshotError("unsupported snapshot mode")
    target = document.get("target")
    if not isinstance(target, str) or not target or "\x00" in target:
        raise SnapshotError("invalid snapshot target")
    exclude = document.get("exclude")
    if not isinstance(exclude, list) or any(
        not isinstance(pattern, str) or not pattern or "\x00" in pattern for pattern in exclude
    ):
        raise SnapshotError("invalid snapshot exclusions")
    _validate_records(document.get("records"), document["mode"])
    if not legacy and document["records"].get(".", {}).get("type") != "directory":
        raise SnapshotError("v2 snapshot is missing its root directory record")


def collect(target: str, mode: str, exclude: list[str]) -> dict:
    """Collect a local or host:path directory; metadata never reads file content."""
    if not isinstance(target, str) or not target or "\x00" in target:
        raise SnapshotError("invalid snapshot target")
    if mode not in ("content", "metadata"):
        raise SnapshotError("snapshot mode must be content or metadata")
    if not isinstance(exclude, list) or any(
        not isinstance(pattern, str) or not pattern or "\x00" in pattern for pattern in exclude
    ):
        raise SnapshotError("exclude must be a list of nonempty glob strings")
    remote = re.fullmatch(r"([A-Za-z0-9_][A-Za-z0-9_.-]*):(.+)", target, re.DOTALL)
    if not remote:
        return _collect_local(target, mode, exclude)
    host, path = remote.groups()
    request = json.dumps({"target": path, "mode": mode, "exclude": exclude}, ensure_ascii=True)
    program = (
        "from __future__ import annotations\nimport json, sys\nclass SnapshotError(RuntimeError):\n    pass\n\n"
        + inspect.getsource(_collect_local)
        + "\nrequest = json.loads(" + repr(request) + ")\n"
        + "try:\n    result = _collect_local(**request)\n"
        + "    print(json.dumps(result, ensure_ascii=True, sort_keys=True))\n"
        + "except Exception as error:\n    print(str(error), file=sys.stderr)\n    sys.exit(2)\n"
    )
    try:
        outcome = subprocess.run(["ssh", host, "python3 -"], input=program,
                                 capture_output=True, text=True, check=False, timeout=SSH_TIMEOUT)
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise SnapshotError("remote snapshot failed: " + str(error)) from error
    if outcome.returncode != 0:
        raise SnapshotError(f"ssh {host}: exit {outcome.returncode}: {outcome.stderr.strip()[:500]}")
    result = _loads(outcome.stdout)
    if not isinstance(result, dict) or result.get("target") != path or result.get("mode") != mode:
        raise SnapshotError("remote snapshot response does not match request")
    _validate_records(result.get("records"), mode)
    stats = result.get("stats")
    required_stats = ("content_file_count", "content_bytes", "metadata_entries", "entry_count")
    if not isinstance(stats, dict) or any(type(stats.get(key)) is not int or stats[key] < 0 for key in required_stats):
        raise SnapshotError("invalid remote snapshot telemetry")
    if stats["entry_count"] != len(result["records"]):
        raise SnapshotError("inconsistent remote snapshot telemetry")
    if mode == "metadata" and (stats["content_file_count"] or stats["content_bytes"] or
                               stats["metadata_entries"] != len(result["records"])):
        raise SnapshotError("remote metadata response reports content reads")
    if mode == "content" and (stats["metadata_entries"] or stats["content_file_count"] !=
                               sum(record["type"] == "file" for record in result["records"].values())):
        raise SnapshotError("inconsistent remote content telemetry")
    if result["records"].get(".", {}).get("type") != "directory":
        raise SnapshotError("remote snapshot is missing its root directory record")
    result["target"] = target
    return result


def write_snapshot(target: str, out: str, mode: str, exclude: list[str]) -> dict:
    """Write one new baseline. An existing output is never overwritten."""
    if os.path.lexists(out):
        raise SnapshotError("snapshot output already exists: " + os.fspath(out))
    result = collect(target, mode, exclude)
    document = {"schema": SCHEMA, "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "target": target, "mode": mode, "exclude": list(exclude), "records": result["records"]}
    # schema 必须位于有界文件头；否则大 records 会使派发表漏识别快照。
    payload = json.dumps(document, ensure_ascii=True, indent=2) + "\n"
    descriptor = None
    identity = None
    try:
        descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        identity = os.fstat(descriptor)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        if identity is not None:
            try:
                current = os.lstat(out)
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    os.unlink(out)
            except OSError:
                pass
        raise SnapshotError("snapshot write failed: " + str(error)) from error
    return {**document, "stats": result["stats"]}


def read_snapshot(path: str) -> dict:
    """Read stable bytes, then parse JSON v2 or a legacy content TSV."""
    try:
        with open(path, "rb") as handle:
            before = os.fstat(handle.fileno())
            raw = handle.read()
            after = os.fstat(handle.fileno())
        current = os.stat(path)
        attributes = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, key) != getattr(after, key) or getattr(before, key) != getattr(current, key)
               for key in attributes):
            raise SnapshotError("snapshot file changed while being read")
    except (OSError, UnicodeError) as error:
        raise SnapshotError("cannot read snapshot: " + str(error)) from error
    return parse_snapshot(raw)


def parse_snapshot(raw: bytes) -> dict:
    """Parse the exact supplied bytes without reading any filesystem paths.

    Callers that authenticate a baseline must pass those authenticated bytes here,
    then pass this document to compare_snapshot(baseline=...), avoiding a second
    read of a mutable baseline path.  JSON key order has no semantic meaning.
    """
    if not isinstance(raw, bytes):
        raise SnapshotError("snapshot input must be bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as error:
        raise SnapshotError("snapshot is not UTF-8: " + str(error)) from error
    if not text.startswith("# snapshot "):
        document = _loads(text)
        _validate_document(document)
        return document
    lines = text.splitlines()
    target = lines[0][len("# snapshot "):].strip()
    records = {}
    exclude = []
    seen_exclude = False
    for line_number, line in enumerate(lines[1:], 2):
        if line.startswith("# exclude "):
            if seen_exclude:
                raise SnapshotError("duplicate legacy exclude header")
            seen_exclude = True
            exclude = [pattern for pattern in line[len("# exclude "):].split(",") if pattern]
            continue
        if line.startswith("#") or not line.strip():
            continue
        digest, separator, rel = line.partition("\t")
        if not separator or not rel or "\t" in rel:
            raise SnapshotError(f"invalid legacy snapshot row {line_number}")
        if digest.startswith("symlink:") and rel.endswith("@"):
            rel = rel[:-1]
            record = {"type": "symlink", "target": digest[len("symlink:"):]}
        else:
            record = {"type": "file", "sha256": digest}
        if rel in records:
            raise SnapshotError("duplicate legacy snapshot path: " + rel)
        records[rel] = record
    document = {"schema": LEGACY_SCHEMA, "target": target, "mode": "content",
                "exclude": exclude, "records": records}
    _validate_document(document, legacy=True)
    return document


def compare_snapshot(path: str, *, baseline: dict | None = None) -> tuple[str, list[str], dict]:
    """Compare an authenticated document, or read path when none was supplied.

    With baseline supplied, path is only a caller-side label and is never opened.
    This function validates structure; authentication remains the caller's duty.
    """
    if baseline is None:
        baseline = read_snapshot(path)
    else:
        _validate_document(baseline, legacy=isinstance(baseline, dict) and
                           baseline.get("schema") == LEGACY_SCHEMA)
    live = collect(baseline["target"], baseline["mode"], baseline["exclude"])
    expected = baseline["records"]
    actual = live["records"]
    if baseline["schema"] == LEGACY_SCHEMA:
        # Legacy TSV only represented files and symlinks, never directories.
        actual = {key: record for key, record in actual.items() if record["type"] != "directory"}
    added = sorted(set(actual) - set(expected))
    removed = sorted(set(expected) - set(actual))
    modified = sorted(key for key in set(actual) & set(expected) if actual[key] != expected[key])
    details = (["+ " + key for key in added] + ["- " + key for key in removed]
               + ["~ " + key for key in modified])
    status = "SNAPSHOT_" + baseline["mode"].upper() + ("_DIFF" if details else "_OK")
    stats = {**live["stats"], "target": baseline["target"], "mode": baseline["mode"],
             "added": len(added), "removed": len(removed), "modified": len(modified),
             "baseline_entries": len(expected), "compared_entries": len(actual)}
    return status, details, stats
