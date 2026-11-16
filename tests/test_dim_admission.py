"""轻量语义回归测试（标准库 unittest，python3 -I 运行）。

运行：python3 -I tests/test_dim_admission.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dim_admission import engine, network, precheck  # noqa: E402
from dim_admission.demo import (_build_store, EXPECTED,  # noqa: E402
                                _assert_expected, _raw_map, _group_map,
                                run_state_proof, PATCH_EDGE)
from dim_admission.store import TaxonomyStore, load_package  # noqa: E402


class DemoTruthTableTests(unittest.TestCase):
    def setUp(self):
        self.store = _build_store(":memory:")
        network.compile_network(self.store)
        self.result = engine.admit_all(self.store)

    def test_every_fact_matches_handwritten_truth_table(self):
        failures = []
        for decision in self.result["decisions"]:
            _assert_expected(decision, failures)
        self.assertEqual(failures, [], msg="\n".join(failures))

    def test_status_distribution(self):
        self.assertEqual(self.result["summary"],
                         {"validated": 10, "rejected": 3, "unconstrained": 1,
                          "invalid_context": 2, "invalid_fact": 0})

    def test_role_isolation_m0(self):
        t = network.member_network(self.store, "D", "t")
        other = network.member_network(self.store, "D", "r_other")
        self.assertFalse(t["members"]["m0"]["usable"])
        self.assertTrue(t["members"]["m1"]["usable"])  # 穿过禁用点
        self.assertTrue(other["members"]["m0"]["usable"])

    def test_patch_opens_m0_only_in_t(self):
        self.store.add_edge(PATCH_EDGE)
        self.store.commit()
        self.store.conn.execute(
            "DELETE FROM reachable_cache WHERE start_concept='D' AND start_role='t'")
        self.store.conn.execute(
            "DELETE FROM reachable_cache WHERE start_concept='H' AND start_role='t'")
        self.store.conn.execute("DELETE FROM admission_cache")
        self.store.conn.execute("DELETE FROM admission_result")
        self.store.commit()
        t = network.member_network(self.store, "D", "t")
        self.assertTrue(t["members"]["m0"]["usable"])
        self.assertEqual(t["members"]["m0"]["usable_edges"], ["e_d_m0_alt"])
        # 旧的被挡证据仍保留
        self.assertEqual(t["members"]["m0"]["blocked_edges"], ["e_d_m0"])
        f2 = engine.evaluate_fact(
            self.store,
            self.store.conn.execute(
                "SELECT * FROM fact WHERE fact_id='F2'").fetchone())
        self.assertEqual((f2["status"], f2["selected_role"]), ("validated", "r"))


class StructuralErrorTests(unittest.TestCase):
    def setUp(self):
        self.store = _build_store(":memory:")

    def test_duplicate_dimension_is_structural(self):
        codes = {i["code"] for i in precheck.check_context(self.store, "C11")}
        self.assertEqual(codes, {"E_DUP_DIMENSION"})
        fact = self.store.conn.execute(
            "SELECT * FROM fact WHERE fact_id='F11'").fetchone()
        d = engine.evaluate_fact(self.store, fact)
        self.assertEqual(d["status"], "invalid_context")
        self.assertEqual(d["groups"], [])

    def test_unknown_member_is_structural_and_not_inverted(self):
        fact = self.store.conn.execute(
            "SELECT * FROM fact WHERE fact_id='F8'").fetchone()
        d = engine.evaluate_fact(self.store, fact)
        self.assertEqual(d["status"], "invalid_context")
        self.assertIn("E_MEMBER_UNKNOWN", [i["code"] for i in d["issues"]])

    def test_unconstrained_is_distinct_status(self):
        fact = self.store.conn.execute(
            "SELECT * FROM fact WHERE fact_id='F14'").fetchone()
        d = engine.evaluate_fact(self.store, fact)
        self.assertEqual(d["status"], "unconstrained")
        self.assertEqual(self.store.get_result("F14")["status"], "unconstrained")


class FiniteStateTests(unittest.TestCase):
    def test_state_proof_assertions_hold(self):
        # run_state_proof 内部已含断言，能正常返回即通过
        lines = run_state_proof()
        self.assertTrue(any("恒定为 3 个" in line for line in lines))

    def test_cycle_does_not_explode_states(self):
        store = TaxonomyStore(":memory:")
        load_package(store, {
            "roles": [{"id": "a"}],
            "concepts": [{"id": "A", "type": "primary"},
                         {"id": "B", "type": "primary"}],
            "edges": [
                {"edge_id": "x1", "role": "a", "source": "A", "target": "B"},
                {"edge_id": "x2", "role": "a", "source": "B", "target": "A"},
            ],
        })
        result = network.bfs_states(store, "A", "a", "inheritance")
        self.assertEqual(sorted(result["states"]),
                         [("A", "a"), ("B", "a")])
        self.assertEqual(result["examined"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
