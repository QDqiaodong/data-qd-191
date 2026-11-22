# data-qd-191 · 色谱积分与标准品定量

保留时间窗与线性基线决定逐段积分，标准品响应和浓度决定校准曲线，未知样据此计算浓度、负值与外推诊断。

integrate.py 保留端点插值及分段面积；calibrate.py 拟合浓度关系；service.py 把同一积分证据用于定量。 详细字段、单位和失败边界见 [数据契约](docs/contract.md)。全部样例为自制合成数据，许可见 [来源](fixtures/SOURCES.md)。

## 独立安装与演示

Python >=3.11，本批实际使用CPython3.12.14，运行/测试仅标准库。每仓建立独立.venv，不共享其他项目运行环境；复用公共构建缓存，锁定setuptools80.9.0/wheel0.45.1。以下命令已在本机已有uv、Python与公共缓存时实际离线执行通过；其他机器首次缺缓存时需准备依赖。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/peak-quant --source fixtures/input.json --output run/report-initial.json
```

实际6项测试通过，演示摘要：

```json
{
  "analytes": 1,
  "extrapolated": 0,
  "quantified": 1,
  "runs": 3
}
```

报告原子新建，不覆盖旧文件或符号链接。演示路径已存在时请改用新输出名；输入结构/约束或文件错误退出2，正常业务诊断见契约。已实际核验重复JSON键拒绝且不生成结果、同名输出字节不变及符号链接不覆盖。

## 当前能力与后续集成

自定义JSON、给定保留时间窗口、等权线性校准；不自动识峰或控制真实仪器。 合理新增需求可沿现有身份和结果接入稀释/内标、加标回收与混合峰识别；当前尚未实现这些扩展，不保证固定数量未来题目。
