#!/usr/bin/env python3
"""Generate small-file references or fill explicit null digests in a NEW draft."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
import report

w = report.w


def fill(root, model):
    root = Path(root).resolve()
    refs = []
    def walk(value):
        if isinstance(value, dict):
            if "path" in value and "sha256" in value:
                path = w.safe_local(root, value["path"])
                sha = value["sha256"]
                w.require(sha is None or isinstance(sha, str) and w.HEX.fullmatch(sha), "use null for an unfilled draft digest")
                w.require(path.is_file() and path.stat().st_size <= w.CONTROL_LIMIT, "missing or oversized small-file input")
                refs.append((value, path))
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)
    walk(model)
    w.require(refs, "no explicit path/sha256 references")
    unique = {str(p): p for _, p in refs}
    w.require(sum(p.stat().st_size for p in unique.values()) <= 64 * 1024 * 1024, "small-file batch exceeds 64 MiB")
    existing_count = sum(node["sha256"] is not None for node, _ in refs)
    cache = {}
    for node, path in refs:
        if node["sha256"] is not None:
            continue  # Existing frozen values are never recalculated/refreshed here.
        if str(path) not in cache:
            cache[str(path)] = w.digest(w.stable_read(path))
        node["sha256"] = cache[str(path)]
    return model, {"hashed_files": len(cache), "existing_values_unchanged": existing_count}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["refs", "fill"])
    p.add_argument("paths", nargs="+", help="explicit files, or exactly one draft JSON for fill")
    p.add_argument("--root", default=".")
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    try:
        root = Path(a.root).resolve()
        out = w.safe_local(root, a.out)
        w.require(not out.exists() and not out.is_symlink(), "output already exists", 1)
        if a.command == "refs":
            w.strings(a.paths, "paths", nonempty=True)
            model = [{"path": path, "sha256": None} for path in a.paths]
        else:
            w.require(len(a.paths) == 1, "fill takes one draft")
            model = w.parse(w.stable_read(w.safe_local(root, a.paths[0])))
        model, stats = fill(root, model)
        w.write_new(out, (json.dumps(model, ensure_ascii=False, indent=2) + "\n").encode())
        print(json.dumps({"result": "DRAFT_WRITTEN", "out": a.out, "authorization": False, **stats}))
        return 0
    except w.WorkflowError as error:
        print("PREPARE_ERROR: " + str(error), file=sys.stderr)
        return error.code
    except (OSError, ValueError, TypeError) as error:
        print("PREPARE_ERROR: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
