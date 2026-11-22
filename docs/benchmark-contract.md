# 按来源隔离的留组盲评 benchmark（v1）

教学离线 JSON 模型的一项评估能力：用带参考浓度的合成标准记录，检查 v1 定量模型在“未见来源”上的误差。全程只用 Python 3.11+ 标准库与自造合成数据，不涉及真实物质、仪器、实验或网络。入口为

```sh
peak-quant benchmark --source SOURCE --plan PLAN --output REPORT
```

`SOURCE` 是完整的 v1 输入，沿用[数据契约](contract.md)的文件大小（≤8,000,000 字节）、轨迹、窗口与参考浓度结构校验；未知运行仍须通过 v1 校验，但不参与分组、拟合或评分。全批能否拟合不是校验条件。成功退出 0；无效文件、非法引用或无法交付时退出 2，stderr 指明问题，且不新建目标、不覆盖已有文件或符号链接。

## PLAN 文档

UTF-8 JSON，不超过 1,000,000 字节，顶层恰有两个键：

* `origins`：对象，从**每条标准运行 ID** 到一个来源 ID 的完整映射。来源 ID 与运行 ID 都沿用 v1 标识语法 `[A-Za-z0-9_:-]{1,64}`。缺项、多项、引用未知运行或未知运行 ID 一律拒绝。
* `score_ids`：非空、无重复的标准运行 ID 数组。空数组、重复或引用非标准运行一律拒绝。

重复 JSON 键、非有限 JSON 常量、顶层不是对象、多余字段、非法标识、超大小均拒绝。

## 分组规则

标准运行满足以下任一关系就不能进入不同的折：

1. 来源 ID 相等；
2. **整个采样定义域与精确分段线性信号**完全相同。

两种关系都可串联（并查集求连通分量）：一条不评分的标准也能把两边连起来。曲线身份先把十进制字符串解析为精确 `Fraction`（等价拼法视为相同），再删除共线内点，只保留定义域端点与真实折点。因此在同域共线段上增删采样点、改变十进制拼法都不会改变身份。以下情况**不**算信号相同：定义域不同；只在分析物窗口内相同（窗口外不同）；仅积分面积相同。参考浓度不是来源身份的一部分。

只为含 `score_ids` 成员的分量生成折；不含评分成员的分量始终留在训练侧。每折整体留出该分量的全部标准运行（含不评分者），被留出记录不能影响拟合参数、可用性判定或训练浓度闭区间。折按“排序后的成员元组”字典序排列，折 ID 为 `F001`、`F002`……报告中每一折都给出 `held_out_run_ids`、`training_run_ids` 与 `scored_run_ids`，审阅人可逐条复核。

## 积分、拟合与盲评

积分沿用 v1 固定窗口、端点插值、线性基线与带符号面积；拟合沿用等权直线、含截距与正斜率门槛。每折用其余标准按分析物独立拟合。评分记录先被当作未知样反推预测，**取得预测后**才读取它自己的参考浓度计算误差。

每个分析物独立给出该折的 `slope`、`intercept`、`min_concentration`、`max_concentration`（均为约分整数/分数字符串），或按以下顺序判定的不可用原因（`available=false` 时四个数值字段为 `null`）：

1. `fewer_than_two_training_standards`：训练标准不足两条；
2. `all_training_concentrations_equal`：训练浓度全同（最小二乘分母为零）；
3. `slope_not_positive`：拟合斜率非正。

不可用分析物不借全批曲线救场，也不阻断同折其他分析物；整份报告照常交付。

每个评分记录 × 分析物一行，字段为 `run_id`、`area`、`reference_concentration`，以及 `prediction`、`error`（预测减参考的有符号误差）、`negative`、`extrapolated`。无可用曲线时后四项为 `null`；有曲线时即使为负或外插也如实计入，不截断、不跳过。外插按该折训练浓度闭区间判定。

## 汇总与总分

每个分析物给出：

* `requested_groups` / `requested_records`：要求评分的组数与记录数；
* `successful_groups` / `successful_records`：取得预测的组数与记录数；
* `group_mean_absolute_errors`：各折组内 MAE，只计该折 `score_ids` 成员；该折不可用时为 `null`（不当零误差）；
* `total_mean_absolute_error`：成功组 MAE 的等权平均（每组一票，复本不抬高组权重）；没有成功组时为 `null`。

## 确定性与数值

所有计算与范围判定使用精确 `fractions.Fraction`，报告数值均为约分后的整数或分数字符串。报告带 `schema_version`（当前为 1）。所有列表与键序都不依赖输入对象顺序：运行 ID 列表排序、分析物按 ID 排序、折按成员元组排序、JSON 以 `sort_keys=True` 序列化。因此对象键顺序、分析物数组、运行数组、评分 ID 排列变化后，报告字节一致。报告经临时文件原子新建，拒绝覆盖已有文件或符号链接。

报告布局（摘要）：

```json
{
  "schema_version": 1,
  "analyte_ids": ["A"],
  "score_ids": ["U", "W"],
  "folds": [
    {
      "fold_id": "F001",
      "held_out_run_ids": ["U", "V", "W"],
      "training_run_ids": ["S0", "S1", "S2"],
      "scored_run_ids": ["U", "W"],
      "analytes": [
        {
          "analyte_id": "A",
          "available": true,
          "unavailable_reason": null,
          "slope": "2",
          "intercept": "0",
          "min_concentration": "0",
          "max_concentration": "4",
          "scores": [
            {"run_id": "U", "area": "2", "reference_concentration": "1",
             "prediction": "1", "error": "0",
             "negative": false, "extrapolated": false}
          ]
        }
      ]
    }
  ],
  "summaries": [
    {
      "analyte_id": "A",
      "requested_groups": 1,
      "requested_records": 2,
      "successful_groups": 1,
      "successful_records": 2,
      "group_mean_absolute_errors": [
        {"fold_id": "F001", "mean_absolute_error": "3/2"}
      ],
      "total_mean_absolute_error": "3/2"
    }
  ]
}
```

## 合成算例

窗口 `[0,2]` 单分析物；标准 S0、S1、S2 的（浓度, 面积）为 (0,0)、(2,4)、(4,8)；U、V 均为 (1,2) 且整条曲线相同（V 多一个共线采样点），U 与 V 来源不同；W 为 (3,12)，与 V 同来源；三个 S 各不同来源。评分 U、W 时，不评分的 V 必须随 U/W 一起留出，斜率 2、截距 0，预测 1 与 6，组 MAE 与总分均为 3/2。仅把 V 的采样形状改为 `(0,0),(0.5,2),(2,0)`（面积与定义域不变但信号不再等于 U），U 单独成折，拟合斜率 13/5、预测 10/13；W 所在折仍预测 6；总分 21/13。

大规模 Linux 合成用例由 `scripts/benchmark_synthetic_demo.py` 生成并核对：200 条标准按相邻两条配成 100 个不同来源组、各组只评分第一条；每条 2,000 个整数时间点（0..1999），第 i 条在偶数时间 2..1996 的信号为 i、其余为 0；30 个分析物窗口为 `[j/100, 1999-j/100]`（j=0..29），参考浓度均为 i；源文件为紧凑 JSON 且在 v1 上限内。benchmark 进程设 `RLIMIT_CPU=180` 秒（不含数据生成与独立核对）。核对覆盖全部 100 折的训练浓度闭区间、斜率（均为 998）、各组与各分析物误差（均为 0），而非仅检查退出码。可复用与留组身份无关的中间结果（曲线解析、窗口端点插值、面积），但每条预测只使用该折允许的训练数据。
