"""SQLite 存储层：分类包结构、上下文/事实、派生闭包与准入缓存。

本模块只负责存取与基础约束，不做语义裁定（裁定在 engine.py）。
所有图遍历都以 (概念 ID, 角色 ID) 二元状态为单位，因此缓存表的键
一律同时携带概念与角色，绝不会只用 concept 或 dimension。
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Iterable

VALID_CONCEPT_TYPES = ("primary", "cube", "dimension", "member")
SCOPES = ("segment", "scenario")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS role (
    role_id     TEXT PRIMARY KEY,
    description TEXT
);

CREATE TABLE IF NOT EXISTS concept (
    concept_id TEXT PRIMARY KEY,
    ctype      TEXT NOT NULL CHECK (ctype IN ('primary','cube','dimension','member'))
);

CREATE TABLE IF NOT EXISTS edge (
    edge_id     TEXT PRIMARY KEY,
    role_id     TEXT NOT NULL REFERENCES role(role_id),
    source_id   TEXT NOT NULL REFERENCES concept(concept_id),
    target_id   TEXT NOT NULL REFERENCES concept(concept_id),
    target_role TEXT REFERENCES role(role_id),
    etype       TEXT NOT NULL CHECK (etype IN ('inheritance','association','cube_dim','domain')),
    usable      INTEGER NOT NULL DEFAULT 1,
    polarity    TEXT CHECK (polarity IN ('all','not_all')),
    context_element TEXT CHECK (context_element IN ('segment','scenario')),
    closed      INTEGER CHECK (closed IN (0,1)),
    CHECK (source_id <> target_id OR 1=1)
);

CREATE TABLE IF NOT EXISTS dimension_default (
    dimension_id TEXT PRIMARY KEY REFERENCES concept(concept_id),
    member_id    TEXT NOT NULL REFERENCES concept(concept_id)
);

-- 上下文登记：空上下文也有记录，用于区分「空上下文」与「未登记上下文」
CREATE TABLE IF NOT EXISTS context_registry (
    context_id TEXT PRIMARY KEY
);

-- 上下文原始成员：保留重复维度行，由 precheck/engine 判为结构错误
CREATE TABLE IF NOT EXISTS context_member_raw (
    context_id   TEXT NOT NULL,
    scope        TEXT NOT NULL CHECK (scope IN ('segment','scenario') OR scope = ''),
    dimension_id TEXT NOT NULL,
    member_id    TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    PRIMARY KEY (context_id, scope, dimension_id, seq)
);

CREATE TABLE IF NOT EXISTS fact (
    fact_id    TEXT PRIMARY KEY,
    concept_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    value      TEXT
);

-- ---- 编译产物（带状态缓存，键含概念+角色） ----

CREATE TABLE IF NOT EXISTS reachable_cache (
    start_concept TEXT NOT NULL,
    start_role    TEXT NOT NULL,
    etype         TEXT NOT NULL,
    state_json    TEXT NOT NULL,
    paths_json    TEXT NOT NULL,
    examined      INTEGER NOT NULL,
    PRIMARY KEY (start_concept, start_role, etype)
);

CREATE TABLE IF NOT EXISTS association_coverage (
    association_edge TEXT NOT NULL,
    fact_concept     TEXT NOT NULL,
    inherit_path_json TEXT NOT NULL,
    PRIMARY KEY (association_edge, fact_concept)
);

CREATE TABLE IF NOT EXISTS admission_cache (
    cache_key   TEXT PRIMARY KEY,
    fact_id     TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admission_result (
    fact_id       TEXT PRIMARY KEY,
    concept_id    TEXT NOT NULL,
    context_id    TEXT NOT NULL,
    status        TEXT NOT NULL,
    decision_json TEXT NOT NULL
);
"""


def derive_edge_type(source_type: str, target_type: str) -> str:
    """按端点概念类型推导边的种类；非法组合抛 ValueError（precheck 会先拦）。"""
    pair = (source_type, target_type)
    if pair == ("primary", "primary"):
        return "inheritance"
    if pair == ("primary", "cube"):
        return "association"
    if pair == ("cube", "dimension"):
        return "cube_dim"
    if pair in (("dimension", "member"), ("member", "member")):
        return "domain"
    raise ValueError(f"非法端点类型组合: {source_type} -> {target_type}")


class TaxonomyStore:
    """一个 SQLite 文件对应一个已装载的分类包 + 上下文 + 事实。"""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "TaxonomyStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- 基础写入 ----------

    def add_role(self, role_id: str, description: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO role(role_id, description) VALUES (?,?)",
            (role_id, description),
        )

    def add_concept(self, concept_id: str, ctype: str) -> None:
        if ctype not in VALID_CONCEPT_TYPES:
            raise ValueError(f"未知概念类型: {ctype}")
        self.conn.execute(
            "INSERT OR REPLACE INTO concept(concept_id, ctype) VALUES (?,?)",
            (concept_id, ctype),
        )

    def set_default(self, dimension_id: str, member_id: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO dimension_default(dimension_id, member_id) VALUES (?,?)",
            (dimension_id, member_id),
        )

    def add_edge(self, edge: dict) -> None:
        """edge 字段：edge_id/role/source/target，可选 target_role/usable；
        association 边另需 polarity/context_element/closed。"""
        source_type = self.conn.execute(
            "SELECT ctype FROM concept WHERE concept_id=?", (edge["source"],)
        ).fetchone()
        target_type = self.conn.execute(
            "SELECT ctype FROM concept WHERE concept_id=?", (edge["target"],)
        ).fetchone()
        if source_type is None or target_type is None:
            raise ValueError(f"边 {edge.get('edge_id')} 引用了未声明概念")
        etype = derive_edge_type(source_type["ctype"], target_type["ctype"])
        usable = 1 if edge.get("usable", True) else 0
        polarity = edge.get("polarity")
        context_element = edge.get("context_element")
        closed = edge.get("closed")
        if etype == "association":
            if polarity not in ("all", "not_all"):
                raise ValueError(f"关联边 {edge['edge_id']} 缺少合法 polarity")
            if context_element not in SCOPES:
                raise ValueError(f"关联边 {edge['edge_id']} 缺少合法 context_element")
            if closed not in (True, False, 0, 1):
                raise ValueError(f"关联边 {edge['edge_id']} 缺少 closed")
            closed = int(closed)
        else:
            polarity = context_element = None
            closed = None
        self.conn.execute(
            """INSERT OR REPLACE INTO edge
               (edge_id, role_id, source_id, target_id, target_role, etype,
                usable, polarity, context_element, closed)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                edge["edge_id"], edge["role"], edge["source"], edge["target"],
                edge.get("target_role"), etype, usable, polarity,
                context_element, closed,
            ),
        )

    def add_context(self, ctx: dict) -> None:
        """ctx = {id, segment: [(dim, member), ...], scenario: [...]}。
        用列表保留重复维度（结构错误），不在这里抛异常；空上下文也登记，
        以便预检区分「空上下文（合法，可走默认）」与「未登记上下文」。"""
        ctx_id = ctx["id"]
        self.conn.execute(
            "INSERT OR IGNORE INTO context_registry(context_id) VALUES (?)",
            (ctx_id,))
        wrote_any = False
        for scope in SCOPES:
            for seq, pair in enumerate(ctx.get(scope, [])):
                dim, mem = pair if isinstance(pair, list) else (pair["dimension"], pair["member"])
                self.conn.execute(
                    """INSERT INTO context_member_raw
                       (context_id, scope, dimension_id, member_id, seq)
                       VALUES (?,?,?,?,?)""",
                    (ctx_id, scope, dim, mem, seq),
                )
                wrote_any = True
        if not wrote_any:
            # 占位行让空上下文可被检索；scope='' 不参与 segment/scenario 成员逻辑
            self.conn.execute(
                """INSERT OR IGNORE INTO context_member_raw
                   (context_id, scope, dimension_id, member_id, seq)
                   VALUES (?, '', '', '', -1)""", (ctx_id,))

    def add_fact(self, fact: dict) -> None:
        context_id = fact.get("context_id", fact.get("context"))
        self.conn.execute(
            "INSERT OR REPLACE INTO fact(fact_id, concept_id, context_id, value) VALUES (?,?,?,?)",
            (fact["fact_id"], fact["concept"], context_id, str(fact.get("value", ""))),
        )

    def commit(self) -> None:
        self.conn.commit()

    # ---------- 读取辅助 ----------

    def concept_type(self, concept_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT ctype FROM concept WHERE concept_id=?", (concept_id,)
        ).fetchone()
        return row["ctype"] if row else None

    def role_exists(self, role_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM role WHERE role_id=?", (role_id,)
        ).fetchone() is not None

    def all_edges(self, etype: str | None = None) -> list[sqlite3.Row]:
        if etype:
            return list(self.conn.execute(
                "SELECT * FROM edge WHERE etype=? ORDER BY edge_id", (etype,)))
        return list(self.conn.execute("SELECT * FROM edge ORDER BY edge_id"))

    def all_concepts(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM concept ORDER BY concept_id"))

    def all_facts(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM fact ORDER BY fact_id"))

    def context_pairs(self, context_id: str, scope: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT * FROM context_member_raw
               WHERE context_id=? AND scope=? ORDER BY seq""",
            (context_id, scope),
        ))

    def context_map(self, context_id: str, scope: str) -> dict[str, str]:
        """去重后的 dimension -> member（重复维度取首条，重复本身由预检报告）。"""
        out: dict[str, str] = {}
        for row in self.context_pairs(context_id, scope):
            out.setdefault(row["dimension_id"], row["member_id"])
        return out

    def default_of(self, dimension_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT member_id FROM dimension_default WHERE dimension_id=?",
            (dimension_id,),
        ).fetchone()
        return row["member_id"] if row else None

    def get_edge(self, edge_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM edge WHERE edge_id=?", (edge_id,)).fetchone()

    # ---------- 编译产物存取（键含概念+角色） ----------

    def load_reachable_cache(self, concept: str, role: str, etype: str):
        row = self.conn.execute(
            """SELECT state_json, paths_json, examined FROM reachable_cache
               WHERE start_concept=? AND start_role=? AND etype=?""",
            (concept, role, etype),
        ).fetchone()
        if row is None:
            return None
        return (json.loads(row["state_json"]),
                json.loads(row["paths_json"]),
                row["examined"])

    def save_reachable_cache(self, concept: str, role: str, etype: str,
                             states: dict, paths: dict, examined: int) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO reachable_cache
               (start_concept, start_role, etype, state_json, paths_json, examined)
               VALUES (?,?,?,?,?,?)""",
            (concept, role, etype, json.dumps(states, ensure_ascii=False),
             json.dumps(paths, ensure_ascii=False), examined),
        )

    def save_coverage(self, association_edge: str, fact_concept: str,
                      inherit_path: list) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO association_coverage
               (association_edge, fact_concept, inherit_path_json) VALUES (?,?,?)""",
            (association_edge, fact_concept, json.dumps(inherit_path, ensure_ascii=False)),
        )

    def get_coverage(self, association_edge: str, fact_concept: str):
        row = self.conn.execute(
            """SELECT inherit_path_json FROM association_coverage
               WHERE association_edge=? AND fact_concept=?""",
            (association_edge, fact_concept),
        ).fetchone()
        return json.loads(row["inherit_path_json"]) if row else None

    def cache_get(self, key: str):
        row = self.conn.execute(
            "SELECT payload_json FROM admission_cache WHERE cache_key=?", (key,)
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def cache_put(self, key: str, payload: Any, fact_id: str | None = None) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO admission_cache(cache_key, fact_id, payload_json)
               VALUES (?,?,?)""",
            (key, fact_id, json.dumps(payload, ensure_ascii=False)),
        )

    def save_result(self, fact_id: str, concept_id: str, context_id: str,
                    status: str, decision: dict) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO admission_result
               (fact_id, concept_id, context_id, status, decision_json)
               VALUES (?,?,?,?,?)""",
            (fact_id, concept_id, context_id, status,
             json.dumps(decision, ensure_ascii=False)),
        )

    def get_result(self, fact_id: str):
        row = self.conn.execute(
            "SELECT * FROM admission_result WHERE fact_id=?", (fact_id,)).fetchone()
        if row is None:
            return None
        return {
            "fact_id": row["fact_id"], "concept_id": row["concept_id"],
            "context_id": row["context_id"], "status": row["status"],
            "decision": json.loads(row["decision_json"]),
        }

    def all_results(self) -> list[dict]:
        return [self.get_result(r["fact_id"])
                for r in self.conn.execute("SELECT fact_id FROM admission_result ORDER BY fact_id")]


# ---------------- JSON 装载 ----------------

def load_package(store: TaxonomyStore, package: dict) -> None:
    """装载分类包 JSON：roles/concepts/defaults/edges。"""
    for role in package.get("roles", []):
        if isinstance(role, str):
            store.add_role(role)
        else:
            store.add_role(role["id"], role.get("description"))
    for c in package.get("concepts", []):
        store.add_concept(c["id"], c["type"])
    for d in package.get("defaults", []):
        store.set_default(d["dimension"], d["member"])
    for edge in package.get("edges", []):
        store.add_edge(edge)
    store.commit()


def load_contexts(store: TaxonomyStore, contexts: Iterable[dict]) -> None:
    for ctx in contexts:
        store.add_context(ctx)
    store.commit()


def load_facts(store: TaxonomyStore, facts: Iterable[dict]) -> None:
    for fact in facts:
        store.add_fact(fact)
    store.commit()


def load_json_file(store: TaxonomyStore, path: str) -> str:
    """按 JSON 顶层字段自动分流装载，返回装载内容种类。"""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    kinds = []
    is_package = "concepts" in data or "roles" in data or "defaults" in data
    if is_package:
        load_package(store, data)
        kinds.append("package")
    if "contexts" in data:
        load_contexts(store, data["contexts"])
        kinds.append("contexts")
    if "facts" in data:
        load_facts(store, data["facts"])
        kinds.append("facts")
    if "edges" in data and not is_package:
        # 纯边文件（如补丁）：概念/角色必须已在库中声明
        for edge in data["edges"]:
            store.add_edge(edge)
        store.commit()
        kinds.append("edge_patch")
    return "+".join(kinds) if kinds else "empty"
