"""自动搜索退化人群切片，并把发现与确认严格分开。

口径（unit_version = user-attempt-maturity-v1）：
- 成熟行：completed 非空且 outcome_known_at <= cutoff。未到观察期的任务只计 pending，
  既不进分母也不算失败。
- attempt：一行一次任务尝试，同一用户跨天的多次尝试是多个 attempt，不是多个用户。
- user：主推断单位。切片内先把每个用户在各版本上的成熟尝试聚合成人均成功率，
  再对同时出现在两个版本上的配对用户做单侧配对检验。
- device：同一设备跑过两个版本时做设备级配对，仅用于诊断版本组成混杂，
  不替代用户级结论。
- 发现（discovery 批次）与确认（confirmation 批次）是两次独立计算：确认只能套用
  已发布的规则，不允许用确认数据修改规则后再声称独立验证。
"""

import argparse
import hashlib
import json
import math
import sys

import numpy as np
from scipy import stats

from .slices import wilson

UNIT_VERSION = 'user-attempt-maturity-v1'
CONFIRM_ALPHA = 0.05
# 业务选规则时三类依据的固定权重：退化幅度、证据强度、新增覆盖。
SCORE_WEIGHTS = {'effect': 0.45, 'evidence': 0.25, 'coverage': 0.30}
# 配对用户占比低于该值时，无法排除版本间人群组成差异，标记混杂。
PAIRING_FLOOR = 0.5
REQUIRED_ROW_FIELDS = ('row_id', 'user_id', 'device_id', 'firmware', 'completed',
                       'outcome_known_at', 'batch')


def _fingerprint(payload):
    """对口径相关输入做内容指纹，用于把规则文件绑定到发现数据。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _validate(document):
    for key in ('rows', 'cutoff', 'metric_version', 'search_config'):
        if key not in document:
            raise ValueError('缺少关键字段：' + key)
    config = document['search_config']
    for key in ('dimensions', 'max_conditions', 'minimum_users',
                'investigation_capacity', 'false_discovery_target'):
        if key not in config:
            raise ValueError('search_config 缺少字段：' + key)
    if not isinstance(config['dimensions'], list) or not config['dimensions']:
        raise ValueError('候选维度为空')
    if len(set(config['dimensions'])) != len(config['dimensions']):
        raise ValueError('候选维度重复')
    if int(config['max_conditions']) < 1 or int(config['max_conditions']) > len(config['dimensions']):
        raise ValueError('规则复杂度超出维度范围')
    if int(config['minimum_users']) < 1:
        raise ValueError('切片大小下限必须为正')
    if int(config['investigation_capacity']) < 1:
        raise ValueError('调查容量必须为正')
    target = float(config['false_discovery_target'])
    if not 0 < target < 1:
        raise ValueError('错误发现目标必须在 (0,1) 之间')

    rows = document['rows']
    seen_ids = set()
    for row in rows:
        for field in REQUIRED_ROW_FIELDS:
            if field not in row:
                raise ValueError('行缺少字段：' + field)
        if row['row_id'] in seen_ids:
            raise ValueError('任务观测编号重复：' + str(row['row_id']))
        seen_ids.add(row['row_id'])
        for dim in config['dimensions']:
            if dim not in row:
                raise ValueError('行缺少维度字段：' + dim)
        if row['completed'] is not None and not isinstance(row['completed'], bool):
            raise ValueError('completed 只能是布尔值或 null')
    versions = sorted({row['firmware'] for row in rows})
    if len(versions) != 2:
        raise ValueError('新旧版本比较需要恰好两个 firmware 取值')
    unknown = {row['batch'] for row in rows} - {'discovery', 'confirmation'}
    if unknown:
        raise ValueError('未知 batch：' + ','.join(sorted(unknown)))
    return versions


class _Frame:
    """一个批次内的成熟行视图：numpy 掩码与统计单位编码。"""

    def __init__(self, rows, versions):
        self.rows = rows
        self.completed = np.array([bool(row['completed']) for row in rows])
        self.success = self.completed.astype(float)
        self.user_ids = sorted({row['user_id'] for row in rows})
        self.device_ids = sorted({row['device_id'] for row in rows})
        user_code = {value: code for code, value in enumerate(self.user_ids)}
        device_code = {value: code for code, value in enumerate(self.device_ids)}
        self.user_code = np.array([user_code[row['user_id']] for row in rows])
        self.device_code = np.array([device_code[row['device_id']] for row in rows])
        self.fw_masks = {version: np.array([row['firmware'] == version for row in rows])
                         for version in versions}
        self.versions = sorted(versions)

    def value_masks(self, dimensions):
        masks = {}
        for dim in dimensions:
            for value in sorted({row[dim] for row in self.rows}, key=str):
                masks[(dim, value)] = np.array([row[dim] == value for row in self.rows])
        return masks


_EXACT_DP_LIMIT = 25
_DIST_CACHE = {}
_DIST_CACHE_CAP = 512
_T_CRIT = {}


def _average_ranks(values):
    """平均秩（结取平均），与 scipy.stats.rankdata 默认口径一致。"""
    values = np.asarray(values, dtype=float)
    order = np.argsort(values, kind='mergesort')
    ranks = np.empty(values.size)
    ranks[order] = np.arange(1, values.size + 1)
    sorted_values = values[order]
    starts = np.flatnonzero(np.r_[True, sorted_values[1:] != sorted_values[:-1]])
    ends = np.r_[starts[1:], values.size]
    for start, end in zip(starts, ends):
        if end - start > 1:
            tied = order[start:end]
            ranks[tied] = ranks[tied].mean()
    return ranks


def _sign_rank_cdf(scaled_ranks):
    """固定秩下 W+ 的精确条件分布（符号随机化），按 2 倍缩放整数秩做 DP。"""
    key = tuple(sorted(int(rank) for rank in scaled_ranks))
    cached = _DIST_CACHE.get(key)
    if cached is not None:
        return cached
    total = int(sum(key))
    dist = np.zeros(total + 1)
    dist[0] = 1.0
    offset = 0
    for rank in key:
        dist[rank:offset + rank + 1] += dist[:offset + 1]
        offset += rank
    cdf = np.cumsum(dist) / 2.0 ** len(key)
    if len(_DIST_CACHE) < _DIST_CACHE_CAP:
        _DIST_CACHE[key] = cdf
    return cdf


def _wilcoxon_less_p(differences):
    """单侧（变差方向）配对符号秩检验。

    n <= 25 时用固定秩的精确条件分布（等价于无结时的精确 Wilcoxon 分布，
    有结时等价于确定性置换检验）；更大样本用结校正与连续性校正的渐近式。
    """
    differences = np.asarray(differences, dtype=float)
    nonzero = differences[differences != 0]
    n = nonzero.size
    if n == 0:
        return 1.0
    ranks = _average_ranks(np.abs(nonzero))
    w_plus = float(ranks[nonzero > 0].sum())
    if n <= _EXACT_DP_LIMIT:
        scaled = np.round(ranks * 2).astype(int)
        cdf = _sign_rank_cdf(scaled)
        index = int(round(w_plus * 2))
        return float(cdf[min(index, cdf.size - 1)])
    _, counts = np.unique(np.abs(nonzero), return_counts=True)
    tie_correct = float((counts ** 3 - counts).sum())
    mean = n * (n + 1) / 4.0
    variance = (n * (n + 1) * (2 * n + 1) - tie_correct / 2.0) / 24.0
    if variance <= 0:
        return 1.0 if w_plus >= mean else 0.0
    z = (w_plus - mean + 0.5) / math.sqrt(variance)
    return float(stats.norm.cdf(z))


def _fisher_less_p(new_success, new_failure, base_success, base_failure):
    """尝试级单侧 Fisher 精确检验（超几何尾概率），仅作描述性参照。"""
    total = new_success + new_failure + base_success + base_failure
    successes = new_success + base_success
    return float(stats.hypergeom.cdf(new_success, total, successes,
                                     new_success + new_failure))


def _t_crit(df):
    cached = _T_CRIT.get(df)
    if cached is None:
        cached = float(stats.t.ppf(0.975, df))
        _T_CRIT[df] = cached
    return cached


def _paired_test(differences, alternative='less'):
    """配对单侧检验；全零差值或样本过小不给伪 p 值。"""
    differences = np.asarray(differences, dtype=float)
    if differences.size < 2:
        return None
    if alternative != 'less':
        raise ValueError('未实现的检验方向')
    return _wilcoxon_less_p(differences)


def _mean_ci(values):
    """配对差值的 95% t 区间；零方差时退化为点。"""
    values = np.asarray(values, dtype=float)
    n = values.size
    if n == 0:
        return None
    mean = float(values.mean())
    if n < 3:
        return [mean, mean]
    sd = float(values.std(ddof=1))
    if sd == 0:
        return [mean, mean]
    half = _t_crit(n - 1) * sd / math.sqrt(n)
    return [mean - half, mean + half]


def _entity_rates(mask, codes, success, size):
    """按统计单位聚合：返回每个实体的尝试数与人均成功率（仅出现过的实体）。"""
    totals = np.bincount(codes[mask], minlength=size)
    present = totals > 0
    successes = np.bincount(codes[mask], weights=success[mask], minlength=size)
    rates = np.zeros(size)
    rates[present] = successes[present] / totals[present]
    return totals, rates, present


def _slice_stats(frame, mask, device_p=False):
    """计算一个切片在两个版本上的三层统计单位口径。"""
    baseline, candidate = frame.versions
    result = {'by_firmware': {}}
    user_totals = {}
    user_rates = {}
    user_present = {}
    device_rates = {}
    device_present = {}
    for firmware in frame.versions:
        part = mask & frame.fw_masks[firmware]
        attempts = int(part.sum())
        successes = int((part & frame.completed).sum())
        totals, rates, present = _entity_rates(
            part, frame.user_code, frame.success, len(frame.user_ids))
        _, drates, dpresent = _entity_rates(
            part, frame.device_code, frame.success, len(frame.device_ids))
        user_totals[firmware] = totals
        user_rates[firmware] = rates
        user_present[firmware] = present
        device_rates[firmware] = drates
        device_present[firmware] = dpresent
        result['by_firmware'][firmware] = {
            'settled_attempts': attempts,
            'successes': successes,
            'failures': attempts - successes,
            'success_rate': successes / attempts if attempts else None,
            'wilson_interval': wilson(successes, attempts),
            'users': int(present.sum()),
            'devices': int(dpresent.sum()),
            'attempts_per_user_max': int(totals.max()) if attempts else 0,
        }
    paired_users = user_present[baseline] & user_present[candidate]
    paired_devices = device_present[baseline] & device_present[candidate]
    union_users = user_present[baseline] | user_present[candidate]
    union_devices = device_present[baseline] | device_present[candidate]
    user_diffs = (user_rates[candidate] - user_rates[baseline])[paired_users]
    device_diffs = (device_rates[candidate] - device_rates[baseline])[paired_devices]

    base = result['by_firmware'][baseline]
    new = result['by_firmware'][candidate]
    result['rate_diff_attempts'] = (new['success_rate'] - base['success_rate']
                                    if new['success_rate'] is not None
                                    and base['success_rate'] is not None else None)
    result['union_users'] = int(union_users.sum())
    result['union_devices'] = int(union_devices.sum())
    result['paired_users'] = int(paired_users.sum())
    result['paired_devices'] = int(paired_devices.sum())
    result['paired_user_fraction'] = (float(paired_users.sum()) / float(union_users.sum())
                                      if union_users.sum() else None)
    result['user_mean_diff'] = float(user_diffs.mean()) if user_diffs.size else None
    result['user_diff_ci95'] = _mean_ci(user_diffs)
    result['device_mean_diff'] = float(device_diffs.mean()) if device_diffs.size else None
    result['raw_p_user'] = _paired_test(user_diffs)
    # 设备级 p 值只在需要混杂诊断时计算，避免对每个候选节点都付出检验开销。
    result['raw_p_device'] = _paired_test(device_diffs) if device_p else None
    # 尝试级 Fisher 仅作描述性参照：重复尝试不独立，不能替代用户级推断。
    if min(new['settled_attempts'], base['settled_attempts']) > 0:
        result['fisher_p_attempts_descriptive'] = _fisher_less_p(
            new['successes'], new['failures'], base['successes'], base['failures'])
    else:
        result['fisher_p_attempts_descriptive'] = None
    result['_user_set'] = {frame.user_ids[code] for code in np.flatnonzero(union_users)}
    return result


def _enumerate_rules(frame, dimensions, max_conditions, minimum_users):
    """深度优先枚举合取规则，按支持度剪枝；相同行集合只保留最短表示。"""
    value_masks = frame.value_masks(dimensions)
    values_by_dim = {}
    for dim, value in value_masks:
        values_by_dim.setdefault(dim, []).append(value)
    all_mask = np.ones(len(frame.rows), dtype=bool)
    evaluated = {}
    pruned = 0

    def support(mask):
        if int(mask.sum()) < minimum_users:
            return 0
        users = np.bincount(frame.user_code[mask], minlength=len(frame.user_ids))
        return int((users > 0).sum())

    def walk(start, depth, mask, conditions):
        nonlocal pruned
        for position in range(start, len(dimensions)):
            dim = dimensions[position]
            for value in values_by_dim[dim]:
                next_mask = mask & value_masks[(dim, value)]
                if not next_mask.any():
                    continue
                users = support(next_mask)
                if users < minimum_users:
                    pruned += 1
                    continue
                next_conditions = conditions + ((dim, value),)
                key = next_mask.tobytes()
                prior = evaluated.get(key)
                if prior is None:
                    evaluated[key] = (next_conditions, _slice_stats(frame, next_mask))
                elif len(next_conditions) < len(prior[0]):
                    evaluated[key] = (next_conditions, prior[1])
                if depth < max_conditions:
                    walk(position + 1, depth + 1, next_mask, next_conditions)

    walk(0, 1, all_mask, ())
    return evaluated, pruned


def _bh_adjust(pvalues):
    """Benjamini-Hochberg 调整；None 视为不可检验，不进入多重比较族。"""
    indexed = [(index, value) for index, value in enumerate(pvalues) if value is not None]
    ordered = sorted(indexed, key=lambda item: item[1])
    size = len(ordered)
    adjusted = [None] * len(pvalues)
    running = 1.0
    for rank in range(size, 0, -1):
        index, p_value = ordered[rank - 1]
        running = min(running, p_value * size / rank)
        adjusted[index] = min(running, 1.0)
    return adjusted, size


def _holm_adjust(pvalues):
    """Holm 调整，用于确认批次上数量已固定的发布规则族。"""
    indexed = [(index, value) for index, value in enumerate(pvalues) if value is not None]
    ordered = sorted(indexed, key=lambda item: item[1])
    size = len(ordered)
    adjusted = [None] * len(pvalues)
    running = 0.0
    for rank, (index, p_value) in enumerate(ordered, start=1):
        running = max(running, (size - rank + 1) * p_value)
        adjusted[index] = min(running, 1.0)
    return adjusted


def _classify(stats_block, q_value, target):
    """发现阶段状态：证据不足、混杂不可排除、候选、未入选。"""
    if stats_block['raw_p_user'] is None:
        return 'underpowered'
    user_diff = stats_block['user_mean_diff']
    device_diff = stats_block['device_mean_diff']
    pairing = stats_block['paired_user_fraction']
    confounded = pairing is not None and pairing < PAIRING_FLOOR
    if device_diff is not None and user_diff is not None and device_diff * user_diff < 0:
        confounded = True
    if confounded:
        return 'confounded'
    if q_value is not None and q_value <= target and user_diff < 0:
        return 'candidate'
    return 'not_selected'


def _pending_masks(rows, dimensions, cutoff):
    """未到观察期任务的口径：只计数，绝不计为失败。返回全量行上的掩码。"""
    pending = np.array([not (row.get('completed') is not None
                             and row['outcome_known_at'] <= cutoff) for row in rows])
    masks = {}
    for dim in dimensions:
        for value in sorted({row[dim] for row in rows}, key=str):
            masks[(dim, value)] = np.array([row[dim] == value for row in rows])
    return pending, masks


def _pending_counts(where, pending, masks):
    mask = np.ones(pending.shape[0], dtype=bool)
    for dim, value in where.items():
        condition = masks.get((dim, value))
        if condition is None:
            return {'attempts_seen': 0, 'pending_excluded': 0}
        mask &= condition
    return {'attempts_seen': int(mask.sum()), 'pending_excluded': int((mask & pending).sum())}


def _select_rules(candidates, capacity, all_affected_users):
    """兼顾退化幅度、证据强度与新增覆盖的贪心选择，而非只取最小 p 值。"""
    if not candidates:
        return []
    max_effect = max(-item['stats']['user_mean_diff'] for item in candidates)
    max_evidence = max(-math.log10(item['q_value']) for item in candidates)
    covered = set()
    chosen = []
    remaining = list(candidates)
    while remaining and len(chosen) < capacity:
        scored = []
        for item in remaining:
            effect = -item['stats']['user_mean_diff'] / max_effect if max_effect else 0.0
            evidence = (-math.log10(item['q_value']) / max_evidence) if max_evidence else 0.0
            new_users = len(item['stats']['_user_set'] - covered)
            coverage = new_users / len(all_affected_users) if all_affected_users else 0.0
            score = (SCORE_WEIGHTS['effect'] * effect
                     + SCORE_WEIGHTS['evidence'] * evidence
                     + SCORE_WEIGHTS['coverage'] * coverage)
            scored.append((score, item['rule_id'], effect, evidence, coverage, new_users))
        scored.sort(key=lambda entry: (-entry[0], entry[1]))
        score, rule_id, effect, evidence, coverage, new_users = scored[0]
        item = next(one for one in remaining if one['rule_id'] == rule_id)
        chosen.append({'rule_id': rule_id, 'where': item['where'],
                       'selection_rank': len(chosen) + 1,
                       'selection_score': score,
                       'score_components': {
                           'effect_norm': effect, 'evidence_norm': evidence,
                           'new_coverage_norm': coverage, 'weights': SCORE_WEIGHTS},
                       'new_users': new_users})
        covered |= item['stats']['_user_set']
        remaining = [one for one in remaining if one['rule_id'] != rule_id]
    return chosen


def _coverage_block(selected, user_sets_by_rule):
    """重叠切片的覆盖关系与去重后的影响规模。"""
    user_sets = {item['rule_id']: set(user_sets_by_rule[item['rule_id']])
                 for item in selected}
    relations = []
    for i, left in enumerate(selected):
        for right in selected[i + 1:]:
            left_set = user_sets[left['rule_id']]
            right_set = user_sets[right['rule_id']]
            intersection = left_set & right_set
            if left_set == right_set:
                relation = 'same_users'
            elif left_set <= right_set:
                relation = 'subset'
            elif right_set <= left_set:
                relation = 'superset'
            elif intersection:
                relation = 'overlap'
            else:
                relation = 'disjoint'
            relations.append({'left': left['rule_id'], 'right': right['rule_id'],
                              'relation': relation, 'shared_users': len(intersection)})
    cumulative = set()
    per_rule = []
    for item in selected:
        users = user_sets[item['rule_id']]
        new_users = len(users - cumulative)
        cumulative |= users
        per_rule.append({'rule_id': item['rule_id'], 'affected_users': len(users),
                         'new_users': new_users, 'cumulative_union_users': len(cumulative)})
    naive_sum = sum(len(users) for users in user_sets.values())
    return {'per_rule': per_rule, 'pairwise_relations': relations,
            'naive_sum_users': naive_sum, 'dedup_union_users': len(cumulative),
            'duplicate_users_removed': naive_sum - len(cumulative)}


def _clean_stats(stats_block):
    return {key: value for key, value in stats_block.items() if not key.startswith('_')}


def discover(document):
    versions = _validate(document)
    config = document['search_config']
    baseline, candidate = versions
    discovery = [row for row in document['rows'] if row['batch'] == 'discovery']
    if not discovery:
        raise ValueError('发现阶段需要 discovery 批次数据')
    rows = [row for row in discovery
            if row.get('completed') is not None and row['outcome_known_at'] <= document['cutoff']]
    if not rows:
        raise ValueError('discovery 批次没有成熟结局，无法搜索')
    frame = _Frame(rows, versions)

    evaluated, pruned = _enumerate_rules(
        frame, config['dimensions'], int(config['max_conditions']),
        int(config['minimum_users']))
    ordered = sorted(evaluated.values(), key=lambda pair: tuple(sorted(pair[0])))
    q_values, family_size = _bh_adjust([item[1]['raw_p_user'] for item in ordered])
    bonferroni = [min(1.0, p * family_size) if p is not None else None
                  for p in (item[1]['raw_p_user'] for item in ordered)]

    pending, pending_value_masks = _pending_masks(discovery, config['dimensions'],
                                                  document['cutoff'])
    evidence_rows = []
    candidates = []
    user_sets = {}
    for sequence, ((conditions, stats_block), q_value, bonf_p) in enumerate(
            zip(ordered, q_values, bonferroni)):
        where = dict(conditions)
        status = _classify(stats_block, q_value, float(config['false_discovery_target']))
        rule_id = 'R{:03d}'.format(sequence + 1)
        evidence_rows.append({
            'rule_id': rule_id, 'where': where, 'status': status,
            'discovery': _clean_stats(stats_block),
            'pending': _pending_counts(where, pending, pending_value_masks),
            'raw_p_user': stats_block['raw_p_user'],
            'q_value_bh': q_value, 'bonferroni_p': bonf_p,
        })
        user_sets[rule_id] = stats_block['_user_set']
        if status == 'candidate':
            candidates.append({'rule_id': rule_id, 'where': where,
                               'q_value': q_value, 'stats': stats_block})

    all_affected = set()
    for item in candidates:
        all_affected |= item['stats']['_user_set']
    selected = _select_rules(candidates, int(config['investigation_capacity']), all_affected)
    selected_ids = {item['rule_id'] for item in selected}
    for row in evidence_rows:
        if row['rule_id'] in selected_ids:
            row['status'] = 'selected_for_investigation'

    rules = []
    for item in selected:
        evidence = next(row for row in evidence_rows if row['rule_id'] == item['rule_id'])
        rules.append({'rule_id': item['rule_id'], 'where': item['where'],
                      'selection_rank': item['selection_rank'],
                      'selection_score': item['selection_score'],
                      'score_components': item['score_components'],
                      'discovery_status': evidence['status'],
                      'user_mean_diff': evidence['discovery']['user_mean_diff'],
                      'rate_diff_attempts': evidence['discovery']['rate_diff_attempts'],
                      'q_value_bh': evidence['q_value_bh']})

    binding_payload = {'rows': discovery, 'cutoff': document['cutoff'],
                       'metric_version': document['metric_version'], 'search_config': config}
    caveats = [
        '发现与确认分离：本文件仅由 discovery 批次生成，规则在发布后不得用确认数据改写。',
        '未到观察期的任务计为 pending，不进入分母也不计为失败；各规则给出 pending 数量。',
        '同一用户的多次尝试先聚合为人均成功率，用户级配对检验是主推断；attempt 级 Fisher 仅描述参照。',
        '多重比较族只包含满足 minimum_users 的可检验合取规则，共 {m} 个；被剪枝规则未进入检验族，'
        '其极端比例不构成证据。'.format(m=family_size),
        'BH q 值控制错误发现率目标 {t}；Bonferroni p 为更保守的族系参照。'.format(
            t=config['false_discovery_target']),
        'candidate 仍是待验证假设，只有 confirmation 批次复现后才作为回归证据。',
        '配对用户占比低于 {floor} 或设备级方向相反时标记 confounded，混杂不可排除。'.format(
            floor=PAIRING_FLOOR),
    ]
    return {
        'binding': {
            'unit_version': UNIT_VERSION,
            'metric_version': document['metric_version'],
            'input_sha256': _fingerprint(binding_payload),
            'search_config_sha256': _fingerprint(config),
            'cutoff': document['cutoff'],
            'firmware_versions': {'baseline': baseline, 'candidate': candidate},
            'batch': 'discovery',
        },
        'rules': rules,
        'discovery_evidence': {
            'family_size': family_size,
            'eligible_rules': len(ordered),
            'underpowered_pruned_count': pruned,
            'false_discovery_target': float(config['false_discovery_target']),
            'multiplicity': 'benjamini_hochberg with bonferroni reference; one-sided paired user test',
            'rules_evaluated': evidence_rows,
        },
        'confirmation': None,
        'coverage': _coverage_block(selected, user_sets),
        'caveats': caveats,
    }


def _rule_mask(frame, where, value_masks):
    mask = np.ones(len(frame.rows), dtype=bool)
    for dim, value in where.items():
        condition = value_masks.get((dim, value))
        if condition is None:
            return np.zeros(len(frame.rows), dtype=bool)
        mask &= condition
    return mask


def confirm(document, rules_document):
    versions = _validate(document)
    config = document['search_config']
    binding = rules_document.get('binding', {})
    if binding.get('metric_version') != document['metric_version']:
        raise ValueError('规则文件的 metric_version 与确认输入不一致')
    if binding.get('unit_version') != UNIT_VERSION:
        raise ValueError('规则文件的统计口径版本不受支持')
    if binding.get('search_config_sha256') != _fingerprint(config):
        raise ValueError('search_config 已变化，不能用新口径解释旧规则')
    if binding.get('firmware_versions') != {'baseline': versions[0], 'candidate': versions[1]}:
        raise ValueError('firmware 版本标签与规则文件不一致')
    discovery = [row for row in document['rows'] if row['batch'] == 'discovery']
    binding_payload = {'rows': discovery, 'cutoff': document['cutoff'],
                       'metric_version': document['metric_version'], 'search_config': config}
    if binding.get('input_sha256') != _fingerprint(binding_payload):
        raise ValueError('发现数据与规则绑定时不一致，确认必须基于同一份发现事实')

    frozen_rules = rules_document['rules']
    if not frozen_rules:
        raise ValueError('规则文件中没有可确认的规则')
    confirmation_rows = [row for row in document['rows'] if row['batch'] == 'confirmation']
    if not confirmation_rows:
        raise ValueError('确认阶段需要 confirmation 批次数据')
    rows = [row for row in confirmation_rows
            if row.get('completed') is not None and row['outcome_known_at'] <= document['cutoff']]
    frame = _Frame(rows, versions)
    value_masks = frame.value_masks(config['dimensions'])
    pending, pending_value_masks = _pending_masks(confirmation_rows, config['dimensions'],
                                                  document['cutoff'])

    per_rule = []
    raw_pvalues = []
    rule_user_sets = {}
    for rule in frozen_rules:
        where = rule['where']
        mask = _rule_mask(frame, where, value_masks)
        if mask.any():
            block = _slice_stats(frame, mask, device_p=True)
        else:
            block = {'union_users': 0, 'union_devices': 0, 'paired_users': 0,
                     'paired_devices': 0, 'paired_user_fraction': None,
                     'raw_p_user': None, 'raw_p_device': None,
                     'user_mean_diff': None, 'user_diff_ci95': None,
                     'device_mean_diff': None, 'by_firmware': {},
                     'rate_diff_attempts': None, 'fisher_p_attempts_descriptive': None,
                     '_user_set': set()}
        raw_pvalues.append(block['raw_p_user'])
        rule_user_sets[rule['rule_id']] = block['_user_set']
        per_rule.append({'rule_id': rule['rule_id'], 'where': where,
                         'pending': _pending_counts(where, pending, pending_value_masks),
                         'confirmation': _clean_stats(block)})
    holm = _holm_adjust(raw_pvalues)
    bh, _ = _bh_adjust(raw_pvalues)

    discovery_users = {row['user_id'] for row in discovery}
    confirmation_users = {row['user_id'] for row in confirmation_rows}
    batch_overlap = len(discovery_users & confirmation_users)

    minimum_users = int(config['minimum_users'])
    for sequence, block in enumerate(per_rule):
        stats_block = block['confirmation']
        if stats_block['union_users'] < minimum_users or stats_block['raw_p_user'] is None:
            status = 'underpowered_confirmation'
        else:
            user_diff = stats_block['user_mean_diff']
            device_diff = stats_block['device_mean_diff']
            confounded = (stats_block['paired_user_fraction'] is not None
                          and stats_block['paired_user_fraction'] < PAIRING_FLOOR)
            if device_diff is not None and user_diff is not None and device_diff * user_diff < 0:
                confounded = True
            if confounded:
                status = 'confounded'
            elif user_diff < 0 and holm[sequence] is not None and holm[sequence] < CONFIRM_ALPHA:
                status = 'reproduced'
            else:
                status = 'not_reproduced'
        stats_block['holm_p'] = holm[sequence]
        stats_block['q_value_bh'] = bh[sequence]
        stats_block['confirmation_status'] = status
        discovery_evidence = next(
            (item for item in rules_document['discovery_evidence']['rules_evaluated']
             if item['rule_id'] == block['rule_id']), None)
        if discovery_evidence is None:
            raise ValueError('规则文件缺少与规则对应的发现证据：' + str(block['rule_id']))
        stats_block['discovery_user_mean_diff'] = discovery_evidence['discovery']['user_mean_diff']

    caveats = list(rules_document.get('caveats', ()))
    caveats.append(
        '确认只在 confirmation 批次上复算已冻结规则，Holm 校正族大小为 {k}；'
        '规则条件与发现证据逐字保留，确认结果不回写规则。'.format(k=len(frozen_rules)))
    if batch_overlap:
        caveats.append('两个批次存在 {n} 个重复用户，批次独立性减弱。'.format(n=batch_overlap))
    else:
        caveats.append('两个批次用户互不重叠，确认样本与发现样本独立。')
    if not rows:
        caveats.append('confirmation 批次没有任何成熟结局，所有规则均无法确认。')
    caveats.append('确认输入必须携带与发现一致的 discovery 批次行，否则绑定校验失败并拒绝确认。')

    return {
        'binding': dict(binding, batch='confirmation',
                        confirmation_input_sha256=_fingerprint(
                            {'rows': confirmation_rows, 'cutoff': document['cutoff']})),
        'rules': frozen_rules,
        'discovery_evidence': rules_document['discovery_evidence'],
        'confirmation': {
            'batch': 'confirmation',
            'family_size': len(frozen_rules),
            'multiplicity': 'holm across frozen published rules at alpha {a}; BH reported alongside'.format(
                a=CONFIRM_ALPHA),
            'batch_user_overlap_with_discovery': batch_overlap,
            'rules_checked': per_rule,
        },
        'coverage': _coverage_block([{'rule_id': rule['rule_id']} for rule in frozen_rules],
                                    rule_user_sets),
        'caveats': caveats,
    }


def main(argv=None):
    """python3 -m regression_slice_lab.search 的命令行入口（discover/confirm）。"""
    from .io import read_json, write_json

    parser = argparse.ArgumentParser(
        description='自动搜索退化人群切片（discover）并在后批数据上确认（confirm）。')
    parser.add_argument('command', choices=['discover', 'confirm'])
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--rules', help='confirm 阶段绑定的已发布规则文件')
    args = parser.parse_args(argv)
    try:
        document = read_json(args.input)
        if args.command == 'discover':
            result = discover(document)
        else:
            if not args.rules:
                raise ValueError('confirm 必须通过 --rules 提供已发布的规则文件')
            result = confirm(document, read_json(args.rules))
        write_json(args.output, result)
    except (ValueError, KeyError, TypeError, OSError) as error:
        print('处理失败：' + str(error), file=sys.stderr)
        return 2
    print('结果已保存：' + args.output)
    return 0


if __name__ == '__main__':
    sys.exit(main())
