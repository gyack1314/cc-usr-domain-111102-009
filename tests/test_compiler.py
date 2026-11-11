import sqlite3
import tempfile
import unittest
from pathlib import Path

from workbench.compiler import compile_query
from workbench.domain import load_database
from workbench.io import read_json
from workbench.semantic import SemanticCatalog
from tests.test_semantic import Helper

ROOT = Path(__file__).resolve().parents[1]
BASE_QUERY = {
    'group_by': ['month', 'project'], 'filters': {},
    'metrics': ['donated', 'spent', 'gifts', 'donors', 'ratio'],
    'start': '2026-10-01', 'end': '2026-12-01',
}


class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root, self.database = Helper.build(self.tmp.name)
        self.catalog = Helper.catalog(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, **overrides):
        query = dict(BASE_QUERY)
        query.update(overrides)
        return compile_query(self.catalog, query).to_json()

    def test_sql_is_parameterized_and_identifier_quoted(self):
        plan = self.plan(id='q')
        step = plan['fact_steps'][0]
        self.assertNotIn('2026', step['sql'])
        self.assertEqual(step['params'], ['2026-10-01', '2026-12-01'])
        self.assertIn('"donations"', step['sql'])
        self.assertIn('LEFT JOIN "donation_projects"', step['sql'])
        self.assertIn('?', step['sql'])

    def test_plan_binds_catalog_digest(self):
        plan = self.plan()
        self.assertEqual(plan['catalog_digest'], self.catalog.digest())
        self.assertEqual(plan['plan_version'], 'grain-safe/1')

    def test_unknown_dimension_metric_and_raw_sql_rejected(self):
        with self.assertRaises(ValueError):
            self.plan(group_by=['project', 'nonexistent'])
        with self.assertRaises(ValueError):
            self.plan(metrics=['donated', 'unknown_metric'])
        evil = dict(BASE_QUERY, metrics=['donated; DROP TABLE donations'])
        with self.assertRaises(ValueError):
            compile_query(self.catalog, evil)

    def test_unreachable_dimension_rejected(self):
        # donor 是身份列，不是登记维度：编译器不猜连接路径。
        with self.assertRaises(ValueError):
            self.plan(group_by=['month', 'donor'])

    def test_bad_interval_and_filter_shapes_rejected(self):
        with self.assertRaises(ValueError):
            self.plan(start='2026-12-01', end='2026-10-01')
        with self.assertRaises(ValueError):
            self.plan(filters={'project': []})
        with self.assertRaises(ValueError):
            self.plan(filters={'month': ['2026-10-01']})
        with self.assertRaises(ValueError):
            self.plan(extra_field=1)

    def test_project_filter_keeps_original_weights(self):
        plan = self.plan(filters={'project': ['P1']})
        step = next(s for s in plan['fact_steps'] if s['fact'] == 'donations')
        self.assertIn("COALESCE(b.\"project\", 'unassigned') IN (?)",
                      step['sql'])
        self.assertIn('P1', step['params'])
        # 过滤条件中没有任何重归一除法或子查询修补。
        self.assertNotIn('1.0 /', step['sql'])
        self.assertNotIn('DISTINCT', step['sql'].upper())

    def test_region_null_member_uses_is_null(self):
        plan = self.plan(filters={'region': [None, 'east']})
        step = plan['fact_steps'][0]
        self.assertIn('IS NULL', step['sql'])
        self.assertNotIn(None, step['params'])
        self.assertEqual(step['params'][-1], 'east')

    def test_ratio_expands_into_component_facts(self):
        plan = self.plan(metrics=['ratio'])
        facts = {step['fact'] for step in plan['fact_steps']}
        self.assertEqual(facts, {'donations', 'expenses'})

    def test_only_involved_facts_are_compiled(self):
        plan = self.plan(metrics=['donated', 'donors', 'gifts'])
        self.assertEqual([s['fact'] for s in plan['fact_steps']],
                         ['donations'])

    def test_region_filter_is_parameterized(self):
        plan = self.plan(filters={'region': ['east']},
                         group_by=['month', 'region'])
        step = plan['fact_steps'][0]
        self.assertIn('"region" IN (?)', step['sql'])
        self.assertEqual(step['params'][-1], 'east')

    def test_empty_grouping_still_compiles_overall_query(self):
        plan = self.plan(group_by=[])
        self.assertEqual(plan['group_by'], [])
        self.assertTrue(plan['fact_steps'])

    def test_duplicate_dimensions_and_metrics_rejected(self):
        with self.assertRaises(ValueError):
            self.plan(group_by=['month', 'month'])
        with self.assertRaises(ValueError):
            self.plan(metrics=['donated', 'donated'])


if __name__ == '__main__':
    unittest.main()
