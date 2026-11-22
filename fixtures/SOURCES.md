# 样例来源

全部样例为作者自制合成数据，采用本仓MIT许可。遵循本仓docs/contract.md明确的有界模型，不来自客户、真实样本或生产作业。

`input.json` 为 v1 单分析物教学输入。`release/` 为 v2 离线签发表演数据，同样完全人工构造：

- `release/input.json`：分析物 A（窗口 0–2）、B（窗口 2–4），三条标准（浓度 0/1/2，积分恰为 0/2/4）、四条登记质控（目标 0.5、容差 0）与两个待签发样 U、V；批内顺序为 `c_early, c_left, U, c_right, V, c_right2`。
- `release/request-1.json`、`request-2.json`、`request-3.json`：三版审阅请求——首版全量签发；第二版把 `c_left` 的 A 观测标无效；第三版解除标记并排除浓度 2 标准的 A 观测。
- `request-1-retry.json`：首版请求的等价 JSON 表示（不同空白、键顺序、等价数值写法），用于重试身份演练。
- `request-1-conflict.json`、`request-stale.json`、`request-badfp.json`：同 ID 不同身份、过期修订号、错误指纹的拒绝用例。

