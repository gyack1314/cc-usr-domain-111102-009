import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .catalog import Catalog
from .compiler import compile_query
from .engine import execute_plan
from .io import inspect_bundle, read_json, write_json
from .semantic import SemanticCatalog, validate_database


def _load_query(samples, name):
    queries = read_json(Path(samples) / 'queries.json')
    for query in queries:
        if query.get('id') == name:
            return query
    raise ValueError('查询未登记')


def _explain(plan):
    lines = [
        f"计划版本：{plan['plan_version']}",
        f"绑定目录摘要：{plan['catalog_digest']}",
        f"查询：{plan.get('query_id')}",
        f"分组维度：{', '.join(plan['group_by']) or '（总体，无分组）'}",
        f"时间半开区间：[{plan['interval']['start']}, {plan['interval']['end']})",
    ]
    if plan['filters']:
        rendered = [f"{k} ∈ {v}" for k, v in plan['filters'].items()]
        lines.append('过滤（各自事实桥展开后生效，不重归权重）：' + '；'.join(rendered))
    lines.append('事实侧求值步骤：')
    for step in plan['fact_steps']:
        metrics = '、'.join(
            f"{m['id']}({m['op']} {m['field']} → {m['kind']})"
            for m in step['metrics'])
        lines.append(
            f"  - {step['fact']}：LEFT JOIN桥表，无桥边记unassigned且权重1；"
            f"指标 {metrics or '（本事实无基础指标，仅参与组并集）'}")
    lines.append('指标组合：')
    for metric in plan['metric_steps']:
        if metric['op'] == 'ratio':
            lines.append(
                f"  - {metric['id']} = {metric['numerator']} / "
                f"{metric['denominator']}，分母为0返回null")
        else:
            lines.append(f"  - {metric['id']}：{metric['kind']}，缺失组按0补齐")
    lines.append('口径：' + '；'.join(plan['combination'].values()))
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='业务资料读取与粒度安全指标编译')
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('inspect'); p.add_argument('directory')
    p = subs.add_parser('ingest'); p.add_argument('directory'); p.add_argument('database')
    p = subs.add_parser('catalog'); p.add_argument('database')
    p = subs.add_parser('validate'); p.add_argument('samples'); p.add_argument('database')
    p = subs.add_parser('compile'); p.add_argument('samples')
    p.add_argument('query'); p.add_argument('plan_out')
    p = subs.add_parser('explain'); p.add_argument('plan')
    p = subs.add_parser('run'); p.add_argument('samples'); p.add_argument('database')
    p.add_argument('plan'); p.add_argument('result_out')
    args = parser.parse_args()
    try:
        if args.command == 'inspect':
            result = inspect_bundle(args.directory)
        elif args.command == 'ingest':
            result = {'import_id': Catalog(args.database).ingest(args.directory)}
        elif args.command == 'catalog':
            result = Catalog(args.database).summary()
        elif args.command == 'validate':
            catalog = SemanticCatalog.from_directory(args.samples)
            with sqlite3.connect(args.database) as conn:
                validate_database(catalog, conn)
            result = {'ok': True, 'catalog_digest': catalog.digest()}
        elif args.command == 'compile':
            catalog = SemanticCatalog.from_directory(args.samples)
            query = _load_query(args.samples, args.query)
            plan = compile_query(catalog, query)
            write_json(Path(args.plan_out), plan.to_json())
            result = {'plan': args.plan_out,
                      'catalog_digest': plan.catalog_digest,
                      'fact_steps': len(plan.fact_steps)}
        elif args.command == 'explain':
            print(_explain(read_json(Path(args.plan)))); return 0
        else:
            catalog = SemanticCatalog.from_directory(args.samples)
            plan = read_json(Path(args.plan))
            with sqlite3.connect(args.database) as conn:
                outcome = execute_plan(catalog, conn, plan)
            write_json(Path(args.result_out), outcome)
            result = {'result': args.result_out, 'groups': len(outcome['rows']),
                      'catalog_digest': outcome['catalog_digest']}
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
    except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
        print(f'操作失败：{exc}', file=sys.stderr); return 2


if __name__ == '__main__':
    raise SystemExit(main())
