"""Shared SCI digest-table parsing and Git identity helpers.

Only the current checker's dependencies live here; there is no alternate CLI,
ENG preservation path, or directory scanner.
"""
from __future__ import annotations

import os
import posixpath
import re
import subprocess

HEX = re.compile(r"^[0-9a-f]{64}$")


PLACEHOLDER = re.compile(r"^<脚本填[^>]*>$")          # 只认这一种占位符；空单元格与 <其它> 不是 digest 行


PATHLIKE = re.compile(r"^[^\s|]+$")                    # 路径：无空白；且须含 / 或 .（见 parse_rows）


DIGEST_LIKE = re.compile(r"^[0-9A-Za-z]{32,}$")   # 非法 digest 也要当数据行，报 BAD_DIGEST 而不是消失


REMOTE = re.compile(r"^([A-Za-z0-9_.-]+):(.+)$")


FULL_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def split_cells(line: str) -> list[str] | None:
    r"""markdown 表行 → 单元格列表；`\|`（转义竖线）不作分隔。非表行返回 None。"""
    s = line.strip()
    if not s.startswith("|"):
        return None
    s = s.replace("\\|", "\x00")
    # 只去两端的表格边界，保留 |path|| 或 ||digest| 中的空单元格。
    return [c.replace("\x00", "\\|").strip() for c in s[1:].removesuffix("|").split("|")]


def split_remote(path: str):
    """`host:path` → (host, path)；本地 → (None, path)。"""
    m = REMOTE.match(path)
    if m and "/" not in m.group(1) and not os.path.exists(path):
        return m.group(1), m.group(2)
    return None, path


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
