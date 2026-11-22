# 离线质量签发与更正清单 v2

v1（见下节与 `docs/contract.md`）只回答未知样浓度；v2 在同一批合成轨迹上增加独立质控审阅、逐版签发和供下游增量应用的更正清单。不接入外部服务、真实样本或仪器；所有示例均为自制匿名合成数据。

## 命令与服务入口

- v1 保持不变：`peak-quant --source INPUT --output REPORT` 与 `peak_quant.service.process(data)`，契约、异常边界、结果字段完全保留。
- 新增签发：`peak-quant release --store DIR --request REQUEST [--source INPUT]`。
- 新增历史查询：`peak-quant release-show --store DIR --batch BATCH --revision N`。
- 两个新命令成功时向标准输出写入 UTF-8 JSON 签发报告（末尾一个换行）；同一版的重试输出与 `release-show` 输出按字节一致。业务暂扣（withheld）属于成功结果，退出 0；非法或冲突输入、缺失查询版本、提交前文件错误退出 2，原因写入 stderr，不输出成功报告，也不改变既有提交。
- 同一状态目录按顺序调用；不要求支持多进程并发写入。提交凭据只来自本地状态目录，stdout 与调用方持有的副本都不是凭据。
- Python API：`peak_quant.release.release(store_dir, request, source_path=None)` 返回 `(report_bytes, revision, retried)`；`peak_quant.release.release_show(store_dir, batch_id, revision)` 返回已提交报告字节。

## REQUEST 契约

严格 JSON 对象，限 8,000,000 字节；拒绝重复 JSON 键、未知或缺失字段、非法数值和不匹配引用。字段：

| 字段 | 规则 |
| --- | --- |
| `batch_id`、`request_id` | 沿用 v1 ID 语法 `[A-Za-z0-9_:-]{1,64}`；`request_id` 在状态目录内全局唯一 |
| `expected_revision` | 非负 JSON 整数（布尔不接受）；0 表示该批次尚无版本 |
| `source_sha256` | INPUT **原始字节**的 SHA-256，64 个小写十六进制字符；不是对 JSON 重排后的摘要 |
| `controls` | 以运行 ID 为键的对象；键必须引用 v1 `unknown` 运行。值是以分析物 ID 为键的非空对象，每项只含 `target`、`tolerance`，均为 v1 有界十进制字符串且非负。可只登记部分分析物 |
| `excluded_standards` | `[run_id, analyte_id]` 对数组；运行必须是 `standard`，分析物必须存在；不得重复 |
| `invalid_controls` | `[run_id, analyte_id]` 对数组；必须是已登记的质控观测（运行是 `unknown` 且在 `controls` 中有该分析物）；不得重复 |

两个审阅清单是当版**完整清单**而非补丁：下一版不再列出某对即解除标记。允许空对象、空数组。被登记为质控的运行整条不进入待签发样；其余 `unknown` 都是待签发样。`runs` 数组顺序即批内先后，距离按数组位置计算，绝不从 ID 或保留时间推断先后。

### 规范身份与重试/冲突判定

忽略 JSON 空白、对象键顺序和两个审阅清单的顺序；质控数值按精确数值比较（`0.50` 与 `0.5` 同身份）。其余身份字段全部参与比较，包括 `expected_revision` 与 INPUT 原始字节指纹。

- 已提交的 `request_id` + 相同规范身份：重试。直接返回原版报告，优先于 `expected_revision` 检查、INPUT 读取和重算；即使 INPUT 已被清理、该结果在后续版本被撤回，也原样返回。
- 已提交的 `request_id` + 不同规范身份：冲突，退出 2，不产生新版本。
- 新 ID：`expected_revision` 必须等于该批次当前版本（新批次为 0），且必须提供可读且指纹匹配的 INPUT；成功后修订号恰加一。
- 首版锁定批次的源字节指纹与质控登记（目标、容差按数值比较）；后续版本不能换源或改登记，只能调整两个审阅清单。

## 每版计算

先完整执行 v1 结构与数据校验（窗口覆盖、时间递增、ID 唯一、标准浓度等）；输入错误永远不会被归类为局部校准失败。通过后：

1. 沿用 v1 的端点插值、线性基线与带符号梯形积分。
2. 以分析物为单位，取所有未被 `excluded_standards` 排除的标准观测，用等权最小二乘线性模型（含截距）拟合。标准不足、浓度全同（拟合分母为零）、斜率非正分别给出原因码；这只让该分析物不可签发，其他分析物与整版提交不受影响。
3. 用当版校准独立判定每条登记质控观测。
4. 对每个待签发样×分析物，按批内位置取该样之前、之后最近的**为同一分析物登记**的质控；失败、被标无效的质控不跳过，其他分析物的登记不补位。

所有浓度、偏差、区间比较都用精确 `Fraction`，报告输出约分整数或分数字符串，不借助浮点判定边界。

### 质控通过条件（全部满足）

- 未在 `invalid_controls` 中标记（标记后保留观测与估计，但带 `marked_invalid` 原因）；
- 该分析物校准可用；
- 精确估计落在当版标准浓度闭区间内（端点算通过）；
- `|估计 - target| <= tolerance`（恰好相等算通过，例如响应 1.2、斜率 3、截距 0 时估计为 `2/5`，目标 `0.1`、容差 `0.3` 恰好通过）。

### 样签发条件（全部满足）

- 左右两侧都存在为该分析物登记的质控，且两条都通过；
- 校准可用；
- 自身估计非负且落在标准浓度闭区间内。

不满足则暂扣并列出全部适用原因；校准不可用时估计为 `null`，不编造数值或范围判定。有校准的暂扣结果保留精确估计，负数不截断。

## 报告结构（schema_version 2）

顶层字段：`schema_version`、`batch_id`、`request_id`、`source_sha256`、`revision`、`calibrations`、`controls`、`samples`、`corrections`。键经稳定排序、缩进固定，因此字节确定。

- `calibrations[]`：`analyte_id`、`available`；可用时含 `standard_run_ids`（当版实际入模观测，按批内顺序）、`slope`、`intercept`、`min_concentration`、`max_concentration`；不可用时含 `reason` 与未被排除的 `standard_run_ids`。
- `controls[]`：按批内位置、再按分析物 ID 排序；含 `run_id`、`analyte_id`、`manually_invalid`、`target`、`tolerance`、`estimate`（校准不可用为 `null`）、`passed`、`reasons`。
- `samples[]`：按批内位置、再按分析物 ID 排序；含 `run_id`、`analyte_id`、`estimate`（校准不可用为 `null`）、`status`（`released`/`withheld`）、`reasons`、`left_control_id`、`right_control_id`（缺失为 `null`）、`release`。
- `release`（仅签发时非空）记录完整签发依据：`concentration`；`calibration`（`standard_run_ids`、`slope`、`intercept`、范围）；`left_control`、`right_control`（各自 `run_id`、`analyte_id`、精确 `estimate`、`target`、`tolerance`，均为当版通过的质控）。
- 不复制 v1 的逐段积分明细。

### 原因码

| 码 | 适用对象 | 含义 |
| --- | --- | --- |
| `insufficient_standards` | 校准 | 入模标准少于两条 |
| `identical_concentrations` | 校准 | 入模标准浓度全同，模型不可辨识 |
| `non_positive_slope` | 校准 | 拟合斜率不满足严格为正 |
| `marked_invalid` | 质控 | 被 `invalid_controls` 人工标无效 |
| `calibration_unavailable` | 质控/样 | 该分析物当版无可用校准 |
| `control_estimate_out_of_range` | 质控 | 估计超出当版标准浓度闭区间 |
| `control_out_of_tolerance` | 质控 | 与目标的绝对偏差超过容差 |
| `missing_left_control` / `missing_right_control` | 样 | 该侧无同分析物登记质控 |
| `left_control_failed` / `right_control_failed` | 样 | 最近的侧位质控未通过（含被标无效） |
| `estimate_negative` | 样 | 自身精确估计为负 |
| `estimate_out_of_range` | 样 | 自身估计超出当版标准浓度闭区间 |

## 更正清单

`corrections[]` 只比较同批次紧邻的上一版与本版，键为样本 ID 与分析物 ID，顺序按批内位置与分析物 ID 稳定排列：

- `added`：上一版不存在或暂扣，本版签发；只有 `current` 记录（`previous` 为 `null`）。
- `withdrawn`：上一版签发、本版暂扣；只有 `previous` 记录。
- `replaced`：两版均签发但签发依据有任何变化（校准标准集合、斜率/截距/范围、侧位质控及其估计、目标容差、浓度），即便浓度相同也替换；旧新记录都给出。
- 两版均暂扣或依据完全相同：不产生更正。未签发的估计不会进入更正记录。

第一版相当于没有旧签发项；全暂扣或零更正都可以正常提交。

## 本地状态与崩溃恢复

状态目录布局：

```
DIR/
  state.json                     # 提交点：批次当前版本、源指纹/登记锁、请求 ID 索引
  requests/<request_id>.json     # 每个已提交请求的批次、修订号与规范身份
  <batch_id>/<revision>.json     # 每版不可变的签发报告字节
  .tmp-*                         # 仅在提交窗口内出现的暂存文件
```

提交流程：先完成全部校验与计算，再把报告、请求记录、新状态写入同目录暂存文件并 fsync，最后按“报告 → 请求记录 → state.json”顺序原子 `os.replace`，并 fsync 目录。`state.json` 是唯一提交点：

- 提交前任一时刻中断：重开时 `recover()` 清理所有未被 `state.json` 命名的暂存与孤立文件，不留下新当前版、半份可查询报告或被占用的请求 ID，原请求可原样重提为第 1 版。
- 提交已持久化但 stdout 未返回：状态与报告已落盘，用同一 `request_id` 重试取回原版，不会生成第二版。
- 历史查询只读指定报告，不依赖原始 INPUT 是否仍在；无需永久保存原始轨迹，也不依赖外部存储。
