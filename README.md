# 维度事实准入器（XBRL 风格子集）

用 Python 标准库 + SQLite 实现的维度事实准入裁定器。它**不解析原始 XML、不计算财务
公式、不宣称完整兼容 XBRL**，只实现本项目合同定义的 XBRL 风格子集语义：给定已抽取的
分类网络（概念、角色、带角色关系）与上下文事实（segment/scenario 维度成员），输出可入仓
事实及逐项正反依据。

## 1. 合同语义（本项目实际实现的规则）

### 1.1 图与遍历状态

- 概念 ID 全局唯一，类型为 `primary / cube / dimension / member`。
- 每条关系有 `edge_id / role / source / target` 与可选 `target_role`。
- 遍历状态是 **(概念, 角色)** 二元组；从 `(c, r)` 只走当前角色 `r` 上的边，进入目标时
  角色变为 `target_role`（无则保持 `r`）。
- 合法端点组合：primary→primary（继承）、primary→cube（适用关联）、cube→dimension、
  dimension→member、member→member（后两类统称 domain/member 边）。
- 图允许环；BFS 用已访问状态集合去重，枚举的是有限的**可达状态**而非所有路径/游走。
- 不同 `edge_id` 的重复关系作为不同来源证据同时保留，不会被合并丢弃。

### 1.2 适用关联与候选组

- 关联（primary→cube）带 `polarity=all/not_all`、`context_element=segment/scenario`、
  `closed`。
- 关联从其 source 与**关联原始 role** 出发，沿 primary 继承网络可达的 primary 事实均适用，
  初始 source 自身也算（零长路径）。
- 关联始终归入它**原始 role** 的候选组；继承途中 `target_role` 切换只影响后续遍历，
  不改变分组。
- cube 网络从 cube 与关联 `target_role`（无则原 role）开始；到达的 dimension 即该 cube
  要求的维度；维度的可用成员从对应进入角色继续沿 domain/member 边求取。

### 1.3 usable=false 的精确含义

- 被标记的入边，其目标 member **不能凭这条入边成为合法终点**，但遍历仍穿过它继续访问
  子 member。
- 同一 member 只要另有一条 usable 入边可达，整体仍可用。
- 结论绑定进入角色：另一角色上让该 member 可用，不会污染本角色结论（缓存也按角色分键）。

### 1.4 上下文与 cube 检查

- 上下文给 segment / scenario 两份 dimension→member；同一位置重复维度是输入结构错误。
- 每个 dimension 可声明一个默认 member：**显式填默认值不合法**；只有缺维度时才可按默认
  补，且默认必须在本次 cube 角色下可用。
- cube 只检查关联指定位置：所有要求维度必须有可用的显式成员或合法默认。
- `closed=true`：该位置不能出现 cube 要求之外的额外维度；`closed=false`：允许额外维度，
  但其显式成员必须是包中合法 member 类型。
- 另一位置的维度不能替本位置补齐，也不会被误算成 closed 额外项。

### 1.5 组裁定与交付状态

- 同一原始 role 组内：所有 `all` 关联的 cube 检查通过、所有 `not_all` 关联的 cube 检查
  **不通过**，该组才通过。
- 不同 role 组之间任选一组通过即可接收。
- 交付状态：
  - `validated`：至少一个候选组通过（附选定组与全部组真值）；
  - `rejected`：有适用关联但所有组失败（逐组附失败原因）；
  - `unconstrained`：primary 完全无适用关联，单独交付，不伪装 validated；
  - `invalid_context` / `invalid_fact`：结构错误，不进入 cube 判定。
- **not_all 只反转完整 cube 判定**；“成员不存在于包”等结构错误不能被反转成成功。

## 2. 目录结构

```
dim_admission/
  store.py        SQLite 建表、JSON 装载、派生闭包/缓存存取
  precheck.py     包/上下文/事实预检（结构错误与裁定错误分离）
  network.py      (概念,角色) 状态 BFS、路径回放、闭包编译、关联覆盖
  engine.py       cube 检查、默认补用、closed、组真值、批量准入
  explain.py      逐项正反依据文本 + 可回查路径与 SQL
  cli.py          命令行入口
  demo.py         端到端演练（16 个手写真值表事实 + 补丁 + 小图状态证明）
  sample_data/    package.json / contexts.json / facts.json / edge_patch.json
dim_admission_cli.py   隔离模式 CLI 启动器
run_demo.py            隔离模式演练启动器
examples/
  api_walkthrough.py   内存库上的最小 API 端到端示例
tests/
  test_dim_admission.py 语义回归测试（9 个用例）
```

仅依赖 Python 3.11 标准库（`sqlite3 / json / argparse / hashlib`），无第三方包。

## 3. 快速开始

```bash
# 完整演练（全程隔离模式，防止工作目录中同名模块污染）
python3 -I run_demo.py

# CLI 流程
python3 -I dim_admission_cli.py load    --db /tmp/demo.sqlite --file dim_admission/sample_data/package.json
python3 -I dim_admission_cli.py load    --db /tmp/demo.sqlite --file dim_admission/sample_data/contexts.json
python3 -I dim_admission_cli.py load    --db /tmp/demo.sqlite --file dim_admission/sample_data/facts.json
python3 -I dim_admission_cli.py precheck --db /tmp/demo.sqlite
python3 -I dim_admission_cli.py compile  --db /tmp/demo.sqlite
python3 -I dim_admission_cli.py query    --db /tmp/demo.sqlite members --concept D --role t
python3 -I dim_admission_cli.py query    --db /tmp/demo.sqlite coverage --edge-id e_a_h
python3 -I dim_admission_cli.py query    --db /tmp/demo.sqlite replay --concept P --role r --edge-ids e_inh_p_q,e_inh_q_q2
python3 -I dim_admission_cli.py admit    --db /tmp/demo.sqlite
python3 -I dim_admission_cli.py explain  --db /tmp/demo.sqlite --fact-id F4
```

`query reachable --concept X --role r --edge-type inheritance` 可查看任意状态的有限
可达集合、代表路径与实际扫描边数。

## 4. 数据格式（JSON）

```json
{
  "roles": [{"id": "r"}],
  "concepts": [{"id": "P", "type": "primary"}],
  "defaults": [{"dimension": "D2", "member": "d2def"}],
  "edges": [
    {"edge_id": "e1", "role": "r", "source": "P", "target": "Q", "target_role": "s"},
    {"edge_id": "e2", "role": "r", "source": "P", "target": "H", "target_role": "t",
     "polarity": "all", "context_element": "segment", "closed": true},
    {"edge_id": "e3", "role": "t", "source": "D", "target": "m0", "usable": false}
  ]
}
```

上下文用**有序对列表**表达（保留重复维度，交由预检报错）：

```json
{"contexts": [{"id": "C1", "segment": [["D", "m1"]], "scenario": []}]}
```

## 5. Python API

```python
from dim_admission.store import TaxonomyStore, load_package, load_contexts, load_facts
from dim_admission import precheck, network, engine, explain

store = TaxonomyStore("/tmp/x.sqlite")
load_package(store, package_dict)
load_contexts(store, contexts_list)
load_facts(store, facts_list)

precheck.run_precheck(store)                 # 结构问题清单
network.compile_network(store)               # 预编译闭包（也可按需懒加载）
network.member_network(store, "D", "t")      # 带角色的可用成员
network.association_coverage(store, "e_a_h") # 关联继承覆盖
result = engine.admit_all(store)             # 批量准入
print(explain.explain_fact(store, "F4"))     # 逐项正反依据
```

## 6. 缓存键设计（不会只用 concept/dimension）

- 网络闭包表 `reachable_cache` 主键 = `(start_concept, start_role, etype)`；
- 准入 cube 缓存键 = 关联 + 上下文 + 位置的哈希；
- 事实裁定缓存键 = fact + concept + context 复合要素的哈希。

因此 `D@t/domain` 与 `D@r_other/domain` 是互不污染的两条缓存。打补丁改变某角色网络后，
只需删除对应状态键（演练 section 5 有示范），其它角色缓存保留。

## 7. 主演练真值表（节选，完整断言见 `demo.py: EXPECTED`）

主图：P 在 r 关联 closed cube H（target_role=t），继承到 Q 途中切到 s；t 下
`D→m0 usable=false`、`m0→m1 usable=true`。

| 事实 | 上下文要点 | 裁定 | 关键说明 |
| --- | --- | --- | --- |
| F1 | segment D=m1 | validated(r) | 穿过禁用点 m0 到达 m1 |
| F2 | segment D=m0 | validated(r3) | H 上 m0 失败，另一 role 组接收 |
| F4 | closed 额外维度 | rejected | 每个候选组都给出失败原因 |
| F7 | 缺 D2 | validated(r3) | 缺维度才补默认，默认在 r3 可用 |
| F6 | 显式 D2=d2def | 经 r 接收 | r3 组因显式默认非法失败 |
| F8 | 成员 GHOST 未知 | invalid_context | 结构错误，not_all 不反转 |
| F11 | 同位置重复维度 | invalid_context | E_DUP_DIMENSION |
| F14 | 概念 U 无关联 | unconstrained | 单独交付 |
| F15 | D 只出现在 scenario | validated(r4) | 另一位置不能替 segment 补齐 |
| F16 | open cube 额外维度 | validated | 开放位置允许合法额外成员 |

补丁（新增 t 下第二条 `D→m0 usable=true` 入边）后，F2/F12 在 r 组即通过；无关角色
r_other 上 m0 始终可用但从不影响 t 的结论。

独立小图含环与自环：朴素游走枚举的游走条数随长度无限增长（1,3,3,5,5,7,…），而 BFS
可达状态恒定 3 个、扫描 5 条边即终止，用输出对照证明不枚举所有路径。

## 8. 明确边界

- 不解析 XML、不读 taxonomy schema/linkbase 原文，输入是已抽取的 JSON 关系。
- 不计算公式、不校验数值类型，事实 value 作为不透明字符串保存。
- 不是完整 XBRL 校验器；维度默认弧、hypercube 语义仅按本合同子集裁定。
- 核心裁定全部在本包 Python/SQLite 内完成，不调用任何外部 XBRL 验证引擎。
