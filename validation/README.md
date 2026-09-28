# 作者验证记录

本目录记录 science-gates 1.1.0 的工具验证，不是独立科学审查，不授予真实实验 PASS。

- Python 3.11 共 **211 项测试通过**：摘要表/Git 15 项、摘要 CLI 及读取预算 48 项、
  目录巡检 29 项、工作流 66 项、SCI/报告集成 53 项。原始输出见
  [test-output.txt](test-output.txt)。其中失败 receipt 是预期的错误传播反例。
- 新增 18 项反例/行为测试：缺来源、伪造执行记录、虚报科学重算、报告命令不执行、
  超出单文件/总读取预算、读取中增长、远端内容与现场 content 快照拦截。
  metadata 巡检不会打开目标文件内容，显式执行阶段原有内容核验能力继续保留。
- 在独立目录复制 scripts/ 与 assets/，skill 和项目路径均含空格；小型合成示例、
  完整 check 与 6 个入口帮助检查共 8 项通过，见
  [portable-smoke-release.json](portable-smoke-release.json)。示例生成代码只实际运行一次，
  保存真实命令/日志；报告 check 不执行该命令，也不重算结果。
- 本版重新用浏览器检查 1280×900 和 390×844：来源折叠区可展开、来源链接可达、
  命令在窄屏换行、无页面横向溢出；图表在窄屏有独立滚动区域。
  原始检查见 [browser-release.txt](browser-release.txt)、[browser-mobile.txt](browser-mobile.txt)，
  截图在 output/playwright/。已实际查看两张截图。
- skill-creator quick_validate 通过，见 [skill-validation.txt](skill-validation.txt)。
  文档链接、来源文件与当前字节身份见 [summary.json](summary.json)。

当前仅有 scripts/ 一套实现。此前版本、协议与验证记录保留在 Git（本轮起点 f3baa37）。
原 swine-CT-article 项目不作修改；只读取来源清单中的 12 份小型脚本与协议核其身份。
本轮不连接科研服务器、不读取大型科学输入、不重跑真实科学分析。

读取预算是默认成本保护，不是沙箱或独立科学正确性证明。完整内容核验的底层命令仍存在，
必须按协议另行明确范围；不得自动以其绕过审查预算。未重算不等于证据缺口自动获得豁免。
