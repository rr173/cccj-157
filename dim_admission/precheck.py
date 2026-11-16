"""分类包与上下文预检。

核心原则（合同要求）：
- 上下文结构错误（重复维度、未知/类型错误的成员）与
  「完整上下文下维度不合格」的语义裁定错误严格分开；
- not_all 只反转完整 cube 判定，结构错误永远不能被反转成成功。
预检只产出问题清单，不做任何准入裁定。
"""
from __future__ import annotations

from .store import SCOPES, TaxonomyStore, derive_edge_type


def _err(code: str, message: str, ref: str | None = None) -> dict:
    return {"level": "error", "code": code, "message": message, "ref": ref}


def _warn(code: str, message: str, ref: str | None = None) -> dict:
    return {"level": "warning", "code": code, "message": message, "ref": ref}


def check_package(store: TaxonomyStore) -> list[dict]:
    """对整个分类包做静态预检，返回问题清单（error + warning）。"""
    issues: list[dict] = []

    # 1. 边的角色声明（role 与 target_role 都必须存在）
    for edge in store.all_edges():
        if not store.role_exists(edge["role_id"]):
            issues.append(_err("E_ROLE_UNDECLARED",
                               f"边 {edge['edge_id']} 使用了未声明角色 {edge['role_id']}",
                               edge["edge_id"]))
        if edge["target_role"] is not None and not store.role_exists(edge["target_role"]):
            issues.append(_err("E_TARGET_ROLE_UNDECLARED",
                               f"边 {edge['edge_id']} 的 target_role {edge['target_role']} 未声明",
                               edge["edge_id"]))

    # 2. 端点类型组合（store.add_edge 已推导 etype；此处复核关联边专属字段）
    for edge in store.all_edges():
        src_t = store.concept_type(edge["source_id"])
        tgt_t = store.concept_type(edge["target_id"])
        try:
            expected_etype = derive_edge_type(src_t, tgt_t)
        except ValueError:
            issues.append(_err(
                "E_EDGE_ENDPOINT_TYPE",
                f"边 {edge['edge_id']} 端点类型组合非法: {src_t} -> {tgt_t}",
                edge["edge_id"]))
            continue
        if expected_etype != edge["etype"]:
            issues.append(_err(
                "E_EDGE_KIND_MISMATCH",
                f"边 {edge['edge_id']} 存储种类 {edge['etype']} 与端点推导 {expected_etype} 不一致",
                edge["edge_id"]))
        if edge["etype"] == "association":
            if edge["polarity"] not in ("all", "not_all"):
                issues.append(_err("E_ASSOC_POLARITY",
                                   f"关联边 {edge['edge_id']} 缺少合法 polarity", edge["edge_id"]))
            if edge["context_element"] not in SCOPES:
                issues.append(_err("E_ASSOC_SCOPE",
                                   f"关联边 {edge['edge_id']} 缺少合法 context_element",
                                   edge["edge_id"]))
            if edge["closed"] not in (0, 1):
                issues.append(_err("E_ASSOC_CLOSED",
                                   f"关联边 {edge['edge_id']} 缺少 closed 标记", edge["edge_id"]))

    # 3. 默认成员声明
    for row in store.conn.execute(
        "SELECT dimension_id, member_id FROM dimension_default"
    ):
        dt = store.concept_type(row["dimension_id"])
        mt = store.concept_type(row["member_id"])
        if dt != "dimension":
            issues.append(_err("E_DEFAULT_DIM_TYPE",
                               f"默认声明的维度 {row['dimension_id']} 类型为 {dt}",
                               row["dimension_id"]))
        if mt != "member":
            issues.append(_err("E_DEFAULT_MEMBER_TYPE",
                               f"维度 {row['dimension_id']} 的默认成员 {row['member_id']} 类型为 {mt}",
                               row["member_id"]))

    # 4. 声明但从未被边使用的角色（警告）
    used_roles = {e["role_id"] for e in store.all_edges()}
    used_roles |= {e["target_role"] for e in store.all_edges() if e["target_role"]}
    for row in store.conn.execute("SELECT role_id FROM role"):
        if row["role_id"] not in used_roles:
            issues.append(_warn("W_ROLE_UNUSED",
                                f"角色 {row['role_id']} 已声明但无任何边使用", row["role_id"]))

    # 5. 孤立概念（警告，不阻断）
    referenced = set()
    for e in store.all_edges():
        referenced.add(e["source_id"])
        referenced.add(e["target_id"])
    for c in store.all_concepts():
        if c["concept_id"] not in referenced:
            issues.append(_warn("W_CONCEPT_ISOLATED",
                                f"概念 {c['concept_id']} 不参与任何关系", c["concept_id"]))

    return issues


def check_context(store: TaxonomyStore, context_id: str) -> list[dict]:
    """单个上下文的结构性检查（跨 scope 分别执行）。

    结构错误类别：
    E_DUP_DIMENSION  同一位置重复维度（输入错误）
    E_DIM_UNKNOWN    维度不在包中
    E_DIM_TYPE       该位置上的概念不是 dimension 类型
    E_MEMBER_UNKNOWN 成员不在包中
    E_MEMBER_TYPE    该位置上的概念不是 member 类型
    """
    issues: list[dict] = []
    for scope in SCOPES:
        seen: dict[str, str] = {}
        for row in store.context_pairs(context_id, scope):
            dim, mem = row["dimension_id"], row["member_id"]
            ref = f"{context_id}/{scope}/{dim}"
            if dim in seen:
                issues.append(_err(
                    "E_DUP_DIMENSION",
                    f"上下文 {context_id} 的 {scope} 位置重复出现维度 {dim}", ref))
            seen[dim] = mem

            dt = store.concept_type(dim)
            if dt is None:
                issues.append(_err("E_DIM_UNKNOWN",
                                   f"{scope} 维度 {dim} 不在分类包中", ref))
            elif dt != "dimension":
                issues.append(_err("E_DIM_TYPE",
                                   f"{scope} 的 {dim} 类型是 {dt}，不是 dimension", ref))

            mt = store.concept_type(mem)
            if mt is None:
                issues.append(_err("E_MEMBER_UNKNOWN",
                                   f"{scope} 维度 {dim} 的成员 {mem} 不在分类包中",
                                   f"{ref}/{mem}"))
            elif mt != "member":
                issues.append(_err("E_MEMBER_TYPE",
                                   f"{scope} 维度 {dim} 的成员 {mem} 类型是 {mt}，不是 member",
                                   f"{ref}/{mem}"))
    return issues


def check_facts(store: TaxonomyStore) -> list[dict]:
    """事实级结构检查：事实概念必须存在且为 primary；引用的上下文必须已登记。"""
    issues: list[dict] = []
    for fact in store.all_facts():
        ct = store.concept_type(fact["concept_id"])
        if ct is None:
            issues.append(_err("E_FACT_CONCEPT_UNKNOWN",
                               f"事实 {fact['fact_id']} 的概念 {fact['concept_id']} 不存在",
                               fact["fact_id"]))
        elif ct != "primary":
            issues.append(_err("E_FACT_CONCEPT_TYPE",
                               f"事实 {fact['fact_id']} 的概念 {fact['concept_id']} 类型为 {ct}，"
                               "事实只能挂 primary", fact["fact_id"]))
        if not _context_registered(store, fact["context_id"]):
            issues.append(_err("E_FACT_CONTEXT_UNKNOWN",
                               f"事实 {fact['fact_id']} 引用了未登记上下文 {fact['context_id']}",
                               fact["fact_id"]))
        elif not (store.context_pairs(fact["context_id"], "segment")
                  or store.context_pairs(fact["context_id"], "scenario")):
            # 空上下文合法（可走默认/无维度 cube），仅提示
            issues.append(_warn("W_FACT_EMPTY_CONTEXT",
                                f"事实 {fact['fact_id']} 的上下文 {fact['context_id']} 为空",
                                fact["fact_id"]))
    return issues


def _context_registered(store: TaxonomyStore, context_id: str) -> bool:
    row = store.conn.execute(
        "SELECT 1 FROM context_registry WHERE context_id=?",
        (context_id,),
    ).fetchone()
    return row is not None


def run_precheck(store: TaxonomyStore) -> dict:
    """预检总入口：返回结构化报告。"""
    package_issues = check_package(store)
    fact_issues = check_facts(store)
    context_ids = [r["context_id"] for r in store.conn.execute(
        "SELECT DISTINCT context_id FROM context_member_raw ORDER BY context_id")]
    context_issues: list[dict] = []
    for ctx_id in context_ids:
        context_issues.extend(check_context(store, ctx_id))
    all_issues = package_issues + context_issues + fact_issues
    return {
        "ok": not any(i["level"] == "error" for i in all_issues),
        "error_count": sum(1 for i in all_issues if i["level"] == "error"),
        "warning_count": sum(1 for i in all_issues if i["level"] == "warning"),
        "issues": all_issues,
    }
