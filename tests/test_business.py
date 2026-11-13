import copy
import json
import tempfile
import unittest
from pathlib import Path


def load(name):
    return json.loads((Path(__file__).resolve().parents[1] / 'fixtures' / name).read_text(encoding='utf-8'))


from regression_slice_lab.slices import wilson, manual_comparison, inspect


class BusinessTests(unittest.TestCase):
    def test_empty_undefined(self):
        self.assertIsNone(wilson(0, 0))

    def test_boundary_not_zero_uncertainty(self):
        self.assertGreater(wilson(0, 10)[1], 0)
        self.assertLess(wilson(10, 10)[0], 1)

    def test_bad_counts(self):
        with self.assertRaises(ValueError):
            wilson(4, 3)

    def test_pending_visible(self):
        document = load('slices.json')
        result = manual_comparison(document['rows'], {}, -1)
        self.assertTrue(all(value['settled'] == 0 for value in result.values()))
        self.assertGreater(sum(value['pending'] for value in result.values()), 0)

    def test_repeated_users_counted(self):
        result = inspect(load('slices.json'))['manual_slices'][0]['observed']
        self.assertTrue(any(value['settled'] > value['users'] for value in result.values()))

    def test_unknown_slice_empty(self):
        document = load('slices.json')
        result = manual_comparison(document['rows'], {'tier': 'never'}, document['cutoff'])
        self.assertTrue(all(value['attempts'] == 0 for value in result.values()))
