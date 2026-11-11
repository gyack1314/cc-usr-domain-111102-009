"""独立对照：绕开编译层，直接从原始JSON逐事实、逐边重算。

该模块不导入compiler/engine，作为第三方口径核对引擎结果，
并显式演示朴素内连接（在最终金额上补救DISTINCT）为何仍会翻倍。
"""
import sqlite3
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from workbench.compiler import compile_query
from workbench.domain import load_database
from workbench.engine import execute_plan
from workbench.io import read_json
from workbench.semantic import SemanticCatalog
from tests.test_semantic import Helper
from tests.test_compiler import BASE_QUERY

ROOT = Path(__file__).resolve().parents[1]


def _edges(fact_rows, bridge_rows):
    bridges = {}
    for edge in bridge_rows:
        bridges.setdefault(edge['fact_id'], []).append(
            (edge['project'], Fraction(edge['numerator'],
                                       edge['denominator'])))
    for fact in fact_rows:
        yield fact, bridges.get(fact['id'], [('unassigned', Fraction(1))])


def _independent_monthly_project(root):
    donations = read_json(root / 'donations.json')
    expenses = read_json(root / 'expenses.json')
    d_edges = read_json(root / 'donation_projects.json')
    e_edges = read_json(root / 'expense_projects.json')
    groups = {}

    def bucket(month, project):
        return groups.setdefault((month, project), {
            'donated': Fraction(0), 'spent': Fraction(0),
            'gifts': Fraction(0), 'donors': set()})

    for fact, edges in _edges(donations, d_edges):
        month = fact['day'][:7]
        for project, weight in edges:
            target = bucket(month, project)
            target['donated'] += weight * fact['amount']
            target['gifts'] += weight
            target['donors'].add(fact['donor'])
    for fact, edges in _edges(expenses, e_edges):
        month = fact['day'][:7]
        for project, weight in edges:
            bucket(month, project)['spent'] += weight * fact['amount']
    return groups


class CrossCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root, self.database = Helper.build(self.tmp.name)
        self.catalog = Helper.catalog(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_engine_matches_independent_recomputation(self):
        expected = _independent_monthly_project(self.root)
        with sqlite3.connect(self.database) as conn:
            outcome = execute_plan(
                self.catalog, conn,
                compile_query(self.catalog, dict(BASE_QUERY)))
        actual = {tuple(row['group']): row['metrics']
                  for row in outcome['rows']}
        self.assertEqual(set(actual), set(expected))
        for key, want in expected.items():
            got = actual[key]
            self.assertEqual(Fraction(got['donated']['numerator'],
                                      got['donated']['denominator']),
                             want['donated'])
            self.assertEqual(Fraction(got['spent']['numerator'],
                                      got['spent']['denominator']),
                             want['spent'])
            self.assertEqual(Fraction(got['gifts']['numerator'],
                                      got['gifts']['denominator']),
                             want['gifts'])
            self.assertEqual(got['donors'], len(want['donors']))

    def test_naive_join_inflates_but_engine_does_not(self):
        # 朴素做法：事实直接内连接桥表后对金额求和，d1有两条边被计两次，
        # 10月捐助从真实301膨胀为402；即便改sum(DISTINCT)，也只是按金额
        # 巧合去重（101+200=301），语义上仍然错误且无法泛化。
        with sqlite3.connect(self.database) as conn:
            naive = conn.execute(
                'SELECT sum(f.amount) FROM donations f '
                "JOIN donation_projects b ON f.id = b.fact_id "
                "WHERE f.day >= '2026-10-01' AND f.day < '2026-11-01'"
            ).fetchone()[0]
            distinct_naive = conn.execute(
                'SELECT sum(DISTINCT f.amount) FROM donations f '
                "JOIN donation_projects b ON f.id = b.fact_id "
                "WHERE f.day >= '2026-10-01' AND f.day < '2026-11-01'"
            ).fetchone()[0]
            outcome = execute_plan(
                self.catalog, conn,
                compile_query(self.catalog, dict(
                    BASE_QUERY, group_by=['month'])))
        engine_october = Fraction(
            outcome['rows'][0]['metrics']['donated']['numerator'],
            outcome['rows'][0]['metrics']['donated']['denominator'])
        self.assertEqual(naive, 402)           # 101*2 + 200 = 402，d1翻倍
        self.assertEqual(distinct_naive, 301)  # DISTINCT金额碰巧掩盖问题但语义错误
        self.assertEqual(engine_october, Fraction(301))
        self.assertEqual(engine_october,
                         Fraction(sum(d['amount'] for d in
                                      read_json(self.root / 'donations.json')
                                      if d['day'] < '2026-11-01')))

    def test_changing_bridge_filter_never_rewrites_raw_ledger(self):
        ledger_before = (read_json(self.root / 'donations.json'),
                         read_json(self.root / 'expenses.json'))
        with sqlite3.connect(self.database) as conn:
            for project_filter in (None, ['P1'], ['P1', 'P2'],
                                   ['unassigned']):
                query = dict(BASE_QUERY, group_by=['month'])
                if project_filter:
                    query['filters'] = {'project': project_filter}
                execute_plan(self.catalog, conn,
                             compile_query(self.catalog, query))
            raw_donations = conn.execute(
                'SELECT id, amount FROM donations ORDER BY id').fetchall()
            raw_expenses = conn.execute(
                'SELECT id, amount FROM expenses ORDER BY id').fetchall()
            raw_bridge = [tuple(r) for r in conn.execute(
                'SELECT fact_id, project, numerator, denominator '
                'FROM donation_projects ORDER BY fact_id, project').fetchall()]
        self.assertEqual([r[1] for r in raw_donations],
                         [row['amount'] for row in ledger_before[0]])
        self.assertEqual([r[1] for r in raw_expenses],
                         [row['amount'] for row in ledger_before[1]])
        self.assertEqual(raw_bridge, [
            ('d1', 'P1', 1, 3), ('d1', 'P2', 2, 3), ('d2', 'P1', 1, 1)])

    def test_partial_project_weights_are_not_renormalized(self):
        # 独立口径：只选P1时，d1保留1/3而非归一为1。
        expected = Fraction(101, 3) + Fraction(200)
        with sqlite3.connect(self.database) as conn:
            outcome = execute_plan(
                self.catalog, conn,
                compile_query(self.catalog, dict(
                    BASE_QUERY, filters={'project': ['P1']},
                    group_by=['month', 'project'])))
        row = next(r for r in outcome['rows']
                   if r['group'] == ['2026-10', 'P1'])
        self.assertEqual(Fraction(row['metrics']['donated']['numerator'],
                                  row['metrics']['donated']['denominator']),
                         expected)


if __name__ == '__main__':
    unittest.main()
