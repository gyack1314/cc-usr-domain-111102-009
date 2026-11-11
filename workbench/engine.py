"""计算计划的执行引擎。

编译与执行分开：执行前重新校验目录摘要，计划与当前目录不一致即拒绝。
SQL只负责沿声明关系取边级明细（参数全部绑定），精确有理聚合、身份集合
去重、组并集与比率口径在Python侧完成，避免浮点误差和跨项目重复计数。
"""
import sqlite3
from fractions import Fraction

from .semantic import NULL_MEMBER, validate_database
from .compiler import PLAN_VERSION


def execute_plan(catalog, conn, plan):
    if not isinstance(plan, dict):
        plan = plan.to_json()
    if plan.get('plan_version') != PLAN_VERSION:
        raise ValueError('计划版本不被支持')
    if plan.get('catalog_digest') != catalog.digest():
        raise ValueError('计划绑定的目录摘要与当前语义目录不一致，拒绝执行旧计划')
    validate_database(catalog, conn)
    conn.row_factory = sqlite3.Row
    group_by = list(plan['group_by'])

    fact_results = {}
    drill = {}
    for step in plan['fact_steps']:
        rows = conn.execute(step['sql'], step['params']).fetchall()
        fact_results[step['fact']] = _aggregate_fact(step, rows, group_by)
        drill[step['fact']] = _expand_drill(step, rows, group_by)

    combined = _combine(catalog, plan, fact_results)
    return {
        'query_id': plan['query_id'],
        'catalog_digest': catalog.digest(),
        'group_by': group_by,
        'rows': combined,
        'drill': drill,
    }


def _aggregate_fact(step, rows, group_by):
    """单事实在自身桥粒度求值：金额/计数按权重分摊，身份列按集合去重。"""
    sums = {}       # (group, metric_id) -> Fraction
    identity = {}   # (group, metric_id) -> set(原始身份)
    groups = {}
    for row in rows:
        group = _group_key(row, group_by)
        groups[group] = True
        weight = Fraction(row['weight_num'], row['weight_den'])
        for metric in step['metrics']:
            key = (group, metric['id'])
            if metric['op'] == 'sum':
                column = step['columns']['amounts'][metric['field']]
                value = row[column]
                if type(value) is not int or value < 0:
                    raise ValueError('金额列必须是非负整数分')
                sums[key] = sums.get(key, Fraction(0)) + weight * value
            elif metric['op'] == 'count':
                sums[key] = sums.get(key, Fraction(0)) + weight
            else:  # count_distinct：原始身份集合，不乘权重
                column = step['columns']['identities'][metric['field']]
                identity.setdefault(key, set()).add(row[column])
    result = {group: {'additive': {}, 'distinct': {}} for group in groups}
    for (group, metric_id), value in sums.items():
        result[group]['additive'][metric_id] = value
    for (group, metric_id), members in identity.items():
        result[group]['distinct'][metric_id] = members
    return result


def _group_key(row, group_by):
    values = []
    for dimension in group_by:
        if dimension == 'day':
            values.append(row['day_value'])
        elif dimension == 'month':
            values.append(row['day_value'][:7])
        elif dimension == 'region':
            values.append(row['region_value'] if row['region_value'] is not None
                          else NULL_MEMBER)
        elif dimension == 'project':
            values.append(row['project_value'])
    return tuple(values)


def _expand_drill(step, rows, group_by):
    """按分组展开原事实ID与权重，供逐事实核对。"""
    buckets = {}
    for row in rows:
        group = _group_key(row, group_by)
        buckets.setdefault(group, []).append({
            'fact_id': row['fact_id'],
            'project': row['project_value'],
            'weight': _fraction(Fraction(row['weight_num'], row['weight_den'])),
        })
    return [
        {'group': list(group), 'edges': edges}
        for group, edges in sorted(buckets.items(), key=_sort_key)
    ]


def _combine(catalog, plan, fact_results):
    """组集合取所有入选指标出现组的并集；缺失可加项为0，比率分母0为null。"""
    chosen = [m['id'] for m in plan['metric_steps']]
    additive_values = {}   # (group, metric_id) -> Fraction
    distinct_values = {}   # (group, metric_id) -> set
    all_groups = set()
    for fact_result in fact_results.values():
        for group, bucket in fact_result.items():
            all_groups.add(group)
            for metric_id, value in bucket['additive'].items():
                additive_values[(group, metric_id)] = value
            for metric_id, members in bucket['distinct'].items():
                distinct_values[(group, metric_id)] = members

    output = []
    for group in sorted(all_groups, key=_sort_key):
        record = {'group': list(group), 'metrics': {}}
        for metric_id in chosen:
            metric = catalog.metrics[metric_id]
            if metric.is_ratio:
                numerator = additive_values.get((group, metric.numerator),
                                                Fraction(0))
                denominator = additive_values.get((group, metric.denominator),
                                                  Fraction(0))
                record['metrics'][metric_id] = (
                    None if denominator == 0
                    else _fraction(Fraction(numerator, denominator)))
            elif metric.op == 'count_distinct':
                members = distinct_values.get((group, metric_id), set())
                record['metrics'][metric_id] = len(members)
            else:
                record['metrics'][metric_id] = _fraction(
                    additive_values.get((group, metric_id), Fraction(0)))
        output.append(record)
    return output


def _fraction(value):
    return {'numerator': value.numerator, 'denominator': value.denominator}


def _sort_key(group):
    return tuple((1, '') if value is None else (0, str(value))
                 for value in group)
