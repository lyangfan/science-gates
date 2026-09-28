# 当前版本的作者验证

本目录记录 science-gates 1.0.1 的工具验证，不是独立科学审查，不授予真实实验 PASS。

- 已移除 agent_gates、agent_gates_v2、agent_gates_v3 三套目录，当前实现位于 scripts/，
  测试集中在 scripts/tests/；旧实现与旧验证日志可从 Git 提交
  `2f4a94258a1ee55bc073ebdf8582f7adb2f5074a` 读取。
- Python 3.11 共 **193 项测试通过**：摘要表与 Git 辅助函数 15 项、摘要 CLI 43 项、
  目录巡检 29 项、工作流 61 项、SCI/报告集成 45 项。原始输出见
  [test-output.txt](test-output.txt)。测试中的失败 receipt 是预期的错误传播反例。
- 保留当前摘要、巡检、工作流和报告的全部原有测试；旧套件的摘要表、Git 映射、diff、
  远端分批等有效回归迁至当前接口，移除旧 ENG 专属或已由当前套件覆盖的用例。
- 从旧入口提取的 7 个解析/Git 函数，AST 与原实现一致；目录巡检模块逐字节不变。
  没有把旧 CLI 或整套旧实现换名藏入辅助模块。
- 将当前 scripts/ 和 assets/ 复制到独立目录后，从外部工作目录调用；skill 与项目路径均含空格。
  合成示例生成、完整 check 及 6 个脚本入口的帮助检查共 8 项通过，见
  [portable-smoke-release.json](portable-smoke-release.json)。
- 重新生成的示例 HTML 与整合前逐字节相同。因此保留创建时 1280×900、390×844 的浏览器
  检查和截图；本轮没有重新运行浏览器。图表、结论、来源和报告合同的测试仍全部执行。
- skill-creator 的 quick_validate 通过，见 [skill-validation.txt](skill-validation.txt)。
  文档链接、来源文件和当前文件摘要见 [summary.json](summary.json)。

原 swine-CT-article 项目不作修改；仅检查来源清单指定的 12 份小型脚本和协议文档的身份。
本轮不连接科研服务器、不读取大型科学输入、不重跑科学分析。
