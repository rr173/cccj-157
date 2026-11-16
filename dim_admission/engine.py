"""维度事实准入裁定引擎。

裁定分层（与 precheck 的结构错误严格分离）：
1. 结构层：上下文重复维度、未知/类型错误的维度或成员、事实概念非法
   → invalid_context / invalid_fact，不进入任何 cube 判定，not_all 也无法反转；
2. 语义层：对每个适用关联做完整 cube 检查（要求维度、可用成员、默认补用、
   closed 额外维度、位置隔离），再按原始 role 组合 all/not_all 真值；
3. 接收层：任一 role 组通过即接收；全部组失败拒收；无任何适用关联的
   primary 单独标 unconstrained。
"""
from __future__ import annotations

import hashlib
import json

from . import network, precheck
from .store import SCOPES, TaxonomyStore


# --------------------------------------------------------------------------
# 适用关联
# --------------------------------------------------------------------------

def applicable_associations(store: TaxonomyStore, concept_id: str) -> list:
    """找出从某 primary 事实概念出发、经继承网络适用的全部关联。

    关联原始 source 自身也算覆盖；关联保留其原始 role 用于候选组分组。
    """
    out = []
    for edge in store.all_edges("association"):
        coverage = network.association_coverage(store, edge["edge_id"])
        hit = next((c for c in coverage["covered_primaries"]
                    if c["concept"] == concept_id), None)
        if hit is not None:
            out.append({
                "edge_id": edge["edge_id"],
                "original_role": edge["role_id"],
                "cube": edge["target_id"],
                "cube_entry_role": edge["target_role"] or edge["role_id"],
                "polarity": edge["polarity"],
                "scope": edge["context_element"],
                "closed": bool(edge["closed"]),
                "inherit_path": hit["inherit_edge_path"],
                "inherit_state_role": hit["state_role"],
            })
    return out


# --------------------------------------------------------------------------
# 单个关联（cube）的完整检查
# --------------------------------------------------------------------------

def evaluate_cube(store: TaxonomyStore, assoc: dict, context_id: str) -> dict:
    """对一条适用关联做完整 cube 判定。

    返回 raw_pass（未经 polarity 反转的完整 cube 真值）与逐项依据。
    """
    scope = assoc["scope"]
    cache_key = "cube:" + hashlib.sha256(
        f"{assoc['edge_id']}|{context_id}|{scope}".encode("utf-8")).hexdigest()[:16]
    cached = store.cache_get(cache_key)
    if cached is not None:
        cached["from_cache"] = True
        return cached

    # 结构错误先行：结构层问题不参与、也不可能被 not_all 反转
    structural = [i for i in precheck.check_context(store, context_id)
                  if i["level"] == "error"]
    if structural:
        result = {
            "association_edge": assoc["edge_id"], "scope": scope,
            "structural_error": True, "raw_pass": False,
            "issues": structural, "dimension_conditions": [],
            "extras": [], "from_cache": False,
        }
        store.cache_put(cache_key, result)
        return result

    explicit = store.context_map(context_id, scope)
    other_scope = "scenario" if scope == "segment" else "segment"
    other_map = store.context_map(context_id, other_scope)

    cube_info = network.cube_dimensions(store, assoc["cube"], assoc["cube_entry_role"])
    dim_conditions = []
    semantic_ok = True

    for dim_entry in cube_info["dimensions"]:
        dim = dim_entry["dimension"]
        # 维度可能经多个角色进入，成员可用性取各进入角色结果的并集
        usable_members: dict[str, dict] = {}
        member_networks = []
        for entry in dim_entry["entry_states"]:
            net = network.member_network(store, dim, entry["role"])
            member_networks.append({"entry_role": entry["role"], "net": net})
            for mem, info in net["members"].items():
                if info["usable"] and mem not in usable_members:
                    usable_members[mem] = {
                        "entry_role": entry["role"],
                        "representative_path": info["representative_path"],
                        "usable_edges": info["usable_edges"],
                    }

        cond = _check_one_dimension(
            store, dim, explicit, other_map, other_scope,
            usable_members, member_networks, dim_entry)
        dim_conditions.append(cond)
        if not cond["satisfied"]:
            semantic_ok = False

    # closed：只检查关联指定位置上 cube 要求之外的额外维度
    required = {d["dimension"] for d in cube_info["dimensions"]}
    extras = []
    for dim, mem in explicit.items():
        if dim in required:
            continue
        extra = {"dimension": dim, "member": mem}
        if not assoc["closed"]:
            # 开放 cube 允许额外维度，但其显式成员必须是包中合法 member
            extra["allowed"] = store.concept_type(mem) == "member"
            extra["reason"] = ("open cube 允许额外维度，成员类型合法"
                               if extra["allowed"] else
                               "开放 cube 的额外维度成员也必须是合法 member")
        else:
            extra["allowed"] = False
            extra["reason"] = "closed=true 且该维度不在 cube 要求集合内"
        extras.append(extra)
    if assoc["closed"] and any(not e["allowed"] for e in extras):
        semantic_ok = False
    if not assoc["closed"] and any(not e["allowed"] for e in extras):
        # 开放 cube 下成员类型不合法属于结构层问题
        structural_note = {
            "level": "error", "code": "E_EXTRA_MEMBER_TYPE",
            "message": f"{scope} 额外维度的成员不是合法 member",
            "ref": context_id,
        }
        result = {
            "association_edge": assoc["edge_id"], "scope": scope,
            "structural_error": True, "raw_pass": False,
            "issues": [structural_note], "dimension_conditions": dim_conditions,
            "extras": extras, "from_cache": False,
        }
        store.cache_put(cache_key, result)
        return result

    result = {
        "association_edge": assoc["edge_id"],
        "cube": assoc["cube"],
        "cube_entry_role": assoc["cube_entry_role"],
        "scope": scope,
        "closed": assoc["closed"],
        "structural_error": False,
        "raw_pass": semantic_ok,
        "dimension_conditions": dim_conditions,
        "extras": extras,
        "examined_dimensions": cube_info["examined"],
        "from_cache": False,
    }
    store.cache_put(cache_key, result)
    return result


def _check_one_dimension(store, dim, explicit, other_map, other_scope,
                         usable_members, member_networks, dim_entry) -> dict:
    """单个要求维度的条件检查：显式成员 / 显式默认非法 / 缺失补默认。"""
    default_member = store.default_of(dim)
    cond: dict = {
        "dimension": dim,
        "entry_roles": [e["role"] for e in dim_entry["entry_states"]],
        "usable_members": sorted(usable_members.keys()),
        "default_member": default_member,
    }

    if dim in explicit:
        mem = explicit[dim]
        cond["provided_as"] = "explicit"
        cond["member"] = mem
        if default_member is not None and mem == default_member:
            cond["satisfied"] = False
            cond["reason"] = "explicit_default_forbidden"
            cond["detail"] = (f"显式填写了默认成员 {mem}；默认只能在缺维度时补用")
            return cond
        if mem in usable_members:
            cond["satisfied"] = True
            cond["reason"] = "explicit_usable"
            cond["evidence"] = usable_members[mem]
            return cond
        cond["satisfied"] = False
        cond["reason"] = "member_not_usable"
        blocked = _why_blocked(member_networks, mem)
        cond["detail"] = blocked
        return cond

    # 另一位置不能替本位置补齐
    if dim in other_map:
        cond["provided_as"] = "missing_here_but_in_other_scope"
        cond["other_scope"] = other_scope
        cond["other_member"] = other_map[dim]

    if default_member is None:
        cond["provided_as"] = cond.get("provided_as", "missing")
        cond["satisfied"] = False
        cond["reason"] = "missing_no_default"
        cond["detail"] = "缺该维度且未声明默认成员"
        return cond

    # 缺维度才可按默认补，且默认必须在本次 cube 角色下可用
    cond["provided_as"] = cond.get("provided_as", "missing")
    if default_member in usable_members:
        cond["satisfied"] = True
        cond["reason"] = "default_applied"
        cond["member"] = default_member
        cond["evidence"] = usable_members[default_member]
        cond["detail"] = f"缺维度，按默认补 {default_member}，且其在 cube 角色下可用"
        return cond

    cond["satisfied"] = False
    cond["reason"] = "default_not_usable"
    cond["member"] = default_member
    cond["detail"] = (f"默认成员 {default_member} 在本次 cube 角色下不可用，"
                      "不能补用")
    return cond


def _why_blocked(member_networks: list, member: str) -> dict:
    """汇总某成员在各进入角色下的入边证据，解释为何不可用。"""
    detail = {"member": member, "per_role": []}
    found = False
    for mn in member_networks:
        info = mn["net"]["members"].get(member)
        if info is None:
            detail["per_role"].append({"entry_role": mn["entry_role"],
                                       "reachable": False})
            continue
        found = True
        detail["per_role"].append({
            "entry_role": mn["entry_role"],
            "reachable": True,
            "usable": info["usable"],
            "blocked_edges": info["blocked_edges"],
            "usable_edges": info["usable_edges"],
        })
    if not found:
        detail["note"] = "成员在该维度的全部进入角色下均不可达"
    return detail


# --------------------------------------------------------------------------
# role 组真值与事实准入
# --------------------------------------------------------------------------

def evaluate_fact(store: TaxonomyStore, fact_row) -> dict:
    """完整事实准入，返回状态 + 每个候选组的逐项依据。"""
    fact_id = fact_row["fact_id"]
    concept_id = fact_row["concept_id"]
    context_id = fact_row["context_id"]

    ctype = store.concept_type(concept_id)
    if ctype is None:
        return _simple(fact_id, concept_id, context_id, "invalid_fact",
                       [{"code": "E_FACT_CONCEPT_UNKNOWN",
                         "message": f"概念 {concept_id} 不在包中"}])
    if ctype != "primary":
        return _simple(fact_id, concept_id, context_id, "invalid_fact",
                       [{"code": "E_FACT_CONCEPT_TYPE",
                         "message": f"概念 {concept_id} 类型为 {ctype}，事实只能挂 primary"}])

    structural = [i for i in precheck.check_context(store, context_id)
                  if i["level"] == "error"]
    if structural:
        return _simple(fact_id, concept_id, context_id, "invalid_context", structural)

    assocs = applicable_associations(store, concept_id)
    if not assocs:
        decision = {
            "fact_id": fact_id, "concept": concept_id, "context": context_id,
            "status": "unconstrained",
            "reason": "该 primary 在任何角色上都没有适用关联，单独交付而非伪装 validated",
            "groups": [],
            "cache_key": _fact_cache_key(fact_id, concept_id, context_id),
        }
        store.save_result(fact_id, concept_id, context_id, "unconstrained", decision)
        store.cache_put(decision["cache_key"], decision, fact_id)
        store.commit()
        return decision

    # 逐关联做完整 cube 检查
    evaluations = []
    any_structural = False
    for assoc in assocs:
        ev = evaluate_cube(store, assoc, context_id)
        evaluations.append({"association": assoc, "cube_result": ev})
        if ev.get("structural_error"):
            any_structural = True
    if any_structural:
        return _simple(
            fact_id, concept_id, context_id, "invalid_context",
            [i for ev in evaluations if ev["cube_result"].get("structural_error")
             for i in ev["cube_result"]["issues"]],
            extra={"evaluations": _serializable_evaluations(evaluations)})

    # 按关联「原始 role」分组（继承途中 target_role 切换不改变分组）
    groups: dict[str, dict] = {}
    for item in evaluations:
        assoc, ev = item["association"], item["cube_result"]
        grp = groups.setdefault(assoc["original_role"], {
            "role": assoc["original_role"], "associations": [],
            "all_results": [], "not_all_results": [], "pass": None,
            "failure_reasons": []})
        polarity = assoc["polarity"]
        # not_all 反转的是完整 cube 判定 raw_pass
        satisfied = (not ev["raw_pass"]) if polarity == "not_all" else ev["raw_pass"]
        grp["associations"].append({
            "edge_id": assoc["edge_id"], "cube": assoc["cube"],
            "polarity": polarity, "scope": assoc["scope"],
            "closed": assoc["closed"], "raw_pass": ev["raw_pass"],
            "polarity_satisfied": satisfied,
            "inherit_path": assoc["inherit_path"],
        })
        if polarity == "all":
            grp["all_results"].append(satisfied)
        else:
            grp["not_all_results"].append(satisfied)

    for role, grp in groups.items():
        # 组内：所有 all 关联的 cube 检查通过，且所有 not_all 关联的检查不通过
        all_ok = all(grp["all_results"]) if grp["all_results"] else True
        not_all_ok = all(grp["not_all_results"]) if grp["not_all_results"] else True
        grp["pass"] = all_ok and not_all_ok
        grp["truth"] = {"all": grp["all_results"], "not_all": grp["not_all_results"],
                        "all_conjunction": all_ok, "not_all_conjunction": not_all_ok}
        if not grp["pass"]:
            grp["failure_reasons"] = _group_failure_reasons(grp)

    passed_roles = [r for r, g in groups.items() if g["pass"]]
    status = "validated" if passed_roles else "rejected"
    decision = {
        "fact_id": fact_id, "concept": concept_id, "context": context_id,
        "status": status,
        "selected_role": passed_roles[0] if passed_roles else None,
        "passing_roles": passed_roles,
        "groups": [groups[r] for r in sorted(groups)],
        "evaluations": _serializable_evaluations(evaluations),
        "cache_key": _fact_cache_key(fact_id, concept_id, context_id),
    }
    store.save_result(fact_id, concept_id, context_id, status, decision)
    store.cache_put(decision["cache_key"], decision, fact_id)
    store.commit()
    return decision


def _group_failure_reasons(grp: dict) -> list:
    reasons = []
    for a in grp["associations"]:
        if not a["polarity_satisfied"]:
            if a["polarity"] == "all":
                reasons.append(f"all 关联 {a['edge_id']} 的 cube {a['cube']} 检查未通过")
            else:
                reasons.append(f"not_all 关联 {a['edge_id']} 的 cube {a['cube']} 竟通过了检查")
    return reasons


def _serializable_evaluations(evaluations: list) -> list:
    out = []
    for item in evaluations:
        out.append({"association_edge": item["association"]["edge_id"],
                    "original_role": item["association"]["original_role"],
                    "cube_result": item["cube_result"]})
    return out


def _fact_cache_key(fact_id: str, concept: str, context: str) -> str:
    # 缓存键必须含真正影响结果的角色与上下文位置；这里以完整复合要素入键
    raw = json.dumps({"fact": fact_id, "concept": concept, "context": context},
                     ensure_ascii=False, sort_keys=True)
    return "fact:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _simple(fact_id, concept_id, context_id, status, issues, extra=None) -> dict:
    decision = {
        "fact_id": fact_id, "concept": concept_id, "context": context_id,
        "status": status, "issues": issues, "groups": [],
        "cache_key": _fact_cache_key(fact_id, concept_id, context_id),
    }
    if extra:
        decision.update(extra)
    return decision


def admit_all(store: TaxonomyStore) -> dict:
    """批量事实准入入口。"""
    summary = {"validated": 0, "rejected": 0, "unconstrained": 0,
               "invalid_context": 0, "invalid_fact": 0}
    decisions = []
    for fact in store.all_facts():
        decision = evaluate_fact(store, fact)
        # 结构类结果也落库，便于回查
        if decision["status"] in ("invalid_context", "invalid_fact"):
            store.save_result(fact["fact_id"], fact["concept_id"],
                              fact["context_id"], decision["status"], decision)
            store.commit()
        summary[decision["status"]] += 1
        decisions.append(decision)
    return {"summary": summary, "decisions": decisions}
