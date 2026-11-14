import copy
import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy import stats

from regression_slice_lab.search import (
    discover, confirm, _bh_adjust, _holm_adjust, _wilcoxon_less_p, _fisher_less_p)

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'slices.json'


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def make_row(row_id, user, firmware, completed, batch, known_at=0, **dims):
    row = {'row_id': row_id, 'user_id': user, 'device_id': user + '-dev',
           'firmware': firmware, 'completed': completed,
           'outcome_known_at': known_at, 'batch': batch}
    row.update(dims)
    return row


def make_document(rows, dimensions, max_conditions=2, minimum_users=8,
                  capacity=3, target=0.1, cutoff=4):
    return {'rows': rows, 'cutoff': cutoff, 'metric_version': 'test-metric-v1',
            'manual_slices': [],
            'search_config': {'dimensions': dimensions,
                              'max_conditions': max_conditions,
                              'minimum_users': minimum_users,
                              'investigation_capacity': capacity,
                              'false_discovery_target': target}}


def null_document(seed, n_users=40, n_dims=6, n_values=3):
    """完全无版本差异的重复模拟：所有行成功率相同，维度与结局无关。"""
    rng = random.Random(seed)
    dimensions = ['dim{}'.format(i) for i in range(n_dims)]
    rows = []
    for batch in ('discovery', 'confirmation'):
        for index in range(n_users):
            user = '{}-u{}'.format(batch, index)
            profile = {dim: '{}-v{}'.format(dim, rng.randrange(n_values))
                       for dim in dimensions}
            for firmware in ('v1', 'v2'):
                for _ in range(2):
                    rows.append(make_row('r{}'.format(len(rows)), user, firmware,
                                         rng.random() < 0.9, batch, **profile))
    return make_document(rows, dimensions)


class AdjustmentTests(unittest.TestCase):
    def test_bh_monotone(self):
        adjusted, size = _bh_adjust([0.01, 0.04, 0.5])
        self.assertEqual(size, 3)
        self.assertAlmostEqual(adjusted[0], 0.03)
        self.assertAlmostEqual(adjusted[1], 0.06)
        self.assertAlmostEqual(adjusted[2], 0.5)

    def test_bh_skips_untestable(self):
        adjusted, size = _bh_adjust([None, 0.02, None])
        self.assertEqual(size, 1)
        self.assertIsNone(adjusted[0])
        self.assertAlmostEqual(adjusted[1], 0.02)

    def test_holm(self):
        adjusted = _holm_adjust([0.01, 0.04, 0.5])
        self.assertAlmostEqual(adjusted[0], 0.03)
        self.assertAlmostEqual(adjusted[1], 0.08)
        self.assertAlmostEqual(adjusted[2], 0.5)


class TestStatisticTests(unittest.TestCase):
    def test_wilcoxon_matches_scipy_exact(self):
        rng = np.random.default_rng(11)
        for _ in range(30):
            diffs = rng.normal(0.2, 1.0, size=int(rng.integers(2, 20)))
            diffs = diffs[diffs != 0]
            if diffs.size < 2:
                continue
            reference = stats.wilcoxon(diffs, alternative='less', zero_method='wilcox',
                                       method='exact').pvalue
            self.assertAlmostEqual(_wilcoxon_less_p(diffs), reference, places=9)

    def test_wilcoxon_matches_scipy_permutation_with_ties(self):
        rng = np.random.default_rng(13)
        for _ in range(30):
            diffs = rng.choice([-1.0, -0.5, 0.5, 1.0, 2.0],
                               size=int(rng.integers(2, 12)))
            reference = stats.wilcoxon(
                diffs, alternative='less', zero_method='wilcox',
                method=stats.PermutationMethod(n_resamples=99999, rng=1)).pvalue
            self.assertAlmostEqual(_wilcoxon_less_p(diffs), reference, places=9)

    def test_wilcoxon_matches_scipy_asymptotic(self):
        rng = np.random.default_rng(17)
        for _ in range(30):
            diffs = rng.choice([-2.0, -1.0, -0.5, 0.5, 1.0, 2.0],
                               size=int(rng.integers(30, 60)))
            reference = stats.wilcoxon(diffs, alternative='less', zero_method='wilcox',
                                       correction=True, method='asymptotic').pvalue
            self.assertAlmostEqual(_wilcoxon_less_p(diffs), reference, places=9)

    def test_fisher_matches_scipy(self):
        rng = np.random.default_rng(19)
        for _ in range(30):
            a, b, c, d = (int(rng.integers(0, 25)) for _ in range(4))
            if a + b == 0 or c + d == 0:
                continue
            reference = stats.fisher_exact([[a, b], [c, d]], alternative='less').pvalue
            self.assertAlmostEqual(_fisher_less_p(a, b, c, d), reference, places=12)


class CaliberTests(unittest.TestCase):
    def test_pending_never_counted_as_failure(self):
        rows = []
        for index in range(16):
            user = 'u{}'.format(index)
            rows.append(make_row('a{}-old'.format(index), user, 'v1', True, 'discovery', grp='a'))
            rows.append(make_row('a{}-new'.format(index), user, 'v2', True, 'discovery', grp='a'))
        # 未到观察期的失败记录：只能计 pending，不能进分母。
        rows.append(make_row('pending-1', 'u0', 'v2', False, 'discovery', known_at=9, grp='a'))
        rows.append(make_row('pending-2', 'u1', 'v2', None, 'discovery', known_at=9, grp='a'))
        rows.append(make_row('pending-3', 'u2', 'v2', None, 'discovery', known_at=0, grp='a'))
        document = make_document(rows, ['grp'], max_conditions=1, minimum_users=4)
        result = discover(document)
        evidence = {tuple(sorted(row['where'].items())): row
                    for row in result['discovery_evidence']['rules_evaluated']}
        entry = evidence[(('grp', 'a'),)]
        self.assertEqual(entry['discovery']['by_firmware']['v2']['settled_attempts'], 16)
        self.assertEqual(entry['discovery']['by_firmware']['v2']['failures'], 0)
        self.assertEqual(entry['pending']['pending_excluded'], 3)
        self.assertEqual(entry['pending']['attempts_seen'], 35)

    def test_repeated_attempts_not_independent_users(self):
        rows = []
        # 一个用户在新版本上失败 10 次：是 1 个受影响用户，不是 10 个。
        rows.append(make_row('heavy-old', 'heavy', 'v1', True, 'discovery', grp='a'))
        for index in range(10):
            rows.append(make_row('heavy-new-{}'.format(index), 'heavy', 'v2', False,
                                 'discovery', grp='a'))
        for index in range(12):
            user = 'u{}'.format(index)
            rows.append(make_row('{}-old'.format(user), user, 'v1', True, 'discovery', grp='a'))
            rows.append(make_row('{}-new'.format(user), user, 'v2', True, 'discovery', grp='a'))
        document = make_document(rows, ['grp'], max_conditions=1, minimum_users=4)
        result = discover(document)
        evidence = {tuple(sorted(row['where'].items())): row
                    for row in result['discovery_evidence']['rules_evaluated']}
        entry = evidence[(('grp', 'a'),)]
        stats_block = entry['discovery']
        self.assertEqual(stats_block['by_firmware']['v2']['attempts_per_user_max'], 10)
        self.assertEqual(stats_block['paired_users'], 13)
        # 用户级口径：人均差 = -1/13；若把重复尝试当独立用户会得到约 -0.45。
        self.assertAlmostEqual(stats_block['user_mean_diff'], -1 / 13, places=9)
        self.assertAlmostEqual(stats_block['rate_diff_attempts'], 12 / 22 - 1, places=9)

    def test_confounded_when_pairing_insufficient(self):
        rows = []
        for index in range(2):
            user = 'paired{}'.format(index)
            rows.append(make_row('{}-old'.format(user), user, 'v1', True, 'discovery', grp='a'))
            rows.append(make_row('{}-new'.format(user), user, 'v2', False, 'discovery', grp='a'))
        for index in range(10):
            rows.append(make_row('old-only-{}'.format(index), 'old-only-{}'.format(index),
                                'v1', True, 'discovery', grp='a'))
            rows.append(make_row('new-only-{}'.format(index), 'new-only-{}'.format(index),
                                 'v2', False, 'discovery', grp='a'))
        document = make_document(rows, ['grp'], max_conditions=1, minimum_users=4)
        result = discover(document)
        evidence = {tuple(sorted(row['where'].items())): row
                    for row in result['discovery_evidence']['rules_evaluated']}
        entry = evidence[(('grp', 'a'),)]
        self.assertEqual(entry['status'], 'confounded')
        self.assertLess(entry['discovery']['paired_user_fraction'], 0.5)

    def test_underpowered_when_no_paired_users(self):
        rows = []
        for index in range(12):
            rows.append(make_row('old-{}'.format(index), 'old-{}'.format(index),
                                 'v1', True, 'discovery', grp='a'))
            rows.append(make_row('new-{}'.format(index), 'new-{}'.format(index),
                                 'v2', False, 'discovery', grp='a'))
        document = make_document(rows, ['grp'], max_conditions=1, minimum_users=4)
        result = discover(document)
        evidence = {tuple(sorted(row['where'].items())): row
                    for row in result['discovery_evidence']['rules_evaluated']}
        entry = evidence[(('grp', 'a'),)]
        self.assertEqual(entry['status'], 'underpowered')
        self.assertIsNone(entry['raw_p_user'])
        self.assertIsNone(entry['q_value_bh'])


class NullSimulationTests(unittest.TestCase):
    def test_null_simulation_controls_false_positives(self):
        # 完全无差异的重复模拟：BH 目标 0.1 下，12 次重复中出现误报批次的次数应很少。
        reps = 12
        alarmed = 0
        for seed in range(reps):
            result = discover(null_document(seed))
            alarmed += 1 if result['rules'] else 0
            self.assertEqual(result['confirmation'], None)
            self.assertIn('rules', result)
            self.assertIn('discovery_evidence', result)
            self.assertIn('coverage', result)
            self.assertIn('caveats', result)
        self.assertLessEqual(alarmed, 4)


class OverlapCoverageTests(unittest.TestCase):
    def _degraded_document(self):
        rng = random.Random(5)
        rows = []
        for index in range(60):
            user = 'u{}'.format(index)
            grp = 'a' if index < 30 else 'b'
            sub = 'x' if index % 2 == 0 else 'y'
            for firmware in ('v1', 'v2'):
                probability = 0.95
                if grp == 'a' and firmware == 'v2':
                    probability = 0.5
                for attempt in range(2):
                    rows.append(make_row('r{}'.format(len(rows)), user, firmware,
                                         rng.random() < probability, 'discovery',
                                         grp=grp, sub=sub))
        return make_document(rows, ['grp', 'sub'], max_conditions=2,
                             minimum_users=8, capacity=3)

    def test_overlapping_rules_deduplicated(self):
        result = discover(self._degraded_document())
        self.assertGreaterEqual(len(result['rules']), 2)
        coverage = result['coverage']
        relations = {entry['relation'] for entry in coverage['pairwise_relations']}
        self.assertTrue(relations & {'subset', 'superset', 'same_users', 'overlap'})
        self.assertLess(coverage['dedup_union_users'], coverage['naive_sum_users'])
        self.assertEqual(coverage['duplicate_users_removed'],
                         coverage['naive_sum_users'] - coverage['dedup_union_users'])
        affected = {entry['rule_id']: entry['affected_users'] for entry in coverage['per_rule']}
        self.assertEqual(coverage['dedup_union_users'], 30)
        self.assertEqual(max(affected.values()), 30)


class FixturePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = load_fixture()
        cls.discovered = discover(cls.document)
        cls.confirmed = confirm(cls.document, cls.discovered)

    def test_planted_regression_found(self):
        rules = self.discovered['rules']
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertEqual(rule['where'], {'tier': 'entry', 'app_pair': 'travel-office'})
        self.assertLess(rule['user_mean_diff'], -0.2)
        self.assertLessEqual(rule['q_value_bh'], 0.1)

    def test_discover_output_shape(self):
        for key in ('binding', 'rules', 'discovery_evidence', 'confirmation',
                    'coverage', 'caveats'):
            self.assertIn(key, self.discovered)
        self.assertIsNone(self.discovered['confirmation'])
        self.assertEqual(self.discovered['binding']['batch'], 'discovery')
        self.assertGreater(self.discovered['discovery_evidence']['family_size'], 0)

    def test_confirmation_reproduces(self):
        checked = self.confirmed['confirmation']['rules_checked']
        self.assertEqual(len(checked), 1)
        block = checked[0]['confirmation']
        self.assertEqual(block['confirmation_status'], 'reproduced')
        self.assertLess(block['user_mean_diff'], -0.2)
        self.assertLess(block['holm_p'], 0.05)
        self.assertEqual(self.confirmed['confirmation']['batch_user_overlap_with_discovery'], 0)

    def test_rules_frozen_when_confirmation_changes(self):
        modified = copy.deepcopy(self.document)
        where = self.discovered['rules'][0]['where']
        for row in modified['rows']:
            if (row['batch'] == 'confirmation' and row['firmware'] == 'v2'
                    and all(row[key] == value for key, value in where.items())):
                row['completed'] = True
        outcome = confirm(modified, self.discovered)
        self.assertEqual(outcome['rules'], self.discovered['rules'])
        self.assertEqual(outcome['discovery_evidence'], self.discovered['discovery_evidence'])
        block = outcome['confirmation']['rules_checked'][0]['confirmation']
        self.assertEqual(block['confirmation_status'], 'not_reproduced')

    def test_binding_rejects_changed_discovery(self):
        modified = copy.deepcopy(self.document)
        for row in modified['rows']:
            if row['batch'] == 'discovery' and row['completed'] is not None:
                row['completed'] = not row['completed']
                break
        with self.assertRaises(ValueError):
            confirm(modified, self.discovered)

    def test_binding_rejects_changed_config(self):
        modified = copy.deepcopy(self.document)
        modified['search_config']['minimum_users'] += 1
        with self.assertRaises(ValueError):
            confirm(modified, self.discovered)

    def test_confirm_requires_confirmation_batch(self):
        document = copy.deepcopy(self.document)
        document['rows'] = [row for row in document['rows'] if row['batch'] == 'discovery']
        with self.assertRaises(ValueError):
            confirm(document, self.discovered)

    def test_confirm_value_absent_in_confirmation_batch(self):
        document = copy.deepcopy(self.document)
        where = self.discovered['rules'][0]['where']
        document['rows'] = [row for row in document['rows']
                            if not (row['batch'] == 'confirmation'
                                    and all(row[key] == value for key, value in where.items()))]
        outcome = confirm(document, self.discovered)
        block = outcome['confirmation']['rules_checked'][0]['confirmation']
        self.assertEqual(block['confirmation_status'], 'underpowered_confirmation')
        self.assertEqual(outcome['rules'], self.discovered['rules'])


class CliTests(unittest.TestCase):
    def test_discover_confirm_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            rules_path = Path(directory) / 'rules.json'
            confirm_path = Path(directory) / 'confirm.json'
            discover_run = subprocess.run(
                [sys.executable, '-m', 'regression_slice_lab.search', 'discover',
                 '--input', str(FIXTURE), '--output', str(rules_path)],
                capture_output=True, text=True)
            self.assertEqual(discover_run.returncode, 0, discover_run.stderr)
            confirm_run = subprocess.run(
                [sys.executable, '-m', 'regression_slice_lab.search', 'confirm',
                 '--input', str(FIXTURE), '--rules', str(rules_path),
                 '--output', str(confirm_path)],
                capture_output=True, text=True)
            self.assertEqual(confirm_run.returncode, 0, confirm_run.stderr)
            result = json.loads(confirm_path.read_text(encoding='utf-8'))
            for key in ('rules', 'discovery_evidence', 'confirmation', 'coverage', 'caveats'):
                self.assertIn(key, result)

    def test_confirm_without_rules_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            run = subprocess.run(
                [sys.executable, '-m', 'regression_slice_lab.search', 'confirm',
                 '--input', str(FIXTURE), '--output', str(Path(directory) / 'out.json')],
                capture_output=True, text=True)
            self.assertEqual(run.returncode, 2)
            self.assertIn('--rules', run.stderr)


if __name__ == '__main__':
    unittest.main()
