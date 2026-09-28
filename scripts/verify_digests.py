#!/usr/bin/env python3
"""SCI 核验：表列内容身份与显式目录巡检分离。

退出码沿用 0 PASS / 1 FAIL / 2 TOOL_ERROR / 3 NOT_VERIFIABLE。
运行位置：待核项目的仓库根目录。仅 write/snapshot 写指定目标。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time

sys.dont_write_bytecode = True
import digest_table as table
import snapshots

PASS, FAIL, TOOL_ERROR, NOT_VERIFIABLE = 0, 1, 2, 3
NAMES = {0: "PASS", 1: "FAIL", 2: "TOOL_ERROR", 3: "NOT_VERIFIABLE"}
PLAN_SCHEMA = "agent-gates.snapshot-plan.v2"


def signature(s):
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


class ContentCache:
    """仅本次调用缓存；元数据变化失效，不提供跨运行内容身份保证。"""
    def __init__(self):
        self.values = {}
        self.snapshot_views = {}
        self.blobs = {}
        self.remote = {}
        self.stats = {"content_files": 0, "content_bytes": 0, "cache_hits": 0,
                      "git_blobs": 0, "git_blob_bytes": 0}

    def local(self, path):
        try:
            before = os.stat(path)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(before.st_mode):
            return None
        key = (os.path.realpath(path), signature(before))
        if key in self.values:
            self.stats["cache_hits"] += 1
            return self.values[key]
        h = hashlib.sha256()
        prefix = bytearray()
        with open(path, "rb") as f:
            if signature(os.fstat(f.fileno())) != signature(before):
                raise RuntimeError(f"读取前文件被替换：{path}")
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
                if len(prefix) < CONTROL_LIMIT + 1:
                    prefix.extend(block[:CONTROL_LIMIT + 1 - len(prefix)])
            if signature(os.fstat(f.fileno())) != signature(before):
                raise RuntimeError(f"摘要读取期间文件发生变化：{path}")
        if signature(os.stat(path)) != signature(before):
            raise RuntimeError(f"摘要读取期间路径发生变化：{path}")
        self.snapshot_views[key] = identify_snapshot(bytes(prefix), path)
        self.values[key] = h.hexdigest()
        self.stats["content_files"] += 1
        self.stats["content_bytes"] += before.st_size
        return self.values[key]

    def snapshot_view(self, path):
        key = (os.path.realpath(path), signature(os.stat(path)))
        if key not in self.snapshot_views:
            raise RuntimeError(f"文件在身份核验后发生变化：{path}")
        return self.snapshot_views[key]

    def blob(self, root, commit, rel):
        key = (root, commit, rel)
        if key not in self.blobs:
            out = subprocess.run(["git", "-C", root, "cat-file", "blob", f"{commit}:{rel}"],
                                 capture_output=True, check=False)
            self.blobs[key] = hashlib.sha256(out.stdout).hexdigest() if out.returncode == 0 else None
            self.stats["git_blobs"] += 1
            self.stats["git_blob_bytes"] += len(out.stdout)
        return self.blobs[key]

    def compute(self, paths):
        result, hosts = {}, {}
        for path in dict.fromkeys(paths):
            host, p = table.split_remote(path)
            if host is None:
                result[path] = self.local(p)
            else:
                hosts.setdefault(host, []).append((path, p))
        for host, pairs in hosts.items():
            # 不跨多份文档复用可变远端路径的旧观测；仅本批按路径去重。
            fresh = pairs
            for batch in remote_batches([p for _, p in fresh]):
                got = remote_content(host, batch)
                for item in got:
                    self.remote[(host, item["path"])] = item.get("sha256")
                    if item.get("sha256"):
                        self.stats["content_files"] += 1
                        self.stats["content_bytes"] += item["size"]
            for path, p in pairs:
                result[path] = self.remote[(host, p)]
        return result


REMOTE_CONTENT = r'''
import hashlib,json,os,stat,sys
def sig(s): return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
out=[]
for p in json.load(sys.stdin):
    try: before=os.stat(p)
    except FileNotFoundError:
        out.append({'path':p,'sha256':None}); continue
    if not stat.S_ISREG(before.st_mode):
        out.append({'path':p,'sha256':None}); continue
    h=hashlib.sha256()
    with open(p,'rb') as f:
        if sig(os.fstat(f.fileno()))!=sig(before): raise RuntimeError('file replaced: '+p)
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
        if sig(os.fstat(f.fileno()))!=sig(before): raise RuntimeError('file changed: '+p)
    if sig(os.stat(p))!=sig(before): raise RuntimeError('path changed: '+p)
    out.append({'path':p,'sha256':h.hexdigest(),'size':before.st_size})
json.dump(out,sys.stdout)
'''


def remote_batches(paths):
    batch = []
    for p in paths:
        proposed = batch + [p]
        if len(json.dumps(proposed).encode()) > 65536:
            if not batch:
                raise RuntimeError("单路径超过远端请求预算")
            yield batch
            batch = [p]
            if len(json.dumps(batch).encode()) > 65536:
                raise RuntimeError("单路径超过远端请求预算")
        else:
            batch = proposed
    if batch:
        yield batch


def remote_content(host, paths):
    command = "python3 -c " + shlex.quote(REMOTE_CONTENT)
    if len(command.encode()) > 65536:
        raise RuntimeError("SSH 命令超过 64 KiB")
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
                       input=json.dumps(paths), capture_output=True, text=True, timeout=900)
    if r.returncode:
        raise RuntimeError(f"ssh {host} 内容核验失败 rc={r.returncode}: {r.stderr[-500:]}")
    values = json.loads(r.stdout)
    if not isinstance(values, list) or [x.get("path") for x in values] != paths:
        raise RuntimeError("远端结果不完整、重复或顺序不符")
    for x in values:
        if x.get("sha256") is not None and (not table.HEX.fullmatch(x["sha256"]) or
                                           not isinstance(x.get("size"), int)):
            raise RuntimeError("远端摘要格式错误")
    return values


CONTROL_LIMIT = 32 * 1024 * 1024


def read_control(path, limit=CONTROL_LIMIT):
    with open(path, "rb") as f:
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"控制文件/对象形 JSON 超过 {limit} 字节预算：{path}")
    return raw


def snapshot_bytes(path):
    """完整识别对象形 JSON，键顺序不影响计划覆盖；不访问目标目录。"""
    with open(path, "rb") as f:
        prefix = b""
        while not prefix.strip() and len(prefix) <= CONTROL_LIMIT:
            chunk = f.read(512)
            if not chunk:
                return None
            prefix += chunk
    if len(prefix) > CONTROL_LIMIT:
        raise ValueError(f"文件头空白超过识别预算：{path}")
    head = prefix.lstrip()
    if head.startswith((b"# snapshot ", b"{", b"\xef\xbb\xbf")):
        raw = read_control(path)
        return raw if identify_snapshot(raw, path) is not None else None
    return None


def identify_snapshot(raw, path):
    head = raw.lstrip()
    if not head.startswith((b"# snapshot ", b"{", b"\xef\xbb\xbf")):
        return None
    if len(raw) > CONTROL_LIMIT:
        raise ValueError(f"控制文件/对象形 JSON 超过 {CONTROL_LIMIT} 字节预算：{path}")
    if head.startswith(b"# snapshot "):
        return snapshots.parse_snapshot(raw)
    obj = snapshots._loads(raw.decode("utf-8-sig"))
    if isinstance(obj, dict) and obj.get("schema") == snapshots.SCHEMA:
        return snapshots.parse_snapshot(raw)
    return None


class IntegrityMismatch(ValueError):
    pass


def authenticated(raw, path, listed):
    matching = [v for p, v in listed.items() if table.split_remote(p)[0] is None
                and os.path.abspath(p) == os.path.abspath(path)]
    actual = hashlib.sha256(raw).hexdigest()
    if not matching or any(v != actual for v in matching):
        raise IntegrityMismatch(f"MISMATCH 控制文件身份不符：{path}")
    return raw



def worst(codes):
    return FAIL if FAIL in codes else max(codes, default=PASS)


def binding(root, path, declaration):
    host, _ = table.split_remote(path)
    if declaration.startswith("external:") and declaration[len("external:"):].strip():
        return None
    if declaration == "git" and host is None:
        return table.repository_path(root, path, source=True)
    if declaration.startswith("git:"):
        return table.repository_path(root, declaration[len("git:"):])
    raise ValueError("保全须为 git / git:<本地路径> / external:<依据>；远端须显式映射")


def patch_touched_bytes(raw_bytes: bytes) -> set[str]:
    r"""从一份 git patch 取出**真正有改动**的路径。

    「声称已修 / 已改写 N 处」是本语料里最密的失效模式（跨三条链复发：声明的修复不在
    字节中、3 处只改 1 处却称已改、同字节重写冒充更正）。同字节重写产不出 diff 段，
    因此「在 patch 里出现」正是「真改过」的机械判据。"""
    def unquote_git(value: str) -> str:
        if not (value.startswith('"') and value.endswith('"')):
            return value
        # Git 的 core.quotePath 会把非 ASCII 路径写成 UTF-8 字节的八进制转义。
        escapes = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11,
                   "f": 12, "r": 13, '"': 34, "\\": 92}
        body, raw, i = value[1:-1], bytearray(), 0
        while i < len(body):
            if body[i] == "\\" and i + 1 < len(body):
                octal = re.match(r"[0-7]{3}", body[i + 1:])
                if octal:
                    raw.append(int(octal.group(), 8)); i += 4; continue
                if body[i + 1] in escapes:
                    raw.append(escapes[body[i + 1]]); i += 2; continue
            raw.extend(body[i].encode("utf-8")); i += 1
        return raw.decode("utf-8", errors="surrogateescape")

    def add(value: str) -> None:
        value = unquote_git(value)
        if value.startswith(("a/", "b/")):
            touched.add(value[2:])

    touched: set[str] = set()
    in_hunk = False
    for line in raw_bytes.decode("utf-8", errors="replace").splitlines(keepends=True):
        m = re.match(r'^diff --git ("(?:\\.|[^"\\])*"|a/.*?) ("(?:\\.|[^"\\])*"|b/.*?)\n?$', line)
        if m:
            in_hunk = False
            add(m.group(1)); add(m.group(2)); continue
        if line.startswith("@@"):
            in_hunk = True
        if not in_hunk and line.startswith(("--- ", "+++ ")):
            add(line[4:].rstrip("\n").split("\t", 1)[0])
    return touched


def load_plan(path, listed, hints):
    if path is None:
        if hints:
            raise ValueError("存在快照行但未提供 --snapshot-plan；未执行目录巡检")
        return []
    canonical = lambda p: os.path.abspath(p)
    allowed = {canonical(p) for p in listed if table.split_remote(p)[0] is None}
    if canonical(path) not in allowed:
        raise ValueError("snapshot-plan 本身必须列入同一派发摘要表")
    raw = authenticated(read_control(path, 1024 * 1024), path, listed)
    obj = snapshots._loads(raw.decode("utf-8"))
    if not isinstance(obj, dict) or obj.get("schema") != PLAN_SCHEMA or not isinstance(obj.get("entries"), list):
        raise ValueError("snapshot-plan schema 错误")
    seen = set()
    for e in obj["entries"]:
        if not isinstance(e, dict) or not isinstance(e.get("baseline"), str):
            raise ValueError("plan 缺 baseline")
        p = canonical(e["baseline"])
        if table.split_remote(e["baseline"])[0] is not None or p not in allowed or p in seen:
            raise ValueError("plan baseline 未列入派发、重复或不是本地清单")
        seen.add(p)
        if e.get("action") not in ("verify", "historical"):
            raise ValueError("plan action 必须为 verify/historical")
        if e["action"] == "historical" and (not isinstance(e.get("reason"), str) or not e["reason"].strip()):
            raise ValueError("历史快照须有明确理由")
    if not {canonical(p) for p in hints} <= seen:
        raise ValueError("snapshot-plan 遗漏派发中的快照")
    return obj["entries"]


def check_one(md, args, cache):
    print(f"### verify_digests check · {md}")
    if not Path(md).is_file():
        print("MISSING_FILE"); return NOT_VERIFIABLE
    raw = Path(md).read_bytes()
    text = raw.decode("utf-8")
    root = table.git_repository(md) if args.precommit or args.commit else None
    if args.commit:
        table.validate_commit(root, args.commit)
        kept = cache.blob(root, args.commit, table.repository_path(root, md, source=True))
        if kept != hashlib.sha256(raw).hexdigest():
            print("DISPATCH_COMMIT_MISMATCH：未消费派发白名单"); return FAIL
        print("DISPATCH_COMMIT_OK")
    rows, skipped, fenced = table.parse_rows(text, args.last, args.digest_col)
    lines = text.splitlines()
    codes, targets, bindings, hints = [], {}, {}, []
    snapshot_raw, baselines = {}, {}
    if fenced:
        print(f"围栏内 {fenced} 行未参与核验")
    if not rows:
        print("NO_DIGEST_TABLE"); codes.append(NOT_VERIFIABLE)
    for i, p, v, why in skipped:
        print(f"STATIC_ERROR line={i+1} {p}: {why}"); codes.append(NOT_VERIFIABLE)
    if args.last_heading:
        regex = re.compile(args.last_heading)
        head = next((l for l in reversed(lines[:rows[0][0]]) if l.startswith("#")), "") if rows else ""
        if not regex.search(head):
            print("LAST_TABLE_NOT_EXPECTED_SECTION"); codes.append(NOT_VERIFIABLE)
    for i, path, value in rows:
        if table.PLACEHOLDER.fullmatch(value):
            print(f"UNFILLED {path}"); codes.append(NOT_VERIFIABLE)
        elif not table.HEX.fullmatch(value):
            print(f"BAD_DIGEST {path}"); codes.append(FAIL)
        if path in targets and targets[path] != value:
            print(f"CONFLICTING_DIGEST {path}"); codes.append(FAIL)
        targets[path] = value
        host, p = table.split_remote(path)
        if host is None:
            if not os.path.isfile(p):
                print(f"MISSING_OR_NOT_FILE {path}"); codes.append(NOT_VERIFIABLE)
            else:
                try:
                    snap_raw = snapshot_bytes(p)
                    if snap_raw is not None:
                        hints.append(path)
                        snapshot_raw[os.path.abspath(path)] = snap_raw
                except (ValueError, RuntimeError, OSError) as e:
                    print(f"SNAPSHOT_FORMAT_ERROR {path}: {e}"); codes.append(NOT_VERIFIABLE)
        if root:
            cells = table.split_cells(lines[i])
            declaration = cells[args.digest_col].strip("` ") if len(cells) > args.digest_col else ""
            try:
                rel = binding(root, path, declaration)
                bindings[path] = rel
                if rel and args.precommit and not os.path.isfile(os.path.join(root, rel)):
                    print(f"MISSING_PRECOMMIT_TARGET {rel}"); codes.append(NOT_VERIFIABLE)
            except ValueError as e:
                print(f"BAD_PRESERVATION {path}: {e}"); codes.append(NOT_VERIFIABLE)
    for req in args.require:
        if req not in targets:
            print(f"MISSING_REQUIRED_ROW {req}"); codes.append(NOT_VERIFIABLE)
    if args.claimed_changed:
        if not args.diff or not Path(args.diff).is_file():
            print("TOOL_ERROR: claimed-changed 缺有效 diff"); codes.append(TOOL_ERROR)
        else:
            listed_local = {os.path.abspath(p) for p in targets if table.split_remote(p)[0] is None}
            if os.path.abspath(args.diff) not in listed_local:
                print("MISSING_REQUIRED_ROW: diff 必须列入派发摘要表")
                codes.append(NOT_VERIFIABLE)
            try:
                diff_raw = authenticated(read_control(args.diff), args.diff, targets)
                touched = patch_touched_bytes(diff_raw)
            except (ValueError, OSError) as e:
                print(f"DIFF_IDENTITY_ERROR: {e}"); codes.append(FAIL if isinstance(e, IntegrityMismatch) else NOT_VERIFIABLE); touched = set()
            for p in args.claimed_changed:
                if p not in touched:
                    print(f"CLAIMED_BUT_NOT_IN_DIFF {p}"); codes.append(FAIL)
    try:
        entries = load_plan(args.snapshot_plan, targets, hints)
        for e in entries:
            path = e["baseline"]
            raw_baseline = snapshot_raw.get(os.path.abspath(path))
            if raw_baseline is None:
                raw_baseline = read_control(path)
            authenticated(raw_baseline, path, targets)
            baselines[path] = snapshots.parse_snapshot(raw_baseline)
    except (ValueError, RuntimeError, OSError) as e:
        print(f"SNAPSHOT_PLAN_ERROR: {e}"); codes.append(FAIL if isinstance(e, IntegrityMismatch) else NOT_VERIFIABLE); entries = []
    if codes:
        print("内容摘要和目录巡检未执行：静态预检失败")
        return worst(codes)

    start = time.monotonic()
    values = cache.compute(targets)
    print("| 文件 | 记录 sha256 | 实算 sha256 | 结果 |")
    print("|---|---|---|---|")
    for path, expected in targets.items():
        actual = values[path]
        code = NOT_VERIFIABLE if actual is None else PASS if actual == expected else FAIL
        label = "MISSING" if actual is None else "OK" if code == PASS else "MISMATCH"
        rel = bindings.get(path)
        if rel:
            kept = cache.blob(root, args.commit, rel) if args.commit else cache.local(os.path.join(root, rel))
            if kept is None or kept != expected:
                code = FAIL; label += "; PRESERVATION_MISMATCH"
            else:
                label += "; COMMIT_BLOB_OK" if args.commit else "; PRECOMMIT_TARGET_OK"
        print(f"| {path} | {expected} | {actual or '—'} | {label} |")
        codes.append(code)
    print(f"content_elapsed_seconds={time.monotonic()-start:.3f}")
    if worst(codes):
        print("目录巡检未执行：表列文件身份未通过")
        return worst(codes)
    # 静态预检的“非快照”观察也可能过时；这里用计算SHA的同一次读取重核覆盖。
    # 不从路径重读内容，不允许分类期间的文件替换绕过计划要求。
    planned = {os.path.abspath(e["baseline"]) for e in entries}
    for path in targets:
        if table.split_remote(path)[0] is None:
            observed = cache.snapshot_view(path)
            if observed is not None and os.path.abspath(path) not in planned:
                print(f"SNAPSHOT_PLAN_ERROR: authenticated snapshot omitted from plan: {path}")
                return NOT_VERIFIABLE
    # 快照及plan只有在本轮内容身份全部通过后才可消费；历史项永不访问原目录。
    directory_count = 0
    for e in entries:
        baseline = e["baseline"]
        if e["action"] == "historical":
            print(f"HISTORICAL_SNAPSHOT {baseline}：仅核清单字节；{e['reason']}")
            continue
        status, details, stats = snapshots.compare_snapshot(baseline, baseline=baselines[baseline])
        directory_count += 1
        print(f"{status} {baseline} {json.dumps(stats, ensure_ascii=False, sort_keys=True)}")
        for detail in details:
            print("  " + detail)
        if status.endswith("_DIFF"):
            codes.append(FAIL)
    print(f"scope: 表列内容身份已核；现场目录巡检 {directory_count} 项；历史快照 {len(entries)-directory_count} 项")
    return worst(codes)


def cmd_write(md, cache):
    p = Path(md)
    raw = p.read_bytes()
    text = raw.decode("utf-8")
    rows, skipped, _ = table.parse_rows(text)
    if not rows or skipped:
        print(f"NOT_VERIFIABLE: 无表或存在未解析行 {skipped}"); return NOT_VERIFIABLE
    pending = [(i, path, value) for i, path, value in rows if table.PLACEHOLDER.fullmatch(value)]
    if not pending:
        print("write: 0 占位符；未读输入，文件字节保持不变"); return PASS
    state = table.git_state(md)
    if state == "clean":
        print("REFUSED: 已提交且未改动的文档不可回填"); return FAIL
    if state == "?":
        raise RuntimeError("Git 不可用，无法判断冻结状态")
    values = cache.compute([x[1] for x in pending])
    missing = [path for _, path, _ in pending if values[path] is None]
    if missing:
        print(f"MISSING: {missing}；未改文档"); return NOT_VERIFIABLE
    lines = text.splitlines(keepends=True)
    for i, path, value in pending:
        cells = lines[i].split("|", 3)
        cells[2] = cells[2].replace(value, values[path])
        lines[i] = "|".join(cells)
    if p.read_bytes() != raw:
        raise RuntimeError("文档在回填期间被其他进程改动；拒绝覆盖")
    p.write_bytes("".join(lines).encode("utf-8"))
    print(f"write: 填入 {len(pending)} 行；保留已有值 {len(rows)-len(pending)} 行")
    return PASS


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check")
    c.add_argument("files", nargs="+")
    c.add_argument("--last", action="store_true")
    c.add_argument("--last-heading")
    c.add_argument("--require", default="")
    c.add_argument("--digest-col", type=int, default=2)
    modes = c.add_mutually_exclusive_group()
    modes.add_argument("--precommit", action="store_true")
    modes.add_argument("--commit")
    c.add_argument("--claimed-changed", default="")
    c.add_argument("--diff")
    c.add_argument("--snapshot-plan")
    w = sub.add_parser("write"); w.add_argument("file")
    s = sub.add_parser("snapshot")
    s.add_argument("target"); s.add_argument("-o", "--out", required=True)
    s.add_argument("--mode", choices=("content", "metadata"), default="content")
    s.add_argument("--exclude", default="")
    a = parser.parse_args(argv)
    cache = ContentCache()
    started = time.monotonic()
    code = TOOL_ERROR
    try:
        if a.command == "write":
            code = cmd_write(a.file, cache)
        elif a.command == "snapshot":
            result = snapshots.write_snapshot(a.target, a.out, a.mode, [x for x in a.exclude.split(",") if x])
            print(f"snapshot: {a.mode} {a.target} → {a.out}")
            if result is not None:
                print(json.dumps(result.get("stats", {}), ensure_ascii=False))
            code = PASS
        else:
            a.require = [x.strip() for x in a.require.split(",") if x.strip()]
            a.claimed_changed = [x.strip() for x in a.claimed_changed.split(",") if x.strip()]
            if a.digest_col < 2 or (a.require and len(a.files) != 1) or (a.last_heading and not a.last):
                raise ValueError("digest-col须≥2；require仅适用单文件；last-heading须配last")
            if a.snapshot_plan and len(a.files) != 1:
                raise ValueError("snapshot-plan 仅适用单个派发文件")
            if a.last_heading:
                re.compile(a.last_heading)
            code = worst([check_one(md, a, cache) for md in a.files])
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, re.error, subprocess.SubprocessError) as e:
        print(f"TOOL_ERROR: {type(e).__name__}: {e}")
        code = TOOL_ERROR
    print("metrics: " + json.dumps({**cache.stats, "elapsed_seconds": round(time.monotonic()-started, 3)}, sort_keys=True))
    print(f"verify_digests: {NAMES[code]}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
