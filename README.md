# regression_slice_lab

对人工指定的终端人群切片计算新旧版本成熟结局与基础差异，保存查询记录；并在候选维度上自动搜索退化人群切片，把发现与确认分开。

公开命令：

- `inspect`：核对数据和查看已记录的人工切片观测，不对观测结果作超出数据支持的推断。
- `discover`：只在 discovery 批次上枚举合取规则（复杂度、切片大小下限、调查容量由 `search_config` 指定），输出候选规则、多重比较校正后的发现证据、覆盖关系与口径说明，并冻结规则文件。
- `confirm`：把已冻结的规则文件原样套用到 confirmation 批次复算，给出复现状态；规则条件与发现证据逐字保留，确认结果不回写规则。

输入和输出使用 UTF-8 JSON。示例数据均为本地虚构记录，不连接终端、供应方或个人账户。

## 安装与运行

使用 Python 3.11.2，依赖精确版本在 `requirements.txt`。依赖可预先装好；运行和测试不执行下载。

```bash
python3 -m pip install -r requirements.txt
python3 -m compileall -q regression_slice_lab
python3 -m unittest discover -s tests -v
python3 -m regression_slice_lab inspect --input fixtures/slices.json --output /tmp/slice-observed.json
python3 -m regression_slice_lab.search discover --input fixtures/slices.json --output /tmp/slice-rules.json
python3 -m regression_slice_lab.search confirm --input fixtures/slices.json --rules /tmp/slice-rules.json --output /tmp/slice-confirm.json
```

`python3 -m regression_slice_lab discover|confirm` 与 `python3 -m regression_slice_lab.search discover|confirm` 等价。confirm 必须提供 `--rules`，且确认输入需携带与发现一致的 discovery 批次行用于绑定校验；绑定不一致会以非零状态退出。

正常运行会写出完整结果文件，标准输出给出保存位置。格式冲突、数据边界错误或缺失关键字段会以非零状态退出；不要将失败输出当作空业务结果。复用临时目录前应清理同名输出与数据库，以免混淆不同运行。

## 数据与目录

`regression_slice_lab/slices.py` 提供当前观测计算；`regression_slice_lab/search.py` 提供自动搜索与确认；`regression_slice_lab/io.py` 负责严格 JSON 读取和原子输出；`fixtures/` 是可重放的演练记录；`tests/` 验证领域边界和输入输出行为。

rows 包含 user_id、device_id、firmware、维度字段和 completed；outcome_known_at 与 cutoff 决定成熟结局。batch 为 discovery 或 confirmation。manual_slices 是当前人工查询的等值条件；自动搜索的允许列和复杂度在 search_config 中登记。

演练规模可在普通 CPU 上完成，运行说明不要求外部服务或真实设备。输出中的未定义值使用 JSON null，不用零替代。涉及版本的输入应保留原文件，方便复查当时的事实。

## 搜索与确认的口径

- 成熟行：completed 非空且 outcome_known_at ≤ cutoff；其余任务计为 pending，只计数，不进分母也不算失败。
- 统计单位分三层：attempt（一次任务尝试）、user（主推断单位，同一用户的多次尝试先聚合为人均成功率，配对用户做单侧符号秩检验）、device（同一设备跨版本时做配对，仅用于混杂诊断）。
- 多重比较：发现阶段对满足 minimum_users 的可检验合取规则族做 BH 校正（附 Bonferroni 参照）；确认阶段对已冻结规则族做 Holm 校正（附 BH 参照）。
- 状态：发现侧为 candidate / not_selected / underpowered / confounded；确认侧为 reproduced / not_reproduced / underpowered_confirmation / confounded。
- 规则文件通过 binding 绑定发现数据指纹、search_config 指纹、metric_version 与统计口径版本；任何一项变化都会拒绝确认。
