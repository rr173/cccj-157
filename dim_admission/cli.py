"""命令行入口。

子命令：
  precheck  包/上下文/事实预检
  compile   预编译关系闭包并输出有限状态统计
  query     查询网络（继承覆盖 / cube 维度 / 成员网络 / 通用状态可达）
  admit     批量事实准入
  explain   单事实逐项正反依据
  demo      运行内置完整演练
所有写库操作落在独立 SQLite 文件，可反复回查。
"""
from __future__ import annotations

import argparse
import json
import sys

from . import engine, explain as explain_mod, network, precheck
from .store import TaxonomyStore, load_json_file


def _open(args) -> TaxonomyStore:
    return TaxonomyStore(args.db)


def cmd_precheck(args) -> int:
    with _open(args) as store:
        report = precheck.run_precheck(store)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


def cmd_compile(args) -> int:
    with _open(args) as store:
        stats = network.compile_network(store)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def cmd_query(args) -> int:
    with _open(args) as store:
        if args.what == "reachable":
            result = network.reachable(store, args.concept, args.role,
                                       args.edge_type, include_start=True)
            out = {
                "start": [args.concept, args.role], "edge_type": args.edge_type,
                "reachable_states": [list(s) for s in result["states"]],
                "representative_paths": {f"{s[0]}|{s[1]}": p
                                         for s, p in result["paths"].items()},
                "examined_edges": result["examined"],
                "from_cache": result.get("from_cache", False),
            }
        elif args.what == "coverage":
            out = network.association_coverage(store, args.edge_id)
        elif args.what == "cube":
            out = network.cube_dimensions(store, args.concept, args.role)
        elif args.what == "members":
            out = network.member_network(store, args.concept, args.role)
        else:  # replay
            out = {"steps": network.replay_path(
                store, args.concept, args.role, args.edge_ids.split(","))}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_admit(args) -> int:
    with _open(args) as store:
        result = engine.admit_all(store)
    if args.detail:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
        for d in result["decisions"]:
            print(f"{d['fact_id']}: {d['status']}"
                  + (f" (role={d.get('selected_role')})" if d.get("selected_role") else ""))
    return 0


def cmd_explain(args) -> int:
    with _open(args) as store:
        print(explain_mod.explain_fact(store, args.fact_id))
    return 0


def cmd_load(args) -> int:
    with _open(args) as store:
        kind = load_json_file(store, args.file)
    print(f"已装载: {kind} -> {args.db}")
    return 0


def cmd_demo(args) -> int:
    from .demo import run_demo
    return run_demo(args.keep_db)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dim-admission",
        description="XBRL 风格子集维度事实准入器（非完整 XBRL 实现）")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("load", help="装载 JSON（包/上下文/事实/边补丁）")
    sp.add_argument("--db", required=True)
    sp.add_argument("--file", required=True)
    sp.set_defaults(func=cmd_load)

    sp = sub.add_parser("precheck", help="包与上下文预检")
    sp.add_argument("--db", required=True)
    sp.set_defaults(func=cmd_precheck)

    sp = sub.add_parser("compile", help="预编译关系闭包")
    sp.add_argument("--db", required=True)
    sp.set_defaults(func=cmd_compile)

    sp = sub.add_parser("query", help="网络查询")
    sp.add_argument("--db", required=True)
    sp.add_argument("what", choices=["reachable", "coverage", "cube", "members", "replay"])
    sp.add_argument("--concept")
    sp.add_argument("--role")
    sp.add_argument("--edge-type", dest="edge_type",
                    choices=["inheritance", "association", "cube_dim", "domain"])
    sp.add_argument("--edge-id", dest="edge_id")
    sp.add_argument("--edge-ids", dest="edge_ids",
                    help="replay 用逗号分隔的 edge_id 序列")
    sp.set_defaults(func=cmd_query)

    sp = sub.add_parser("admit", help="批量事实准入")
    sp.add_argument("--db", required=True)
    sp.add_argument("--detail", action="store_true")
    sp.set_defaults(func=cmd_admit)

    sp = sub.add_parser("explain", help="单事实逐项解释")
    sp.add_argument("--db", required=True)
    sp.add_argument("--fact-id", dest="fact_id", required=True)
    sp.set_defaults(func=cmd_explain)

    sp = sub.add_parser("demo", help="运行内置完整演练")
    sp.add_argument("--keep-db", action="store_true", help="保留演练 SQLite 文件")
    sp.set_defaults(func=cmd_demo)
    return p


def main(argv: list | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
