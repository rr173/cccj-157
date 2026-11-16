"""逐项正反依据解释器。

把 engine 的结构化裁定渲染成人可读的解释，覆盖合同要求的全部要素：
- 适用关联及其继承来源路径（逐步标注角色）
- 所选 role 组与每个候选组的真值
- 每个 cube 的逐维度条件、默认补用、closed 额外维度、失配成员证据
- 可独立回查的关系路径（edge_id 序列 + 展开步骤 + 回查 SQL）
- 失败时逐组说明为何不能接收
"""
from __future__ import annotations

from . import network
from .store import TaxonomyStore

_REASON_CN = {
    "explicit_usable": "显式成员在 cube 角色下可用",
    "explicit_default_forbidden": "显式填写默认成员（非法）：默认仅可在缺维度时补用",
    "member_not_usable": "显式成员在 cube 角色下不可用",
    "missing_no_default": "缺少该维度且无默认成员可补",
    "default_applied": "缺维度，按默认成员补用，且默认在 cube 角色下可用",
    "default_not_usable": "默认成员在本次 cube 角色下不可用，无法补用",
}


def _path_line(store: TaxonomyStore, start_concept: str, start_role: str,
               edge_ids: list) -> list:
    if not edge_ids:
        return [f"    路径: 零长路径（初始概念 {start_concept} 自身，角色 {start_role}）"]
    lines = [f"    路径 edge_id 序列: {' -> '.join(edge_ids)}"]
    try:
        steps = network.replay_path(store, start_concept, start_role, edge_ids)
    except ValueError as exc:
        lines.append(f"    路径回放失败: {exc}")
        return lines
    for i, st in enumerate(steps, 1):
        tr = f"，target_role={st['target_role']}" if st["target_role"] else "，无 target_role（保持原角色）"
        flag = "" if st["usable"] else " [usable=false：不能凭此边成为终点，但可继续向下]"
        lines.append(
            f"    {i}. {st['edge_id']}: ({st['from']}, {st['role_before']}) "
            f"-> ({st['to']}, {st['role_after']}){tr}{flag}")
    return lines


def _recheck_sql(edge_ids: list) -> str:
    if not edge_ids:
        return ("-- 零长路径无边可查；起点状态可用 "
                "SELECT c.concept_id, c.ctype FROM concept c; 复核")
    quoted = ",".join(f"'{e}'" for e in edge_ids)
    return (f"SELECT edge_id, role_id, source_id, target_id, target_role, usable "
            f"FROM edge WHERE edge_id IN ({quoted}) ORDER BY edge_id;")


def explain_decision(store: TaxonomyStore, decision: dict) -> str:
    """渲染单个事实的完整解释文本。"""
    L: list[str] = []
    fid = decision["fact_id"]
    status = decision["status"]
    L.append(f"=== 事实 {fid} | 概念 {decision.get('concept')} | "
             f"上下文 {decision.get('context')} | 裁定: {status} ===")
    L.append(f"缓存键: {decision.get('cache_key')}（含 fact/concept/context 复合要素，"
             "网络层缓存另含 concept+role+edge 种类）")

    if status in ("invalid_context", "invalid_fact"):
        L.append("结构层错误（不进入 cube 判定，not_all 也无法反转）：")
        for i in decision.get("issues", []):
            L.append(f"  - [{i['code']}] {i['message']}")
        for ev in decision.get("evaluations", []):
            cr = ev["cube_result"]
            if cr.get("structural_error"):
                L.append(f"  关联 {ev['association_edge']} 因结构错误中止: "
                         f"{[i['code'] for i in cr.get('issues', [])]}")
        return "\n".join(L)

    if status == "unconstrained":
        L.append(f"  {decision.get('reason')}")
        L.append("  交付方式: unconstrained 单独交付，不伪装为 validated。")
        return "\n".join(L)

    # 适用关联与继承来源
    L.append("")
    L.append("【适用关联与继承来源】")
    seen = set()
    for ev in decision.get("evaluations", []):
        assoc_edge = ev["association_edge"]
        if assoc_edge in seen:
            continue
        seen.add(assoc_edge)
        edge = store.get_edge(assoc_edge)
        L.append(f"- 关联 {assoc_edge}（原始 role={ev['original_role']}，"
                 f"cube={edge['target_id']}，polarity={edge['polarity']}，"
                 f"位置={edge['context_element']}，closed={bool(edge['closed'])}）")
        L.append(f"  继承来源: 从 {edge['source_id']} @ {ev['original_role']} "
                 f"沿 primary 继承网络到达事实概念 {decision['concept']}")
        cov = store.get_coverage(assoc_edge, decision["concept"]) or []
        L.extend(_path_line(store, edge["source_id"], ev["original_role"], cov))
        L.append(f"  回查 SQL: {_recheck_sql(cov)}")

    # 逐 cube 维度条件
    L.append("")
    L.append("【每个 cube 的逐维度条件】")
    for ev in decision.get("evaluations", []):
        cr = ev["cube_result"]
        L.append(f"- cube {cr.get('cube')} via 关联 {ev['association_edge']} "
                 f"@ 位置 {cr['scope']}，进入角色 {cr.get('cube_entry_role')}，"
                 f"closed={cr.get('closed')} → 完整 cube 原始判定 raw_pass={cr['raw_pass']}")
        for cond in cr.get("dimension_conditions", []):
            mark = "✓" if cond["satisfied"] else "✗"
            L.append(f"  {mark} 维度 {cond['dimension']}（进入角色 {cond['entry_roles']}）"
                     f"提供方式={cond['provided_as']}，原因: {_REASON_CN.get(cond['reason'], cond['reason'])}")
            if "member" in cond:
                L.append(f"      成员: {cond['member']}")
            if cond.get("detail") and not isinstance(cond["detail"], dict):
                L.append(f"      说明: {cond['detail']}")
            L.append(f"      该角色下可用成员集合: {cond['usable_members']}")
            if cond.get("evidence"):
                evi = cond["evidence"]
                L.append(f"      可用性证据角色: {evi['entry_role']}，"
                         f"可用入边: {evi['usable_edges']}")
                L.extend(_path_line(store, cond["dimension"], evi["entry_role"],
                                    evi["representative_path"]))
                L.append(f"      回查 SQL: {_recheck_sql(evi['representative_path'])}")
            if cond["reason"] == "member_not_usable":
                if cond["detail"].get("note"):
                    L.append(f"      说明: {cond['detail']['note']}")
                for pr in cond["detail"].get("per_role", []):
                    if not pr.get("reachable"):
                        L.append(f"      角色 {pr['entry_role']}: 成员不可达")
                    else:
                        L.append(f"      角色 {pr['entry_role']}: 可达但 usable="
                                 f"{pr['usable']}，被挡入边 {pr['blocked_edges']}，"
                                 f"可用入边 {pr['usable_edges']}")
        for extra in cr.get("extras", []):
            tag = "允许" if extra["allowed"] else "拒绝"
            L.append(f"  [{tag}] 额外维度 {extra['dimension']}={extra['member']}: {extra['reason']}")

    # role 组真值
    L.append("")
    L.append("【角色组真值裁定】（关联按原始 role 分组，继承途中换角色不重分组）")
    for grp in decision.get("groups", []):
        t = grp["truth"]
        L.append(f"- 组 role={grp['role']}，组通过={grp['pass']}")
        L.append(f"  all 关联逐值={t['all']}（合取={t['all_conjunction']}）；"
                 f"not_all 关联逐值={t['not_all']}（合取={t['not_all_conjunction']}）")
        for a in grp["associations"]:
            L.append(f"    · {a['edge_id']}: polarity={a['polarity']}，"
                     f"cube raw_pass={a['raw_pass']}，反转后满足={a['polarity_satisfied']}")
        if not grp["pass"]:
            L.append("  不能接收的原因:")
            for r in grp["failure_reasons"]:
                L.append(f"    ✗ {r}")

    L.append("")
    if decision["status"] == "validated":
        L.append(f"结论: 接收。通过的候选组 {decision['passing_roles']}，"
                 f"选定 role 组 = {decision['selected_role']}（不同 role 组任选其一）。")
    else:
        L.append("结论: 拒收。全部候选组均失败，逐组原因见上。")
    return "\n".join(L)


def explain_fact(store: TaxonomyStore, fact_id: str) -> str:
    result = store.get_result(fact_id)
    if result is None:
        return f"未找到事实 {fact_id} 的准入结果"
    return explain_decision(store, result["decision"])
