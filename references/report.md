# HTML 科学报告：合同、数据和验收

报告是 spec 的完整科学呈现。先说明要回答什么、实际做了什么，再展示结果与科学结论；
审计状态不代替内容。默认中文、离线可打开、无 CDN 和遥测。

## 三份文件

1. 门 A 固定的报告合同：schema 为 science-gates.report-contract.v1，包含 spec 的
   path/sha256，以及 analyses 数组。每项有 id/title/visualization；visualization 为
   required 或 not_applicable，后者必须给出 reason。ID 全集及顺序固定。
2. 报告源：schema 为 science-gates.report.v1，顶层包含 title/overview/summary/spec/sources/analyses。
3. 输出 HTML 及机械核验 JSON：由 report.py 生成，均作为门 B 受审产物保全。

scripts/make_example.py 生成完整的合法示例和实算摘要。示例是接口样例，不是本项目的实验设计。

## 报告源结构

sources 每项有 id/path/sha256/format/label；format 支持 csv、tsv、text、png、jpeg。
表格必须指定 keys 数组作为唯一行键，缺键、重复键、空表、重复表头和错位列均失败。
路径为项目相对路径，单文件最大 8 MiB，表格最多 5000 行。
大型原始数据通过已经科学验收的小型结果表进入报告；脚本不扫描大目录。

analyses 每项：

| 字段 | 要求 |
|---|---|
| id/title | 必须对应报告合同，不能漏项、重排或替换 |
| status | completed / partial / not_executed；后两种有 reason |
| purpose | 该实验要回答的具体科学问题 |
| methods | 实际方法的非空字符串数组，说明影响解释的计算顺序和统计方法 |
| sample | 实际对象、分组、数量、分母、排除与单位 |
| results | 每项有 id/label/unit/source/where/column；显示值从表格取，不手抄 |
| figures | 可用 bar/scatter/table/image；绑定实际数据或图片，提供 title/caption/alt |
| conclusions | 每项 text/result_ids/source_ids；必须连接本节结果或证据来源 |
| limitations | 非空字符串数组；明确推断、外部证据及泛化边界 |

where 为列名到精确字符串值的对象，result 必须包括全部唯一键并且恰好选中一行。
figure 可以省略 where；省略时展示源表全部行，不静默抽样。
bar/scatter 还需 x/y/x_label/y_label；table 需 columns。
image 绑定已审 PNG/JPEG 小图，内嵌到 HTML；不接受外部图片 URL 或活动 SVG，
不将影像本体搬到本地。图片的选例、像素来源及科学含义仍按科学合同验收。
柱形图始终包括零点，最多 40 个唯一类别；散点图最多 200 点，拒绝缺失或非有限数值。
点更多或需要置信区间、热图等时，可以嵌入已审图像，或在门 A 批准相应渲染器与等效验收；默认脚本不悄悄聚合、
截断或猜测科学口径。折叠数据表显示图中完整原始行。

completed 必须有结果、结论和适用的可视化；not_executed 不得填入观测结果或科学结论。
报告的 summary、sample、方法描述及结论文本由作者撰写，脚本不声称已核其科学真实性。

## 与工作流连接

验收源目录 source_catalog 在 checks 之外增加 report：

~~~json
{
  "report": {
    "contract": {"path": "reviews/report-contract.json", "sha256": null},
    "source_object": "report-source",
    "html_object": "report-html",
    "verification_object": "report-check"
  }
}
~~~

null 仅供草案，用 prepare.py 填好后才能认证。报告合同自身必须在 manifest 中作为
present 的 authority 或 dependency。门 A 合同包含 A1–A6。
门 B 完整科学验收源目录及 requirements 增加：

| ID | 独立审查内容 |
|---|---|
| RPT-COVERAGE | 对照 spec 核完整分析范围和结果维度；缺结果仍显式呈现 |
| RPT-METHODS | 目的、实际方法、样本、排除、分母与执行相符 |
| RPT-RESULTS | 数字、方向、单位、不确定性与独立复算结果相符 |
| RPT-VISUALS | 图表绑定正确、轴和图例合理、浏览器中可读 |
| RPT-CONCLUSIONS | 结论强度有支持；无把缺证据说成无效应或无报道 |
| RPT-PROVENANCE | spec、版本、实际表格和外部原文可定位 |

六项均覆盖 c1/c2/c3，最终阶段不允许 NA/deferred；required_objects 包含以上三个对象，
required_roles 包含 product 和 evidence。报告源及 HTML 使用 product，核验 JSON 使用 evidence。
执行前如未生成报告，可以按合同将其最终验收留待结果阶段；不得凭缺文件自行改适用性。
所有报告消费的 spec/来源表必须同时出现在认证 manifest 或 authority 中。
source_catalog 不能只包含报告六项而漏掉本次科学验收；示例仅用于合成报告补证。

## 命令

从项目根运行，路径指向实际 skill：

~~~bash
python3 /absolute/path/to/science-gates/scripts/report.py render report-source.json \
  --source-sha256 "$REPORT_SOURCE_SHA" \
  --contract reviews/report-contract.json --contract-sha256 "$REPORT_CONTRACT_SHA" \
  --html reports/attempt_001/report.html --out reports/attempt_001/report-check.json

python3 /absolute/path/to/science-gates/scripts/report.py check report-source.json \
  --source-sha256 "$REPORT_SOURCE_SHA" \
  --contract reviews/report-contract.json --contract-sha256 "$REPORT_CONTRACT_SHA" \
  --html reports/attempt_001/report.html
~~~

目标父目录先按合同由唯一负责者创建；文件独占生成，不覆盖旧报告。
check 会从固定报告源和来源表重建 HTML，与受审 HTML 逐字节比较，不能只核页面存在。
模板版本也进入核验结果；模板变更需要重生成并复核受影响展示。
science.py 在结果 check/record 中强制检查上述材料；通过后仍仅为 MECHANICAL_OK。

## 人工和浏览器审查

独立 Reviewer 阅读实际报告，核图表/文本/数据一致性、完整结果、结论与文献支持。
在浏览器检查桌面及窄屏、链接锚点、可折叠数据表、轴标签和可读性；
历史执行身份和原文证据缺失如实列出，不用合成例子或当前 hash 补造。
每项发现有实际定位、反例和关闭条件，随最终门 B 报告入库。
公网发布或上传数据遵守项目单独授权。
