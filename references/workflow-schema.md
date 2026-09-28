# SCI 阶段工作流工具

本页记录包内通用后端的 schema。新 SCI 链使用 `scripts/science.py`，它在此后端前追加 report.md 规定的报告合同和验收；旧科学材料及历史记录原字节保留。它提供 `render/check/run/record`，不提交 Git、不调用调度器、不自行作科学 PASS，也不自动推进轮次。

运行依赖为 Python 3.10+ 标准库，以及只读的：

- `scripts/workflow.py`
- `scripts/verify_digests.py`
- `scripts/snapshots.py`
- `scripts/digest_table.py`

当前工作流必须把实际工具依赖列入对象表。合同可用 `required_paths` 强制这些路径齐全；它们的具体版本由本轮 manifest SHA 和派发表绑定。scripts/ 中只有这套当前实现；历史版本保存在 Git。已冻结链不会因本包升级而自动获得改用新字节的许可。

工作流对 `.jsonl/.ndjson` 科学记录作局部识别适配：在摘要后端同次内容读取的 bytes 上逐行检查，允许多个普通 JSON 对象，不把它们误解析为快照。空表、非法行、重复键、超过 32 MiB 或任一行伪装为快照均失败；不能靠改后缀绕过快照计划。适配只作用于本次加载的模块实例，不修改共享模块的全局行为。

## 信任起点与最少文件

每次命令必须从启动消息或已经核定的记录传入两个独立的 SHA256：`--manifest-sha256` 与 `--contract-sha256`。不能从刚被修改的文件重新算一个值再称原授权仍有效。工具核的是这些外部指定身份；工具无法自行证明哪个人批准了一个 hash。

冻结验收合同、阶段 manifest、执行 receipt 是不同对象。合同的完整 ID 集合还必须等于独立、已认证的源验收目录，不能从本次通过的结果反推。已有科学合同仍是科学判据权威；通用合同只固定阶段适用性、对象角色和证据要求，不能改写科学 scopes 或判据。二者的映射需在冻结/迁移审查中核对。

普通新链在门 A 冻结源目录及通用合同，后续按阶段创建新 manifest。现有冻结链的补证使用授权文件和 `scope: scoped_addendum`，阶段仅为 `addendum`；既有 PASS、报告和科学 spec 不重写，也不借补证重跑科学分析。

## 冻结通用合同

所有路径为仓库根下的规范相对路径；下列 SHA 为格式说明，实际文件用 prepare.py 填写真实值；脚本拒绝占位符。下方 JSON 展示底层字段，完整可运行的 SCI 示例由 make_example.py 生成，另须包含 report.md 的报告扩展。

```json
{
  "schema": "agent-gates.workflow-contract.v1",
  "chain": "example",
  "gate": "B",
  "authority": {"path": "reviews/AUTHORIZATION.md", "sha256": "<sha256>"},
  "source_catalog": {
    "path": "reviews/scientific-contract.json",
    "sha256": "<sha256>",
    "collection": "checks",
    "id_key": "id"
  },
  "requirements": [
    {
      "id": "V-EXAMPLE",
      "stages": ["addendum"],
      "na_stages": [],
      "deferred_stages": [],
      "required_roles": ["evidence", "product"],
      "required_objects": ["validation-report", "measured-product"],
      "receipt_required": false
    }
  ],
  "allowed_hosts": ["local", "compute_host"],
  "write_roots": ["reviews/repair/attempt_001"],
  "forbidden_paths": ["protected/history"],
  "required_paths": ["scripts/workflow.py"],
  "actors": {
    "coordinator": "<actual-coordinator-id>",
    "implementer": "<actual-implementer-id>",
    "reviewer": "<actual-independent-reviewer-id>"
  },
  "scope": "scoped_addendum"
}
```

`requirements` 的 ID 集合须恰好等于认证源目录 `checks[].id`。示例只画一项；不能因此缩减真实目录。集合为空、重复、来源不符均失败。NA/deferred 只允许出现在合同预授阶段，且实例中仍须明确列出 ID 与理由；缺文件或空证据不变成 NA。`deferred` 永远不能支撑整链候选 PASS。

合同与 manifest 必须显式指定同一 `gate: A|B`；report、receipt、history 和执行许可也绑定该门，不能混用。合同允许的 scope 只有 `full_chain` 与 `scoped_addendum`。门 A 仅 `b1/c1`：b1 可直接形成候选门 A PASS，c1 仅为一次关闭复核，必须引用 b1 非 PASS。门 A 不消费执行许可。

门 B 的阶段为 `prepare/b1/r1/r2/execute/c1/c2/c3/addendum`；执行前与结果阶段各最多两次修复。`r3/c4` 不被接受；同一阶段序列的全部前轮必须显式列在 history，紧邻前轮须为已认证非 PASS。已有 r2 的 history 不能以新 b1 绕过预算。工具验证所提供且经外部身份批准的历史；不扫描账本寻找被隐瞒的轮次，独立 Reviewer 仍须核历史完整性与真实链身份。`scoped_addendum` 仅用于门 B 的 addendum，不获得新一轮科学执行许可。

## 阶段 manifest

```json
{
  "schema": "agent-gates.workflow-manifest.v1",
  "chain": "example",
  "gate": "B",
  "stage": "addendum",
  "contract": {"path": "reviews/workflow-contract.json", "sha256": "<sha256>"},
  "objects": [
    {
      "id": "validation-report",
      "path": "reviews/repair/attempt_001/validator.json",
      "role": "evidence",
      "state": "present",
      "sha256": "<sha256>",
      "preservation": "git"
    },
    {
      "id": "measured-product",
      "path": "runs/existing_attempt/product.tsv",
      "role": "product",
      "state": "present",
      "sha256": "<sha256>",
      "preservation": "external:<existing-policy-reference>"
    }
  ],
  "steps": [],
  "acceptance": [
    {
      "id": "V-EXAMPLE",
      "status": "evidenced",
      "evidence": ["validation-report", "measured-product"]
    }
  ]
}
```

角色全集为 `authority/candidate/dependency/scientific_input/product/evidence`。`present` 对象必须存在且带 SHA256；`planned` 对象无 SHA，只可为 product/evidence，必须有唯一 `producer` step。不同对象不能使用同一路径或同一符号链接落点。合同、manifest、权威记录与源目录自动出现在派发输入表；其它实际依赖必须逐项列出。

路径不得含空白、反斜线、NUL、表格分隔符、`..` 或 `.git` 段。符号链接逃逸、禁止路径的链接别名均拒绝。远端输入仅接受合同显式 host 的规范 `host:/absolute/path`，并按 preservation 使用原有 `git:<local-path>` 或 `external:<依据>`。`forbidden_paths` 支持仓库相对路径或显式的远端 `host:/absolute/path`；各主机边界分别列出，不推测本地与镜像路径的对应关系。控制文件始终本地读取，不连接远端猜测依赖。

可选字段：

- `snapshot_plan`：一个 present 对象 ID，指向已有 `agent-gates.snapshot-plan.v2`。所有被列出的快照必须恰好被计划覆盖。先认证计划和基线的同一份 bytes、预检全部扫描范围，再启动普通对象哈希及目录巡检；基线与摘要后端同次内容摘要解析值再次比较，不按可变路径重读来决定扫描目标。本地 target 必须为仓库内绝对路径，避免进程 cwd 改变扫描面。
- `history`：`[{"gate":"B","stage":"c1","report":{"path":"...","sha256":"..."}}]`。引用原报告，不能改旧报告或仅改轮次文本。
- `execution_permit`：门 B 已认证执行前独立审查 JSON 的 `{path,sha256}`。门 B 科学执行与结果候选记录必须验证它；其 `object_sha256` 要覆盖实际执行输入的 ID、路径与 SHA。许可还必须带 `manifest_path/manifest_sha256`，绑定原 manifest，并通过与 record 相同的完整独立报告、派发、覆盖和 finding 核验；只写 PASS、空 checks、OPEN BLOCKER 或伪造原 manifest 都失败。历史 planned 产物现已存在不妨碍复核许可，但不因此获得覆盖权限。
- `acceptance[].receipts`：本次要核的 receipt 对象 ID。使用时须同时列出 receipt 的原 manifest、原始 stdout/stderr 和输入/输出对象，并绑定各自 SHA。

执行命令用结构化 argv：

```json
{
  "id": "validate",
  "kind": "acceptance",
  "argv": ["python3", "scripts/example_validator.py", "--contract", "reviews/scientific-contract.json"],
  "cwd": ".",
  "host": "local",
  "inputs": ["validator-code", "scientific-contract", "measured-product"],
  "outputs": ["new-validation-report"]
}
```

`kind` 为 `auxiliary/scientific/acceptance`。argv 作为参数数组直接执行，不经隐式 shell。需要 shell 时必须在已审 argv 中显式指定；该命令的真实科学性质仍由 Reviewer 审核，工具不能从命令名判定。

## 混合目录保护

`contract.protected` 可列 `id/path/policy/evidence_refs`；policy 为 `frozen/candidate_diff/new_attempt`，`evidence_refs` 为冻结的基线或授权 `{path,sha256}` 数组。manifest 的 `protection` 逐项给出 `id/policy/evidence:[对象ID]`，不得漏项、改变 policy、替换冻结锚的路径或 SHA。

该表检查证据覆盖和锚身份，不自动宣称候选 diff 的科学含义或整个目录内容未变。实际目录断言仍由显式巡检计划实施，候选写入范围仍须与冻结授权及固定 Git diff 独立核对；新 attempt 的产物仍须不覆盖。工具不刷新基线，也不把历史 metadata 当作内容证明。

活动快照的扫描根不能位于禁止子树内，也不能成为其祖先；本地路径同时核词面和解析后的链接落点。即使 baseline 声明 exclude 也静态拒绝祖先重叠，不把 glob 当越界授权。metadata 虽不读普通文件内容，仍枚举名称和属性，遵守相同限制。history 只核基线字节，不遍历历史 target，因此不会补造旧目录事实。若远端主机声明了禁止路径，当前薄层拒绝在该主机进行任何活动快照：静态检查不能排除远端祖先链接别名；应使用明确的小文件清单或单独获批的核验方案，不能静默穿越禁止面。

## 四个命令

从仓库根运行。变量是已经获得批准的 SHA，不是命令执行时从待检查文件临时替换出来的身份。

```bash
python3 /absolute/path/to/science-gates/scripts/science.py render reviews/stage.json \
  --manifest-sha256 "$MANIFEST_SHA" --contract-sha256 "$CONTRACT_SHA" \
  --out reviews/repair/attempt_001/dispatch.md

python3 /absolute/path/to/science-gates/scripts/science.py check reviews/stage.json \
  --manifest-sha256 "$MANIFEST_SHA" --contract-sha256 "$CONTRACT_SHA"

python3 /absolute/path/to/science-gates/scripts/science.py check reviews/stage.json \
  --manifest-sha256 "$MANIFEST_SHA" --contract-sha256 "$CONTRACT_SHA" \
  --commit "<fixed-full-commit>"

python3 /absolute/path/to/science-gates/scripts/science.py run reviews/stage.json \
  --manifest-sha256 "$MANIFEST_SHA" --contract-sha256 "$CONTRACT_SHA" \
  --step validate --receipt-dir reviews/repair/attempt_001/validate-run1

python3 /absolute/path/to/science-gates/scripts/science.py record reviews/stage.json \
  --manifest-sha256 "$MANIFEST_SHA" --contract-sha256 "$CONTRACT_SHA" \
  --review reviews/repair/attempt_001/review.json --review-sha256 "$REVIEW_SHA" \
  --out reviews/repair/attempt_001/ledger-append-candidate.md
```

`render` 只生成确定性派发，不声称对象内容已核。`check` 返回 `MECHANICAL_OK`，明确不等于科学 PASS。存在明确的结构、host、路径、缺文件、重复输出或创建归属错误时，先失败再停止内容核验。`--commit` 只接受完整固定 commit，并核 manifest/合同/源目录/授权及声明 Git 保全对象的 blob；不提供 commit 时只核当前身份和本地保全映射，不证明已提交。

`run` 当前只执行本地单条 argv，不提交或包装远端作业。远端既有产物可由摘要后端按显式清单只读核 SHA；现有调度证据通过经审查的科学验收器消费，不伪造成当前 run 的 receipt。`scoped_addendum` 禁止 `scientific` kind，科学执行还须满足阶段与独立执行前 PASS。run 独占新 receipt 目录，生成不可覆盖的 `start.json/stdout.bin/stderr.bin/receipt.json`；保存原始 argv/cwd/host/hostname/时间/原命令退出码及对象绑定。失败原码向调用者传播；信号退出使用 `128+signal`；命令为 0 但产物缺失或身份变化时工具非零。原始日志不截断，不删失败 receipt。

工具会在执行前后核本次显式输入身份，但不是文件系统沙箱：命令是否暗读暗写、输出创建的并发竞争及真实科学边界仍需代码审查与显式保护证据。receipt 由执行方可写，因此不是独立可信的调度器锚；科学复算/独立锚要求仍保留。

## 独立报告与候选追加记录

结构化报告 schema 为 `agent-gates.workflow-review.v1`，必要字段为：

```json
{
  "schema": "agent-gates.workflow-review.v1",
  "chain": "example",
  "gate": "B",
  "stage": "addendum",
  "manifest_path": "reviews/stage.json",
  "manifest_sha256": "<sha256>",
  "contract_sha256": "<sha256>",
  "reviewer_id": "<designated-independent-reviewer-id>",
  "verdict": "PASS",
  "checks": [{"id":"V-EXAMPLE","verdict":"PASS","evidence":["validation-report","measured-product"]}],
  "findings": [],
  "dispatch": {"path":"reviews/repair/attempt_001/dispatch.md","sha256":"<sha256>"}
}
```

`dispatch_commit` 可提供完整固定提交；提供时必须核其真实 blob。`checks` 必须覆盖本阶段全部必需 ID；判定为 `PASS/NA/DEFERRED_BY_OWNER`，且与 manifest 中的证据和合同预授一致。NA/deferred 要有 reason。每条 finding 必含 SCI §3 的全部字段 `id/check/severity/location/excerpt/authority/counterexample/closure`，另加 `state/owner`。所有文本非空；字段缺失或空白即退回，包括声称 CLOSED 的 finding，不自动降级或放行。

`authority` 是非空的权威条文定位文本；`closure` 写出恢复行为与可验证关闭条件，科学成立与否仍由独立 Reviewer 判断。开放 BLOCKER/MAJOR 拒绝；MINOR/OPINION 同样登记 owner。REFUTED 另须已认证用户裁定 `decision_ref:{path,sha256}`，不得拿 authority 条文文本代替裁定、也不得由工具自行否定 finding。候选片段保留 finding/check/severity/状态/owner/依据，完整原文仍在已绑定报告中。

`record` 重新核实际对象、报告与确定性派发身份，机械生成一个**候选**追加片段并明确门别：门 A 的 b1/c1 条件齐全可为 `CANDIDATE_PASS`；门 B 的 b1/r1/r2 为 `CANDIDATE_EXECUTION_PERMISSION`，c1/c2/c3 条件齐全才可为 `CANDIDATE_PASS`；本轮限定补证始终为 `SCOPED_ADDENDUM`。候选标题故意不是旧账本识别的 `## PASS`。正式入库与登记仍按协议办理。工具不追加旧 ledger，也不修改历史报告的可读性；需要更正时另建报告/附录，保留旧 SHA 并引用更正依据。

actor ID 与外部报告 SHA 由调度者/用户从真实独立会话提供。工具可以检查身份声明不同、字节绑定和字段完备，不能仅凭 JSON 证明报告实际出自独立人员，更不能替他们评价科学结论。

## 限额、错误与测试

manifest、合同、源目录、报告及 receipt 控制读取上限为 8 MiB；拒绝重复 JSON key、未知 schema/key、非规范路径和控制文件 symlink。解析直接消费已核 SHA 的同一份 bytes，并检查读取前后状态。摘要核验的对象形 JSON 限额为 32 MiB。没有跨调用 SHA 缓存；目录采集和普通文件检查都不是原子文件系统快照。

机械退出码为 `0` 成功、`1` 身份/完整性失败、`2` 工具故障、`3` 不可核验；`run` 的非零子进程码直接传播。使用 `tee` 时仍须 `pipefail`。所有输出路径要求父目录已经存在、目标不存在；不自动清理或覆盖旧结果。

```bash
python3 -B scripts/run_tests.py
```

测试只用 `/tmp` 中的小文件，覆盖独立必需集合、空表/空证据、NA 权限、源与当前对象身份、禁止路径/链接及祖先快照、创建归属、错误传播、原始 receipt 绑定、完整 finding、门别混用、许可空壳、预算回退和保护锚不可刷新。禁止子树反例使用普通 `.dat` 文件，不读取项目报告；不连接真实远端，不执行科学分析。
