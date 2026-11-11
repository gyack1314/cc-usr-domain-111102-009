import copy
import sqlite3
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from workbench.compiler import compile_query
from workbench.domain import load_database, raw_total
from workbench.engine import execute_plan
from workbench.io import read_json
from workbench.semantic import SemanticCatalog
from tests.test_semantic import Helper
from tests.test_compiler import BASE_QUERY


def frac(value):
    return {'numerator': value.numerator, 'denominator': value.denominator}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root, self.database = Helper.build(self.tmp.name)
        self.catalog = Helper.catalog(self.root)
        self.conn = sqlite3.connect(self.database)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def execute(self, **overrides):
        query = dict(BASE_QUERY)
        query.update(overrides)
        plan = compile_query(self.catalog, query)
        return execute_plan(self.catalog, self.conn, plan)

    def index(self, outcome):
        return {tuple(row['group']): row['metrics']
                for row in outcome['rows']}

    def test_monthly_weighted_amounts_and_ratio(self):
        rows = self.index(self.execute())
        self.assertEqual(rows[('2026-10', 'P1')]['donated'],
                         frac(Fraction(701, 3)))
        self.assertEqual(rows[('2026-10', 'P1')]['spent'], frac(Fraction(40)))
        self.assertEqual(rows[('2026-10', 'P1')]['gifts'],
                         frac(Fraction(4, 3)))
        self.assertEqual(rows[('2026-10', 'P2')]['donated'],
                         frac(Fraction(202, 3)))
        self.assertEqual(rows[('2026-10', 'P2')]['ratio'],
                         frac(Fraction(60, Fraction(202, 3))))
        self.assertEqual(rows[('2026-10', 'P2')]['donors'], 1)

    def test_expense_only_group_keeps_zero_additives_and_null_ratio(self):
        rows = self.index(self.execute())
        p3 = rows[('2026-11', 'P3')]
        self.assertEqual(p3['donated'], frac(Fraction(0)))
        self.assertEqual(p3['spent'], frac(Fraction(90)))
        self.assertEqual(p3['donors'], 0)
        self.assertIsNone(p3['ratio'])

    def test_unassigned_member_survives_left_join(self):
        rows = self.index(self.execute())
        unassigned = rows[('2026-11', 'unassigned')]
        self.assertEqual(unassigned['donated'], frac(Fraction(300)))
        self.assertEqual(unassigned['gifts'], frac(Fraction(1)))

    def test_distinct_donor_is_identity_set_not_weighted(self):
        overall = self.index(self.execute(group_by=['month']))
        self.assertEqual(overall[('2026-10',)]['donors'], 1)
        # 同一捐助者A经d1同时落在P1/P2：项目小计去重为1，
        # 总体仍是1，不能把项目小计相加充当总体。
        project = self.index(self.execute())
        subtotal = sum(
            metrics['donors']
            for group, metrics in project.items()
            if group[0] == '2026-10' and group[1] in ('P1', 'P2'))
        self.assertEqual(subtotal, 2)
        self.assertEqual(overall[('2026-10',)]['donors'], 1)

    def test_group_union_across_facts(self):
        groups = set(self.index(self.execute()))
        self.assertIn(('2026-11', 'P3'), groups)
        self.assertIn(('2026-11', 'unassigned'), groups)

    def test_null_region_member_distinct_from_unassigned(self):
        outcome = self.execute(group_by=['month', 'region'],
                           metrics=['donated', 'gifts', 'donors'])
        rows = self.index(outcome)
        self.assertEqual(rows[('2026-11', None)]['donated'],
                         frac(Fraction(300)))
        self.assertNotIn(('2026-11', 'unassigned'), rows)

    def test_project_filter_preserves_original_weights(self):
        filtered = self.index(self.execute(filters={'project': ['P1']}))
        self.assertEqual(filtered[('2026-10', 'P1')]['donated'],
                         frac(Fraction(701, 3)))
        # d1原始权重1/3被保留，未被重新归一为1。
        self.assertEqual(filtered[('2026-10', 'P1')]['gifts'],
                         frac(Fraction(4, 3)))

    def test_drill_expands_fact_ids_and_weights(self):
        outcome = self.execute()
        donation_drill = {
            tuple(item['group']): item['edges']
            for item in outcome['drill']['donations']}
        p1 = {(edge['fact_id'], edge['weight']['numerator'],
               edge['weight']['denominator'])
              for edge in donation_drill[('2026-10', 'P1')]}
        self.assertEqual(p1, {('d1', 1, 3), ('d2', 1, 1)})
        p2 = donation_drill[('2026-10', 'P2')]
        self.assertEqual(p2, [
            {'fact_id': 'd1', 'project': 'P2',
             'weight': {'numerator': 2, 'denominator': 3}}])
        # 每组边权重和可核对：d1在P1/P2的边合计为1。
        total = sum(Fraction(e['weight']['numerator'],
                             e['weight']['denominator'])
                    for edges in donation_drill.values() for e in edges
                    if e['fact_id'] == 'd1')
        self.assertEqual(total, Fraction(1))

    def test_stale_plan_is_rejected_after_catalog_change(self):
        plan = compile_query(self.catalog, dict(BASE_QUERY)).to_json()
        tampered = copy.deepcopy(plan)
        tampered['catalog_digest'] = '0' * 64
        with self.assertRaises(ValueError):
            execute_plan(self.catalog, self.conn, tampered)
        # 计划SQL被手工篡改同样因目录绑定失效而拒绝。
        tampered = copy.deepcopy(plan)
        tampered['fact_steps'][0]['sql'] += ' -- injected'
        tampered['catalog_digest'] = '1' * 64
        with self.assertRaises(ValueError):
            execute_plan(self.catalog, self.conn, tampered)

    def test_bridge_violation_blocks_execution(self):
        plan = compile_query(self.catalog, dict(BASE_QUERY)).to_json()
        with self.conn:
            self.conn.execute(
                "INSERT INTO expense_projects VALUES('e1','P2',1,1)")
        with self.assertRaises(ValueError):
            execute_plan(self.catalog, self.conn, plan)

    def test_time_half_open_interval_excludes_end_day(self):
        outcome = self.execute(start='2026-10-01', end='2026-10-03',
                           group_by=['day'])
        days = {row['group'][0] for row in outcome['rows']}
        self.assertEqual(days, {'2026-10-01', '2026-10-02'})

    def test_weighted_totals_reconcile_to_ledger(self):
        outcome = self.execute(group_by=['month'])
        donated = sum(Fraction(r['metrics']['donated']['numerator'],
                               r['metrics']['donated']['denominator'])
                      for r in outcome['rows'])
        spent = sum(Fraction(r['metrics']['spent']['numerator'],
                             r['metrics']['spent']['denominator'])
                    for r in outcome['rows'])
        self.assertEqual(donated, Fraction(raw_total(self.database,
                                                     'donations')))
        self.assertEqual(spent, Fraction(raw_total(self.database,
                                                   'expenses')))


if __name__ == '__main__':
    unittest.main()
