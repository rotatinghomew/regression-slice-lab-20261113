# 记录口径

对人工指定的终端人群切片计算新旧版本成熟结局与基础差异，保存查询记录；自动搜索命令在候选维度上枚举合取规则，并把发现与确认分开。

rows 包含 user_id、device_id、firmware、维度字段和 completed；outcome_known_at 与 cutoff 决定成熟结局。batch 为 discovery 或 confirmation。manual_slices 是当前人工查询的等值条件；自动搜索的允许列和复杂度在 search_config 中登记。

数据字段采用稳定的英文标识符。编号是业务身份，不由输入数组中的位置决定。未到达的结果与已经观察到的否定结果是不同事实。货币、时间和统计单位应按字段约定解释，不在读取时默默变换。

当前命令的输出只解释当前公开计算，不承诺示例覆盖全部业务情形。批量重放应使用新的输出位置，避免人工查看上一次运行留下的文件。

## 自动搜索的统计单位

- attempt：一行一次任务尝试。同一用户跨天的重复记录是多个 attempt，不是多个独立用户；attempt 级 Fisher 只作描述性参照。
- user：主推断单位。切片内先把每个用户在各版本上的成熟尝试聚合为人均成功率，再对同时出现在两个版本上的配对用户做单侧符号秩检验（n≤25 用固定秩精确条件分布，更大样本用结校正与连续性校正的渐近式）。
- device：同一设备跑过两个版本时做设备级配对，仅用于诊断版本间人群组成混杂，不替代用户级结论。
- pending：completed 为 null 或 outcome_known_at 晚于 cutoff 的任务。pending 只计数，不进入任何分母，不计为失败。

## 发现与确认

discover 只读取 discovery 批次，枚举 1 至 max_conditions 个条件的等值合取，按 minimum_users 做支持度剪枝；被剪枝的规则不进入多重比较族，其极端比例不构成证据。可检验规则族用 BH 控制错误发现率（false_discovery_target），并附 Bonferroni 参照。发现侧状态：candidate（校正后显著且方向为退化）、not_selected、underpowered（配对用户不足，不给 p 值）、confounded（配对用户占比过低或设备级方向相反，混杂不可排除）。

confirm 只读取 confirmation 批次，逐字套用已冻结规则，Holm 校正族大小为发布的规则数。确认侧状态：reproduced、not_reproduced、underpowered_confirmation、confounded。确认输入必须携带与发现一致的 discovery 批次行；binding 中的发现数据指纹、search_config 指纹、metric_version、口径版本任一不一致都会拒绝确认。确认结果不回写规则文件。

## 规则选择与覆盖

交给业务的规则不超过 investigation_capacity，选择兼顾退化幅度、证据强度与新增覆盖（固定权重 0.45/0.25/0.30），不是只取最小的 p 值。coverage 给出所选规则两两之间的用户集合关系（subset/superset/same_users/overlap/disjoint）、每条规则的新增用户、朴素求和与去重并集，重叠切片的影响规模按去重后的用户并集解释。
