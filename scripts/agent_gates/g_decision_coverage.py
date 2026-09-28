#!/usr/bin/env python3
"""g_decision_coverage —— AGENT GATE PROTOCOL v3 §4 预检：上游决策覆盖（A1 的机械半）。

断言：
- 上游已拍板的编号 ⊆ 下游落点表（防「漏落地」）；
- 下游落点表的编号 ⊆ 上游已知编号（防「回指不存在的编号」）；
- 上游待定 / 被推翻的编号不得出现在落点表（防「未决当已决」「被推翻方案复活」）；
- **每个编号在上游恰好一个当前状态**：同时命中已拍板与待定 / 被推翻 → STATUS_CONFLICT
  （§4 写 DECIDED、决策日志里已推翻，正是 draft 过时的典型形态）。

上游的四种状态必须分开建模：已拍板（必须落表）、被推翻（不得落表）、待定（不得落表）、
**已登记为无对象**（编号保留、义务转移到别处；允许且应当落表 —— 早期版本把它并入
「被推翻」，结果把正确的记账判成缺陷）。

本脚本只判「编号命中」，判不了「语义落实」；后者是 Reviewer 条目 A1 的职责，
不得用本脚本的 PASS 替代。编号体例随项目而异，全部由正则参数注入。

退出码：0 PASS · 1 FAIL · 2 TOOL_ERROR（正则语法错 / 多捕获组 / 文件编码）·
3 NOT_VERIFIABLE（缺文件 / 落点节找不到 / 上游零命中致断言恒真）。输出为 markdown 表格。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys

PASS, FAIL, TOOL_ERROR, NOT_VERIFIABLE = 0, 1, 2, 3


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find(pattern: str | None, text: str) -> set[str]:
    return set(re.findall(pattern, text, re.M)) if pattern else set()


def main() -> int:
    ap = argparse.ArgumentParser(description="上游决策覆盖（v3 §4 预检）")
    ap.add_argument("--upstream", required=True, nargs="+",
                    help="上游决策文档，可多份 —— 工程链常有设计稿 / 详细合同 / 决策 draft / "
                         "对话文档多个上游。多份时取并集，「每编号恰好一个当前状态」因此能跨文件生效："
                         "A 文件标已拍板、B 文件标已推翻 → STATUS_CONFLICT（跨文件撤销未回写的典型形态）")
    ap.add_argument("--downstream", required=True, help="下游 spec")
    ap.add_argument("--decided-pattern", required=True, help="上游『已拍板』编号正则（含一个捕获组）")
    ap.add_argument("--superseded-pattern", help="上游『被推翻』编号正则（不得落表）")
    ap.add_argument("--retired-pattern", help="上游『已登记为无对象』编号正则（允许且应当落表）")
    ap.add_argument("--open-pattern", help="上游『待定 / 未决』编号正则（不得落表）")
    ap.add_argument("--landing-pattern", required=True, help="下游落点表编号正则（含一个捕获组）")
    ap.add_argument("--landing-section", help="只在下游该节内取落点（正则，到下一同级标题为止）")
    ap.add_argument("--landing-section-none", action="store_true",
                    help="显式声明本 spec 无落点节，作用域取全文 —— 与 --landing-section 二选一，"
                         "必须显式给其一：省略时作用域悄悄变成全文，「正文里顺口提及的编号」会被算成已落地，"
                         "正好遮蔽 A1 的「漏落地 → BLOCKER」")
    ap.add_argument("--require-counts", default="",
                    help="对上游实算数的显式断言，如 decided=27,open=0；键取 decided/superseded/retired/open/landed。"
                         "不等即 FAIL —— 正则写窄会静默少捞编号，双向差集照样为空判 PASS，这是唯一能发现它的手段")
    a = ap.parse_args()

    # 正则由使用者注入 —— 语法错、或多于一个捕获组（findall 会返回元组，集合运算全部失配
    # 并打印满屏假缺陷），都是工具故障而非被审对象的缺陷。
    for name in ("decided_pattern", "superseded_pattern", "retired_pattern",
                 "open_pattern", "landing_pattern", "landing_section"):
        p = getattr(a, name)
        if p is None:
            continue
        try:
            n = re.compile(p).groups
        except re.error as e:
            print(f"TOOL_ERROR: --{name.replace('_', '-')} 正则语法错：{e}"); return TOOL_ERROR
        if name != "landing_section" and n > 1:
            print(f"TOOL_ERROR: --{name.replace('_', '-')} 有 {n} 个捕获组，须恰好 1 个"
                  f"（多组会让 findall 返回元组，产出满屏假缺陷）"); return TOOL_ERROR

    if bool(a.landing_section) == bool(a.landing_section_none):
        print("TOOL_ERROR: --landing-section 与 --landing-section-none 必须且只能给其一"
              "（省略即作用域全文，是一种静默放宽）"); return TOOL_ERROR

    want: dict[str, int] = {}
    for kv in (x.strip() for x in a.require_counts.split(",") if x.strip()):
        k, _, v = kv.partition("=")
        if k.strip() not in ("decided", "superseded", "retired", "open", "landed") or not v.strip().isdigit():
            print(f"TOOL_ERROR: --require-counts 项 {kv!r} 非法，形如 decided=27"); return TOOL_ERROR
        want[k.strip()] = int(v)

    for f in (*a.upstream, a.downstream):
        if not os.path.isfile(f):
            print(f"NOT_VERIFIABLE: {f} 不存在"); return NOT_VERIFIABLE
    up = "\n".join(open(f, encoding="utf-8").read() for f in a.upstream)
    down = open(a.downstream, encoding="utf-8").read()

    scope = down
    if a.landing_section:
        m = re.search(a.landing_section, down, re.M)
        if not m:
            print(f"NOT_VERIFIABLE: 落点节 {a.landing_section!r} 未找到"); return NOT_VERIFIABLE
        level = len(re.match(r"#*", down[m.start():]).group(0))
        if not level:
            # 匹配位置不在 ^#+ 上 → 无从推断标题层级。原先默认 2，会让三级节的作用域一路
            # 吞到下一个 ##，把「顺口提及但从未落地」的编号算成已落表 —— 正好遮蔽 A1 的
            # 「漏落地 → BLOCKER」。实测：少打两个 # 就能让 FAIL 变 PASS。
            print(f"NOT_VERIFIABLE: --landing-section 须锚在标题上（匹配位置应以 ^#+ 开头），"
                  f"否则无从推断节层级、作用域会吞掉兄弟节；"
                  f"当前匹配到 {down[m.start():m.start() + 30]!r}")
            return NOT_VERIFIABLE
        nxt = re.search(rf"^#{{1,{level}}}\s", down[m.end():], re.M)
        scope = down[m.start(): m.end() + nxt.start()] if nxt else down[m.start():]

    decided = find(a.decided_pattern, up)
    superseded = find(a.superseded_pattern, up)
    retired = find(a.retired_pattern, up)
    open_items = find(a.open_pattern, up)
    superseded -= retired                       # 无对象优先于被推翻
    landed = find(a.landing_pattern, scope)

    # 空集会让「上游已拍板编号 ⊆ 落点表」恒真 —— 一个正则笔误就把门变成零断言，
    # 而零断言正是 B2 判 BLOCKER 的「不可被击败的断言」。上游本就没有编号决策时，
    # 按协议 §4 预检 1 应记 NA 而不是跑本脚本。
    if not decided:
        print(f"NOT_VERIFIABLE: --decided-pattern 在 {' + '.join(a.upstream)} 未命中任何编号；"
              f"双向差集恒真。上游无编号决策应记 NA，不跑本脚本"); return NOT_VERIFIABLE

    got = {"decided": len(decided), "superseded": len(superseded), "retired": len(retired),
           "open": len(open_items), "landed": len(landed)}
    states = {"decided": decided, "superseded": superseded, "retired": retired, "open": open_items}
    failures: list[tuple[str, str, str]] = []
    for k, v in want.items():
        if got[k] != v:
            failures.append(("COUNT_MISMATCH", k, f"声明 {v}，实算 {got[k]} —— 正则可能写窄或上游已变"))
    for d in sorted(decided | superseded | retired | open_items):
        hit = [k for k, v in states.items() if d in v]
        if len(hit) > 1:
            failures.append(("STATUS_CONFLICT", d, " + ".join(hit)))
    for d in sorted(decided - landed):
        failures.append(("DECIDED_NOT_LANDED", d, "上游已拍板，下游落点表无此编号"))
    for d in sorted(landed - decided - superseded - retired - open_items):
        failures.append(("LANDED_ID_UNKNOWN_UPSTREAM", d, "落点表回指的编号上游不存在"))
    for d in sorted(landed & superseded):
        failures.append(("SUPERSEDED_DECISION_REVIVED", d, "被推翻的方案出现在落点表"))
    for d in sorted(landed & open_items):
        failures.append(("OPEN_ITEM_TREATED_AS_DECIDED", d, "未决项出现在落点表"))

    print(f"### g_decision_coverage · {' + '.join(a.upstream)} → {a.downstream}")
    for f in a.upstream:
        print(f"upstream sha256: {sha256_file(f)}  {f}")
    print(f"downstream sha256: {sha256_file(a.downstream)}")
    print(f"counts: decided={len(decided)} superseded={len(superseded)} retired={len(retired)} "
          f"open={len(open_items)} landed={len(landed)}")
    print("| 结果 | 编号 | 说明 |")
    print("|---|---|---|")
    for why, d, note in failures:
        print(f"| {why} | {d} | {note} |")
    if not failures:
        print("| OK | — | 双向差集为空，状态无冲突 |")
    if not open_items and not superseded:
        # 「未决当已决」「已推翻方案复活」两种 A1 失效全靠这两个集合。两者皆空时，
        # 既可能是上游真的清零（正常终态），也可能是正则锚在了标题形状而非状态标记上
        # —— 后者会让两条断言双双恒真。脚本分不出，只能让它显形。
        print("\n⚠ 上游未检出任何「待定」或「被推翻」编号 —— 若上游并非真的清零，"
              "请确认 --open-pattern / --superseded-pattern 锚在状态标记而非标题形状上"
              "（可用 --require-counts open=0 把「确已清零」写成显式断言）")
    print("\n编号命中 ≠ 语义落实；A1 的语义等价判定不得由本脚本替代。")
    print(f"g_decision_coverage: {'PASS' if not failures else 'FAIL'}")
    return PASS if not failures else FAIL


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, UnicodeDecodeError, re.error) as e:
        print(f"TOOL_ERROR: {type(e).__name__}: {e}")
        sys.exit(TOOL_ERROR)
