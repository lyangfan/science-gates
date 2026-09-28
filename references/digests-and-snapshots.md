# 摘要与目录巡检后端

包内 `scripts/verify_digests.py` 是 [SCI 协议](protocol.md) 的 SCI 专用入口。它将实际输入的内容 SHA256 核验与禁写目录巡检分开，保留 `write`、`check`、`snapshot` 三个入口及 SCI 使用的 `check` 参数。不支持 ENG 旧 subject 副本参数 `--review-root` / `--large-suffix`；ENG 不由此 skill 维护。

包内仅保留当前 `verify_digests.py`，它直接依赖同目录的 `snapshots.py` 和 `digest_table.py`，不加载旧版脚本。这三个实际依赖都须列入派发 SHA256 表，不能只登记入口。历史实现可从 Git 固定提交读取；原项目中的冻结文件不随本包升级而改写。进行中的链须有明确的治理替换授权，不能通过换路径绕过冻结依赖约束。

## 内容身份与目录巡检

| 对象或模式 | 核验内容 | 证据边界 |
|---|---|---|
| 派发表中的实际输入 | 逐文件 SHA256；SCI 模式另核保全与固定提交 | metadata 不能代替内容身份 |
| `snapshot --mode content` | 基线中的文件内容摘要与现场清单 | 旧 SHA256 快照仍按此模式解释 |
| `snapshot --mode metadata` | 路径、类型、大小、`mtime_ns`、`ctime_ns`、mode、symlink target | 属性相同不证明内容相同；普通文件内容不读取 |
| 计划中的 `historical` | 快照文件本身的 SHA256 | 不扫描目录，不表示本轮现场通过 |

没有可信不可变源或独立只读保障时，metadata 仅能支撑属性巡检。实际消费的科学输入、代码、spec、依赖与证据仍核内容 SHA256；科学结果复算及结果与候选字节的绑定规则不变。

目录遍历及采集期间的变动检查不构成原子目录快照。

科学输入核验按本阶段实际执行消费面组织，留存对应执行证据；c1 核实际依赖的内容身份，并沿用协议 B2 的证据绑定与独立复算要求。spec 中尚无本阶段消费者的其它输入，不因列在完整冻结表里就每轮重扫。既有科学输入摘要不改，也不以 metadata 替代；没有可信不可变保证时，过去的核验结果不能证明当前字节未变，不能跨时跳过实际消费输入的身份核验。

## 命令

以下为仓库根目录下的模板，路径须换成该轮白名单中的实际路径。

```bash
# 只填摘要占位符，不刷新已有值，不扫描快照目录。
python3 /absolute/path/to/science-gates/scripts/verify_digests.py write runs/example/reviews/B/dispatch-c1.md

# 新建内容基线；--mode 缺省也是 content。
python3 /absolute/path/to/science-gates/scripts/verify_digests.py snapshot data/example/ --mode content -o runs/example/reviews/B/snapshot-data-content.txt

# 新建 metadata 基线；真实采集时点是新监测起点。
python3 /absolute/path/to/science-gates/scripts/verify_digests.py snapshot data/example/ --mode metadata -o runs/example/reviews/B/snapshot-data-metadata.txt

# 含快照的派发必须显式传计划，提交前、提交后使用同一份已冻结计划。
python3 /absolute/path/to/science-gates/scripts/verify_digests.py check runs/example/reviews/B/dispatch-c1.md --precommit --snapshot-plan runs/example/reviews/B/snapshot-plan-c1.json
python3 /absolute/path/to/science-gates/scripts/verify_digests.py check runs/example/reviews/B/dispatch-c1.md --commit "<完整固定commit hash>" --snapshot-plan runs/example/reviews/B/snapshot-plan-c1.json

# 以下是显式全表核验，可能读取大量影像；不是每轮默认检查。
# 只有确实要求核整个 spec 输入表时才使用；sha256 在第三列时加 --digest-col 3。
python3 /absolute/path/to/science-gates/scripts/verify_digests.py check docs/example-spec-v001.md --digest-col 3
```

`snapshot` 目标已存在时拒绝覆盖。`--exclude` 沿用逗号分隔的相对路径 glob，排除面写入基线，复核采用同一范围。远端目标沿用 `host:/absolute/directory/` 写法；只读扫描权限仍由派发白名单决定。

JSON 基线通过 schema `agent-gates.snapshot.v2` 识别，JSON 键序无关。schema 的 `.v2` 是数据格式标识，不表示包内保留多套脚本实现。对象形 JSON 控制 / 数据文件最多读取 32 MiB 做静态解析；超出预算显式报错，不静默按普通文件处理而略过快照。

r1 / Closure 的 `check` 仍须带 `--claimed-changed <逗号分隔的Git相对路径>` 与 `--diff <本轮patch>`，并遵守协议的比较基准规则。`--precommit` 与 `--commit` 互斥；`--commit` 不能使用 `HEAD`、分支或短 hash。

## 协议升级后的门 A PASS 核验

协议升级获得本链授权后，不直接用 plain `check` 将旧 PASS 中的协议摘要与当前新版协议比较，也不修改旧 PASS。应从原 PASS 表逐行派生当前核验表，并附映射说明：

- spec、上游及其它输入保留原路径和原 SHA256，每行都保留并核验。
- 只有协议行改指向历史证据小文件；旧协议字节从原记录所引用的固定 commit 提取，摘要仍使用原 PASS 的旧值。
- 映射写明原 PASS 的位置、历史协议固定 commit 与原路径。Reviewer 将派生表逐行对回原表，核对唯一的路径替换和旧协议摘要。
- 派生表、映射、历史协议证据列入本轮派发；当前新版协议另行列入。派生表不是新 PASS，不改变旧 PASS 的结论或任何字节。

例如，对已生成的派生核验表执行：

```bash
python3 /absolute/path/to/science-gates/scripts/verify_digests.py check runs/example/reviews/B/gate-a-current-inputs.md
```

这不是科学输入跳过机制，也不允许把其它输入替换成历史副本。完整科学输入表的显式核验能力继续保留；每阶段的实际消费面由本轮派发与执行证据明确，而非工具自动猜测。

## 巡检计划最小示例

```json
{
  "schema": "agent-gates.snapshot-plan.v2",
  "entries": [
    {
      "baseline": "runs/example/reviews/B/snapshot-data-metadata.txt",
      "action": "verify"
    }
  ]
}
```

计划文件自身与 baseline 都必须列入同一派发的 SHA256 表。表内每份快照必须恰好对应一项；缺项、重复、表外 baseline 或含快照却未传计划均拒绝，不自动全盘扫描。先核计划和快照文件的身份，身份不符便停止，不采用其中内容安排扫描。

plan、baseline 和 diff 实际消费的字节均须绑定表列 SHA256。核验通过后使用同一份已认证字节或由其解析的对象，不再次按路径读取并解析另一份内容；这不构成对被巡检目录的原子快照保证。

保留迁移前基线时，在 `entries` 增加一项，例如：

```json
{
  "baseline": "runs/example/reviews/B/snapshot-data-content.txt",
  "action": "historical",
  "reason": "迁移前内容基线，仅留作历史证据；适用轮次、连续性证据和用户接受的边界见本链账本迁移裁定。"
}
```

`historical` 必须有非空 `reason`，并保留真实的裁定依据；示例文字不能代替授权。该动作只核基线文件身份，不给目录发出现场 PASS。计划不授予省略全部禁写范围的权限，Reviewer 仍逐项核对 spec 的禁写范围；任何 `verify` 巡检 FAIL 都必须使整次命令非零。

## 旧基线迁移

旧快照继续保留，不覆盖、不自动转成 metadata。旧 SHA256 和目录生成时间不能推算历史文件的 `mtime_ns` / `ctime_ns` 等属性；新 metadata 必须从当前实际采集时点开始。

要连接旧内容身份，需有实际内容核验或可信不可变来源的证据；若没有，明确记录连续性证据缺口，并由用户接受本链限定边界，不能称此前禁写已验证。已经发现的增删改、尚未获得授权的变更继续单独处理，不能通过重建基线或改标 historical 吸收。

## 执行与退出码

先做表格、参数、保全、固定提交、变更声明等静态检查，发现明确错误即停止昂贵读取。同一次调用内，本地内容摘要通过缓存去重，远端内容摘要按同批去重；目录巡检按计划逐份执行，不承诺目录去重。不使用跨调用或跨会话的持久缓存；Reviewer 必须独立执行核验，不能继承 Coordinator 的运行结果。

| 退出码 | 含义 |
|---|---|
| `0` | 本次所声明核验通过；不替代科学审查 verdict |
| `1` | `FAIL` |
| `2` | `TOOL_ERROR` |
| `3` | `NOT_VERIFIABLE` |

`FAIL` 与 `NOT_VERIFIABLE` 并存时取 `FAIL`。保存日志不能吞掉失败码；需要 `tee` 时必须启用管道失败传播：

```bash
set -o pipefail
python3 /absolute/path/to/science-gates/scripts/verify_digests.py check runs/example/reviews/B/dispatch-c1.md --precommit --snapshot-plan runs/example/reviews/B/snapshot-plan-c1.json 2>&1 | tee /tmp/agent-gates-check.txt
```

核验非零时停止后续派发。正式 evidence 仍按协议使用独占递增文件名；日志写出成功不表示核验成功。
