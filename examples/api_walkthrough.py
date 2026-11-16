#!/usr/bin/env python3
"""最小 API 用法示例（隔离模式运行：python3 -I examples/api_walkthrough.py）。

不读取任何外部数据，直接在内存 SQLite 上建一个最小分类包并裁定两条事实。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dim_admission import engine, explain, network, precheck  # noqa: E402
from dim_admission.store import (TaxonomyStore, load_contexts,  # noqa: E402
                                 load_facts, load_package)

PACKAGE = {
    "roles": [{"id": "r"}, {"id": "t"}],
    "concepts": [
        {"id": "P", "type": "primary"}, {"id": "H", "type": "cube"},
        {"id": "D", "type": "dimension"},
        {"id": "m_ok", "type": "member"}, {"id": "m_bad", "type": "member"},
    ],
    "edges": [
        {"edge_id": "a1", "role": "r", "source": "P", "target": "H",
         "target_role": "t", "polarity": "all",
         "context_element": "segment", "closed": True},
        {"edge_id": "hd", "role": "t", "source": "H", "target": "D"},
        {"edge_id": "dm_bad", "role": "t", "source": "D",
         "target": "m_bad", "usable": False},
        {"edge_id": "dm_ok", "role": "t", "source": "D", "target": "m_ok"},
    ],
}
CONTEXTS = [
    {"id": "ctx_ok", "segment": [["D", "m_ok"]]},
    {"id": "ctx_bad", "segment": [["D", "m_bad"]]},
]
FACTS = [
    {"fact_id": "fact_ok", "concept": "P", "context_id": "ctx_ok", "value": "1"},
    {"fact_id": "fact_bad", "concept": "P", "context_id": "ctx_bad", "value": "2"},
]


def main() -> int:
    store = TaxonomyStore(":memory:")
    load_package(store, PACKAGE)
    load_contexts(store, CONTEXTS)
    load_facts(store, FACTS)

    print("预检:", precheck.run_precheck(store)["ok"])
    print("编译:", network.compile_network(store))
    result = engine.admit_all(store)
    for decision in result["decisions"]:
        print(explain.explain_decision(store, decision))
        print("-" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
