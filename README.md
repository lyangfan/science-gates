# Science Gates

可跨项目使用的独立 SCI skill：从科学 spec、门 A 和门 B 到完整的 HTML 科学报告。
入口是 [SKILL.md](SKILL.md)。协议、脚本、报告模板及测试均随仓库提供；ENG 单独维护。

科学计算默认只做一次。门 B 审方法与代码、原始运行记录、已有结果及报告的一致性，
不默认再跑一遍实验或统计。HTML 在每个分析下引用生成代码、输入清单、实际命令和运行记录；
保留追溯材料，但不把这称为已经独立复现。必要的重算或额外大数据读取须单独说明成本并获准。

## 使用

把整个仓库作为一个 skill 目录提供给支持 SKILL.md 的 agent，或在任务中明确给出
本目录下 SKILL.md 的绝对路径。安装位置和自动发现方式由使用的 agent 宿主决定。
首次使用按 [项目接入](references/project-setup.md) 登记项目配置；仓库本身不含数据、
凭据或固定的服务器地址。

需要 Python 3.10+；脚本使用标准库。Git / SSH 仅在对应核验中使用。

~~~bash
python3 scripts/run_tests.py
python3 scripts/make_example.py --out /tmp/science-gates-demo
~~~

第二条命令要求目标目录不存在，生成小型合成数据、完整报告源、合同、工作流材料和
demo.html，可直接离线打开。示例数值不是真实科学结论。

工具不自行作出科学 PASS，也不能证明声明的 Reviewer 身份真实独立；
最终放行需要固定对象、独立审查及入库记录。报告校验可发现缺项、错源和渲染漂移，
结论是否由证据支持仍须科学审查。

scripts/ 下仅保留一套当前实现，测试集中在 scripts/tests/；历史实现留在 Git，
不维护 v2/v3 并行目录。文件中的 schema 版本号只标识数据格式。

通用脚本的来源、原始摘要与整合映射见 [source-provenance.json](references/source-provenance.json)。
版本固定与迁移规则见 [SCI 协议](references/protocol.md)。
