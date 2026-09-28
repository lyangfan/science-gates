#!/usr/bin/env python3
"""verify_digests —— AGENT GATE PROTOCOL §8 的 digest 核验工具。

一个解析器管三种 markdown 表：派发文件、账本里的 PASS 记录、spec 的冻结输入表。
它们是同一种东西：第一列路径（本地 `path` 或远端 `host:path`），第二列 sha256
（或占位符 `<脚本填>`）。第四类「目录快照」是 TSV（`sha256<TAB>相对路径`，两列顺序相反），
由上述表**引用**后顺带重列目录比对，不能直接 `check`。

子命令
  check <md>... [--last]        逐行实算比对。远端按 host 和命令字节预算分批 ssh sha256sum；仓库内文件
                                另报 git 状态（clean / dirty / untracked）；被引用的快照文件
                                （首行 `# snapshot …`）额外重新列目录，打印增 / 删 / 改。
        --precommit             SCI 提交前预检：digest 后一列须声明 git / git:<仓库路径> /
                                external:<依据>；核当前字节及本地映射，不证明已经入库。
        --commit <完整 SHA>     SCI 审查：另核固定提交的 blob 及派发本身，完全不依赖 HEAD
                                clean 状态。两参数互斥；均省略则保留 ENG 的 subject 副本规则。
  write <md>                    把实算值填进表格里的占位符 `<脚本填>`；已有 64-hex 值一律不动
                                （账本里的旧 PASS 记录必须保留）。文件已入库且与 HEAD 一致
                                （= 已冻结）则拒绝写入 —— 更正须新建文件。
  snapshot <dir/|host:dir/> -o <file>   生成禁写目录的基线清单。

退出码：0 PASS · 1 FAIL · 2 TOOL_ERROR（ssh / 权限）· 3 NOT_VERIFIABLE（缺文件 / 无表 /
表内有未解析行）。同一次 check 里 FAIL 与 NOT_VERIFIABLE 并存时取 FAIL —— 字节不符是确证，
不得被「缺文件」吸收成不消耗轮次的 NOT_VERIFIABLE。
只读：除 `write` 与 `snapshot -o` 的目标外不写任何文件；远端只执行只读的
`sha256sum` / `find | sort | xargs sha256sum` 管道。
不截断 digest：实测中一次伪造值与真值共同前缀恰好 16 位，截断会把「不同」显示成「相同」。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import fnmatch
import os
import posixpath
import re
import shlex
import subprocess
import sys

PASS, FAIL, TOOL_ERROR, NOT_VERIFIABLE = 0, 1, 2, 3
HEX = re.compile(r"^[0-9a-f]{64}$")
ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|")   # 仅 cmd_write 回填第二列时用
PLACEHOLDER = re.compile(r"^<脚本填[^>]*>$")          # 只认这一种占位符；空单元格与 <其它> 不是 digest 行
PATHLIKE = re.compile(r"^[^\s|]+$")                    # 路径：无空白；且须含 / 或 .（见 parse_rows）
DIGEST_LIKE = re.compile(r"^[0-9A-Za-z]{32,}$")   # 非法 digest 也要当数据行，报 BAD_DIGEST 而不是消失
REMOTE = re.compile(r"^([A-Za-z0-9_.-]+):(.+)$")
SNAPSHOT_HEAD = "# snapshot "
SSH_TIMEOUT = 900
ROUND_IN_NAME = re.compile(r"dispatch-(b1|c1)")        # 轮次从被核的派发文件名推出，不加参数
SUBJECT_SEG = "/subject-"
FULL_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


# ── 基础 ────────────────────────────────────────────────────────────
def split_cells(line: str) -> list[str] | None:
    r"""markdown 表行 → 单元格列表；`\|`（转义竖线）不作分隔。非表行返回 None。"""
    s = line.strip()
    if not s.startswith("|"):
        return None
    s = s.replace("\\|", "\x00")
    # 只去两端的表格边界，保留 |path|| 或 ||digest| 中的空单元格。
    return [c.replace("\x00", "\\|").strip() for c in s[1:].removesuffix("|").split("|")]


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def split_remote(path: str):
    """`host:path` → (host, path)；本地 → (None, path)。"""
    m = REMOTE.match(path)
    if m and "/" not in m.group(1) and not os.path.exists(path):
        return m.group(1), m.group(2)
    return None, path


def ssh(host: str, command: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", host, command], capture_output=True, text=True,
                          timeout=SSH_TIMEOUT, check=False, stdin=subprocess.DEVNULL)


def remote_sha256(host: str, paths: list[str]) -> dict[str, str]:
    """按远端命令的 UTF-8 字节数分批；每批只读计算，缺失文件不出现在结果里。"""
    prefix = "sha256sum -- "
    limit = 64 * 1024
    commands = []
    command = prefix
    for path in paths:
        quoted = shlex.quote(path)
        if len((prefix + quoted).encode("utf-8")) > limit:
            raise RuntimeError(f"ssh {host}: 单个路径超过远端命令长度预算")
        candidate = command + (" " if command != prefix else "") + quoted
        if len(candidate.encode("utf-8")) > limit:
            commands.append(command)
            command = prefix + quoted
        else:
            command = candidate
    if command != prefix:
        commands.append(command)
    res = {}
    for command in commands:
        out = ssh(host, command)
        if out.returncode not in (0, 1):      # 1 = 有文件缺失，其余视为工具故障
            raise RuntimeError(f"ssh {host}: exit {out.returncode}: {out.stderr.strip()[:300]}")
        for line in out.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and HEX.match(parts[0]):
                res[parts[1].lstrip("*")] = parts[0]
    return res


def git_state(path: str) -> str:
    """clean / dirty / untracked / -（不在任何 git 仓库内）/ ?（git 不可用）。

    以**文件自身所在目录**为 git 工作目录（`-C`）并传绝对路径，不依赖进程的 cwd ——
    否则 Reviewer 从别处运行时整列静默退化成 `-`，与「不在仓库内」同形，而 §8 声称
    这一列堵的正是「发出后原地改写」。实测：同一文件在仓库根跑报 clean、从 /tmp 跑报 -。"""
    ap = os.path.abspath(path)
    base = ["git", "-C", os.path.dirname(ap) or "."]
    try:
        out = subprocess.run(base + ["status", "--porcelain", "--", ap],
                             capture_output=True, text=True, check=False)
    except OSError:
        return "?"
    if out.returncode != 0:
        return "-"
    line = out.stdout.strip()
    if not line:
        tracked = subprocess.run(base + ["ls-files", "--error-unmatch", "--", ap],
                                 capture_output=True, text=True, check=False).returncode == 0
        return "clean" if tracked else "-"
    return "untracked" if line.startswith("??") else "dirty"


def git_repository(md: str) -> str:
    """仓库身份由派发所在目录确定，不用进程 cwd 或当前 HEAD 推断。"""
    out = subprocess.run(["git", "-C", os.path.dirname(os.path.abspath(md)),
                          "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"派发文件不在 Git 工作树内：{md}")
    return os.path.realpath(out.stdout.strip())


def repository_path(root: str, path: str, *, source: bool = False) -> str:
    """返回安全的仓库相对路径；source 允许本地原路径为仓库内绝对路径。

    映射必须直指一个文件，禁止目录、.. 与符号链接逃逸。
    目标可尚未存在，由预检或固定提交的 blob 核验报告缺失。
    """
    if not path or "\\" in path or "\x00" in path or any(c.isspace() for c in path):
        raise ValueError("路径为空、含空白或无效字符")
    if ".." in path.split("/"):
        raise ValueError("路径含 ..")
    if not source and (posixpath.isabs(path) or path.endswith("/") or
                       path != posixpath.normpath(path)):
        raise ValueError("映射必须为规范的仓库相对文件路径")
    full = os.path.abspath(path) if source else os.path.join(root, path)
    if os.path.commonpath([root, full]) != root:
        raise ValueError("路径在派发所属仓库之外")
    if os.path.commonpath([root, os.path.realpath(full)]) != root:
        raise ValueError("路径通过符号链接逃逸仓库")
    rel = os.path.relpath(full, root)
    if rel == "." or ".git" in rel.split("/") or os.path.isdir(full):
        raise ValueError("目标不是仓库内文件路径")
    return rel


def validate_commit(root: str, commit: str) -> None:
    if not FULL_COMMIT.fullmatch(commit):
        raise RuntimeError("--commit 须为完整小写 commit SHA；不接受 HEAD、分支、标签或短 SHA")
    out = subprocess.run(["git", "-C", root, "cat-file", "-t", commit],
                         capture_output=True, text=True, check=False)
    if out.returncode != 0 or out.stdout.strip() != "commit":
        raise RuntimeError(f"--commit 不是该仓库中可读取的 commit 对象：{commit}")
    resolved = subprocess.run(["git", "-C", root, "rev-parse", "--verify", commit],
                              capture_output=True, text=True, check=False)
    if resolved.returncode != 0 or resolved.stdout.strip() != commit:
        raise RuntimeError(f"--commit 不是该仓库对象格式的完整 SHA：{commit}")


def commit_digest(root: str, commit: str, rel: str) -> str | None:
    """对指定提交内 blob 原字节算 sha256；缺路径、目录或 gitlink 均不当作文件。"""
    out = subprocess.run(["git", "-C", root, "cat-file", "blob", f"{commit}:{rel}"],
                         capture_output=True, check=False)
    return hashlib.sha256(out.stdout).hexdigest() if out.returncode == 0 else None


def check_preservation(root: str, path: str, recorded: str, declaration: str,
                       commit: str | None) -> tuple[str, int]:
    """声明列紧接 digest；external 的政策授权仍须 Reviewer 逐项判断。"""
    host, local = split_remote(path)
    if declaration.startswith("external:"):
        if declaration[len("external:"):].strip():
            return "EXTERNAL（仅核原路径 sha256；授权由 Reviewer 核）", PASS
        return "BAD_PRESERVATION（external 缺依据）", NOT_VERIFIABLE
    try:
        if declaration == "git":
            if host is not None:
                raise ValueError("远端文件须显式 git:<仓库相对路径> 映射")
            rel = repository_path(root, local, source=True)
        elif declaration.startswith("git:"):
            rel = repository_path(root, declaration[len("git:"):])
        else:
            return "BAD_PRESERVATION（须声明 git / git:<路径> / external:<依据>）", NOT_VERIFIABLE
    except ValueError as e:
        return f"BAD_PRESERVATION（{e}）", NOT_VERIFIABLE
    if commit is None:
        target = os.path.join(root, rel)
        kept = sha256_file(target) if os.path.isfile(target) else None
        label = "PRECOMMIT_TARGET"
    else:
        kept = commit_digest(root, commit, rel)
        label = "COMMIT_BLOB"
    if kept is None:
        return f"MISSING_{label} ({rel})", FAIL if commit else NOT_VERIFIABLE
    if HEX.match(recorded) and kept != recorded:
        return f"{label}_MISMATCH ({rel}; sha256={kept})", FAIL
    return f"{label}_OK ({rel})", PASS


# ── 表格解析 ─────────────────────────────────────────────────────────
def parse_rows(text: str, last_only: bool = False, digest_col: int = 2):
    """([(行号, 路径, 记录值)], [(行号, 第一列, 第二列, 跳过原因)])。

    显式 sha256 表头标识摘要表；历史无表头格式仍按摘要值识别。规则：
    1. ``` 围栏内的行一律不算 —— 派发按 §8 要粘贴预检原始输出，那本身就是一张
       「路径 | sha256」表，不隔离就会被当成 digest 行重算。
    2. **显式摘要表的全部数据行都必须参与解析**，失败行登记进第二个返回值，由调用方
       判 `NOT_VERIFIABLE`；整表零合法行也不能丢弃。表头的 sha256 列须与 digest_col
       一致，不自动改列。没有摘要表头的普通参数表、状态表保持原有跳过规则。

    3. **第一列不像路径、而第二列是合法 digest 或占位符的行，登记为 `NON_PATH_FIRST_COLUMN`**
       （git ref / 包名 / 链名当锚正是这种形态）。第二列也不像 digest 的才静默跳过 ——
       那是参数表、状态表、账本轮次表（`| b1 | <40-hex commit> |`）。判据挂**第二列**
       而非第一列：挂第一列会把账本轮次表整张点亮。

    返回值第三项 = 被 ``` 围栏挡掉、但形状像 digest 行的行数。围栏隔离本身是对的
    （§8 要求粘贴预检原始输出），但整张输入表被围栏吞掉时无人报警 —— 计数让它显形。

    --last 只取最后一张摘要表（及其跳过行），显式坏表或空表也占据这个位置。"""
    lines = text.splitlines()
    blocks, cur, fenced, fenced_digest = [], [], False, 0
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            if cur:
                blocks.append(cur); cur = []
            continue
        if fenced:
            cells = split_cells(line)
            if cells and len(cells) >= digest_col and (
                    HEX.match(cells[digest_col - 1]) or PLACEHOLDER.match(cells[digest_col - 1])):
                fenced_digest += 1
            continue
        if line.lstrip().startswith("|"):
            cur.append(i)
        elif cur:
            blocks.append(cur); cur = []
    if cur:
        blocks.append(cur)
    tables, orphan = [], []
    for blk in blocks:
        rows, skipped = [], []
        header = split_cells(lines[blk[0]])
        header_cols = [n for n, cell in enumerate(header, 1)
                       if cell.strip("`* ").lower() == "sha256"]
        explicit = bool(header_cols)
        if explicit and header_cols != [digest_col]:
            val = header[digest_col - 1] if len(header) >= digest_col else ""
            tables.append(([], [(blk[0], header[0], val, "DIGEST_COLUMN_MISMATCH")]))
            continue
        for i in blk:
            cells = split_cells(lines[i])
            if not cells:
                continue
            if explicit and (i == blk[0] or all(re.fullmatch(r":?-+:?", c) for c in cells)):
                continue                                   # 已识别的表头 / 分隔行
            if len(cells) < digest_col:
                # 列数不足时原先静默跳过：--digest-col 给大一位，整张真表消失而无任何痕迹。
                if explicit or (len(cells) >= 2 and (
                        HEX.match(cells[1].strip("` ")) or PLACEHOLDER.match(cells[1].strip("` ")))):
                    skipped.append((i, cells[0].strip("` "), "", "ROW_TOO_FEW_COLUMNS"))
                continue
            path, val = cells[0].strip("` "), cells[digest_col - 1].strip("` ")
            if explicit and not path:
                skipped.append((i, path, val, "EMPTY_PATH")); continue
            if not explicit and (not path or set(path) <= set("-: ")):
                continue                                   # 分隔行
            if path.startswith("<"):
                skipped.append((i, path, val, "UNEXPANDED_TEMPLATE_ROW")); continue
            if not PATHLIKE.match(path):
                skipped.append((i, path, val, "PATH_HAS_WHITESPACE")); continue
            if not ("/" in path or "." in path):
                if explicit or HEX.match(val) or PLACEHOLDER.match(val):
                    skipped.append((i, path, val, "NON_PATH_FIRST_COLUMN")); continue
                continue                                   # 参数表 / 状态表 / 账本轮次表
            if not (HEX.match(val) or PLACEHOLDER.match(val) or DIGEST_LIKE.match(val)):
                skipped.append((i, path, val, "BAD_VALUE_CELL")); continue
            rows.append((i, path, val))
        if explicit and not rows and not skipped:
            skipped.append((blk[0], header[0], header[digest_col - 1], "EMPTY_DIGEST_TABLE"))
        if rows or explicit:
            tables.append((rows, skipped))
        else:
            # 整张表零解析行时，原先连 skipped 一并丢弃 —— 「忘记展开模板」因此静默绿灯，
            # 而那正是本函数 docstring 第 2 条自陈要守的第一种情形。模板行无条件留下。
            orphan.extend([k for k in skipped
                           if k[3] in ("UNEXPANDED_TEMPLATE_ROW", "ROW_TOO_FEW_COLUMNS")])
    if not tables:
        return [], orphan, fenced_digest
    if last_only:
        r, sk = tables[-1]
        return r, sk + orphan, fenced_digest
    return ([r for t, _s in tables for r in t],
            [s for _t, sk in tables for s in sk] + orphan, fenced_digest)


def patch_touched(path: str) -> set[str]:
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
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r'^diff --git ("(?:\\.|[^"\\])*"|a/.*?) ("(?:\\.|[^"\\])*"|b/.*?)\n?$', line)
            if m:
                in_hunk = False
                add(m.group(1)); add(m.group(2)); continue
            if line.startswith("@@"):
                in_hunk = True
            if not in_hunk and line.startswith(("--- ", "+++ ")):
                add(line[4:].rstrip("\n").split("\t", 1)[0])
    return touched


# ── 快照 ────────────────────────────────────────────────────────────
def list_dir(target: str, exclude: list[str] | None = None) -> dict[str, str]:
    """{相对路径: sha256}；本地 os.walk，远端 find | sort | xargs sha256sum。

    `exclude` 是相对路径的 glob 清单（如 `__pycache__/*`、`*.pyc`）。不给即不排除 ——
    禁写目录若含每次运行都变的派生缓存，下一轮必 SNAPSHOT_DIFF，那是误报不是缺陷。
    排除面写进快照头，`diff_snapshot` 读回后用同一面重列，防止两次用不同口径比。"""
    host, d = split_remote(target)
    d = d.rstrip("/") or "/"
    if host is None:
        if not os.path.isdir(d):
            raise RuntimeError(f"{d} 不是目录（路径打错会产出 0 文件快照，此后每轮恒真）")
        res = {}
        for root, dirs, files in os.walk(d):
            # os.walk 默认不下降软连接目录 —— 整棵子树在快照里静默不存在。
            # 本仓库的 data/{train,val,test}/{images,labels}/ 在远端正是软连接目录。
            for name in list(dirs):
                full = os.path.join(root, name)
                if os.path.islink(full):
                    res[os.path.relpath(full, d) + "@"] = "symlink:" + os.readlink(full)
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, d)
                if os.path.islink(full):
                    res[rel + "@"] = "symlink:" + os.readlink(full); continue
                res[rel] = sha256_file(full)
        if exclude:
            res = {k: v for k, v in res.items()
                   if not any(fnmatch.fnmatch(k, g) or fnmatch.fnmatch(k.rstrip("@"), g) for g in exclude)}
        return res
    cmd = (f"cd {shlex.quote(d)} && LC_ALL=C find . -type f -print0 | LC_ALL=C sort -z "
           f"| xargs -0 --no-run-if-empty sha256sum --")
    out = ssh(host, cmd)
    if out.returncode != 0:
        raise RuntimeError(f"ssh {host}: exit {out.returncode}: {out.stderr.strip()[:300]}")
    res = {}
    for line in out.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and HEX.match(parts[0]):
            res[parts[1].lstrip("*").removeprefix("./")] = parts[0]
    return res


def write_snapshot(target: str, out_path: str, allow_empty: bool = False,
                   exclude: list[str] | None = None) -> int:
    listing = list_dir(target, exclude)
    if not listing and not allow_empty:
        print(f"REFUSED: {target} 下 0 个文件 —— 空快照是恒真断言，禁写目录若确实为空请显式 --allow-empty")
        return FAIL
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(f"{SNAPSHOT_HEAD}{target if target.endswith('/') else target + '/'}\n")
        fh.write(f"# created {_dt.datetime.now().isoformat(timespec='seconds')} files={len(listing)}\n")
        fh.write(f"# exclude {','.join(exclude or [])}\n")
        for rel in sorted(listing):
            fh.write(f"{listing[rel]}\t{rel}\n")
    print(f"snapshot: {target} → {out_path} ({len(listing)} files)")
    return PASS


def read_snapshot(path: str):
    with open(path, encoding="utf-8") as fh:
        first = fh.readline()
        if not first.startswith(SNAPSHOT_HEAD):
            return None, {}, []
        target = first[len(SNAPSHOT_HEAD):].strip()
        base, exclude = {}, []
        for line in fh:
            if line.startswith("# exclude "):
                exclude = [g for g in line[len("# exclude "):].strip().split(",") if g]
                continue
            if line.startswith("#") or not line.strip():
                continue
            h, _, rel = line.rstrip("\n").partition("\t")
            base[rel] = h
    return target, base, exclude


def diff_snapshot(path: str) -> tuple[str, list[str]]:
    target, base, exclude = read_snapshot(path)
    if target is None:
        return "NOT_A_SNAPSHOT", []
    live = list_dir(target, exclude)          # 用建快照时的同一排除面重列，否则两次口径不同
    added = sorted(set(live) - set(base))
    removed = sorted(set(base) - set(live))
    modified = sorted(k for k in set(base) & set(live) if base[k] != live[k])
    detail = ([f"+ {k}" for k in added] + [f"- {k}" for k in removed]
              + [f"~ {k}" for k in modified])
    if not detail:
        return f"SNAPSHOT_OK ({len(base)} files)", []
    return f"SNAPSHOT_DIFF +{len(added)} -{len(removed)} ~{len(modified)}", detail


# ── check / write ──────────────────────────────────────────────────
def compute(rows):
    """为每行算实算值；远端按 host 分组一次 ssh。返回 {行号: 实算值或 None}。"""
    actual: dict[int, str | None] = {}
    by_host: dict[str, list[tuple[int, str]]] = {}
    for i, path, _v in rows:
        host, p = split_remote(path)
        if host is None:
            actual[i] = sha256_file(p) if os.path.isfile(p) else None
        else:
            by_host.setdefault(host, []).append((i, p))
    for host, items in by_host.items():
        got = remote_sha256(host, [p for _i, p in items])
        for i, p in items:
            actual[i] = got.get(p)
    return actual


def live_of_subject(path: str, rnd: str) -> str | None:
    """`…/subject-<rnd>/<rel>` → 它对应的实路径；不是**本轮**副本则 None。

    布局按 §7：本地按仓库根相对路径原样铺开；远端铺在 `_remote/<host>/<去掉开头 / 的绝对路径>`。
    只认本轮 —— 往轮副本（如 c1 轮里的 subject-b1/）是历史记录，与实路径本就应当不同。"""
    key = f"{SUBJECT_SEG}{rnd}/"
    if key not in path:
        return None
    rel = path.split(key, 1)[1]
    if rel.startswith("_remote/"):
        host, _, abspath = rel[len("_remote/"):].partition("/")
        return f"{host}:/{abspath}" if host and abspath else None
    return rel


def cmd_check(files: list[str], last_only: bool, require: list[str] | None = None,
              review_root: str | None = None, digest_col: int = 2,
              large_suffix: list[str] | None = None, last_heading: str | None = None,
              claimed_changed: list[str] | None = None, diff_path: str | None = None,
              precommit: bool = False, commit: str | None = None) -> int:
    worst, saw_fail = PASS, False
    git_bound = precommit or commit is not None
    if precommit and commit is not None:
        print("TOOL_ERROR: --precommit 与 --commit 互斥"); return TOOL_ERROR
    if git_bound and (review_root is not None or large_suffix is not None):
        print("TOOL_ERROR: SCI --precommit/--commit 不得混用旧 --review-root/--large-suffix")
        return TOOL_ERROR
    review_root = review_root if review_root is not None else "docs/reviews/"
    if require and len(files) != 1:
        print("TOOL_ERROR: --require 只在恰好核一个文件时有意义"
              "（跨文件累加会让「X 出现在任一文件即满足」）"); return TOOL_ERROR
    seen_paths: set[str] = set()
    for md in files:
        if not os.path.isfile(md):
            print(f"| {md} | — | — | MISSING_FILE | — |")
            worst = max(worst, NOT_VERIFIABLE)
            continue
        with open(md, "rb") as fh:
            md_bytes = fh.read()
        text = md_bytes.decode("utf-8")
        root = None
        if git_bound:
            try:
                root = git_repository(md)
                md_rel = repository_path(root, md, source=True)
                if commit is not None:
                    validate_commit(root, commit)
                    kept = commit_digest(root, commit, md_rel)
                    actual_md = hashlib.sha256(md_bytes).hexdigest()
                    if kept != actual_md:
                        print(f"| {md} | {kept or '—'} | {actual_md} | DISPATCH_COMMIT_MISMATCH | — |")
                        # 派发本体是输入白名单；未证明身份前不可按其内容继续读文件或 ssh。
                        print("\nverify_digests: FAIL")
                        return FAIL
                    else:
                        print(f"| {md} | {kept} | {actual_md} | DISPATCH_COMMIT_OK | {commit} |")
            except (RuntimeError, ValueError) as e:
                print(f"TOOL_ERROR: {e}"); return TOOL_ERROR
        rows, skipped, fenced_digest = parse_rows(text, last_only, digest_col)
        lines = text.splitlines()
        print(f"\n### verify_digests check · {md}" + (" (--last)" if last_only else ""))
        if fenced_digest:
            # 围栏内的表不参与核算（§8 要求粘贴预检原始输出）。但整张输入表被围栏吞掉时
            # 旧版无任何痕迹 —— 只要文件里别处还有一张能解析的表，就照样 PASS。
            print(f"⚠ ``` 围栏内另有 {fenced_digest} 行形如 digest 的表行，未参与核算 —— "
                  f"若那是本该受核的输入表，它现在是零断言")
        if not rows:
            print("| — | — | — | NO_DIGEST_TABLE | — |")
            # 零解析行时原先直接 continue，把 skipped 一并扔掉 —— 于是「列数不足」「模板忘展开」
            # 这类本该显形的行，在整张表都没解析成功时反而彻底消失。
            for i, cell1, cell2, why in skipped:
                print(f"| {cell1} | {cell2 or '—'} | — | {why} (行 {i + 1}) | — |")
            worst = max(worst, NOT_VERIFIABLE)
            continue
        if last_only and last_heading:
            # §6 要求 NON_CONVERGENT 也以 PASS 记录的格式记在账本末尾 —— 只取「最后一张表」
            # 会把 NON_CONVERGENT 当成 PASS 记录核，门 A 判了不收敛而门 B 的预检 1 绿灯。
            above = text.split("\n")[:rows[0][0]]
            head = next((l for l in reversed(above) if l.startswith("#")), "")
            if not re.search(last_heading, head):
                print(f"| {md} | — | — | LAST_TABLE_NOT_EXPECTED_SECTION（最近标题 {head.strip()[:40]!r}"
                      f" 不匹配 {last_heading!r}）| — |")
                worst = max(worst, NOT_VERIFIABLE)
        seen_paths.update(pth for _i, pth, _v in rows)
        print(f"表内候选行 {len(rows) + len(skipped)} / 实核 {len(rows)}"
              + (f" / 未解析 {len(skipped)}" if skipped else ""))
        try:
            actual = compute(rows)
        except RuntimeError as e:
            print(f"TOOL_ERROR: {e}")
            return TOOL_ERROR
        print("| 文件 | 记录值 | 实算值 | 结果 | " + ("保全" if git_bound else "git") + " |")
        print("|---|---|---|---|---|")
        snapshots, gitmap = [], {}
        for i, path, recorded in rows:
            act = actual.get(i)
            host, p = split_remote(path)
            if git_bound:
                cells = split_cells(lines[i])
                git = cells[digest_col].strip("` ") if len(cells) > digest_col else ""
            else:
                git = git_state(p) if host is None else "-"
            gitmap[i] = git
            if act is None:
                verdict, code = "MISSING", NOT_VERIFIABLE
            elif PLACEHOLDER.match(recorded):
                verdict, code = "UNFILLED", NOT_VERIFIABLE
            elif not HEX.match(recorded):
                verdict, code = "BAD_DIGEST (非 64-hex)", FAIL
            elif act == recorded:
                verdict, code = "OK", PASS
            else:
                k = next((n for n, (a, b) in enumerate(zip(recorded, act)) if a != b), 64)
                verdict, code = f"MISMATCH (first diff @{k})", FAIL
            if git_bound:
                preserved, preservation_code = check_preservation(root, path, recorded, git, commit)
                verdict += "; " + preserved
                code = FAIL if FAIL in (code, preservation_code) else max(code, preservation_code)
            if host is None and act is not None and code != NOT_VERIFIABLE:
                try:
                    with open(p, encoding="utf-8") as fh:
                        if fh.readline().startswith(SNAPSHOT_HEAD):
                            snapshots.append(p)
                except (OSError, UnicodeDecodeError):
                    pass
            print(f"| {path} | {recorded or '—'} | {act or '—'} | {verdict} | {git} |")
            worst = max(worst, code)
            saw_fail = saw_fail or code == FAIL
        for i, cell1, cell2, why in skipped:            # 静默丢行 = 假绿灯，必须显形
            print(f"| {cell1} | {cell2 or '—'} | — | {why} (行 {i + 1}) | — |")
            worst = max(worst, NOT_VERIFIABLE)
        # §7 副本不变量：本轮副本 == 实路径；且 git 未保全的受审字节必须有本轮副本。
        # 「原样副本」在协议里本是一句散文，没有任何核验会触发 —— 两份内容不同、各自
        # sha256 正确时逐行比对全部 OK。留在 git 里可追溯的是副本，真正跑出证据的是实路径。
        rnd_m = None if git_bound else ROUND_IN_NAME.search(os.path.basename(md))
        if not git_bound and not rnd_m and SUBJECT_SEG in text:
            # 原先匹配失败一个字都不说 —— 派发改名（dispatch-g1.md）即整条副本不变量静默关闭。
            print(f"| {md} | — | — | ROUND_UNRECOGNIZED（文件名非 dispatch-b1/c1，subject 副本不变量本次未执行）| — |")
            worst = max(worst, NOT_VERIFIABLE)
        if rnd_m:
            rnd = rnd_m.group(1)
            by_path = {p: i for i, p, _v in rows}
            paired: set[str] = set()
            for i, path, _v in rows:
                live = live_of_subject(path, rnd)
                if live is None:
                    continue
                paired.add(live)
                j = by_path.get(live)
                if j is None:
                    print(f"| {path} | — | — | SUBJECT_WITHOUT_LIVE_ROW | — |")
                    worst = max(worst, NOT_VERIFIABLE); continue
                a1, a2 = actual.get(i), actual.get(j)
                if a1 is not None and a2 is not None and a1 != a2:
                    print(f"| {path} ↔ {live} | {a1} | {a2} | SUBJECT_LIVE_MISMATCH | — |")
                    worst = max(worst, FAIL); saw_fail = True
            for i, path, _v in rows:                    # 反向：该有副本却没做
                if SUBJECT_SEG in path or path.startswith(review_root) or path in paired:
                    continue
                if large_suffix and path.endswith(tuple(large_suffix)):
                    # §7：<大文件类型> 免 subject 副本、只记 sha256。脚本原先没有这个豁免，
                    # 任何带大产物的链预检永远转不了绿（gitignore → git=- → MISSING_SUBJECT_COPY）。
                    print(f"| {path} | — | — | LARGE_NO_COPY（按 --large-suffix 豁免副本，仅核 sha256）| {gitmap.get(i)} |")
                    continue
                if gitmap.get(i) != "clean":
                    print(f"| {path} | — | — | MISSING_SUBJECT_COPY (git={gitmap.get(i)}) | — |")
                    worst = max(worst, NOT_VERIFIABLE)
        for snap in snapshots:
            try:
                status, detail = diff_snapshot(snap)
            except RuntimeError as e:
                print(f"TOOL_ERROR: {e}")
                return TOOL_ERROR
            print(f"| {snap} → 目录 | — | — | {status} | — |")
            for line in detail[:200]:
                print(f"    {line}")
            if status.startswith("SNAPSHOT_DIFF"):
                worst = max(worst, FAIL); saw_fail = True
    if claimed_changed:
        if not diff_path or not os.path.isfile(diff_path):
            print(f"TOOL_ERROR: --claimed-changed 需要 --diff <patch>，且该文件须存在（给的是 {diff_path!r}）")
            return TOOL_ERROR
        touched = patch_touched(diff_path)
        print(f"\n### claimed-changed · 对照 {diff_path}（patch 内有改动的路径 {len(touched)} 个）")
        for c in claimed_changed:
            if c in touched:
                print(f"| {c} | — | — | CLAIM_OK | — |")
            else:
                # 同字节重写 / 声明了没改 / 改错了文件，三种都落在这里
                print(f"| {c} | — | — | CLAIMED_BUT_NOT_IN_DIFF | — |")
                worst = max(worst, FAIL); saw_fail = True
    for req in (require or []):
        # 「对已列出内容的断言」堵不住「该列而未列」：列错对象、漏列对象旧版都 PASS。
        # 必列清单由约定层给（协议正文只写槽位名），脚本不含任何领域知识。
        if req not in seen_paths:
            print(f"| {req} | — | — | MISSING_REQUIRED_ROW | — |")
            worst = max(worst, NOT_VERIFIABLE)
    if saw_fail:
        worst = FAIL        # 字节不符是确证，不得被同表里的「缺文件」吸收成不消耗轮次的 NOT_VERIFIABLE
    names = {PASS: "PASS", FAIL: "FAIL", TOOL_ERROR: "TOOL_ERROR", NOT_VERIFIABLE: "NOT_VERIFIABLE"}
    print(f"\nverify_digests: {names[worst]}")
    return worst


def cmd_write(md: str) -> int:
    if not os.path.isfile(md):
        print(f"NOT_VERIFIABLE: {md} 不存在"); return NOT_VERIFIABLE
    if git_state(md) == "clean":
        print(f"REFUSED: {md} 已入库且与 HEAD 一致（已冻结），不得改写；更正须新建文件")
        return FAIL
    with open(md, encoding="utf-8") as fh:
        text = fh.read()
    lines = text.splitlines(keepends=True)
    rows, skipped, _fenced = parse_rows(text)
    if not rows:
        print("NOT_VERIFIABLE: 无 digest 表"); return NOT_VERIFIABLE
    try:
        actual = compute(rows)
    except RuntimeError as e:
        print(f"TOOL_ERROR: {e}"); return TOOL_ERROR
    missing, filled, kept = [], 0, 0
    for i, path, recorded in rows:
        if not PLACEHOLDER.match(recorded):   # 只填占位符；已有值（含非法值）一律不动，账本旧 PASS 记录必须保留
            kept += 1; continue
        act = actual.get(i)
        if act is None:
            missing.append(path); continue
        m = ROW.match(lines[i].strip())
        cell = m.group(2)
        # 只替换第二个单元格的内容，保留原有反引号与其余列
        head, sep, rest = lines[i].partition("|")            # 去掉行首 '|'
        c1, sep2, tail = rest.partition("|")                 # 第一列
        c2, sep3, tail2 = tail.partition("|")                # 第二列
        new_c2 = c2.replace(cell, act) if cell else f" {act} "
        lines[i] = head + sep + c1 + sep2 + new_c2 + sep3 + tail2
        filled += 1
    with open(md, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    for i, cell1, cell2, why in skipped:
        print(f"UNPARSED 行 {i + 1}: {why} · | {cell1} | {cell2 or '—'} |")
    print(f"write: {md} 填入 {filled} 行，保留已有值 {kept} 行"
          + (f"；缺文件 {len(missing)}：{missing}" if missing else "")
          + (f"；未解析 {len(skipped)} 行（见上，须先展开或修正）" if skipped else "")
          + "（只填占位符 <脚本填>；要刷新某行，先把它改回占位符）")
    return NOT_VERIFIABLE if (missing or skipped) else PASS


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("files", nargs="+"); c.add_argument("--last", action="store_true")
    c.add_argument("--require", default="", help="必须出现在表里的路径，逗号分隔；缺一行即 MISSING_REQUIRED_ROW（取值由约定层给）")
    c.add_argument("--review-root", default=None, help="ENG 审查工件根（默认 docs/reviews/）—— 该前缀下的行免 subject 副本义务")
    mode = c.add_mutually_exclusive_group()
    mode.add_argument("--precommit", action="store_true", help="SCI 提交前核当前字节与保全声明，不证明已入库")
    mode.add_argument("--commit", help="SCI 固定提交核验：完整 commit SHA，核 blob、派发自身和当前字节")
    c.add_argument("--digest-col", type=int, default=2, help="digest 所在列（1 起，默认 2）；三列冻结表用 3")
    c.add_argument("--large-suffix", default=None, help="ENG 免 subject 副本、只核 sha256 的后缀（逗号分隔），取值来自约定层 <大文件类型>")
    c.add_argument("--claimed-changed", default="", help="声称本轮改动过的文件（逗号分隔）；须配 --diff。"
                                                        "不在 patch 里 → CLAIMED_BUT_NOT_IN_DIFF → FAIL（同字节重写也在此列）")
    c.add_argument("--diff", default="", help="本轮的 diff patch（§7 的 diff-c1.patch），供 --claimed-changed 比对")
    c.add_argument("--last-heading", default="", help="配 --last：要求最后一张表的最近上方标题匹配该正则（如 '^## PASS '），"
                                                      "防止把 NON_CONVERGENT 记录当成 PASS 记录核")
    w = sub.add_parser("write"); w.add_argument("file")
    sn = sub.add_parser("snapshot"); sn.add_argument("target"); sn.add_argument("-o", "--out", required=True)
    sn.add_argument("--allow-empty", action="store_true", help="允许对空目录生成快照（默认拒绝）")
    sn.add_argument("--exclude", default="", help="排除的相对路径 glob（逗号分隔），如 '__pycache__/*,*.pyc'；"
                                                  "排除面写进快照头，diff 时用同一面重列")
    a = ap.parse_args()
    try:
        if a.cmd == "check":
            req = [x.strip() for x in a.require.split(",") if x.strip()]
            if a.digest_col < 2:
                print("TOOL_ERROR: --digest-col 须 ≥ 2（第一列是对象）"); return TOOL_ERROR
            large = ([x.strip() for x in a.large_suffix.split(",") if x.strip()]
                     if a.large_suffix is not None else None)
            return cmd_check(a.files, a.last, req, a.review_root, a.digest_col,
                             large, a.last_heading or None,
                             [x.strip() for x in a.claimed_changed.split(",") if x.strip()],
                             a.diff or None, a.precommit, a.commit)
        if a.cmd == "write":
            return cmd_write(a.file)
        return write_snapshot(a.target, a.out, a.allow_empty,
                              [x.strip() for x in a.exclude.split(",") if x.strip()])
    except PermissionError as e:
        print(f"TOOL_ERROR: {e}"); return TOOL_ERROR
    except RuntimeError as e:
        print(f"TOOL_ERROR: {e}"); return TOOL_ERROR
    except FileNotFoundError as e:
        print(f"NOT_VERIFIABLE: {e}"); return NOT_VERIFIABLE


if __name__ == "__main__":
    sys.exit(main())
