import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from workbench.domain import load_database
from tests.test_semantic import Helper

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root, self.database = Helper.build(self.tmp.name)
        self.plan = Path(self.tmp.name) / 'plan.json'
        self.result = Path(self.tmp.name) / 'result.json'

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, '-m', 'workbench.cli', *args],
            cwd=ROOT, capture_output=True, text=True)

    def test_validate_compile_explain_run_pipeline(self):
        validate = self.cli('validate', self.root, self.database)
        self.assertEqual(validate.returncode, 0, validate.stderr)
        compile_run = self.cli('compile', self.root, 'monthly', self.plan)
        self.assertEqual(compile_run.returncode, 0, compile_run.stderr)
        plan = json.loads(self.plan.read_text(encoding='utf-8'))
        self.assertEqual(plan['query_id'], 'monthly')
        explain = self.cli('explain', self.plan)
        self.assertEqual(explain.returncode, 0, explain.stderr)
        self.assertIn('事实侧求值步骤', explain.stdout)
        run = self.cli('run', self.root, self.database, self.plan, self.result)
        self.assertEqual(run.returncode, 0, run.stderr)
        outcome = json.loads(self.result.read_text(encoding='utf-8'))
        self.assertEqual(len(outcome['rows']), 4)
        self.assertIn('drill', outcome)

    def test_unknown_query_exits_nonzero(self):
        bad = self.cli('compile', self.root, 'missing', self.plan)
        self.assertEqual(bad.returncode, 2)
        self.assertIn('操作失败', bad.stderr)

    def test_stale_plan_rejected_through_cli(self):
        self.cli('compile', self.root, 'monthly', self.plan)
        plan = json.loads(self.plan.read_text(encoding='utf-8'))
        plan['catalog_digest'] = 'f' * 64
        self.plan.write_text(json.dumps(plan), encoding='utf-8')
        run = self.cli('run', self.root, self.database, self.plan, self.result)
        self.assertEqual(run.returncode, 2)
        self.assertIn('目录摘要', run.stderr)

    def test_invalid_bridge_fails_validation(self):
        with sqlite3.connect(self.database) as conn:
            conn.execute(
                "INSERT INTO donation_projects VALUES('d2','P2',1,2)")
        result = self.cli('validate', self.root, self.database)
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
