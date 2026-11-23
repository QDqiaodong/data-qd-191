# data-qd-191 · 色谱积分与标准品定量

保留时间窗与线性基线决定逐段积分，标准品响应和浓度决定校准曲线，未知样据此计算浓度、负值与外插诊断。v2 进一步在本地状态目录上实现逐版质量签发、独立质控判定与供下游增量应用的更正清单。v3 导出不含原始轨迹的候选峰解释包，并支持在离线目录联查同一最佳解释内的浓度差与固定窗口假设。v4 在同一份固定合成快照上增加合计披露的离线审查与可独立复核的扣留/变化证明。

integrate.py 保留端点插值及分段面积；calibrate.py 拟合浓度关系；service.py 把同一积分证据用于定量。request.py 严格校验签发请求并计算规范身份；release.py 负责校准/质控/签发计算、原子提交、崩溃恢复与历史查询。selection.py 以精确动态规划求最佳解释集合而不逐套枚举；candidates.py 校验候选批次；explain.py 生成自包含解释包；contrast.py 离线校验包并回答带 pins 的极值联查。assemble.py 从不可重读的分片流按锚点精确对时、精确比对重叠并去共线，装配出可直接定量的 v1 源文件。benchmark.py 按来源身份与精确曲线身份做留组盲评，逐折用 v1 拟合规则反推并精确计分。disclosure.py 以精确有理数高斯消元按业务优先级审查合计行，并生成只含对外行的 public.json 与仅供内部复核的 audit.json。reconcile.py 在各批本地 v1 定量之上做跨批零点对齐：生成树残差与树上差分判定整板取舍，精确 Fraction 给出每笔查询净差的极值、见证与入账值。v1 字段与失败边界见 [数据契约](docs/contract.md)，分片装配契约见 [装配契约](docs/assemble-contract.md)，签发字段、原因码与状态布局见 [签发契约](docs/release-contract.md)，解释包与联查字段见 [v3 契约](docs/explain-contract.md)，留组盲评字段、分组与计分规则见 [benchmark 契约](docs/benchmark-contract.md)，合计披露审查与离线复核边界见 [披露契约](docs/disclosure-contract.md)。全部样例为自制合成数据，许可见 [来源](fixtures/SOURCES.md)。

## 独立安装与演示

Python >=3.11，本批实际使用CPython3.12.14，运行/测试仅标准库。每仓建立独立.venv，不共享其他项目运行环境；复用公共构建缓存，锁定setuptools80.9.0/wheel0.45.1。以下命令在本机已有uv、Python与公共缓存时可离线执行；其他机器首次缺缓存时需准备依赖。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/peak-quant --source fixtures/input.json --output run/report-initial.json
```

### v2 离线签发表演

同一状态目录按顺序提交三版；重试与历史查询返回与首次提交逐字节一致的报告：

```sh
peak-quant release --store run/store --request fixtures/release/request-1.json --source fixtures/release/input.json
peak-quant release --store run/store --request fixtures/release/request-2.json --source fixtures/release/input.json
peak-quant release --store run/store --request fixtures/release/request-3.json --source fixtures/release/input.json
# INPUT 被清理后仍可按原请求 ID 取回原版（等价 JSON 表示、不传 --source）
peak-quant release --store run/store --request fixtures/release/request-1-retry.json
peak-quant release-show --store run/store --batch batch1 --revision 1
```

首版四项全签发；第二版撤回 U/A（最近左质控被标无效，不能借用更早的质控）；第三版 U/A 因超范围暂扣、V/A 因校准依据变化而替换，两条 B 始终不动。所有浓度与边界比较均为精确 Fraction 运算。

### v3 离线解释包与浓度联查表演

```sh
peak-quant explain --source fixtures/explain/input.json \
  --candidates fixtures/explain/candidates.json --output run/package.json
peak-quant contrast --package run/package.json \
  --query fixtures/explain/query-a-minus-b.json --output run/range.json
# 仅复制 package.json 与查询文件到新目录、挪走 INPUT/CANDIDATES 后仍可联查
peak-quant contrast --package package.json \
  --query fixtures/explain/query-pin-a2.json --output run/pinned.json
```

最佳解释先最大化已选峰数、再最小化总 penalty；包内记录两项指标、字典序代表和每分析物在最佳集合中可取的全部候选及可否缺位，从不按每套最佳解释存一份记录。联查只在原最佳集合内筛选 pins 与 term 覆盖，分数用精确 Fraction 求和并输出约分整数/分数字符串；无联合解释时成功写出 `no_joint_explanation`。30 个分析物各有两个同 penalty 候选（共 2³⁰ 套最佳解释）时，全系数 1 的极值 30/60 由动态规划直接求得，测试另用独立穷举器在小实例上校对指标、可取集合与带 pins 的极值见证。

### 分片流装配表演（v1 源文件重建）

归档只剩 PLAN 清单与一次性分片流；锚点给出每片精确仿射对时，重叠是同一条曲线的冗余观测。`assemble` 把子进程标准输入的 JSON Lines 落本地临时空间，精确 Fraction 对时、比对重叠（含单点相接）、按真实斜率转折去共线，最后原子新建一份只有 v1 字段的源文件，可直接跑旧定量入口：

```sh
peak-quant assemble --plan fixtures/assemble/plan.json \
  --output run/assembled.json < fixtures/assemble/stream.jsonl
peak-quant --source run/assembled.json --output run/quant.json
# 未知运行恰为 (0,0),(1,3),(2,0)；旧入口面积 3、浓度 1
```

大规模实跑由独立驱动完成：200 条运行 × 8 片 × 1,024 采样，按片序号轮转发送（最后一轮前没有任何运行集齐），只给消费进程设 `RLIMIT_AS=192 MiB`，独立 oracle 逐点核对全部 200 条运行：

```sh
python scripts/assemble_pipeline_demo.py --workdir run/pipeline
```

实测流约 33.7 MB、消费峰值 RSS 约 22 MiB、最终文件约 50 KB——归档体量不能按最终文件估算。输入、覆盖空档、重叠冲突、不可精确表示与文件错误均退出 2 且不留下目标，错误类别与运行/分片定位见[装配契约](docs/assemble-contract.md)。

### 按来源隔离的留组盲评表演（benchmark）

PLAN 把每条标准运行映射到来源 ID 并给出评分 ID；来源相同，或整条定义域与精确分段线性信号相同（含串联、共线点与十进制拼法差异）的标准被并查集归入同一折。每折整体留出后用其余标准按 v1 等权直线规则独立拟合，先盲推后才读取参考浓度；不足两条、浓度全同、斜率非正按固定顺序判为不可用，绝不借全批曲线救场。

```sh
# 单分析物工作算例（U/V/W 的来源与曲线关系，见 docs/benchmark-contract.md）
.venv/bin/python -I -m unittest discover -s tests -p 'test_benchmark.py' -v
# 大规模合成用例：200 标准 × 2000 点 × 30 分析物、100 折，
# 仅给 benchmark 进程设 RLIMIT_CPU=180 秒，逐折核对范围、斜率 998 与零误差
.venv/bin/python -I scripts/benchmark_synthetic_demo.py --workdir run/benchmark
# 缩小规模的快速离线核对
.venv/bin/python -I scripts/benchmark_synthetic_demo.py --workdir run/benchmark-small \
  --runs 8 --points 200 --analytes 3 --cpu 60
```

报告含版本与逐折 `held_out_run_ids`/`training_run_ids`/`scored_run_ids`，可逐折复核；全部数值为精确 `Fraction` 的约分整数/分数字符串，输入对象与数组重排后报告字节一致；无效文件、非法引用或输出冲突退出 2 且不覆盖已有文件或符号链接。

实际105项测试通过，v1 演示摘要：

```json
{
  "analytes": 1,
  "extrapolated": 0,
  "quantified": 1,
  "runs": 3
}
```

报告原子新建，不覆盖旧文件或符号链接。演示路径已存在时请改用新输出名；输入结构/约束或文件错误退出2，正常业务诊断见契约。已实际核验重复JSON键拒绝且不生成结果、同名输出字节不变及符号链接不覆盖。v2 的非法请求、身份冲突、过期修订号、错误指纹与缺失历史版本同样退出2且不改变既有提交；暂扣是成功报告并退出0。v3 的候选/查询非法字段与引用、超 8MB 输入、不支持的包版本或损坏包同样退出2且不新建目标；解释包与查询结果重复生成逐字节一致。

### 合计披露的离线审查与复核表演（v4）

同一固定合成快照（曲线「面积＝3×浓度」，X/Y/Z/W 浓度 1/3、2/3、4/3、5/3，只保护 X）按业务优先级逐行试发；历史 H=X+Y 已公开，请求 B=X+Z、C=Y+Z、D=Y+Z+W：

```sh
peak-quant disclosure --source fixtures/disclosure/input.json \
  --plan fixtures/disclosure/plan.json --output run/disclosure-main
peak-quant disclosure-check --plan fixtures/disclosure/plan.json \
  --public run/disclosure-main/public.json \
  --audit run/disclosure-main/audit.json
# 仅携带 plan.json、public.json、audit.json 到没有 SOURCE 与缓存的干净目录，复核仍通过
```

B=5/3 与 D=11/3 发出、C 扣留；C 的内部证明是有理数线性组合 (H+B−C)/2，成员系数恰为受保护单元 X 的单位向量，而最终变化证明给出一个移动 X 却不改变任何已发等式的方向。全扣留是成功（公开列表为空）；历史已确定任一受保护值、伪造历史总值、拟合或源校验失败均退出 2 且不留下 BUNDLE。同一语义输入重复生成两份文件逐字节一致，证明用精确 `Fraction` 高斯-若尔当消元求得；测试另用独立的素数模可行性 oracle 在 60 组随机 PLAN 上校对逐行决定。

### 跨批对账与对接板取舍表演（v5）

各离线工作站按本批 v1 曲线反推本地浓度；对接板把同一份虚拟对照件登记在两个不同批次的未知运行上，只允许加性零点修正、不重拟批内斜率。板号绑定整条记录及其全部读数，因此取舍以整板为单位：先验全板，全板一致即只承认不剔板；全板不连通直接判无法对账；否则枚举只剔一块板、且对所有所选分析物同时成立并保持全体连到 anchor 的情形，取各分析物候选的交集，不允许换分析物换板号或剔两块。

```sh
peak-quant reconcile --sources fixtures/reconcile/sources \
  --plan fixtures/reconcile/plan.json --output run/reconcile.json
# 可剔板恰为 AB、BC、CA（剔 CD 会断开 D）；查询区间 [0,1] 与 [1,1]，
# 前者见证 AB/BC、不入账，后者两端见证均为 AB、入账值 1
.venv/bin/python -I -m unittest discover -s tests -p 'test_reconcile.py' -v
# 月末大例：12,000 批共用一份底稿、12,000 块板的单环；只给对账进程
# 设 RLIMIT_CPU=90 秒，生成 fixture 与读报告断言不计入该配额
.venv/bin/python -I scripts/reconcile_month_end_demo.py --workdir run/month-end
```

每块保留板要求左右统一浓度精确相等，环上每条约束都参与判定。实现对每张批次图建一棵生成树，用非树边精确残差与树上差分（坏/好残差计数、带符号和、平方和）一次性判定每条树边割集能否由单一平移修复；查询只在同一可行情形内统一左右读数后相减，取精确最小/最大值与字典序最小的见证板号，仅当两端相等才给可入账净差。无法对账（区分 `full_graph_disconnected` 与 `no_common_single_panel_repair`）是成功报告、退出 0，仍列出全部查询与本地浓度；计划/引用/拟合/交付错误退出 2 且不新建、不覆盖目标或符号链接。报告对输入重排逐字节稳定，字段与复核步骤见 [对账契约](docs/reconcile-contract.md)。

## 当前能力与后续集成

自定义JSON、给定保留时间窗口、等权线性校准、逐版质控签发与更正清单；不自动识峰或控制真实仪器。 合理新增需求可沿现有身份和结果接入稀释/内标、加标回收与混合峰识别；当前尚未实现这些扩展，不保证固定数量未来题目。
