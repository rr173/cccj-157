"""端到端主演练（合同验收主体）。

覆盖：
1. 主图：P --继承(切角色)--> Q，closed cube H；D->m0 usable=false、m0->m1 可用；
   Q 显式 m1 通过、显式 m0 在 H 上失败；无关角色 r_other 让 m0 可用不污染 t；
   补一条 t 下另一条 usable 入边后 m0 才通过。
2. 默认成员：缺维度补默认可过、显式填默认非法、默认在 cube 角色下不可用。
3. segment/scenario 位置隔离与混放（另一位置不能补齐、不算 closed 额外项）。
4. closed 额外维度拒绝；open cube 额外维度（合法成员允许）。
5. not_all 反转完整 cube 判定；结构错误不被反转。
6. 另一 role 组成功即可接收；全组失败拒收并逐组解释。
7. unconstrained primary 单独交付。
8. 独立小图按有限 (概念,角色) 状态枚举：环导致游走无限但状态有限，
   含共享分支，验证路径逐步角色并证明不枚举所有路径。
每个 cube 与每个角色组的期望值手写为真值表并断言实际输出。
"""
from __future__ import annotations

import os
import tempfile

from . import engine, explain as explain_mod, network, precheck
from .store import TaxonomyStore, load_facts, load_package, load_contexts

# ==========================================================================
# 分类包定义
# ==========================================================================

ROLES = [
    {"id": "r", "description": "主继承起始角色（H 组）"},
    {"id": "s", "description": "继承切换后的角色"},
    {"id": "t", "description": "cube H/HW 的展开角色"},
    {"id": "r3", "description": "K cube 组"},
    {"id": "r4", "description": "not_all 组（继承只覆盖 P,Q）"},
    {"id": "r5", "description": "DW 默认不可用演示组"},
    {"id": "r6", "description": "open cube 组（继承只覆盖 P,Q）"},
    {"id": "r_other", "description": "无关角色，验证角色隔离"},
]

CONCEPTS = [
    {"id": "P", "type": "primary"}, {"id": "Q", "type": "primary"},
    {"id": "Q2", "type": "primary"}, {"id": "Q3", "type": "primary"},
    {"id": "Q4", "type": "primary"}, {"id": "U", "type": "primary"},
    {"id": "H", "type": "cube"}, {"id": "K", "type": "cube"},
    {"id": "N", "type": "cube"}, {"id": "HW", "type": "cube"},
    {"id": "HO", "type": "cube"},
    {"id": "D", "type": "dimension"}, {"id": "D2", "type": "dimension"},
    {"id": "DW", "type": "dimension"}, {"id": "DX", "type": "dimension"},
    {"id": "m0", "type": "member"}, {"id": "m1", "type": "member"},
    {"id": "n1", "type": "member"}, {"id": "d2def", "type": "member"},
    {"id": "wdef", "type": "member"}, {"id": "x1", "type": "member"},
    # 注意：上下文 C8 引用的 GHOST 故意不在概念表中，构成 E_MEMBER_UNKNOWN
]

EDGES = [
    # --- primary 继承：按角色独立的继承网络 ---
    # r/r3/r5 上 P->Q 切到 s，再经 s 链覆盖 Q2/Q3/Q4
    {"edge_id": "e_inh_p_q", "role": "r", "source": "P", "target": "Q",
     "target_role": "s"},
    {"edge_id": "e_inh3_p_q", "role": "r3", "source": "P", "target": "Q",
     "target_role": "s"},
    {"edge_id": "e_inh5_p_q", "role": "r5", "source": "P", "target": "Q",
     "target_role": "s"},
    # r4/r6 上 P->Q 不切角色，且 Q 在该角色无出边 -> 只覆盖 {P,Q}
    {"edge_id": "e_inh4_p_q", "role": "r4", "source": "P", "target": "Q"},
    {"edge_id": "e_inh6_p_q", "role": "r6", "source": "P", "target": "Q"},
    {"edge_id": "e_inh_q_q2", "role": "s", "source": "Q", "target": "Q2"},
    {"edge_id": "e_inh_q_q3", "role": "s", "source": "Q", "target": "Q3"},
    {"edge_id": "e_inh_q_q4", "role": "s", "source": "Q", "target": "Q4"},

    # --- 适用关联：原始 role 决定候选组，target_role 决定 cube 进入角色 ---
    {"edge_id": "e_a_h", "role": "r", "source": "P", "target": "H",
     "target_role": "t", "polarity": "all",
     "context_element": "segment", "closed": True},
    {"edge_id": "e_a_k", "role": "r3", "source": "P", "target": "K",
     "target_role": "r3", "polarity": "all",
     "context_element": "scenario", "closed": True},
    {"edge_id": "e_a_n", "role": "r4", "source": "P", "target": "N",
     "target_role": "r4", "polarity": "not_all",
     "context_element": "segment", "closed": True},
    {"edge_id": "e_a_dw", "role": "r5", "source": "P", "target": "HW",
     "target_role": "t", "polarity": "all",
     "context_element": "segment", "closed": True},
    {"edge_id": "e_a_open", "role": "r6", "source": "P", "target": "HO",
     "target_role": "r6", "polarity": "all",
     "context_element": "segment", "closed": False},

    # --- cube -> dimension ---
    {"edge_id": "e_h_d", "role": "t", "source": "H", "target": "D"},
    {"edge_id": "e_hw_dw", "role": "t", "source": "HW", "target": "DW"},
    {"edge_id": "e_k_d2", "role": "r3", "source": "K", "target": "D2"},
    {"edge_id": "e_n_d", "role": "r4", "source": "N", "target": "D"},
    {"edge_id": "e_ho_d", "role": "r6", "source": "HO", "target": "D"},

    # --- domain/member：t 下 D->m0 不可用，m0->m1 可用（可穿过禁用点） ---
    {"edge_id": "e_d_m0", "role": "t", "source": "D", "target": "m0",
     "usable": False},
    {"edge_id": "e_m0_m1", "role": "t", "source": "m0", "target": "m1"},
    # r3 下 D2 的成员（含默认 d2def，默认在 r3 下可用）
    {"edge_id": "e_d2_n1", "role": "r3", "source": "D2", "target": "n1"},
    {"edge_id": "e_d2_def", "role": "r3", "source": "D2", "target": "d2def"},
    # r4 下 N 复用维度 D：只有 m1 可用，m0 不可达
    {"edge_id": "e_d_m1_r4", "role": "r4", "source": "D", "target": "m1"},
    # r6 下 open cube 复用 D：只有 m1 可用
    {"edge_id": "e_d_m1_r6", "role": "r6", "source": "D", "target": "m1"},
    # t 下 DW：默认 wdef 唯一入边 usable=false -> 默认在 cube 角色下不可用
    {"edge_id": "e_dw_wdef", "role": "t", "source": "DW", "target": "wdef",
     "usable": False},
    # 无关角色 r_other 上 m0 可用——绝不能污染 t 的结论
    {"edge_id": "e_d_m0_other", "role": "r_other", "source": "D",
     "target": "m0"},
    # 额外维度 DX 的成员
    {"edge_id": "e_dx_x1", "role": "t", "source": "DX", "target": "x1"},
]

DEFAULTS = [
    {"dimension": "D2", "member": "d2def"},
    {"dimension": "DW", "member": "wdef"},
]

# 补丁：在 t 下给 m0 增加第二条、usable 的入边（不同 edge_id 的独立来源证据）
PATCH_EDGE = {"edge_id": "e_d_m0_alt", "role": "t", "source": "D",
              "target": "m0", "usable": True}

CONTEXTS = [
    {"id": "C1", "segment": [["D", "m1"]]},
    {"id": "C2", "segment": [["D", "m0"]]},
    {"id": "C3", "segment": [["D", "m1"]], "scenario": [["D2", "n1"]]},
    {"id": "C4", "segment": [["D", "m1"], ["DX", "x1"]],
     "scenario": [["DX", "x1"]]},                       # 两位置都有 closed 额外
    {"id": "C5", "segment": [], "scenario": [["D2", "d2def"]]},  # 显式默认
    {"id": "C6", "segment": [["D", "m1"]], "scenario": [["D2", "d2def"]]},
    {"id": "C7", "segment": [], "scenario": []},        # 缺维度，走默认
    {"id": "C8", "segment": [["D", "m1"], ["DX", "GHOST"]]},     # 未知成员
    {"id": "C9", "segment": [["D", "m1"]], "scenario": [["D2", "n1"]]},
    {"id": "C10", "segment": [["D", "m0"]]},
    {"id": "C11", "segment": [["D", "m1"], ["D", "m0"]]},        # 重复维度
    {"id": "C12", "segment": [["D", "m0"]]},
    {"id": "C13", "segment": [], "scenario": [["D2", "d2def"]]},
    {"id": "C15", "segment": [], "scenario": [["D", "m1"]]},     # 位置错位
    {"id": "C16", "segment": [["D", "m1"], ["DX", "x1"]]},       # open cube 额外
]

FACTS = [
    {"fact_id": "F1", "concept": "Q", "context_id": "C1", "value": "100"},
    {"fact_id": "F2", "concept": "Q", "context_id": "C2", "value": "101"},
    {"fact_id": "F3", "concept": "Q", "context_id": "C3", "value": "102"},
    {"fact_id": "F4", "concept": "Q4", "context_id": "C4", "value": "103"},
    {"fact_id": "F5", "concept": "Q4", "context_id": "C5", "value": "104"},
    {"fact_id": "F6", "concept": "Q", "context_id": "C6", "value": "105"},
    {"fact_id": "F7", "concept": "Q", "context_id": "C7", "value": "106"},
    {"fact_id": "F8", "concept": "Q", "context_id": "C8", "value": "107"},
    {"fact_id": "F9", "concept": "Q", "context_id": "C9", "value": "108"},
    {"fact_id": "F10", "concept": "Q", "context_id": "C10", "value": "109"},
    {"fact_id": "F11", "concept": "Q", "context_id": "C11", "value": "110"},
    {"fact_id": "F12", "concept": "Q2", "context_id": "C12", "value": "111"},
    {"fact_id": "F13", "concept": "Q4", "context_id": "C13", "value": "112"},
    {"fact_id": "F14", "concept": "U", "context_id": "C1", "value": "113"},
    {"fact_id": "F15", "concept": "Q", "context_id": "C15", "value": "114"},
    {"fact_id": "F16", "concept": "Q", "context_id": "C16", "value": "115"},
]

# 手写期望值。raw: 每个关联 cube 的完整判定（未经 polarity 反转）；
# 组真值按关联原始 role：r=e_a_h(all) r3=e_a_k(all) r4=e_a_n(not_all)
# r5=e_a_dw(all) r6=e_a_open(all)。None 表示该事实概念不在此关联覆盖内。
EXPECTED = {
    "F1": {"status": "validated", "selected": "r",
           "raw": {"e_a_h": True, "e_a_k": True, "e_a_n": True,
                   "e_a_dw": False, "e_a_open": True},
           "groups": {"r": True, "r3": True, "r4": False, "r5": False, "r6": True}},
    "F2": {"status": "validated", "selected": "r3",
           "raw": {"e_a_h": False, "e_a_k": True, "e_a_n": False,
                   "e_a_dw": False, "e_a_open": False},
           "groups": {"r": False, "r3": True, "r4": True, "r5": False, "r6": False}},
    "F3": {"status": "validated", "selected": "r",
           "raw": {"e_a_h": True, "e_a_k": True, "e_a_n": True,
                   "e_a_dw": False, "e_a_open": True},
           "groups": {"r": True, "r3": True, "r4": False, "r5": False, "r6": True}},
    "F4": {"status": "rejected", "selected": None,
           "raw": {"e_a_h": False, "e_a_k": False, "e_a_n": None,
                   "e_a_dw": False, "e_a_open": None},
           "groups": {"r": False, "r3": False, "r4": None, "r5": False, "r6": None}},
    "F5": {"status": "rejected", "selected": None,
           "raw": {"e_a_h": False, "e_a_k": False, "e_a_n": None,
                   "e_a_dw": False, "e_a_open": None},
           "groups": {"r": False, "r3": False, "r4": None, "r5": False, "r6": None}},
    "F6": {"status": "validated", "selected": "r",
           "raw": {"e_a_h": True, "e_a_k": False, "e_a_n": True,
                   "e_a_dw": False, "e_a_open": True},
           "groups": {"r": True, "r3": False, "r4": False, "r5": False, "r6": True}},
    "F7": {"status": "validated", "selected": "r3",
           "raw": {"e_a_h": False, "e_a_k": True, "e_a_n": False,
                   "e_a_dw": False, "e_a_open": False},
           "groups": {"r": False, "r3": True, "r4": True, "r5": False, "r6": False}},
    "F8": {"status": "invalid_context", "selected": None,
           "raw": None, "groups": None, "codes": ["E_MEMBER_UNKNOWN"]},
    "F9": {"status": "validated", "selected": "r",
           "raw": {"e_a_h": True, "e_a_k": True, "e_a_n": True,
                   "e_a_dw": False, "e_a_open": True},
           "groups": {"r": True, "r3": True, "r4": False, "r5": False, "r6": True}},
    "F10": {"status": "validated", "selected": "r3",
            "raw": {"e_a_h": False, "e_a_k": True, "e_a_n": False,
                    "e_a_dw": False, "e_a_open": False},
            "groups": {"r": False, "r3": True, "r4": True, "r5": False, "r6": False}},
    "F11": {"status": "invalid_context", "selected": None,
            "raw": None, "groups": None, "codes": ["E_DUP_DIMENSION"]},
    "F12": {"status": "validated", "selected": "r3",
            "raw": {"e_a_h": False, "e_a_k": True, "e_a_n": None,
                    "e_a_dw": False, "e_a_open": None},
            "groups": {"r": False, "r3": True, "r4": None, "r5": False, "r6": None}},
    "F13": {"status": "rejected", "selected": None,
            "raw": {"e_a_h": False, "e_a_k": False, "e_a_n": None,
                    "e_a_dw": False, "e_a_open": None},
            "groups": {"r": False, "r3": False, "r4": None, "r5": False, "r6": None}},
    "F14": {"status": "unconstrained", "selected": None,
            "raw": None, "groups": None},
    "F15": {"status": "validated", "selected": "r4",
            "raw": {"e_a_h": False, "e_a_k": False, "e_a_n": False,
                    "e_a_dw": False, "e_a_open": False},
            "groups": {"r": False, "r3": False, "r4": True, "r5": False, "r6": False}},
    "F16": {"status": "validated", "selected": "r3",
            "raw": {"e_a_h": False, "e_a_k": True, "e_a_n": False,
                    "e_a_dw": False, "e_a_open": True},
            "groups": {"r": False, "r3": True, "r4": True, "r5": False, "r6": True}},
}

# 补丁后期望（m0 在 t 下获得第二条 usable 入边）
EXPECTED_AFTER_PATCH = {
    "F2": {"status": "validated", "selected": "r"},
    "F12": {"status": "validated", "selected": "r"},
}


def _build_store(path: str = ":memory:") -> TaxonomyStore:
    store = TaxonomyStore(path)
    load_package(store, {"roles": ROLES, "concepts": CONCEPTS,
                         "edges": EDGES, "defaults": DEFAULTS})
    load_contexts(store, CONTEXTS)
    load_facts(store, FACTS)
    return store


def _raw_map(decision: dict) -> dict | None:
    if "evaluations" not in decision:
        return None
    return {e["association_edge"]: e["cube_result"]["raw_pass"]
            for e in decision["evaluations"]}


def _group_map(decision: dict) -> dict | None:
    if "groups" not in decision or not decision["groups"]:
        return None
    return {g["role"]: g["pass"] for g in decision["groups"]}


def _assert_expected(decision: dict, failures: list) -> None:
    exp = EXPECTED[decision["fact_id"]]
    fid = decision["fact_id"]
    if decision["status"] != exp["status"]:
        failures.append(f"{fid}: 状态 {decision['status']} != 期望 {exp['status']}")
    if exp["raw"] is not None:
        actual_raw = _raw_map(decision)
        for edge_id, want in exp["raw"].items():
            got = actual_raw.get(edge_id)
            if want is None:
                if edge_id in actual_raw:
                    failures.append(f"{fid}: 关联 {edge_id} 不应适用却出现了")
            elif got != want:
                failures.append(f"{fid}: cube raw {edge_id}={got} != 期望 {want}")
    if exp["groups"] is not None:
        actual_groups = _group_map(decision)
        # 期望里 None 表示该组不适用，实际映射中该 role 应缺失
        for role, want in exp["groups"].items():
            got = actual_groups.get(role)
            if want is None:
                if role in actual_groups:
                    failures.append(f"{fid}: 角色组 {role} 不应出现却出现了")
            elif got != want:
                failures.append(f"{fid}: 组 {role} 真值 {got} != 期望 {want}")
    if decision.get("selected_role") != exp["selected"]:
        failures.append(f"{fid}: 选定组 {decision.get('selected_role')} "
                        f"!= 期望 {exp['selected']}")
    if "codes" in exp:
        codes = [i["code"] for i in decision.get("issues", [])]
        for code in exp["codes"]:
            if code not in codes:
                failures.append(f"{fid}: 缺少结构错误码 {code}，实际 {codes}")


# ==========================================================================
# 独立小图：有限状态枚举 + 环不产生无限路径枚举
# ==========================================================================

MINI_ROLES = [{"id": "a"}, {"id": "b"}]
MINI_CONCEPTS = [
    {"id": "X", "type": "primary"}, {"id": "Y", "type": "primary"},
    {"id": "Z", "type": "primary"},
    {"id": "MD", "type": "dimension"}, {"id": "mm1", "type": "member"},
    {"id": "mm2", "type": "member"},
]
MINI_EDGES = [
    # 环：X@a -> Y@b -> X@a（带 target_role 切换）
    {"edge_id": "m_xy", "role": "a", "source": "X", "target": "Y",
     "target_role": "b"},
    {"edge_id": "m_yx", "role": "b", "source": "Y", "target": "X",
     "target_role": "a"},
    # 共享分支：X@a 有两条不同 edge_id 的出边到同一目标 Z
    {"edge_id": "m_xz_1", "role": "a", "source": "X", "target": "Z"},
    {"edge_id": "m_xz_2", "role": "a", "source": "X", "target": "Z"},
    # 自环：Z@a -> Z@a，使从 X 出发的游走条数随长度单调增长（2 的幂）
    {"edge_id": "m_zz", "role": "a", "source": "Z", "target": "Z"},
    # 成员网络上的环：mm1 <-> mm2（同角色 a）
    {"edge_id": "m_md_mm1", "role": "a", "source": "MD", "target": "mm1",
     "usable": False},
    {"edge_id": "m_mm1_mm2", "role": "a", "source": "mm1", "target": "mm2"},
    {"edge_id": "m_mm2_mm1", "role": "a", "source": "mm2", "target": "mm1"},
]


def _count_walks(store: TaxonomyStore, start: tuple, etype: str,
                 max_len: int) -> list:
    """朴素「枚举所有游走」的计数（含环、含重复 edge_id），用于对照证明。"""
    counts = [1]  # 长度 0：1 条空游走
    frontier = [start]
    for _ in range(max_len):
        nxt = []
        for concept, role in frontier:
            for step in network.successors(store, concept, role, etype):
                nxt.append((step["target"], step["entered_role"]))
        counts.append(len(nxt))
        frontier = nxt
    return counts


def run_state_proof() -> list:
    lines = []
    store = TaxonomyStore(":memory:")
    load_package(store, {"roles": MINI_ROLES, "concepts": MINI_CONCEPTS,
                         "edges": MINI_EDGES})
    lines.append("【小图：有限 (概念,角色) 状态枚举 vs 朴素游走枚举】")

    result = network.bfs_states(store, "X", "a", "inheritance")
    reachable = sorted(result["states"])
    lines.append(f"状态空间上界 = 概念数×角色数 = 6×2 = 12；"
                 f"BFS 实际可达状态仅 {len(reachable)} 个: {reachable}")
    lines.append(f"BFS 扫描出边 {result['examined']} 条即终止"
                 "（visited 状态去重，环不重复展开）")

    lines.append("代表路径逐步角色回放（X@a -> Y@b）:")
    for st in network.replay_path(store, "X", "a", result["paths"][("Y", "b")]):
        lines.append(f"  {st['edge_id']}: ({st['from']},{st['role_before']}) -> "
                     f"({st['to']},{st['role_after']})")

    walks = _count_walks(store, ("X", "a"), "inheritance", 8)
    lines.append("朴素游走枚举的游走条数（长度 0..8）: " + ", ".join(map(str, walks)))
    growing = all(walks[i + 1] >= walks[i] for i in range(len(walks) - 1)) and walks[-1] > walks[0]
    lines.append(f"游走数随长度不减且末端({walks[-1]})远大于起点({walks[0]})，"
                 "自环/环使可枚举游走无限多：" + ("成立" if growing else "不成立"))
    lines.append("=> 朴素枚举永远不会终止；而 BFS 的 visited 是有限状态集合，"
                 f"可达状态恒定为 {len(reachable)} 个、扫描 {result['examined']} "
                 "条边即终止。本项目枚举状态而非路径，故不会无限枚举。")
    assert growing

    z_evidence = result["states"][("Z", "a")]
    edge_ids = sorted(e["edge_id"] for e in z_evidence)
    lines.append(f"共享分支证据: 状态 (Z,a) 的到达证据 edge_id = {edge_ids}"
                 "（两条并行入边各自保留为独立来源，自环 m_zz 同样登记但不重复入队）")
    assert {"m_xz_1", "m_xz_2"}.issubset(set(edge_ids))

    mem = network.member_network(store, "MD", "a")
    lines.append(f"成员网络（含 mm1<->mm2 环）可达成员: {sorted(mem['members'])}")
    lines.append(
        f"  mm1 usable={mem['members']['mm1']['usable']}：入边 "
        f"{mem['members']['mm1']['all_incoming_edges']} 中 m_md_mm1 标 "
        "usable=false，但环边 m_mm2_mm1 是另一条 usable 来源，成员仍可用")
    lines.append(
        f"  mm2 usable={mem['members']['mm2']['usable']}：经被禁用的 mm1 "
        "继续向下到达（禁用点不阻断穿越）")
    assert mem["members"]["mm1"]["usable"] is True
    assert mem["members"]["mm1"]["blocked_edges"] == ["m_md_mm1"]
    assert mem["members"]["mm2"]["usable"] is True
    store.close()
    return lines


# ==========================================================================
# 主流程
# ==========================================================================

def _print_truth_table(decisions: list) -> list:
    lines = ["", "================ 手写真值表对照 ================",
             "事实  状态             cube raw (h/k/n/dw/op)      "
             "组 (r/r3/r4/r5/r6)       选定组"]
    for d in decisions:
        fid = d["fact_id"]
        raw = _raw_map(d)
        grp = _group_map(d)
        if raw is None:
            raw_s = "--"
        else:
            def cell(v):
                return "-" if v is None else ("T" if v else "F")
            raw_s = "".join(cell(raw.get(k)) for k in
                            ("e_a_h", "e_a_k", "e_a_n", "e_a_dw", "e_a_open"))
        if grp is None:
            grp_s = "--"
        else:
            grp_s = "/".join(
                "-" if grp.get(r) is None else ("T" if grp.get(r) else "F")
                for r in ("r", "r3", "r4", "r5", "r6"))
        lines.append(f"{fid:<5} {d['status']:<17} {raw_s:<27} {grp_s:<24} "
                     f"{d.get('selected_role') or '-'}")
    return lines


def run_demo(keep_db: bool = False) -> int:
    failures: list = []
    out: list = []
    tmpdir = tempfile.mkdtemp(prefix="dim_admission_demo_")
    db_path = os.path.join(tmpdir, "demo.sqlite")

    out.append("############ 1. 装载分类包、上下文与事实 ############")
    store = _build_store(db_path)

    out.append("")
    out.append("############ 2. 预检（结构错误在此暴露，不参与裁定） ############")
    report = precheck.run_precheck(store)
    out.append(f"预检 ok={report['ok']}，error={report['error_count']}，"
               f"warning={report['warning_count']}")
    ctx11 = precheck.check_context(store, "C11")
    ctx8 = precheck.check_context(store, "C8")
    out.append(f"C11 重复维度 -> {[i['code'] for i in ctx11]}")
    out.append(f"C8 未知成员 -> {[i['code'] for i in ctx8]}")
    assert {i["code"] for i in ctx11} == {"E_DUP_DIMENSION"}
    assert {i["code"] for i in ctx8} == {"E_MEMBER_UNKNOWN"}

    out.append("")
    out.append("############ 3. 编译网络（有限状态统计） ############")
    stats = network.compile_network(store)
    out.append(f"闭包起点: inheritance={stats['inheritance']} "
               f"cube_dim={stats['cube_dim']} domain={stats['domain']}；"
               f"缓存状态总数={stats['states_cached']}；"
               f"理论状态空间上界={stats['finite_state_count']}；"
               f"扫描边={stats['edges_scanned']}")

    out.append("")
    out.append("############ 4. 主演练语义检查 ############")
    # 4.1 关联覆盖：P 在 r 关联，继承（切到 s）到达 Q/Q2/Q3/Q4
    cov = network.association_coverage(store, "e_a_h")
    covered = sorted(c["concept"] for c in cov["covered_primaries"])
    out.append(f"关联 e_a_h 覆盖的 primary: {covered}（初始 P 自身 + 继承到达的 Q 系）")
    assert covered == ["P", "Q", "Q2", "Q3", "Q4"]
    q_path = next(c["inherit_edge_path"] for c in cov["covered_primaries"]
                  if c["concept"] == "Q")
    out.append(f"P->Q 继承路径: {q_path}（关联仍归原始组 r，不因切到 s 重分组）")
    assert q_path == ["e_inh_p_q"]
    # r4 角色的关联只覆盖 {P,Q}
    cov4 = network.association_coverage(store, "e_a_n")
    out.append(f"关联 e_a_n(r4) 覆盖: "
               f"{sorted(c['concept'] for c in cov4['covered_primaries'])}"
               "（r4 继承网络独立，Q4 不在覆盖内）")
    assert sorted(c["concept"] for c in cov4["covered_primaries"]) == ["P", "Q"]

    # 4.2 角色隔离：t 下 m0 不可用；r_other 下可用但不污染
    net_t = network.member_network(store, "D", "t")
    net_other = network.member_network(store, "D", "r_other")
    out.append(f"t 角色: m0 usable={net_t['members']['m0']['usable']}，"
               f"挡路入边={net_t['members']['m0']['blocked_edges']}；"
               f"m1 usable={net_t['members']['m1']['usable']}"
               "（穿过不可用的 m0 继续到达）")
    out.append("r_other 角色: m0 "
               f"usable={net_other['members']['m0']['usable']}"
               "（独立缓存键，不写入 t 的结论）")
    assert net_t["members"]["m0"]["usable"] is False
    assert net_t["members"]["m1"]["usable"] is True
    assert net_other["members"]["m0"]["usable"] is True

    # 4.3 批量准入并逐条断言手写真值表
    result = engine.admit_all(store)
    out.append(f"批量准入汇总: {result['summary']}")
    for d in result["decisions"]:
        _assert_expected(d, failures)
    out.extend(_print_truth_table(result["decisions"]))

    out.append("")
    out.append("############ 5. 补丁：t 下增加 m0 的另一条 usable 入边 ############")
    store.add_edge(PATCH_EDGE)
    store.commit()
    # 补丁改变 domain 网络（D@t）与从 (H,t) 进入的 cube 网络；
    # 缓存键含角色，r_other/r4/r6 等其它角色缓存不受影响、予以保留
    store.conn.execute(
        "DELETE FROM reachable_cache WHERE start_concept='D' AND start_role='t'")
    store.conn.execute(
        "DELETE FROM reachable_cache WHERE start_concept='H' AND start_role='t'")
    store.conn.execute("DELETE FROM admission_cache")
    store.conn.execute("DELETE FROM admission_result")
    store.commit()
    for fid, want in EXPECTED_AFTER_PATCH.items():
        after = engine.evaluate_fact(
            store, store.conn.execute(
                "SELECT * FROM fact WHERE fact_id=?", (fid,)).fetchone())
        out.append(f"补丁后 {fid}: 状态={after['status']}，"
                   f"选定组={after.get('selected_role')}")
        if after["status"] != want["status"] or after.get("selected_role") != want["selected"]:
            failures.append(f"补丁后 {fid} 不符合期望 {want}")
    net_t2 = network.member_network(store, "D", "t")
    out.append(f"补丁后 t 下 m0 usable={net_t2['members']['m0']['usable']}，"
               f"可用入边={net_t2['members']['m0']['usable_edges']}，"
               f"被挡入边仍保留={net_t2['members']['m0']['blocked_edges']}")
    assert net_t2["members"]["m0"]["usable"] is True
    # r_other 与 r4 的结论未被补丁影响
    assert network.member_network(store, "D", "r_other")["members"]["m0"]["usable"] is True
    assert "m0" not in network.member_network(store, "D", "r4")["members"]

    out.append("")
    out.append("############ 6. 逐项解释样例（F4 全组失败 / F7 默认补用 / "
               "F15 位置错位 / F14 无约束） ############")
    # 补丁验证曾清空结果表，这里重新批量准入落库后再解释
    engine.admit_all(store)
    for fid in ("F4", "F7", "F15", "F14"):
        out.append(explain_mod.explain_fact(store, fid))
        out.append("")

    out.append("############ 7. 独立小图有限状态证明 ############")
    out.extend(run_state_proof())

    out.append("")
    out.append("############ 8. 缓存键角色/位置要素核查 ############")
    keys = [r[0] for r in store.conn.execute(
        "SELECT cache_key FROM admission_cache ORDER BY cache_key")]
    out.append(f"准入缓存键示例: {keys[:3]}")
    reach_keys = [(r["start_concept"], r["start_role"], r["etype"]) for r in
                  store.conn.execute(
                      "SELECT DISTINCT start_concept, start_role, etype "
                      "FROM reachable_cache WHERE start_concept='D' "
                      "ORDER BY start_role")]
    out.append(f"成员网络缓存按 (D, 角色, domain) 分键: {reach_keys}")
    assert ("D", "t", "domain") in reach_keys
    assert ("D", "r_other", "domain") in reach_keys

    if failures:
        out.append("")
        out.append("!!!!!!!!!! 真值表断言失败 !!!!!!!!!!")
        out.extend(f"- {f}" for f in failures)
        rc = 1
    else:
        out.append("")
        out.append("全部手写真值表断言通过：16 个事实 + 补丁前后 + 小图状态证明。")
        rc = 0

    print("\n".join(out))
    if not keep_db:
        store.close()
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        print(f"\n演练数据库保留在: {db_path}")
        store.close()
    return rc
