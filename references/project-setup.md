# 项目接入

只收集任务真正需要的配置；不把某个项目的数据规模、网络、统计方法或远端地址变成通用默认值。
项目入口只记录配置与 skill 的固定版本，详细流程由 skill 单点维护。

## 首次接入需要确定

| 配置 | 约束 |
|---|---|
| 项目根与当前链 | 明确真实 Git 根、链名、上游、账本和当前阶段；不以文件存在推定通过 |
| 执行主机 | 主机别名、实际项目根、环境与调度入口；需要哪个主机才登记哪个 |
| 数据边界 | 输入角色、来源、schema、字段、单位、样本键、敏感性、大文件原地策略 |
| 写入边界 | 候选代码范围、新 attempt 根、只读历史及输出创建者 |
| 工程授权 | CLI 接线、路径、资源、有限重试的范围；哪些决定仍由用户确认 |
| 保全与发布 | 哪些小文件进入 Git，哪些产物原地保留；推送、镜像和公开发布各自的授权 |
| 报告 | HTML 路径、语言、章节对应的分析 ID、图表需求、证据入口 |

将这些值写入本链的 spec/认证合同或被其引用的小型项目配置，不新增另一套状态数据库。
缺少必需值时记 NOT_VERIFIABLE；可先完成不依赖它的工作。

## 版本与可移植性

- 记录本 skill 的 VERSION、固定 Git commit、实际协议与依赖脚本的 SHA256。
- 新链使用 scripts/science.py。脚本从自身位置寻找随包依赖，项目数据从 --root 解析。
- 配套通用工作流要求控制文件和本地对象采用规范的项目相对路径；路径段不能含空白、
  反斜线、表格分隔符或 ..。项目根和 skill 所在绝对路径可以有空格，调用时正确引用。
- 需要将代码纳入受审项目保全时，可把本 skill 固定版本的小型脚本复制到获准的工具目录，
  保持脚本目录结构；或者引用版本锁定的 skill 仓库并在交接时登记完整依赖身份。
  不依赖可变的全局安装路径来证明历史执行版本。
- 不默认安装第三方依赖，不默认 SSH，不默认提交作业。run 当前只运行已列出的本地 argv；
  远端执行使用项目已批准的调度入口，登记真实作业证据与验收器。
- 进行中的旧链保留原协议、PASS 和脚本字节；只有明确批准的迁移范围采用本 skill，
  旁置合同经过语义等价审查后才可用。旧记录不会因新版本发布自动失效或自动升级。

## 调用示例

先从已认证启动材料取得 manifest 与合同的 SHA，不能在核验失败后现场重算成“批准值”。

~~~bash
python3 "/absolute/path/to/science-gates/scripts/science.py" check reviews/stage.json \
  --root "/absolute/path/to/project" \
  --manifest-sha256 "$MANIFEST_SHA" --contract-sha256 "$CONTRACT_SHA"
~~~

报告只消费经过科学验收的小型结果表。影像、模型权重和完整 GWAS 数据不进入报告包，
也不因为生成报告而重新扫描。

## 程序生成摘要

先生成一份草案 JSON，把尚未填写的 path/sha256 引用写成 sha256: null，
再产生新文件供审查。已有摘要保持原值，核验是否匹配使用 check。

~~~bash
python3 /absolute/path/to/science-gates/scripts/prepare.py fill reviews/contract-draft.json \
  --root "/absolute/path/to/project" --out reviews/contract-candidate.json

python3 /absolute/path/to/science-gates/scripts/prepare.py refs docs/spec.md results/summary.csv \
  --root "/absolute/path/to/project" --out reviews/input-refs.json
~~~

该入口只读显式的小文件清单，每文件上限 8 MiB、批次上限 64 MiB；
不递归，不连接远端，不更新已有冻结摘要，不产生授权或 PASS。
