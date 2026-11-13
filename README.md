# regression_slice_lab

对人工指定的终端人群切片计算新旧版本成熟结局与基础差异，保存查询记录。

当前公开命令是 `inspect`，用于核对数据和查看已记录的观测，不对观测结果作超出数据支持的推断。输入和输出使用 UTF-8 JSON。示例数据均为本地虚构记录，不连接终端、供应方或个人账户。

## 安装与运行

使用 Python 3.11.2，依赖精确版本在 `requirements.txt`。依赖可预先装好；运行和测试不执行下载。

```bash
python3 -m pip install -r requirements.txt
python3 -m compileall -q regression_slice_lab
python3 -m unittest discover -s tests -v
python3 -m regression_slice_lab inspect --input fixtures/slices.json --output /tmp/slice-observed.json
```

正常运行会写出完整结果文件，标准输出给出保存位置。格式冲突、数据边界错误或缺失关键字段会以非零状态退出；不要将失败输出当作空业务结果。复用临时目录前应清理同名输出与数据库，以免混淆不同运行。

## 数据与目录

`regression_slice_lab/slices.py` 提供当前观测计算；`regression_slice_lab/io.py` 负责严格 JSON 读取和原子输出；`fixtures/` 是可重放的演练记录；`tests/` 验证领域边界和输入输出行为。

rows 包含 user_id、device_id、firmware、维度字段和 completed；outcome_known_at 与 cutoff 决定成熟结局。batch 为 discovery 或 confirmation。manual_slices 是当前人工查询的等值条件；自动搜索的允许列和复杂度在 search_config 中登记。

演练规模可在普通 CPU 上完成，运行说明不要求外部服务或真实设备。输出中的未定义值使用 JSON null，不用零替代。涉及版本的输入应保留原文件，方便复查当时的事实。
