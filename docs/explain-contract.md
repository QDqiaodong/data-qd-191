# 候选峰解释包与离线联查 v3

v1（`docs/contract.md`）回答固定窗口的未知样浓度，v2（`docs/release-contract.md`）在本地状态目录做逐版质量签发；v3 在同一批纯合成轨迹上增加两步离线工作：先导出**不含原始采样数组**的候选峰解释包，再在没有源文件、缓存与运行进程的目录里，仅凭包和查询文件联查同一最佳解释内的浓度组合。不联网、不控制仪器、不涉及真实物质。

所有运算仍为精确 `Fraction`；比较、求和、排名都不使用浮点。失败（结构、引用、包版本或损坏）退出 2，原因写入 stderr，不新建目标文件，也不改动已有目标或符号链接；成功退出 0，输出经临时文件 + 硬链接原子新建，拒绝覆盖同名文件和符号链接。

## 命令

- 生成解释包：`peak-quant explain --source INPUT --candidates CANDIDATES --output PACKAGE`
- 离线联查：`peak-quant contrast --package PACKAGE --query QUERY --output REPORT`
- v1 的 `peak-quant --source ... --output ...` 与 v2 的 `release`、`release-show` 行为完全不变。
- Python 入口：`peak_quant.explain.build_package(source_raw, candidates_raw)`、`peak_quant.contrast.answer_query(package, query)`、`peak_quant.contrast.validate_package(data)`。

## INPUT 与标准校准（沿用 v1）

INPUT 仍是合法 v1 文档，沿用其全部上限、校验与数值口径（有界十进制字符串、2..2000 采样、1..200 运行、1..30 分析物、8,000,000 字节）。标准运行仍只用 v1 声明的**固定窗口**做端点插值、扣线性基线的带符号梯形积分，并对每个分析物作含截距等权最小二乘线性拟合。候选窗口不参与标准窗口选择，也不改变拟合。任一分析物的标准拟合不成立（标准不足、浓度全同、斜率不严格为正）时，本次 `explain` 整体失败，不交付部分解释包。

## CANDIDATES 契约

严格 JSON 对象，限 8,000,000 字节，拒绝重复 JSON 键、未知/缺失字段。恰含两个字段：

| 字段 | 规则 |
| --- | --- |
| `analyte_order` | 源中**全部**分析物 ID 的一个排列；洗脱次序只由该数组约定，不从候选时间或字母顺序推断 |
| `candidates` | 记录数组，0..30,000 项；每项恰含 `run_id, analyte_id, candidate_id, window, penalty` |

每条候选记录的规则：

- `run_id` 必须引用源中的 `unknown` 运行；`analyte_id` 必须存在；二者与 `candidate_id` 都沿用 v1 ID 语法 `[A-Za-z0-9_:-]{1,64}`；`candidate_id` 在同一运行、同一分析物内唯一（不同 ID 即使窗口或响应相同也是不同选择）。
- `window` 为两个 v1 十进制字符串，满足 `0 ≤ 起点 < 终点` 且完全落在该运行的采样范围（首末采样时间闭区间）内；不要求落在原固定窗口内。
- `penalty` 是 0..10000 的 JSON 整数，布尔值不接受。
- 同一（运行、分析物）对最多 12 个候选；省略某对即该对没有候选，允许整个 `candidates` 为空数组。
- 候选记录在数组中的排列不影响输出：同一输入重复生成到不同新路径逐字节相同。

## 最佳解释的定义（每个未知运行独立）

按 `analyte_order` 逐位决定：每位至多选择一个候选，也可缺位。任意两个相邻的**已选**窗口必须满足前者终点 ≤ 后者起点（端点相接允许）；缺位不切断该约束（即已选窗口之间隔着缺位位仍要比较）。

1. 先最大化已选窗口数；
2. 再在最大已选数中最小化已选候选的 penalty 之和；
3. 同时达到这两个指标的所有解释构成**最佳集合**，展示代表不淘汰其中任何成员。

代表解释仅用于展示与同值见证决胜：按分析物次序比较 `candidate_id` 序列取字典序最小者，缺位排在所有合法 ID 之后。没有可选窗口的运行只有唯一的全缺位解释（指标 0、0）。

候选响应用**候选自己的窗口**调用 v1 同一套积分口径，再用固定标准曲线 `浓度 = (面积 − intercept) / slope` 换算。保留精确浓度以及 v1 的 `negative`、`extrapolated` 标记（负浓度不归零，超标准浓度范围不截断）；这些标记不改变解释排名。

## PACKAGE 布局（package_version 3）

顶层恰含六个键，键排序、双空格缩进、末尾换行，因此字节确定：

| 键 | 内容 |
| --- | --- |
| `package_version` | 固定为 `3`；其他版本被 `contrast` 拒绝 |
| `source_sha256` | INPUT **原始字节**的 SHA-256（64 个小写十六进制字符），不是重序列化后的摘要 |
| `analyte_order` | 与 CANDIDATES 一致的分析物洗脱次序 |
| `calibrations` | 每分析物一条标准校准依据：`analyte_id`、`slope`、`intercept`、`min_concentration`、`max_concentration`、`standard_run_ids`（按 INPUT 中标准运行顺序） |
| `candidates` | 全部候选，按 `(run_id, 分析物次序, candidate_id)` 排序；每条见下 |
| `runs` | 每个未知运行一块最佳集合摘要（不按每套最佳解释存记录） |

`candidates[]` 每条恰含：`run_id`、`analyte_id`、`candidate_id`、`window`（两个十进制字符串）、`penalty`、`area`（积分面积的精确字符串）、`concentration`（约分整数或分数字符串）、`negative`、`extrapolated`。

`runs[]` 每块恰含：

- `run_id`、`sample_range`（该运行首末采样时间，两个十进制字符串）；
- `best_selected`、`best_penalty`：最佳集合共同的两项指标；
- `representative`：每分析物一位的代表序列，元素为候选 ID 或 `null`（缺位）；
- `attainable`：每分析物一位，含 `analyte_id`、`candidate_ids`（在至少一个最佳解释中出现过的全部已登记候选 ID，排序）、`vacancy_available`（该位是否可在某个最佳解释中缺位）。

包内不含采样数组、源路径、临时缓存或进程信息；把 PACKAGE 与 QUERY 复制到任意新目录即可独立联查。包结构在查询时被完整重新校验：字段集合、ID 语法、窗口与 penalty 范围、浓度是否与面积和校准一致、两个标记、两项最佳指标、代表序列与可取集合都从候选表重算比对，任何不一致按损坏拒绝。包文件读取上限为 16,000,000 字节（候选数受 30,000 条约束，正常体积远低于此）；超限按文件错误退出 2。

## QUERY 契约

严格 JSON 对象，限 8,000,000 字节，拒绝重复 JSON 键与未知/缺失字段，恰含 `run_id, terms, pins`：

| 字段 | 规则 |
| --- | --- |
| `run_id` | 包中某个未知运行的 ID；未知引用是输入错误 |
| `terms` | 以分析物 ID 为键的**非空**对象；值为 −1000..1000 内的非零 JSON 整数系数（布尔不接受）。键必须引用包中分析物 |
| `pins` | 以分析物 ID 为键的对象，允许为空；值为该运行该分析物的一个**已登记**候选 ID，或 `null`（要求该位缺位） |

- 引用未登记的候选 ID 是输入错误；引用已登记但从未进入最佳集合的候选不是输入错误，只会得到 `no_joint_explanation`。
- 对象键的排列不影响结果。

## 联查语义

只在包中该运行的**原最佳集合**内，筛选同时满足：

1. 每个 pin：固定候选已选，或固定缺位（`null`）；
2. `terms` 中每个分析物在解释里都已选峰——不能把缺位当浓度 0，也不能为满足查询退到较少峰或较高 penalty 的解释。

没有符合项时成功写出 `status: "no_joint_explanation"`，`minimum`、`maximum` 均为 `null`。否则对同一解释计算分数

```
score = Σ(term 系数 × 该解释中该分析物候选的精确浓度)
```

返回 `status: "joint_explanation"` 与最小值、最大值，各附一套达到该值的**完整解释见证**：见证逐分析物列出是否已选、候选 ID、窗口、penalty、精确浓度以及该位是否参与计分（`included_in_score`），缺位位的这些值为 `null`/`false`。同一极值存在多套见证时，沿用代表的字典序规则（缺位排在所有 ID 之后）决胜。分数为约分整数或分数字符串。

## REPORT 布局

顶层恰含：`package_version`、`run_id`、`terms`（按键排序）、`pins`（按键排序，保留 `null`）、`best_selected`、`best_penalty`（所依据的原最佳集合指标）、`result`。`result.status` 为 `joint_explanation` 时含 `minimum`/`maximum`，每个有 `score` 与 `witness[]`；为 `no_joint_explanation` 时两个极值均为 `null`、见证为空。

## 离线示例（自制合成数据）

```sh
peak-quant explain --source fixtures/explain/input.json \
  --candidates fixtures/explain/candidates.json --output run/package.json
peak-quant contrast --package run/package.json \
  --query fixtures/explain/query-a-minus-b.json --output run/range.json
# 把 package.json 与查询文件移到没有 INPUT/CANDIDATES 的新目录后仍可查询
peak-quant contrast --package package.json --query query-pin-a2.json --output run/pinned.json
```

该例两条标准曲线均为「面积＝浓度」，未知运行在常数基线 7 上有面积 1、3、5 的三个三角峰；最佳指标为 (2, 0)、代表 `(a1,b1)`；A−B 的范围是 [−4, −2]，最小见证 `(a1,b2)`、最大见证 `(a1,b1)`；固定 A=a2 后两极值同为 −2（见证 `(a2,b2)`）；同时固定 A=a2、B=b1 得到 `no_joint_explanation`。`candidates-b2-penalty1.json` 把 b2 的 penalty 改为 1，此时范围塌缩为 [−2, −2]，固定 A=a2 不再有联合解释。
