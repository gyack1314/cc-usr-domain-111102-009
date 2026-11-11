import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from workbench.compiler import compile_query
from workbench.domain import load_database, raw_total
from workbench.engine import execute_plan
from workbench.io import read_json, write_json
from workbench.semantic import (
    SemanticCatalog, validate_database,
)

ROOT = Path(__file__).resolve().parents[1]


class Helper:
    @staticmethod
    def build(tmp):
        root = Path(tmp) / 'samples'
        shutil.copytree(ROOT / 'samples', root)
        database = Path(tmp) / 'facts.sqlite3'
        load_database(database, root)
        return root, database

    @staticmethod
    def catalog(root):
        return SemanticCatalog.from_directory(root)

    @staticmethod
    def query(root, name):
        for item in read_json(root / 'queries.json'):
            if item.get('id') == name:
                return item
        raise KeyError(name)


class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root, self.database = Helper.build(self.tmp.name)
        self.catalog = Helper.catalog(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_catalog_digest_is_stable(self):
        again = SemanticCatalog.from_directory(self.root)
        self.assertEqual(self.catalog.digest(), again.digest())

    def test_digest_changes_when_metric_registry_changes(self):
        before = self.catalog.digest()
        metrics = read_json(self.root / 'metrics.json')
        metrics = [m for m in metrics if m['id'] != 'gifts']
        write_json(self.root / 'metrics.json', metrics)
        self.assertNotEqual(before, SemanticCatalog.from_directory(self.root).digest())

    def test_metric_whitelist_rejections(self):
        cases = [
            {'id': 'x', 'op': 'avg', 'fact': 'donations', 'field': 'amount'},
            {'id': 'x', 'op': 'sum', 'fact': 'donations', 'field': 'donor'},
            {'id': 'x', 'op': 'count', 'fact': 'donations', 'field': 'amount'},
            {'id': 'x', 'op': 'count_distinct', 'fact': 'expenses',
             'field': 'amount'},
            {'id': 'x', 'op': 'ratio', 'numerator': 'spent',
             'denominator': 'ratio'},
        ]
        for case in cases:
            write_json(self.root / 'metrics.json', [case])
            with self.assertRaises(ValueError):
                SemanticCatalog.from_directory(self.root)

    def test_validate_accepts_seed(self):
        with sqlite3.connect(self.database) as conn:
            self.assertTrue(validate_database(self.catalog, conn))

    def _reject_bridge(self, table, mutation, *params):
        with sqlite3.connect(self.database) as conn:
            conn.execute(f'DELETE FROM {table}')
            conn.executemany(
                f'INSERT INTO {table} VALUES(?,?,?,?)', mutation)
            with self.assertRaises(ValueError):
                validate_database(self.catalog, conn)

    def test_duplicate_edge_blocks(self):
        self._reject_bridge('expense_projects', [
            ('e1', 'P1', 1, 1), ('e1', 'P1', 0, 1)])

    def test_dangling_fact_blocks(self):
        self._reject_bridge('expense_projects', [('ghost', 'P1', 1, 1)])

    def test_illegal_weight_blocks(self):
        self._reject_bridge('expense_projects', [('e1', 'P1', 1, 0)])

    def test_weight_sum_not_one_blocks(self):
        self._reject_bridge('expense_projects', [
            ('e1', 'P1', 1, 3), ('e1', 'P2', 1, 3)])

    def test_negative_weight_blocks(self):
        self._reject_bridge('expense_projects', [
            ('e1', 'P1', -1, 1), ('e1', 'P2', 2, 1)])


if __name__ == '__main__':
    unittest.main()
