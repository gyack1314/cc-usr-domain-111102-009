"""受限JSON查询的编译器。

调用方只能选择目录登记的维度与指标；编译器沿声明关系校验可达性，
只产出参数化SQL（标识符来自白名单，值全部以?绑定），不执行任何自带SQL。
编译结果绑定语义目录摘要，目录变化后旧计划无法通过执行期校验。
"""
import re
from datetime import date

from .semantic import DIMENSIONS, NULL_MEMBER, UNASSIGNED, _quote

PLAN_VERSION = 'grain-safe/1'
QUERY_KEYS = {'id', 'group_by', 'filters', 'metrics', 'start', 'end'}
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
MONTH_RE = re.compile(r'^\d{4}-\d{2}$')


class Plan:
    def __init__(self, payload):
        self.payload = payload

    def to_json(self):
        return self.payload

    @property
    def catalog_digest(self):
        return self.payload['catalog_digest']

    @property
    def fact_steps(self):
        return self.payload['fact_steps']


def compile_query(catalog, query):
    _check(query)
    group_by = list(query['group_by'])
    if len(set(group_by)) != len(group_by):
        raise ValueError('分组维度重复')
    filters = _normalize_filters(query.get('filters', {}))
    start, end = _check_interval(query['start'], query['end'])
    chosen = list(query['metrics'])
    if not chosen or len(set(chosen)) != len(chosen):
        raise ValueError('指标列表为空或重复')

    metric_steps, involved_facts = _expand_metrics(catalog, chosen)
    for dimension in group_by + list(filters):
        if dimension not in DIMENSIONS:
            raise ValueError(f'维度{dimension}未登记')
        for fact_name in involved_facts:
            if not catalog.facts[fact_name].reachable(dimension):
                raise ValueError(f'维度{dimension}无法沿声明关系到达事实{fact_name}')

    fact_steps = []
    for fact_name in sorted(involved_facts):
        fact_steps.append(_compile_fact(catalog, catalog.facts[fact_name],
                                        group_by, filters, (start, end),
                                        metric_steps))
    payload = {
        'plan_version': PLAN_VERSION,
        'catalog_digest': catalog.digest(),
        'query_id': query.get('id'),
        'group_by': group_by,
        'filters': {key: list(values) for key, values in filters.items()},
        'interval': {'start': start, 'end': end},
        'metric_steps': metric_steps,
        'fact_steps': fact_steps,
        'combination': {
            'group_union': '所有入选指标出现组的并集',
            'missing_additive': '缺失可加项按0补齐',
            'ratio_zero_denominator': '返回null',
            'distinct_rule': '按每组原始身份集合计数，不乘权重，不跨项目相加',
        },
    }
    return Plan(payload)


def _check(query):
    if not isinstance(query, dict) or set(query) - QUERY_KEYS:
        raise ValueError('查询含未登记字段或不是对象')
    for key in ('group_by', 'metrics', 'start', 'end'):
        if key not in query:
            raise ValueError(f'查询缺少{key}')
    if not isinstance(query['group_by'], list) or not all(
            isinstance(x, str) for x in query['group_by']):
        raise ValueError('group_by必须是维度名数组')
    if not isinstance(query['metrics'], list) or not all(
            isinstance(x, str) for x in query['metrics']):
        raise ValueError('metrics必须是指标ID数组')
    if not isinstance(query.get('filters', {}), dict):
        raise ValueError('filters必须是维度到等值集合的对象')


def _normalize_filters(raw):
    filters = {}
    for dimension, values in raw.items():
        if not isinstance(values, list) or not values:
            raise ValueError('过滤值必须是非空数组')
        normalized = []
        for value in values:
            if dimension in ('day',) and not _is_date(value):
                raise ValueError('day过滤值必须是YYYY-MM-DD')
            if dimension == 'month' and not _is_month(value):
                raise ValueError('month过滤值必须是YYYY-MM')
            if dimension in ('region', 'project') and value is not NULL_MEMBER \
                    and not isinstance(value, str):
                raise ValueError(f'{dimension}过滤值必须是字符串或null成员')
            if value not in normalized:
                normalized.append(value)
        filters[dimension] = normalized
    return filters


def _check_interval(start, end):
    if not _is_date(start) or not _is_date(end):
        raise ValueError('时间半开区间端点必须是YYYY-MM-DD')
    if not date.fromisoformat(start) < date.fromisoformat(end):
        raise ValueError('时间区间必须满足start < end')
    return start, end


def _is_date(value):
    return isinstance(value, str) and DATE_RE.match(value) is not None


def _is_month(value):
    return isinstance(value, str) and MONTH_RE.match(value) is not None


def _expand_metrics(catalog, chosen):
    """展开比率指标为两个事实侧基础指标，确定涉及的事实集合。"""
    steps = []
    involved = set()
    seen = set()

    def add_basic(identifier):
        if identifier in seen:
            return
        metric = catalog.metrics.get(identifier)
        if metric is None:
            raise ValueError(f'指标{identifier}未登记')
        if metric.is_ratio:
            raise ValueError('事实侧不能直接求值比率')
        involved.add(metric.fact)
        steps.append({
            'id': metric.identifier, 'fact': metric.fact,
            'op': metric.op, 'field': metric.field,
            'kind': 'weighted_additive' if metric.op in ('sum', 'count')
                    else 'identity_set',
        })
        seen.add(identifier)

    ratio_steps = []
    for identifier in chosen:
        metric = catalog.metrics.get(identifier)
        if metric is None:
            raise ValueError(f'指标{identifier}未登记')
        if metric.is_ratio:
            ratio_steps.append({
                'id': metric.identifier, 'op': 'ratio',
                'numerator': metric.numerator,
                'denominator': metric.denominator,
            })
        else:
            add_basic(identifier)
    for ratio in ratio_steps:
        add_basic(ratio['numerator'])
        add_basic(ratio['denominator'])
        steps.append(ratio)
    return steps, involved


def _compile_fact(catalog, spec, group_by, filters, interval, metric_steps):
    table = _quote(spec.name)
    pk = _quote(spec.primary_key)
    bridge = _quote(spec.bridge_table)
    bkey = _quote(spec.bridge_key)
    wanted = [m for m in metric_steps
              if m.get('fact') == spec.name and m['op'] != 'ratio']
    selected = [
        f'f.{pk} AS fact_id',
        f'f.{_quote(spec.time_column)} AS day_value',
        f'f.{_quote(spec.single_dimensions[0])} AS region_value'
        if spec.single_dimensions else 'NULL AS region_value',
        f"COALESCE(b.{_quote('project')}, '{UNASSIGNED}') AS project_value",
        "CASE WHEN b." + bkey + " IS NULL THEN 1 ELSE b.numerator END AS weight_num",
        "CASE WHEN b." + bkey + " IS NULL THEN 1 ELSE b.denominator END AS weight_den",
    ]
    amount_fields = sorted({m['field'] for m in wanted if m['op'] == 'sum'})
    for field_name in amount_fields:
        selected.append(f'f.{_quote(field_name)} AS {_quote("v_" + field_name)}')
    identity_fields = sorted({m['field'] for m in wanted
                              if m['op'] == 'count_distinct'})
    for field_name in identity_fields:
        column = spec.identity_columns[field_name]
        selected.append(f'f.{_quote(column)} AS {_quote("id_" + field_name)}')

    where = [f'f.{_quote(spec.time_column)} >= ?',
             f'f.{_quote(spec.time_column)} < ?']
    params = [interval[0], interval[1]]
    for dimension in filters:
        clause, clause_params = _filter_clause(spec, dimension, filters[dimension])
        where.append(clause)
        params.extend(clause_params)

    sql = (
        f"SELECT {', '.join(selected)} FROM {table} f "
        f"LEFT JOIN {bridge} b ON f.{pk} = b.{bkey} "
        f"WHERE {' AND '.join(where)} ORDER BY f.{pk}, b.project"
    )
    return {
        'fact': spec.name,
        'sql': sql,
        'params': params,
        'columns': {
            'primary_key': 'fact_id', 'time': 'day_value',
            'region': 'region_value', 'project': 'project_value',
            'weight_num': 'weight_num', 'weight_den': 'weight_den',
            'amounts': {name: 'v_' + name for name in amount_fields},
            'identities': {name: 'id_' + name for name in identity_fields},
        },
        'metrics': [{k: m[k] for k in ('id', 'op', 'field', 'kind')}
                    for m in wanted],
    }


def _filter_clause(spec, dimension, values):
    if dimension == 'project':
        placeholders = ', '.join('?' for _ in values)
        return (f"COALESCE(b.{_quote('project')}, '{UNASSIGNED}') "
                f"IN ({placeholders})"), list(values)
    if dimension == 'region':
        return _single_value_clause(f'f.{_quote(spec.single_dimensions[0])}',
                                    values)
    if dimension == 'day':
        placeholders = ', '.join('?' for _ in values)
        return f'f.{_quote(spec.time_column)} IN ({placeholders})', list(values)
    if dimension == 'month':
        placeholders = ', '.join('?' for _ in values)
        return (f'substr(f.{_quote(spec.time_column)}, 1, 7) '
                f'IN ({placeholders})'), list(values)
    raise ValueError(f'维度{dimension}不支持过滤')


def _single_value_clause(expression, values):
    """单值维度的null成员需要IS NULL分支，不能用IN匹配NULL。"""
    params = [value for value in values if value is not NULL_MEMBER]
    clauses = []
    if params:
        clauses.append(f'{expression} IN ({", ".join("?" for _ in params)})')
    if NULL_MEMBER in values:
        clauses.append(f'{expression} IS NULL')
    if not clauses:
        raise ValueError('过滤条件为空')
    return '(' + ' OR '.join(clauses) + ')', params
