# 粒度安全指标编译层

编译层解决的问题：两张事实（donations、expenses）主键空间独立，新增项目维度后事实经桥表一对多展开，朴素内连接求和会让金额随边数翻倍，最终金额上的 `DISTINCT` 无法补救（见 tests/test_crosscheck.py 的对照）。本层只允许语义明确的查询，全部规则来自语义目录，不接受调用方自带 SQL。

## 组件与职责

- `workbench/semantic.py`：语义目录。登记每张事实的稳定主键、时间列、金额列、可去重身份列、单值维度和项目桥表；登记指标只允许白名单算子 `sum`、`count`、`count_distinct` 及两个登记基础指标之比。`validate_database` 在查询前检查重复主键、悬空事实、重复边、非负有理数权重与每事实权重和为1，任一不满足即阻止查询。
- `workbench/compiler.py`：受限 JSON 查询编译器。校验维度沿声明关系可达，不猜连接路径；标识符只取自目录白名单并加双引号转义，所有值以 `?` 参数绑定。产出 `grain-safe/1` 计划，内含每个事实一条参数化 SQL 与组合规则，计划绑定目录摘要 `catalog_digest`。
- `workbench/engine.py`：执行引擎。执行前复核计划版本与目录摘要，目录变化后旧计划被拒绝；先在各自事实及所需桥粒度求值，再按分组键组合。
- `workbench/cli.py`：`validate`、`compile`、`explain`、`run` 四个子命令，编译与执行是两个独立步骤，计划与结果分别落盘。

## 固定口径

- 日期：`day` 为 `YYYY-MM-DD` 文本，时间过滤是半开区间 `[start, end)`；`month` 取 `substr(day,1,7)` 的 `YYYY-MM`，不做时区换算。
- 空维度成员：单值维度的 SQL NULL 是独立 null 成员（JSON 里写 `null`，SQL 用 `IS NULL` 匹配）；无桥记录的项目成员是字符串 `unassigned`，经 LEFT JOIN + COALESCE 显式保留，二者不混用。
- 精确金额：金额是非负整数分；权重是整数比 `numerator/denominator`。分摊与聚合全程使用 `fractions.Fraction`，输出为约分后的 `{numerator, denominator}`，展示舍入只能发生在最后一步，系统内部不产生浮点金额。
- 分摊：`sum` 与主键 `count` 按边权重分摊；`count_distinct` 对每组的原始身份集合计数，不乘权重，也不把不同项目的小计相加充当总体。
- 部分项目：过滤发生在桥展开之后，保留原始权重，不重新归一。
- 无桥事实：归入 `unassigned`，权重 1，不会因内连接失踪。
- 组合：组集合取所有入选指标出现组的并集；缺失的可加项按 0 补齐；比率分母为 0 返回 `null`。

## 样本对照（samples）

- 两侧事实各有多行：donations 三行、expenses 三行；d1 同时属于 P1（1/3）与 P2（2/3），d2 只在 P1（1/1），d3 无桥归 unassigned；e1/e2/e3 分属 P1/P2/P3。
- 一个捐助者跨项目：捐助者 A 的 d1 横跨 P1、P2。按月+项目看，两个项目组 donors 各为 1；按月总体看 donors 仍为 1（身份集合 {A}），不是项目小计 1+1。
- 只有支出没有捐助：2026-11 的 P3 组 spent=90、donated=0，比率返回 null。
- 独立逐事实重算：tests/test_crosscheck.py 不导入编译器/引擎的聚合代码，直接从原始 JSON 按边重算，与引擎输出逐组核对。
- 桥表过滤不改账：同一连接上依次运行无过滤、P1、P1+P2、unassigned 过滤后，原始事实与桥表字节级不变。

## 核对方式

结果 JSON 的 `drill` 字段按分组展开每个事实参与计算的原始事实 ID、项目与权重，可直接对回桥表；例如 d1 在 P1/P2 两条边的权重之和为 1。`explain` 子命令输出计划的可读版本，包含每条事实侧 SQL 的指标语义与组合口径。

全部账户、桥表与核算规则均为虚构合成资料，仅用于工程演示，不代表任何慈善监管要求。
