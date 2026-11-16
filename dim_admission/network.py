"""带状态的分类网络遍历与闭包编译。

遍历状态严格为 (concept, role) 二元组：
- 从状态 (c, r) 出发，只沿当前角色 r 上、指定种类的边前进；
- 进入边的目标时，角色变为 target_role（若边给出），否则保持 r；
- 图允许环：用已访问状态集合去重，状态空间有限，枚举的是「可达状态」
  而非「所有路径/游走」，因此不会无限枚举；
- 重复关系（不同 edge_id、相同端点与角色）各自保留为独立来源证据。
"""
from __future__ import annotations

from collections import deque

from .store import TaxonomyStore


# --------------------------------------------------------------------------
# 底层：按 (状态, 边种类) 求后继
# --------------------------------------------------------------------------

def successors(store: TaxonomyStore, concept: str, role: str, etype: str) -> list:
    """当前角色 role 上、种类为 etype 的全部出边（含重复 edge_id 证据）。"""
    rows = store.conn.execute(
        """SELECT * FROM edge
           WHERE source_id=? AND role_id=? AND etype=?
           ORDER BY edge_id""",
        (concept, role, etype),
    ).fetchall()
    out = []
    for row in rows:
        entered_role = row["target_role"] or role
        out.append({
            "edge_id": row["edge_id"],
            "role": role,
            "target_role": row["target_role"],
            "target": row["target_id"],
            "entered_role": entered_role,
            "usable": bool(row["usable"]),
        })
    return out


def bfs_states(store: TaxonomyStore, start_concept: str, start_role: str,
               etype: str, include_start: bool = True) -> dict:
    """有限状态 BFS。

    返回：
      states:   可达状态 -> 该状态的到达证据列表
                [{"edge_id","from_state","usable","entered_role"}, ...]
      paths:    状态 -> 一条最短代表路径（edge_id 序列，起点为空列表）
      examined: 实际扫描的出边条数（用于证明工作量受状态数约束）
    """
    start = (start_concept, start_role)
    visited: dict[tuple, list] = {}
    parent: dict[tuple, tuple] = {}
    if include_start:
        visited[start] = []
    queue = deque([start])
    examined = 0

    while queue:
        concept, role = queue.popleft()
        for step in successors(store, concept, role, etype):
            examined += 1
            nxt = (step["target"], step["entered_role"])
            evidence = {
                "edge_id": step["edge_id"],
                "from_state": [concept, role],
                "usable": step["usable"],
                "entered_role": step["entered_role"],
            }
            if nxt not in visited:
                visited[nxt] = [evidence]
                parent[nxt] = (concept, role, step["edge_id"])
                queue.append(nxt)
            else:
                # 重复关系 / 汇聚边：保留为额外来源证据，但不重新入队
                visited[nxt].append(evidence)

    paths: dict[tuple, list] = {}
    for state in visited:
        paths[state] = _reconstruct(parent, start, state)
    return {"states": visited, "paths": paths, "examined": examined}


def _reconstruct(parent: dict, start: tuple, target: tuple) -> list:
    """从 BFS 父母表回放最短代表路径的 edge_id 序列。"""
    if target == start:
        return []
    edge_ids: list[str] = []
    cur = target
    while cur != start:
        prev_concept, prev_role, edge_id = parent[cur]
        edge_ids.append(edge_id)
        cur = (prev_concept, prev_role)
    edge_ids.reverse()
    return edge_ids


def replay_path(store: TaxonomyStore, start_concept: str, start_role: str,
                edge_ids: list) -> list:
    """把 edge_id 序列展开成可独立回查的逐步路径，每步标注经过角色。"""
    steps = []
    concept, role = start_concept, start_role
    for edge_id in edge_ids:
        edge = store.get_edge(edge_id)
        if edge is None or edge["source_id"] != concept or edge["role_id"] != role:
            raise ValueError(f"路径在边 {edge_id} 处与状态 ({concept}, {role}) 不吻合")
        entered = edge["target_role"] or role
        steps.append({
            "edge_id": edge_id,
            "from": concept,
            "role_before": role,
            "target_role": edge["target_role"],
            "to": edge["target_id"],
            "role_after": entered,
            "usable": bool(edge["usable"]),
        })
        concept, role = edge["target_id"], entered
    return steps


# --------------------------------------------------------------------------
# 带 SQLite 缓存的闭包（缓存键含 start_concept + start_role + etype）
# --------------------------------------------------------------------------

def reachable(store: TaxonomyStore, start_concept: str, start_role: str,
              etype: str, include_start: bool = True) -> dict:
    cached = store.load_reachable_cache(start_concept, start_role, etype)
    if cached is not None:
        raw_states, raw_paths, examined = cached
        states = {tuple(k.split("|", 1)): v for k, v in raw_states.items()}
        paths = {tuple(k.split("|", 1)): v for k, v in raw_paths.items()}
        return {"states": states, "paths": paths, "examined": examined, "from_cache": True}

    result = bfs_states(store, start_concept, start_role, etype, include_start)
    serializable_states = {json_key(k): v for k, v in result["states"].items()}
    serializable_paths = {json_key(k): v for k, v in result["paths"].items()}
    store.save_reachable_cache(start_concept, start_role, etype,
                               serializable_states, serializable_paths,
                               result["examined"])
    store.commit()
    result["from_cache"] = False
    return result


def json_key(state: tuple) -> str:
    return f"{state[0]}|{state[1]}"


# --------------------------------------------------------------------------
# 三类语义网络
# --------------------------------------------------------------------------

def inheritance_closure(store: TaxonomyStore, primary: str, role: str) -> dict:
    """primary 继承网络（含初始 source 自身，零长路径）。"""
    return reachable(store, primary, role, "inheritance", include_start=True)


def association_coverage(store: TaxonomyStore, association_edge_id: str) -> dict:
    """某条 primary→cube 适用关联，从其 source 与原始 role 出发，
    沿 primary 继承网络能到达的全部 primary（初始 source 也算）。

    注意：关联始终归入它的「原始 role」候选组，继承途中 target_role
    切换只影响后续遍历，不改变分组。
    """
    edge = store.get_edge(association_edge_id)
    if edge is None or edge["etype"] != "association":
        raise ValueError(f"{association_edge_id} 不是适用关联边")
    # 整体替换该关联的覆盖表，防止继承网络变化后残留陈旧 primary
    store.conn.execute(
        "DELETE FROM association_coverage WHERE association_edge=?",
        (association_edge_id,))
    closure = inheritance_closure(store, edge["source_id"], edge["role_id"])
    covered = []
    for (concept, role), path in closure["paths"].items():
        if store.concept_type(concept) != "primary":
            continue
        covered.append({
            "concept": concept,
            "state_role": role,
            "inherit_edge_path": path,
        })
        store.save_coverage(association_edge_id, concept, path)
    store.commit()
    return {
        "association_edge": association_edge_id,
        "original_role": edge["role_id"],
        "covered_primaries": sorted(covered, key=lambda r: r["concept"]),
        "examined": closure["examined"],
    }


def cube_dimensions(store: TaxonomyStore, cube_id: str, entry_role: str) -> dict:
    """cube 网络：从 (cube, 进入角色) 沿 cube→dimension 边求要求维度。
    返回每个维度的进入状态（维度可能经多条路径、多个角色进入）。"""
    closure = reachable(store, cube_id, entry_role, "cube_dim", include_start=True)
    dims: dict[str, dict] = {}
    for (concept, role), evidence in closure["states"].items():
        if store.concept_type(concept) != "dimension":
            continue
        entry = dims.setdefault(concept, {"dimension": concept, "entry_states": [],
                                          "representative_path": None})
        entry["entry_states"].append({"role": role, "arrival": evidence})
        if entry["representative_path"] is None:
            entry["representative_path"] = closure["paths"][(concept, role)]
    return {"cube": cube_id, "entry_role": entry_role,
            "dimensions": list(dims.values()), "examined": closure["examined"]}


def member_network(store: TaxonomyStore, dimension_id: str, entry_role: str) -> dict:
    """维度可用成员网络：从 (dimension, 进入角色) 沿 domain/member 边求取。

    关键语义（usable=false）：
    - 不可用入边的目标 member 不能「凭这条入边」成为合法终点，但 BFS
      仍会穿过它继续访问子 member；
    - 同一 member 只要另有一条 usable 入边可达，整体仍可用；
    - 环存在时，同一对前驱→成员可能经多条路径重复到达，按「直接前驱状态」
      去重后聚合，环边不会凭空制造一条新的可用来源；不同 edge_id 的并行
      入边（不同来源证据）仍各自保留在 all_incoming_edges 中；
    - 角色隔离：结论绑定 entry_role，别的角色上的可用入边不会污染本结果。
    """
    closure = reachable(store, dimension_id, entry_role, "domain", include_start=True)
    members: dict[str, dict] = {}
    for (concept, role), evidence in closure["states"].items():
        if store.concept_type(concept) != "member":
            continue
        # 按直接前驱 (from_state, edge_id) 归并：环路上同一前驱的重复到达
        # 不产生新的可用性来源；不同 edge_id 的并行边仍然各自计数
        unique_sources: dict[tuple, dict] = {}
        for e in evidence:
            key = (tuple(e["from_state"]), e["edge_id"])
            unique_sources.setdefault(key, e)
        incoming = list(unique_sources.values())
        usable_via = sorted(e["edge_id"] for e in incoming if e["usable"])
        blocked_via = sorted(e["edge_id"] for e in incoming if not e["usable"])
        members[concept] = {
            "member": concept,
            "state_role": role,
            "usable": bool(usable_via),
            "usable_edges": usable_via,
            "blocked_edges": blocked_via,
            "all_incoming_edges": sorted(e["edge_id"] for e in evidence),
            "representative_path": closure["paths"][(concept, role)],
        }
    return {"dimension": dimension_id, "entry_role": entry_role,
            "members": members, "examined": closure["examined"]}


# --------------------------------------------------------------------------
# 全量编译
# --------------------------------------------------------------------------

def compile_network(store: TaxonomyStore) -> dict:
    """预编译全部关系闭包与关联覆盖，写入 SQLite 缓存表。

    起点集合 = 实际作为某类边 source 的 (concept, role) 状态，
    因此编译工作量同样受有限状态空间约束。
    """
    stats = {"inheritance": 0, "cube_dim": 0, "domain": 0,
             "associations": 0, "states_cached": 0, "edges_scanned": 0}
    starts: dict[str, set] = {"inheritance": set(), "cube_dim": set(), "domain": set()}
    for edge in store.all_edges():
        if edge["etype"] in starts:
            starts[edge["etype"]].add((edge["source_id"], edge["role_id"]))

    for etype, seeds in starts.items():
        for concept, role in sorted(seeds):
            result = reachable(store, concept, role, etype, include_start=True)
            stats[etype] += 1
            stats["states_cached"] += len(result["states"])
            stats["edges_scanned"] += result["examined"]

    for edge in store.all_edges("association"):
        association_coverage(store, edge["edge_id"])
        stats["associations"] += 1
    store.commit()
    stats["finite_state_count"] = _state_space_size(store)
    return stats


def _state_space_size(store: TaxonomyStore) -> int:
    """理论状态空间上界 = 概念数 × 角色数（实际可达状态不超过它）。"""
    c = store.conn.execute("SELECT COUNT(*) AS n FROM concept").fetchone()["n"]
    r = store.conn.execute("SELECT COUNT(*) AS n FROM role").fetchone()["n"]
    return c * r
