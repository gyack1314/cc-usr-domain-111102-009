"""语义目录：声明事实表的稳定主键、时间列、金额列、单值维度与项目桥表。

目录是编译层唯一的元数据来源：字段名、表名、维度与指标都必须在此登记，
编译器不接受调用方自带的SQL片段或未登记表达式。
"""
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .io import digest, read_json

# 全局维度登记：维度名 -> 取值方式。
# month/day 为时间维度；region 为事实自带单值维度（含null成员）；
# project 为项目桥维度（无桥事实归显式 unassigned 成员）。
DIMENSIONS = {
    'day': {'kind': 'time', 'grain': 'day'},
    'month': {'kind': 'time', 'grain': 'month'},
    'region': {'kind': 'single', 'column': 'region'},
    'project': {'kind': 'bridge'},
}
TIME_DIMENSIONS = {'day', 'month'}
UNASSIGNED = 'unassigned'
NULL_MEMBER = None
SUM_OPS = ('sum',)
COUNT_OPS = ('count', 'count_distinct')
WHITELIST_OPS = SUM_OPS + COUNT_OPS + ('ratio',)


@dataclass(frozen=True)
class FactSpec:
    name: str
    primary_key: str
    time_column: str
    amount_columns: tuple
    identity_columns: dict          # 可做 count_distinct 的身份列
    single_dimensions: tuple        # 事实自带单值维度
    bridge_table: str
    bridge_key: str

    def reachable(self, dimension):
        if dimension in TIME_DIMENSIONS:
            return True
        spec = DIMENSIONS[dimension]
        if spec['kind'] == 'single':
            return spec['column'] in self.single_dimensions
        if spec['kind'] == 'bridge':
            return True
        return False


@dataclass(frozen=True)
class MetricSpec:
    identifier: str
    op: str
    fact: object = None
    field: object = None
    numerator: object = None
    denominator: object = None

    @property
    def is_ratio(self):
        return self.op == 'ratio'

    def required_facts(self, metrics_by_id):
        if not self.is_ratio:
            return {self.fact}
        return {metrics_by_id[self.numerator].fact,
                metrics_by_id[self.denominator].fact}


@dataclass(frozen=True)
class SemanticCatalog:
    facts: dict
    metrics: dict
    source_digest: str

    @classmethod
    def from_directory(cls, root):
        root = Path(root)
        facts = {
            'donations': FactSpec(
                name='donations', primary_key='id', time_column='day',
                amount_columns=('amount',), identity_columns={'donor': 'donor'},
                single_dimensions=('region',),
                bridge_table='donation_projects', bridge_key='fact_id'),
            'expenses': FactSpec(
                name='expenses', primary_key='id', time_column='day',
                amount_columns=('amount',), identity_columns={},
                single_dimensions=('region',),
                bridge_table='expense_projects', bridge_key='fact_id'),
        }
        rows = read_json(root / 'metrics.json')
        metrics = {}
        for row in rows:
            metric = _build_metric(row)
            if metric.identifier in metrics:
                raise ValueError('指标重复登记')
            metrics[metric.identifier] = metric
        catalog = cls(facts=facts, metrics=metrics,
                      source_digest=digest(rows))
        catalog._cross_check()
        return catalog

    def digest(self):
        """目录摘要：事实声明与指标登记的规范化哈希，计划绑定该值。"""
        payload = {
            'facts': {
                name: {
                    'table': spec.name,
                    'primary_key': spec.primary_key,
                    'time_column': spec.time_column,
                    'amount_columns': list(spec.amount_columns),
                    'identity_columns': dict(spec.identity_columns),
                    'single_dimensions': list(spec.single_dimensions),
                    'bridge_table': spec.bridge_table,
                    'bridge_key': spec.bridge_key,
                }
                for name, spec in sorted(self.facts.items())
            },
            'metrics': [
                {key: getattr(metric, key) for key in metric.__dataclass_fields__}
                for metric in sorted(self.metrics.values(), key=lambda m: m.identifier)
            ],
            'dimensions': DIMENSIONS,
            'unassigned': UNASSIGNED,
        }
        return digest(payload)

    def _cross_check(self):
        for metric in self.metrics.values():
            if metric.is_ratio:
                for reference in (metric.numerator, metric.denominator):
                    if reference not in self.metrics:
                        raise ValueError('比率指标引用未登记指标')
                    referenced = self.metrics[reference]
                    if referenced.is_ratio:
                        raise ValueError('比率指标不能嵌套比率')
                continue
            if metric.fact not in self.facts:
                raise ValueError('指标引用未登记事实')
            spec = self.facts[metric.fact]
            if metric.op in SUM_OPS and metric.field not in spec.amount_columns:
                raise ValueError('sum指标必须登记在金额/数量列')
            if metric.op == 'count' and metric.field != spec.primary_key:
                raise ValueError('count指标只能登记在稳定主键列')
            if metric.op == 'count_distinct':
                if metric.field not in spec.identity_columns:
                    raise ValueError('count_distinct指标必须登记在身份列')


def _build_metric(row):
    allowed = {'id', 'op', 'fact', 'field', 'numerator', 'denominator'}
    if set(row) - allowed:
        raise ValueError('指标含未登记字段')
    op = row['op']
    if op not in WHITELIST_OPS:
        raise ValueError('指标算子不在白名单')
    if op == 'ratio':
        if set(row) != {'id', 'op', 'numerator', 'denominator'}:
            raise ValueError('比率指标声明不合法')
        return MetricSpec(identifier=row['id'], op=op,
                          numerator=row['numerator'], denominator=row['denominator'])
    if set(row) != {'id', 'op', 'fact', 'field'}:
        raise ValueError('基础指标声明不合法')
    return MetricSpec(identifier=row['id'], op=op,
                      fact=row['fact'], field=row['field'])


def validate_database(catalog, conn):
    """对已装载SQLite库做桥表合法性与引用完整性检查。

    重复边、悬空事实、非负有理数权重不合法或每事实权重和不为1均阻止查询。
    两张事实的主键空间独立，检查分别进行。
    """
    for spec in catalog.facts.values():
        pk = _quote(spec.primary_key)
        rows = conn.execute(
            f'SELECT {pk}, count(*) FROM {_quote(spec.name)} GROUP BY {pk} HAVING count(*) > 1'
        ).fetchall()
        if rows:
            raise ValueError(f'事实{spec.name}存在重复主键')
        bridge = _quote(spec.bridge_table)
        key = _quote(spec.bridge_key)
        dangling = conn.execute(
            f'SELECT b.{key} FROM {bridge} b LEFT JOIN {_quote(spec.name)} f '
            f'ON b.{key} = f.{pk} WHERE f.{pk} IS NULL'
        ).fetchall()
        if dangling:
            raise ValueError(f'桥表{spec.bridge_table}存在悬空事实')
        duplicated = conn.execute(
            f'SELECT {key}, project, count(*) FROM {bridge} '
            f'GROUP BY {key}, project HAVING count(*) > 1'
        ).fetchall()
        if duplicated:
            raise ValueError(f'桥表{spec.bridge_table}存在重复边')
        weights = {}
        for fact_id, project, numerator, denominator in conn.execute(
            f'SELECT {key}, project, numerator, denominator FROM {bridge} ORDER BY project'
        ):
            if type(numerator) is not int or type(denominator) is not int:
                raise ValueError('权重必须为整数有理表示')
            if numerator < 0 or denominator <= 0:
                raise ValueError('权重不是非负有理数')
            total = weights.setdefault(fact_id, Fraction(0))
            weights[fact_id] = total + Fraction(numerator, denominator)
        for fact_id, total in weights.items():
            if total != 1:
                raise ValueError(f'桥表{spec.bridge_table}中事实{fact_id}权重和不为1')
    return True


def _quote(identifier):
    """所有SQL标识符来自目录白名单，再做一次双引号转义。"""
    if not isinstance(identifier, str) or not identifier:
        raise ValueError('SQL标识符非法')
    return '"' + identifier.replace('"', '""') + '"'
