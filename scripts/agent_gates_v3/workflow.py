#!/usr/bin/env python3
"""SCI workflow facts and candidate records; never a scientific Reviewer.

All control inputs are externally SHA-pinned. The acceptance source independently
defines the complete check-ID set. Existing v2 code remains a read-only dependency.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys

sys.dont_write_bytecode = True
CONTROL_LIMIT = 8 * 1024 * 1024
HEX = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
ROLES = {"authority", "candidate", "dependency", "scientific_input", "product", "evidence"}
STAGES = {"prepare", "b1", "r1", "r2", "execute", "c1", "c2", "c3", "addendum"}
GATE_STAGES = {"A": {"b1", "c1"}, "B": STAGES}
PREEXEC = {"b1", "r1", "r2"}
RESULT = {"c1", "c2", "c3"}
SCOPES = {"full_chain", "scoped_addendum"}
CONTRACT = "agent-gates.workflow-contract.v1"
MANIFEST = "agent-gates.workflow-manifest.v1"
REVIEW = "agent-gates.workflow-review.v1"
RECEIPT = "agent-gates.workflow-receipt.v1"


class WorkflowError(RuntimeError):
    def __init__(self, message, code=3):
        super().__init__(message)
        self.code = code


def require(condition, message, code=3):
    if not condition:
        raise WorkflowError(message, code)


def keys(obj, required, optional=()):
    require(isinstance(obj, dict), "expected a JSON object")
    require(set(required) <= set(obj), "missing keys: " + str(sorted(set(required) - set(obj))))
    require(set(obj) <= set(required) | set(optional), "unknown keys: " + str(sorted(set(obj) - set(required) - set(optional))))


def text(value, label):
    require(isinstance(value, str) and bool(value.strip()), label + " must be a nonempty string")
    return value


def strings(value, label, *, nonempty=False):
    require(isinstance(value, list) and (bool(value) or not nonempty), label + " must be a list")
    for item in value:
        text(item, label)
    require(len(value) == len(set(value)), label + " contains duplicates")
    return value


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def no_duplicates(pairs):
    obj = {}
    for key, value in pairs:
        require(key not in obj, "duplicate JSON key: " + key)
        obj[key] = value
    return obj


def parse(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (ValueError, UnicodeError) as error:
        raise WorkflowError("invalid JSON control: " + str(error)) from error


def stable_read(path, expected=None):
    """Bounded, no-follow read; authenticate the same bytes that are parsed."""
    if expected is not None:
        require(isinstance(expected, str) and bool(HEX.fullmatch(expected)), "expected SHA256 is not 64 lowercase hex")
    try:
        before = os.lstat(path)
        require(stat.S_ISREG(before.st_mode), "control is not a regular file: " + str(path))
        require(before.st_size <= CONTROL_LIMIT, "control exceeds 8 MiB: " + str(path))
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as handle:
            require(identity(os.fstat(handle.fileno())) == identity(before), "control changed before read")
            raw = handle.read(CONTROL_LIMIT + 1)
            require(len(raw) <= CONTROL_LIMIT, "control exceeds 8 MiB")
            require(identity(os.fstat(handle.fileno())) == identity(before), "control changed during read")
        require(identity(os.lstat(path)) == identity(before), "control path changed during read")
    except OSError as error:
        raise WorkflowError("cannot read control: " + str(error), 2) from error
    if expected is not None:
        require(digest(raw) == expected, "control SHA256 mismatch: " + str(path), 1)
    return raw


def safe_local(root, value, *, allow_root=False):
    text(value, "path")
    require(not any(c.isspace() for c in value) and not any(c in value for c in ("\\", "\x00", "|")),
            "paths must have no whitespace, backslash, NUL, or table separator")
    require(not value.startswith("/") and ":" not in value, "local paths must be repository-relative")
    parts = value.split("/")
    require(value == "." and allow_root or all(p not in ("", ".", "..") for p in parts), "noncanonical path")
    full = root / value
    require(full.resolve().is_relative_to(root), "path or symlink escapes repository")
    require(".git" not in parts, "Git internals are not workflow artifacts")
    return full


def reference(root, ref, label):
    keys(ref, ("path", "sha256"))
    path = safe_local(root, ref["path"])
    require(isinstance(ref["sha256"], str) and bool(HEX.fullmatch(ref["sha256"])), label + " has invalid SHA256")
    return path


def below(path, prefix):
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def remote_path(path, allowed_hosts):
    host, remote = path.split(":", 1)
    require(host in allowed_hosts and host != "local", "undeclared object host")
    require(remote.startswith("/") and (remote == "/" or all(part not in ("", ".", "..") for part in remote[1:].split("/"))) and
            not any(c.isspace() for c in path) and not any(c in path for c in ("\x00", "\\", "|")), "invalid remote path")
    return path


def load_v2():
    path = Path(__file__).resolve().parents[1] / "agent_gates_v2" / "verify_digests.py"
    spec = importlib.util.spec_from_file_location("workflow_v2_backend", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_identify = module.identify_snapshot

    def identify_with_jsonl(raw, path):
        # The frozen backend treats every leading object as one JSON document.
        # JSONL is a data format, never a way to hide a snapshot from its plan.
        if Path(path).suffix.lower() in {".jsonl", ".ndjson"}:
            require(len(raw) <= module.CONTROL_LIMIT, "JSONL exceeds 32 MiB: " + str(path))
            lines = [line for line in raw.splitlines() if line.strip()]
            require(lines, "JSONL is empty: " + str(path))
            for line in lines:
                obj = parse(line)
                require(isinstance(obj, dict), "JSONL rows must be objects: " + str(path))
                require(obj.get("schema") not in {module.snapshots.SCHEMA, module.snapshots.LEGACY_SCHEMA},
                        "snapshot document cannot be embedded in JSONL: " + str(path))
            return None
        return original_identify(raw, path)

    module.identify_snapshot = identify_with_jsonl
    return module


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_new(path, raw):
    """Create exactly one new artifact; existing paths, including links, fail."""
    try:
        with open(path, "xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        raise WorkflowError("no-clobber write failed: " + str(error), 2) from error


class Context:
    def __init__(self, root, manifest_path, manifest_sha256, contract_sha256, *, historical=False):
        self.root = Path(root).resolve()
        self.manifest_path = manifest_path
        self.manifest_sha256 = manifest_sha256
        self.contract_sha256 = contract_sha256
        self.manifest_raw = stable_read(safe_local(self.root, manifest_path), manifest_sha256)
        self.manifest = parse(self.manifest_raw)
        keys(self.manifest, ("schema", "chain", "gate", "stage", "contract", "objects", "steps", "acceptance"),
             ("snapshot_plan", "execution_permit", "history", "protection"))
        m = self.manifest
        require(m["schema"] == MANIFEST, "unsupported manifest schema")
        require(m["gate"] in GATE_STAGES, "unsupported gate")
        require(m["stage"] in GATE_STAGES[m["gate"]], "unsupported gate stage or round budget exceeded")
        require(m["gate"] == "B" or "execution_permit" not in m, "Gate A cannot consume an execution permit")
        text(m["chain"], "chain")
        contract_path = reference(self.root, m["contract"], "contract")
        require(m["contract"]["sha256"] == contract_sha256, "manifest cannot select another contract", 1)
        self.contract_raw = stable_read(contract_path, contract_sha256)
        self.contract = parse(self.contract_raw)
        keys(self.contract, ("schema", "chain", "gate", "authority", "source_catalog", "requirements", "allowed_hosts",
                             "write_roots", "forbidden_paths", "actors", "scope"),
             ("required_paths", "protected"))
        c = self.contract
        require(c["schema"] == CONTRACT and c["chain"] == m["chain"], "contract schema/chain mismatch")
        require(c["gate"] == m["gate"], "contract/manifest gate mismatch")
        require(c["scope"] in SCOPES, "unsupported record scope")
        require(c["scope"] != "scoped_addendum" or m["stage"] == "addendum", "addendum contract cannot grant another stage")
        for field in ("allowed_hosts", "write_roots", "forbidden_paths", "required_paths"):
            strings(c.get(field, []), field, nonempty=field in ("allowed_hosts", "write_roots"))
        require(all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", host) for host in c["allowed_hosts"]), "invalid host")
        for path in c["write_roots"] + c.get("required_paths", []):
            safe_local(self.root, path)
        for path in c["forbidden_paths"]:
            remote_path(path, c["allowed_hosts"]) if ":" in path else safe_local(self.root, path)
        keys(c["actors"], ("coordinator", "implementer", "reviewer"))
        for actor in c["actors"].values():
            text(actor, "actor identifier")
        require(c["actors"]["reviewer"] not in (c["actors"]["coordinator"], c["actors"]["implementer"]),
                "Reviewer must be a distinct declared actor")
        # Authority and catalog are fixed controls, not a self-generated list of
        # successful checks. No HTML or excluded object is opened here.
        self.authority_path = reference(self.root, c["authority"], "authority")
        self.forbidden(c["authority"]["path"])
        stable_read(self.authority_path, c["authority"]["sha256"])
        source = c["source_catalog"]
        keys(source, ("path", "sha256", "collection", "id_key"))
        source_ref = {key: source[key] for key in ("path", "sha256")}
        source_path = reference(self.root, source_ref, "source catalog")
        self.forbidden(source["path"])
        source_obj = parse(stable_read(source_path, source["sha256"]))
        text(source["collection"], "source collection")
        text(source["id_key"], "source id key")
        require(isinstance(source_obj, dict) and isinstance(source_obj.get(source["collection"]), list),
                "source acceptance collection is absent")
        source_rows = source_obj[source["collection"]]
        require(bool(source_rows), "source acceptance collection is empty")
        source_ids = []
        for row in source_rows:
            require(isinstance(row, dict), "source check must be an object")
            source_ids.append(text(row.get(source["id_key"]), "source check ID"))
        require(len(source_ids) == len(set(source_ids)), "duplicate source check IDs")
        require(isinstance(c["requirements"], list) and bool(c["requirements"]), "empty requirements are not acceptance")
        self.requirements = {}
        for item in c["requirements"]:
            keys(item, ("id", "stages", "na_stages", "required_roles", "required_objects"),
                 ("deferred_stages", "receipt_required"))
            ident = text(item["id"], "requirement ID")
            require(ident not in self.requirements, "duplicate requirement ID")
            for field in ("stages", "na_stages", "required_roles", "required_objects", "deferred_stages"):
                strings(item.get(field, []), field, nonempty=field == "stages")
            require(set(item["stages"]) <= GATE_STAGES[m["gate"]], "unknown required gate stage")
            require(set(item["na_stages"]) <= set(item["stages"]), "NA stages exceed requirement stages")
            require(set(item.get("deferred_stages", [])) <= set(item["stages"]), "deferred stages exceed requirement stages")
            require(set(item["required_roles"]) <= ROLES, "unknown required role")
            require(type(item.get("receipt_required", False)) is bool, "receipt_required must be boolean")
            self.requirements[ident] = item
        require(set(source_ids) == set(self.requirements), "required ID set differs from frozen source catalog", 1)
        self.objects = {}
        seen_paths = set()
        require(isinstance(m["objects"], list) and bool(m["objects"]), "empty object table")
        for obj in m["objects"]:
            keys(obj, ("id", "path", "role", "state", "preservation"), ("sha256", "producer"))
            ident = text(obj["id"], "object ID")
            require(ident not in self.objects, "duplicate object ID")
            self.path(obj["path"])
            canonical = self.canonical(obj["path"])
            require(canonical not in seen_paths, "duplicate object path or alias")
            seen_paths.add(canonical)
            require(obj["role"] in ROLES and obj["state"] in ("present", "planned"), "invalid object role/state")
            preservation = text(obj["preservation"], "preservation")
            require(preservation == "git" or preservation.startswith("git:") or
                    preservation.startswith("external:") and bool(preservation[9:].strip()), "invalid preservation")
            if preservation.startswith("git:"):
                safe_local(self.root, preservation[4:])
            require(not (":" in obj["path"] and preservation == "git"), "remote Git input needs explicit local mapping")
            if obj["state"] == "present":
                require(isinstance(obj.get("sha256"), str) and bool(HEX.fullmatch(obj["sha256"])), "present object needs SHA256")
                if ":" not in obj["path"]:
                    require(safe_local(self.root, obj["path"]).is_file(), "missing present object: " + obj["path"])
            else:
                require("sha256" not in obj, "planned output must not pretend to have a digest")
                require(obj["role"] in ("product", "evidence"), "planned run outputs must be products or evidence")
                text(obj.get("producer"), "output producer")
                self.output_path(obj["path"], must_be_new=not historical)
            self.objects[ident] = obj
        require(set(c.get("required_paths", [])) <= {obj["path"] for obj in self.objects.values()}, "required dependency path missing")
        self.steps = {}
        output_ids = set()
        require(isinstance(m["steps"], list), "steps must be a list")
        for step in m["steps"]:
            keys(step, ("id", "kind", "argv", "cwd", "host", "inputs", "outputs"))
            ident = text(step["id"], "step ID")
            require(ident not in self.steps, "duplicate step ID")
            require(step["kind"] in ("auxiliary", "scientific", "acceptance"), "unknown step kind")
            require(isinstance(step["argv"], list) and bool(step["argv"]) and
                    all(isinstance(arg, str) and "\x00" not in arg for arg in step["argv"]) and bool(step["argv"][0]), "invalid argv")
            require(step["host"] in c["allowed_hosts"], "undeclared execution host")
            if step["host"] == "local":
                require(safe_local(self.root, step["cwd"], allow_root=True).is_dir(), "step cwd missing")
            else:
                require(isinstance(step["cwd"], str) and step["cwd"].startswith("/") and
                        ".." not in step["cwd"].split("/"), "remote cwd must be absolute")
            for field in ("inputs", "outputs"):
                strings(step[field], field)
                require(set(step[field]) <= set(self.objects), "step references unknown object")
            for oid in step["inputs"]:
                require(self.objects[oid]["state"] == "present", "step input is not present")
            for oid in step["outputs"]:
                obj = self.objects[oid]
                require(oid not in output_ids, "output has multiple creators")
                require(obj["state"] == "planned" and obj.get("producer") == ident, "output creation ownership mismatch")
                output_ids.add(oid)
            self.steps[ident] = step
        require(output_ids == {ident for ident, obj in self.objects.items() if obj["state"] == "planned"}, "planned output has no creator")
        self.acceptance = {}
        require(isinstance(m["acceptance"], list), "acceptance must be a list")
        applicable = {ident for ident, req in self.requirements.items() if m["stage"] in req["stages"]}
        require(bool(applicable) or m["stage"] in ("prepare", "execute"), "stage has an empty acceptance set")
        for item in m["acceptance"]:
            keys(item, ("id", "status", "evidence"), ("reason", "receipts"))
            ident = text(item["id"], "acceptance ID")
            require(ident not in self.acceptance and ident in applicable, "duplicate or inapplicable acceptance ID")
            requirement = self.requirements[ident]
            strings(item["evidence"], "acceptance evidence")
            strings(item.get("receipts", []), "receipts")
            require(set(item["evidence"] + item.get("receipts", [])) <= set(self.objects), "unlisted acceptance evidence")
            for oid in item["evidence"] + item.get("receipts", []):
                require(self.objects[oid]["state"] == "present", "planned or missing evidence is not acceptance")
            if item["status"] == "evidenced":
                require(bool(item["evidence"]), "empty evidence is not acceptance")
                require(set(requirement["required_roles"]) <= {self.objects[oid]["role"] for oid in item["evidence"]}, "required evidence role missing")
                require(set(requirement["required_objects"]) <= set(item["evidence"]), "required acceptance artifact missing")
                require(not requirement.get("receipt_required") or bool(item.get("receipts")), "required execution receipt missing")
            elif item["status"] in ("na", "deferred"):
                field = "na_stages" if item["status"] == "na" else "deferred_stages"
                require(m["stage"] in requirement.get(field, []), "NA/defer was not authorized by frozen contract")
                text(item.get("reason"), "NA/defer reason")
            else:
                raise WorkflowError("unknown acceptance status; pending is not completed acceptance")
            self.acceptance[ident] = item
        require(set(self.acceptance) == applicable, "required acceptance coverage is incomplete", 1)
        if "snapshot_plan" in m:
            oid = m["snapshot_plan"]
            require(isinstance(oid, str) and oid in self.objects and self.objects[oid]["state"] == "present" and
                    ":" not in self.objects[oid]["path"], "snapshot plan must be a present local object")
        self._protection()
        self._history()

    def forbidden(self, path):
        alternatives = [path]
        if ":" not in path:
            resolved = safe_local(self.root, path).resolve()
            alternatives.append(str(resolved.relative_to(self.root)))
        require(not any(below(candidate, p) for p in self.contract["forbidden_paths"] for candidate in alternatives),
                "forbidden read/write path: " + path)

    def path(self, path):
        text(path, "object path")
        self.forbidden(path)
        if ":" in path:
            return remote_path(path, self.contract["allowed_hosts"])
        return safe_local(self.root, path)

    def canonical(self, path):
        return path if ":" in path else str(safe_local(self.root, path).resolve())

    def output_path(self, path, *, must_be_new=True):
        require(":" not in path, "v1 runner only creates local outputs; remote execution is not a hidden SSH action")
        self.path(path)
        require(any(below(path, prefix) for prefix in self.contract["write_roots"]), "output outside authorized write roots")
        require(not must_be_new or not os.path.lexists(safe_local(self.root, path)), "output already exists: " + path, 1)

    def _protection(self):
        required = self.contract.get("protected", [])
        declarations = self.manifest.get("protection", [])
        require(isinstance(required, list) and isinstance(declarations, list), "protection must be lists")
        expected = {}
        for item in required:
            keys(item, ("id", "path", "policy", "evidence_refs"))
            text(item["id"], "protected ID")
            require(item["id"] not in expected, "duplicate protected ID")
            self.path(item["path"])
            require(item["policy"] in ("frozen", "candidate_diff", "new_attempt"), "unknown protection policy")
            require(isinstance(item["evidence_refs"], list) and bool(item["evidence_refs"]), "protection needs frozen baseline/authorization references")
            for ref in item["evidence_refs"]:
                reference(self.root, ref, "protection anchor")
                self.forbidden(ref["path"])
            expected[item["id"]] = item
        seen = set()
        for item in declarations:
            keys(item, ("id", "policy", "evidence"))
            require(item["id"] in expected and item["id"] not in seen, "unknown/duplicate protected declaration")
            require(item["policy"] == expected[item["id"]]["policy"], "protection policy cannot change or refresh baseline")
            strings(item["evidence"], "protection evidence", nonempty=True)
            require(set(item["evidence"]) <= set(self.objects), "unlisted protection evidence")
            require(all(self.objects[oid]["state"] == "present" for oid in item["evidence"]), "missing protection evidence")
            declared = {(self.objects[oid]["path"], self.objects[oid]["sha256"]) for oid in item["evidence"]}
            require(all((ref["path"], ref["sha256"]) in declared for ref in expected[item["id"]]["evidence_refs"]),
                    "frozen protection anchor cannot be replaced or refreshed", 1)
            seen.add(item["id"])
        require(seen == set(expected), "protected scope coverage missing")

    def _history(self):
        history = self.manifest.get("history", [])
        require(isinstance(history, list), "history must be a list")
        previous = {}
        for item in history:
            keys(item, ("gate", "stage", "report"))
            require(item["gate"] == self.manifest["gate"], "historical gate mismatch")
            require(item["stage"] in GATE_STAGES[item["gate"]] and item["stage"] not in previous, "duplicate/unknown historical stage")
            path = reference(self.root, item["report"], "historical report")
            self.forbidden(item["report"]["path"])
            report = parse(stable_read(path, item["report"]["sha256"]))
            require(report.get("schema") == REVIEW and report.get("chain") == self.manifest["chain"] and
                    report.get("gate") == self.manifest["gate"] and
                    report.get("stage") == item["stage"] and report.get("reviewer_id") == self.contract["actors"]["reviewer"] and
                    report.get("contract_sha256") == self.contract_sha256, "historical review identity mismatch")
            previous[item["stage"]] = report
        predecessors = {"c1": "b1"} if self.manifest["gate"] == "A" else {"r1": "b1", "r2": "r1", "c2": "c1", "c3": "c2"}
        phase = ["b1", "c1"] if self.manifest["gate"] == "A" else ["b1", "r1", "r2"] if self.manifest["stage"] in PREEXEC else ["c1", "c2", "c3"]
        if self.manifest["stage"] in phase:
            earlier = set(phase[:phase.index(self.manifest["stage"])])
            require(set(previous) & set(phase) == earlier, "review history is incomplete or resets the round budget")
            if self.manifest["stage"] == "b1":
                require(not previous, "b1 cannot reset an existing review history")
        predecessor = predecessors.get(self.manifest["stage"])
        if predecessor:
            require(predecessor in previous and previous[predecessor].get("verdict") in
                    ("FAIL", "NEEDS_USER_DECISION", "NOT_VERIFIABLE"), "repair stage lacks an authenticated preceding non-PASS review")

    def permit(self, input_ids):
        require(self.manifest["gate"] == "B", "execution permission belongs only to Gate B")
        require("execution_permit" in self.manifest, "execution permission evidence missing")
        ref = self.manifest["execution_permit"]
        path = reference(self.root, ref, "execution permit")
        self.forbidden(ref["path"])
        permit = parse(stable_read(path, ref["sha256"]))
        require(permit.get("schema") == REVIEW and permit.get("stage") in PREEXEC and permit.get("verdict") == "PASS" and
                permit.get("gate") == "B" and
                permit.get("chain") == self.manifest["chain"] and
                permit.get("reviewer_id") == self.contract["actors"]["reviewer"] and
                permit.get("contract_sha256") == self.contract_sha256, "invalid pre-execution review")
        origin_path = text(permit.get("manifest_path"), "permit origin manifest path")
        self.forbidden(origin_path)
        origin = Context(self.root, origin_path, permit.get("manifest_sha256"), self.contract_sha256, historical=True)
        require(origin.manifest["gate"] == "B" and origin.manifest["stage"] in PREEXEC, "permit origin is not pre-execution Gate B")
        # Use the same full report/dispatch/coverage/finding validator. Historical
        # planned outputs may now exist; this does not authorize a new write.
        record(origin, ref["path"], ref["sha256"])
        bindings = permit.get("object_sha256", {})
        require(isinstance(bindings, dict) and all(oid in origin.objects and origin.objects[oid]["path"] == self.objects[oid]["path"] and
                origin.objects[oid].get("sha256") == bindings.get(oid) == self.objects[oid]["sha256"] for oid in input_ids),
                "execution input bytes not covered by permit", 1)
        approved_steps = {step["id"]: step for step in origin.manifest.get("steps", [])}
        for step in self.manifest.get("steps", []):
            if step["kind"] != "scientific":
                continue
            require(step["id"] in approved_steps and step == approved_steps[step["id"]],
                    "scientific command/host/cwd/input/output set differs from reviewed step", 1)
            for oid in step["outputs"]:
                require(oid in origin.objects and origin.objects[oid]["path"] == self.objects[oid]["path"] and
                        origin.objects[oid]["role"] == self.objects[oid]["role"] and
                        origin.objects[oid].get("producer") == self.objects[oid].get("producer"),
                        "scientific output route differs from reviewed step", 1)

    def receipt(self, obj):
        require(":" not in obj["path"], "receipt controls must be local")
        receipt = parse(stable_read(self.root / obj["path"], obj["sha256"]))
        require(receipt.get("schema") == RECEIPT and receipt.get("status") == "complete" and
                type(receipt.get("command_rc")) is int and receipt["command_rc"] == 0 and
                receipt.get("gate") == self.manifest["gate"] and
                receipt.get("chain") == self.manifest["chain"] and receipt.get("contract_sha256") == self.contract_sha256,
                "failed, incomplete or unrelated execution receipt", 1)
        indexed = {item["path"]: item for item in self.objects.values() if item["state"] == "present"}
        controls = [(receipt.get("manifest_path"), receipt.get("manifest_sha256"))]
        for stream in ("stdout", "stderr"):
            stream_ref = receipt.get(stream)
            require(isinstance(stream_ref, dict), "receipt has no raw " + stream)
            controls.append((stream_ref.get("path"), stream_ref.get("sha256")))
        for path, sha in controls:
            require(path in indexed and indexed[path]["sha256"] == sha, "receipt raw log/origin manifest is not an authenticated input", 1)
        origin_path, origin_sha = controls[0]
        origin = parse(stable_read(self.root / origin_path, origin_sha))
        require(origin.get("schema") == MANIFEST and origin.get("chain") == self.manifest["chain"] and
                origin.get("gate") == self.manifest["gate"] and
                origin.get("contract", {}).get("sha256") == self.contract_sha256, "receipt origin manifest mismatch")
        steps = [step for step in origin.get("steps", []) if step.get("id") == receipt.get("step")]
        require(len(steps) == 1, "receipt step not found in origin manifest")
        step = steps[0]
        require(receipt.get("argv") == step.get("argv") and receipt.get("host") == step.get("host"), "receipt command differs from approved origin")
        for field in ("inputs", "outputs"):
            refs = receipt.get(field)
            require(isinstance(refs, list) and len(refs) == len(step.get(field, [])), "receipt object set is incomplete")
            require({ref.get("id") for ref in refs} == set(step.get(field, [])), "receipt object IDs differ from origin")
            for ref in refs:
                oid = ref.get("id")
                require(oid in self.objects and self.objects[oid]["path"] == ref.get("path") and
                        self.objects[oid].get("sha256") == ref.get("sha256"), "receipt object binding mismatch", 1)

    def snapshot_scope(self, target, *, scan):
        """Check the complete declared traversal surface, without walking it.

        Excludes are deliberately not accepted as authority to cross a forbidden
        subtree. Metadata still enumerates names, so uses the same boundary.
        """
        text(target, "snapshot target")
        target = target.rstrip("/") or "/"
        if ":" in target:
            self.path(target)
            require(not scan or not any(below(p, target) for p in self.contract["forbidden_paths"]),
                    "snapshot traversal overlaps forbidden subtree: " + target)
            host = target.split(":", 1)[0]
            require(not scan or not any(p.startswith(host + ":") for p in self.contract["forbidden_paths"]),
                    "remote snapshot host has forbidden paths; remote ancestor aliases cannot be ruled out statically")
            return
        require(os.path.isabs(target), "local snapshot target must be absolute; implicit process cwd is not authorized")
        if os.path.isabs(target):
            absolute = Path(target)
            require(absolute.is_relative_to(self.root), "snapshot target escapes repository")
            target = str(absolute.relative_to(self.root))
        path = safe_local(self.root, target, allow_root=True)
        resolved = str(path.resolve().relative_to(self.root))
        alternatives = (target, resolved)
        for forbidden in self.contract["forbidden_paths"]:
            if ":" in forbidden:
                continue
            forbidden_resolved = str(safe_local(self.root, forbidden).resolve().relative_to(self.root))
            for boundary in (forbidden, forbidden_resolved):
                for candidate in alternatives:
                    require(not below(candidate, boundary), "forbidden snapshot target: " + target)
                    require(not scan or candidate != "." and not below(boundary, candidate),
                            "snapshot traversal overlaps forbidden subtree: " + forbidden)

    def snapshot_preflight(self, backend):
        """Authenticate small controls and validate every scan before file hashes."""
        oid = self.manifest.get("snapshot_plan")
        if oid is None:
            return None
        obj = self.objects[oid]
        plan = parse(stable_read(self.root / obj["path"], obj["sha256"]))
        require(plan.get("schema") == backend.PLAN_SCHEMA and isinstance(plan.get("entries"), list), "invalid snapshot plan")
        indexed = {obj["path"]: obj for obj in self.objects.values() if obj["state"] == "present"}
        seen = set()
        entries = []
        for entry in plan["entries"]:
            require(isinstance(entry, dict) and entry.get("baseline") in indexed and entry["baseline"] not in seen,
                    "snapshot baseline is missing or duplicate")
            baseline = indexed[entry["baseline"]]
            require(":" not in baseline["path"], "baseline controls must be local")
            require(entry.get("action") in ("verify", "historical"), "invalid snapshot action")
            if entry["action"] == "historical":
                text(entry.get("reason"), "historical snapshot reason")
            parsed = backend.snapshots.parse_snapshot(stable_read(self.root / baseline["path"], baseline["sha256"]))
            self.snapshot_scope(parsed["target"], scan=entry["action"] == "verify")
            entries.append((entry, baseline, parsed))
            seen.add(entry["baseline"])
        return entries

    def check(self, commit=None):
        """Verify the explicitly listed current content, never the whole spec tree."""
        backend = load_v2()
        cache = backend.ContentCache()
        controls = [(self.manifest_path, self.manifest_sha256),
                    (self.manifest["contract"]["path"], self.contract_sha256),
                    (self.contract["authority"]["path"], self.contract["authority"]["sha256"]),
                    (self.contract["source_catalog"]["path"], self.contract["source_catalog"]["sha256"])]
        for path, expected in controls:
            stable_read(self.root / path, expected)
        snapshot_entries = self.snapshot_preflight(backend)
        present = [obj for obj in self.objects.values() if obj["state"] == "present"]
        paths = [obj["path"] if ":" in obj["path"] else str(self.root / obj["path"]) for obj in present]
        if commit:
            backend.base.validate_commit(str(self.root), commit)
            for path, expected in controls:
                rel = backend.base.repository_path(str(self.root), str(self.root / path), source=True)
                require(cache.blob(str(self.root), commit, rel) == expected, "control is not bound to supplied commit: " + path, 1)
        bindings = {obj["id"]: backend.binding(str(self.root), path, obj["preservation"]) for obj, path in zip(present, paths)}
        values = cache.compute(paths)
        snapshots = {}
        for obj, path in zip(present, paths):
            require(values[path] == obj["sha256"], "object SHA256 mismatch: " + obj["id"], 1)
            rel = bindings[obj["id"]]
            if rel:
                kept = cache.blob(str(self.root), commit, rel) if commit else cache.local(str(self.root / rel))
                require(kept == obj["sha256"], "Git preservation mismatch: " + obj["id"], 1)
            if ":" not in obj["path"]:
                view = cache.snapshot_view(path)
                if view is not None:
                    snapshots[obj["path"]] = view
        require(not snapshots or self.manifest.get("snapshot_plan"), "snapshot inputs require an explicit plan")
        for item in self.acceptance.values():
            for oid in item.get("receipts", []):
                self.receipt(self.objects[oid])
        if snapshot_entries is not None:
            require({baseline["path"] for _, baseline, _ in snapshot_entries} == set(snapshots), "snapshot plan coverage is incomplete")
            for entry, baseline, parsed in snapshot_entries:
                require(snapshots[baseline["path"]] == parsed, "snapshot parsed bytes changed after preflight", 1)
                if entry["action"] == "verify":
                    self.snapshot_scope(parsed["target"], scan=True)
                    status, _, _ = backend.snapshots.compare_snapshot(str(self.root / baseline["path"]), baseline=parsed)
                    require(status.endswith("_OK"), "directory snapshot differs", 1)
        for path, expected in controls:
            stable_read(self.root / path, expected)
        return cache.stats


def render(ctx):
    m, c = ctx.manifest, ctx.contract
    lines = [f"# SCI 工作流派发 · {m['chain']} · 门 {m['gate']} · {m['stage']}", "", "仅进行本阶段授权范围内的独立审查；机械核验不代替科学判定。", "",
             f"阶段 manifest：{ctx.manifest_path}", f"manifest SHA256：{ctx.manifest_sha256}",
             f"冻结合同：{m['contract']['path']}", f"合同 SHA256：{ctx.contract_sha256}",
             f"独立 Reviewer：{c['actors']['reviewer']}", f"记录范围：{c['scope']}", "", "| 路径 | sha256 | 保全 | 角色 |", "|---|---|---|---|"]
    controls = [(ctx.manifest_path, ctx.manifest_sha256), (m["contract"]["path"], ctx.contract_sha256),
                (c["authority"]["path"], c["authority"]["sha256"]), (c["source_catalog"]["path"], c["source_catalog"]["sha256"])]
    controls += [(item["report"]["path"], item["report"]["sha256"]) for item in m.get("history", [])]
    if "execution_permit" in m:
        controls.append((m["execution_permit"]["path"], m["execution_permit"]["sha256"]))
    done = set()
    for path, sha in controls:
        if path not in done:
            lines.append(f"| {path} | {sha} | git | authority |")
            done.add(path)
    for obj in ctx.objects.values():
        if obj["state"] == "present" and obj["path"] not in done:
            lines.append(f"| {obj['path']} | {obj['sha256']} | {obj['preservation']} | {obj['role']} |")
            done.add(obj["path"])
    lines += ["", "## 必需验收覆盖", "", "| ID | 声明 | 证据对象 |", "|---|---|---|"]
    for ident, item in ctx.acceptance.items():
        lines.append(f"| {ident} | {item['status']} | {', '.join(item['evidence'])} |")
    lines += ["", "## 科学判定与交付", "", "逐项核实证据适用范围、真实边界与独立绑定方式；不得将未执行、空集或缺文件当作 NA。",
              "结构化报告使用 agent-gates.workflow-review.v1；记录 gate、manifest 路径及 manifest/contract SHA256、Reviewer、逐项判定、完整 finding 及本派发身份。",
              "门 A 仅 b1/c1（一次关闭复核）；门 B 的 b1/r1/r2 只授执行许可，c1/c2/c3 核结果。scoped_addendum 只能形成限定补证结论，不改变旧 PASS。", ""]
    return "\n".join(lines).encode("utf-8")


def run_step(ctx, step_id, receipt_dir):
    require(step_id in ctx.steps, "unknown step")
    step = ctx.steps[step_id]
    require(step["host"] == "local", "run v1 is local-only; it does not submit or wrap remote jobs", 2)
    if step["kind"] == "scientific":
        require(ctx.manifest["stage"] == "execute" and ctx.contract["scope"] == "full_chain", "scientific execution is forbidden in this stage/scope")
        ctx.permit(step["inputs"])
    ctx.output_path(receipt_dir)
    # Mechanical checks and overwrite detection finish before any child process.
    ctx.check()
    for oid in step["outputs"]:
        ctx.output_path(ctx.objects[oid]["path"])
    target = ctx.root / receipt_dir
    try:
        target.mkdir()
    except OSError as error:
        raise WorkflowError("receipt directory must be a new exclusive directory: " + str(error), 2) from error
    start = {"schema": RECEIPT, "chain": ctx.manifest["chain"], "gate": ctx.manifest["gate"], "stage": ctx.manifest["stage"], "step": step_id,
             "manifest_path": ctx.manifest_path, "manifest_sha256": ctx.manifest_sha256, "contract_sha256": ctx.contract_sha256,
             "argv": step["argv"], "cwd": str(ctx.root / step["cwd"]), "host": step["host"],
             "hostname": socket.gethostname(), "started_utc": utc(),
             "inputs": [{"id": oid, "path": ctx.objects[oid]["path"], "sha256": ctx.objects[oid]["sha256"]} for oid in step["inputs"]]}
    write_new(target / "start.json", (json.dumps(start, ensure_ascii=False, indent=2) + "\n").encode())
    rc, error_text = None, None
    try:
        with open(target / "stdout.bin", "xb") as stdout, open(target / "stderr.bin", "xb") as stderr:
            result = subprocess.run(step["argv"], cwd=ctx.root / step["cwd"], stdout=stdout, stderr=stderr, check=False)
        rc = result.returncode
    except (OSError, KeyboardInterrupt) as error:
        error_text = str(error)
        rc = 130 if isinstance(error, KeyboardInterrupt) else 127
    outputs = []
    complete = rc == 0
    integrity_failure = False
    cache = load_v2().ContentCache()
    for oid in step["outputs"]:
        obj = ctx.objects[oid]
        try:
            sha = cache.local(str(ctx.root / obj["path"]))
            if sha is not None:
                outputs.append({"id": oid, "path": obj["path"], "sha256": sha})
            else:
                complete = False
        except (OSError, RuntimeError) as error:
            complete = False
            error_text = str(error)
    try:
        ctx.check()
    except (WorkflowError, OSError, RuntimeError) as error:
        complete = False
        integrity_failure = True
        error_text = "post-run identity check: " + str(error)
    final = {**start, "finished_utc": utc(), "command_rc": rc, "status": "complete" if complete else "failed",
             "stdout": {"path": receipt_dir + "/stdout.bin", "sha256": cache.local(str(target / "stdout.bin"))},
             "stderr": {"path": receipt_dir + "/stderr.bin", "sha256": cache.local(str(target / "stderr.bin"))},
             "outputs": outputs, "error": error_text}
    write_new(target / "receipt.json", (json.dumps(final, ensure_ascii=False, indent=2) + "\n").encode())
    print(json.dumps({"receipt": receipt_dir + "/receipt.json", "command_rc": rc, "status": final["status"]}))
    return rc if rc and rc > 0 else 128 - rc if rc and rc < 0 else 0 if complete else 1 if integrity_failure else 3


def record(ctx, report_path, report_sha256):
    ctx.forbidden(report_path)
    report = parse(stable_read(safe_local(ctx.root, report_path), report_sha256))
    keys(report, ("schema", "chain", "gate", "stage", "manifest_path", "manifest_sha256", "contract_sha256", "reviewer_id", "verdict", "checks", "findings", "dispatch"),
         ("object_sha256", "dispatch_commit"))
    require(report["schema"] == REVIEW and report["chain"] == ctx.manifest["chain"] and report["gate"] == ctx.manifest["gate"] and
            report["stage"] == ctx.manifest["stage"] and report["manifest_path"] == ctx.manifest_path, "review scope mismatch")
    require(report["manifest_sha256"] == ctx.manifest_sha256 and report["contract_sha256"] == ctx.contract_sha256, "review does not bind current manifest/contract", 1)
    require(report["reviewer_id"] == ctx.contract["actors"]["reviewer"], "report is not from the designated independent Reviewer")
    require(report["verdict"] == "PASS", "a non-PASS review cannot produce a candidate positive record", 1)
    dispatch_path = reference(ctx.root, report["dispatch"], "dispatch")
    ctx.forbidden(report["dispatch"]["path"])
    require(stable_read(dispatch_path, report["dispatch"]["sha256"]) == render(ctx), "review dispatch is not the deterministic manifest projection", 1)
    if "dispatch_commit" in report:
        require(isinstance(report["dispatch_commit"], str) and bool(COMMIT.fullmatch(report["dispatch_commit"])), "invalid dispatch commit")
        backend = load_v2()
        backend.base.validate_commit(str(ctx.root), report["dispatch_commit"])
        require(backend.ContentCache().blob(str(ctx.root), report["dispatch_commit"], report["dispatch"]["path"]) == report["dispatch"]["sha256"], "dispatch commit mismatch", 1)
    require(isinstance(report["checks"], list) and bool(report["checks"]), "empty review checks")
    checked = set()
    for check in report["checks"]:
        keys(check, ("id", "verdict", "evidence"), ("reason",))
        ident = check["id"]
        require(ident in ctx.acceptance and ident not in checked, "unknown or duplicate review check")
        item = ctx.acceptance[ident]
        expected = {"evidenced": "PASS", "na": "NA", "deferred": "DEFERRED_BY_OWNER"}[item["status"]]
        require(check["verdict"] == expected, "review judgment contradicts supplied acceptance disposition", 1)
        strings(check["evidence"], "review evidence")
        require(set(check["evidence"]) == set(item["evidence"]), "review evidence differs from manifest")
        if expected != "PASS":
            text(check.get("reason"), "review NA/deferred reason")
        checked.add(ident)
    require(checked == set(ctx.acceptance), "Reviewer did not cover all required checks", 1)
    require(isinstance(report["findings"], list), "findings must be a list")
    seen = set()
    for finding in report["findings"]:
        keys(finding, ("id", "check", "severity", "location", "excerpt", "authority", "counterexample", "closure", "state", "owner"),
             ("decision_ref",))
        for field in ("id", "check", "location", "excerpt", "authority", "counterexample", "closure", "owner"):
            text(finding[field], "finding " + field)
        require(finding["id"] not in seen, "duplicate finding")
        seen.add(finding["id"])
        require(finding["severity"] in ("BLOCKER", "MAJOR", "MINOR", "OPINION"), "unknown finding severity")
        require(finding["state"] in ("OPEN", "CLOSED", "ACCEPTED_LIMITATION", "REFUTED"), "unknown finding state")
        require(finding["state"] in ("CLOSED", "REFUTED") or finding["severity"] in ("MINOR", "OPINION"), "open blocking finding", 1)
        if finding["state"] == "REFUTED":
            require("decision_ref" in finding, "REFUTED requires a pinned Decision Owner ruling")
            path = reference(ctx.root, finding["decision_ref"], "refutation decision")
            ctx.forbidden(finding["decision_ref"]["path"])
            stable_read(path, finding["decision_ref"]["sha256"])
    ctx.check(report.get("dispatch_commit"))
    stage = ctx.manifest["stage"]
    if ctx.contract["scope"] == "scoped_addendum":
        kind = "SCOPED_ADDENDUM"
    elif ctx.manifest["gate"] == "A":
        require(all(item["status"] != "deferred" for item in ctx.acceptance.values()), "deferred acceptance forbids whole-chain PASS", 1)
        kind = "CANDIDATE_PASS"
    elif stage in PREEXEC:
        kind = "CANDIDATE_EXECUTION_PERMISSION"
    else:
        require(stage in RESULT, "this stage cannot generate a candidate gate PASS")
        require(all(item["status"] != "deferred" for item in ctx.acceptance.values()), "deferred acceptance forbids whole-chain PASS", 1)
        ctx.permit([oid for oid, obj in ctx.objects.items() if obj["role"] in ("candidate", "dependency", "scientific_input") and obj["state"] == "present"])
        kind = "CANDIDATE_PASS"
    lines = [f"## {kind} · {ctx.manifest['chain']} · 门 {ctx.manifest['gate']} · {stage}", "", "这是独立审查凭据通过机械核验后的候选追加片段；不是工具作出的科学判定，也不改写旧 PASS 或旧报告。",
             "提交与正式登记仍须遵守 SCI 协议；限定补证不得解释为整链 PASS。", "", "| 对象 | sha256 |", "|---|---|"]
    bindings = [(ctx.manifest_path, ctx.manifest_sha256), (ctx.manifest["contract"]["path"], ctx.contract_sha256),
                (report_path, report_sha256), (report["dispatch"]["path"], report["dispatch"]["sha256"])]
    bindings += [(obj["path"], obj["sha256"]) for obj in ctx.objects.values() if obj["state"] == "present"]
    done = set()
    for path, sha in bindings:
        if path not in done:
            lines.append(f"| {path} | {sha} |")
            done.add(path)
    lines += ["", "| finding | check | severity | 状态 | owner | 依据 |", "|---|---|---|---|---|---|"]
    for finding in report["findings"]:
        cells = [finding[key].replace("|", "\\|").replace("\n", "<br>").replace("\r", "")
                 for key in ("id", "check", "severity", "state", "owner", "authority")]
        lines.append("| " + " | ".join(cells) + " |")
    return ("\n".join(lines) + "\n").encode("utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("render", "check", "run", "record"))
    parser.add_argument("manifest")
    parser.add_argument("--root", default=".")
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--out")
    parser.add_argument("--commit")
    parser.add_argument("--step")
    parser.add_argument("--receipt-dir")
    parser.add_argument("--review")
    parser.add_argument("--review-sha256")
    args = parser.parse_args(argv)
    try:
        allowed = {"render": {"out"}, "check": {"commit"}, "run": {"step", "receipt_dir"},
                   "record": {"out", "review", "review_sha256"}}[args.command]
        supplied = {name for name in ("out", "commit", "step", "receipt_dir", "review", "review_sha256")
                    if getattr(args, name) is not None}
        require(supplied <= allowed, "option is not applicable to this command", 2)
        required = {"render": {"out"}, "check": set(), "run": {"step", "receipt_dir"},
                    "record": {"out", "review", "review_sha256"}}[args.command]
        require(required <= supplied, "required command options are missing", 2)
        ctx = Context(args.root, args.manifest, args.manifest_sha256, args.contract_sha256)
        if args.command in ("render", "record"):
            require(args.out is not None, "--out is required")
            ctx.output_path(args.out)
        if args.command == "render":
            write_new(ctx.root / args.out, render(ctx))
        elif args.command == "check":
            print(json.dumps({"result": "MECHANICAL_OK", "gate": ctx.manifest["gate"], "stage": ctx.manifest["stage"], "scope": ctx.contract["scope"], "stats": ctx.check(args.commit)}))
        elif args.command == "run":
            require(args.step and args.receipt_dir, "run needs --step and --receipt-dir")
            return run_step(ctx, args.step, args.receipt_dir)
        else:
            require(args.review and args.review_sha256, "record needs --review and --review-sha256")
            write_new(ctx.root / args.out, record(ctx, args.review, args.review_sha256))
        return 0
    except WorkflowError as error:
        print(f"WORKFLOW_ERROR: {error}", file=sys.stderr)
        return error.code
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"TOOL_ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
