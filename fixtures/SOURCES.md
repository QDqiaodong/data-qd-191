# 样例来源

全部样例为作者自制合成数据，采用本仓MIT许可。遵循本仓docs/contract.md明确的有界模型，不来自客户、真实样本或生产作业。

`input.json` 为 v1 单分析物教学输入。`release/` 为 v2 离线签发表演数据，同样完全人工构造：

- `release/input.json`：分析物 A（窗口 0–2）、B（窗口 2–4），三条标准（浓度 0/1/2，积分恰为 0/2/4）、四条登记质控（目标 0.5、容差 0）与两个待签发样 U、V；批内顺序为 `c_early, c_left, U, c_right, V, c_right2`。
- `release/request-1.json`、`request-2.json`、`request-3.json`：三版审阅请求——首版全量签发；第二版把 `c_left` 的 A 观测标无效；第三版解除标记并排除浓度 2 标准的 A 观测。
- `request-1-retry.json`：首版请求的等价 JSON 表示（不同空白、键顺序、等价数值写法），用于重试身份演练。
- `request-1-conflict.json`、`request-stale.json`、`request-badfp.json`：同 ID 不同身份、过期修订号、错误指纹的拒绝用例。

`explain/` 为 v3 候选峰解释包与离线联查表演数据，完全人工构造，不含任何真实物质信息：

- `explain/input.json`：分析物 A（窗口 0–2）、B（窗口 2–4），三条标准使两条曲线均为「面积＝浓度」；未知运行 `u1` 在常数基线 7 上于 [0,2]、[2,4]、[4,6] 各有一个对称三角峰，扣基线面积恰为 1、3、5。
- `explain/candidates.json`：A 的 a1/a2 与 B 的 b1/b2 四个候选，penalty 全为 0；`candidates-b2-penalty1.json` 仅把 b2 的 penalty 改为 1，用于演示最佳集合收缩。
- `query-a-minus-b.json`、`query-pin-a2.json`、`query-pin-conflict.json`：分别演示 A−B 极值、固定 A=a2、以及互相冲突的 pins（后者成功返回 `no_joint_explanation`）。

`assemble/` 为分片流装配的手工小样例，完全人工构造，不含任何真实仪器信息：

- `assemble/plan.json`：单分析物（窗口 0–2），两条标准（浓度 0/2）与一条未知运行；未知运行区间 [0,2] 由左右两片拼成，锚点分别为 (0,0)/(3,1) 与 (0,1)/(3,2)。
- `assemble/stream.jsonl`：四片各一次的 JSON Lines（含标准片），未知运行装配后恰为 `(0,0),(1,3),(2,0)`，旧定量入口面积 3、浓度 1。
- `scripts/assemble_pipeline_demo.py`：200×8×1024 的大规模合成分片管道驱动（轮转发送、消费进程 192 MiB 内存上限、独立 oracle 逐点核对），数据全部在脚本内现造。

`scripts/benchmark_synthetic_demo.py` 为按来源隔离的留组盲评驱动，数据全部在脚本内现造：200 条标准（各 2,000 个整数时间点）、相邻两条配成 100 个不同来源组、30 个分析物窗口与统一参考浓度；不含任何真实物质、仪器或实验信息，benchmark 进程仅设 CPU 时间上限。`tests/test_benchmark.py` 的工作算例（S0/S1/S2/U/V/W）同为人工构造。

`disclosure/` 为 v4 合计披露审查的人工小样例，完全人工构造，不含真实物质信息：

- `input.json`：分析物 A（窗口 0–2）、B（窗口 2–4），三条标准使两条曲线均为「面积＝3×浓度」；未知运行 X、Y、Z、W 的 A 浓度恰为 1/3、2/3、4/3、5/3，W 的 B 浓度为 1。
- `plan.json`：主验收计划——只保护 (X,A)，历史 H=X+Y（总值 1），请求 B=X+Z、C=Y+Z、D=Y+Z+W；结果发出 B=5/3、D=11/3 并扣留 C。
- `plan-e.json`、`plan-swapped.json`、`plan-yw.json`：分别演示追加同成员请求 E 仍扣留、交换 B/C 优先级后决定反转、历史 H 改为 Y+W（7/3）后 B/C 发出而 D 扣留。
- `plan-hbc-history.json`、`plan-bad-total.json`、`plan-all-withheld.json`、`plan-analyte-b.json`：分别演示历史已使保护失效（生成失败）、伪造历史总值、单请求全扣留（公开列表为空）、同一运行另一分析物独立发布。
- `input-negative.json`、`plan-negative.json`：N 的浓度为 −1 且同时为 negative/extrapolated，演示负值与外推值原样进入合计而非被过滤成小计。


