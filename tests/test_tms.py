"""规则引擎 (app/tms.py) 逻辑测试 —— 仅使用标准库 unittest。"""

import unittest

from app.tms import (
    TMS,
    CyclicRuleError,
    DanglingRuleError,
    DuplicateIdError,
    TMSError,
    UnknownFactError,
)


def two_path_tms():
    """安全员的标准规程: 结论 c 有两条独立支持路径, d 仅依赖 c。"""
    t = TMS()
    for f in ("f1", "f2", "f3"):
        t.add_fact(f)
    t.add_rule("r1", "c", ["f1"])          # 路径一
    t.add_rule("r2", "c", ["f2", "f3"])    # 路径二
    t.add_rule("r3", "d", ["c"])           # 唯一下游依赖
    return t


class ForwardEvaluationTests(unittest.TestCase):
    def test_firing_saves_complete_premise_sets(self):
        t = two_path_tms()
        c = t.nodes["c"]
        self.assertEqual(c.status, "active")
        rules = {s.rule_id for s in c.supports}
        self.assertEqual(rules, {"r1", "r2"})
        # 每次触发保存完整前提集合 + 事实层完整依据
        by_rule = {s.rule_id: s for s in c.supports}
        self.assertEqual(by_rule["r1"].antecedents, ("f1",))
        self.assertEqual(by_rule["r1"].basis, ("f1",))
        self.assertEqual(by_rule["r2"].antecedents, ("f2", "f3"))
        self.assertEqual(by_rule["r2"].basis, ("f2", "f3"))

    def test_multi_hop_basis_expansion(self):
        t = TMS()
        for f in ("a", "b"):
            t.add_fact(f)
        t.add_rule("r1", "m", ["a"])
        t.add_rule("r2", "n", ["m", "b"])
        support = t.explain("n")[0]
        self.assertEqual(support.antecedents, ("m", "b"))
        self.assertEqual(support.basis, ("a", "b"))  # 完整可复算依据展开到事实层


class RetractionTests(unittest.TestCase):
    def test_retract_one_path_conclusion_survives_with_remaining_basis(self):
        t = two_path_tms()
        rec = t.retract("f1")
        # 结论保持有效, 页面可列出剩余的完整可复算依据
        self.assertEqual(t.nodes["c"].status, "active")
        self.assertEqual(t.nodes["d"].status, "active")
        self.assertEqual(rec.survived, ["c"])
        remaining = t.explain("c")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].rule_id, "r2")
        self.assertEqual(remaining[0].basis, ("f2", "f3"))
        # 耗尽的支持留痕
        self.assertEqual([s.rule_id for s in t.nodes["c"].retired_supports], ["r1"])
        # 受影响集合只含事实本身 (无结论失效)
        self.assertEqual([a.node_id for a in rec.affected], ["f1"])

    def test_retract_last_support_propagates_downstream(self):
        t = two_path_tms()
        t.retract("f1")
        rec = t.retract("f2")  # 撤回最后一条支持事实
        self.assertEqual(t.nodes["c"].status, "inactive")
        self.assertEqual(t.nodes["d"].status, "inactive")
        # 该结论及仅依赖它的下游结论均失效
        self.assertEqual([a.node_id for a in rec.affected], ["f2", "c", "d"])
        # 支持耗尽形成的传播链: f2 -> c -> d
        chain = [(s.node_id, s.triggered_by, s.rule_id)
                 for s in rec.propagation_chain]
        self.assertEqual(chain, [("c", "f2", "r2"), ("d", "c", "r3")])
        # 每步都保存了当时的完整依据 (d 的依据在第一次撤回后已刷新为替代路径)
        self.assertEqual(rec.propagation_chain[0].exhausted_basis, ("f2", "f3"))
        self.assertEqual(rec.propagation_chain[1].exhausted_basis, ("f2", "f3"))
        # 失效结论不再有任何当前完整支持
        self.assertEqual(t.explain("c"), [])
        self.assertEqual(t.explain("d"), [])

    def test_conclusion_only_fails_when_no_complete_support(self):
        # 中间节点多路径, 下游共享: 撤回一条路径不应误杀
        t = TMS()
        for f in ("a", "b", "x"):
            t.add_fact(f)
        t.add_rule("r1", "m", ["a"])
        t.add_rule("r2", "m", ["b"])
        t.add_rule("r3", "n", ["m", "x"])
        rec = t.retract("a")
        self.assertEqual(t.nodes["m"].status, "active")
        self.assertEqual(t.nodes["n"].status, "active")
        self.assertEqual(rec.survived, ["m"])
        # 再撤回 b: m 失效并传播, n 因 m 失效而失效 (x 不足以独立支持)
        rec2 = t.retract("b")
        self.assertEqual(t.nodes["m"].status, "inactive")
        self.assertEqual(t.nodes["n"].status, "inactive")
        self.assertEqual([s.node_id for s in rec2.propagation_chain], ["m", "n"])

    def test_repeated_retraction_returns_stable_verdict(self):
        t = two_path_tms()
        first = t.retract("f2")
        second = t.retract("f2")
        self.assertTrue(second.already_retracted)
        self.assertEqual([a.node_id for a in second.affected],
                         [a.node_id for a in first.affected])
        self.assertEqual([s.node_id for s in second.propagation_chain],
                         [s.node_id for s in first.propagation_chain])
        # 状态不发生变化
        self.assertEqual(t.nodes["c"].status, "active")
        self.assertEqual(t.retraction_order.count("f2"), 1)

    def test_unknown_fact_rejected(self):
        t = two_path_tms()
        with self.assertRaises(UnknownFactError):
            t.retract("ghost")


class ValidationTests(unittest.TestCase):
    def test_unknown_fact_and_dangling_reference_rejected(self):
        t = TMS()
        t.add_fact("a")
        with self.assertRaises(DanglingRuleError):
            t.add_rule("r1", "q", ["a", "ghost"])
        # 被拒绝的规则不得污染规程
        self.assertNotIn("r1", t.rules)
        self.assertNotIn("q", t.nodes)

    def test_self_supporting_loop_rejected(self):
        t = TMS()
        t.add_fact("a")
        t.add_rule("r0", "x", ["a"])
        with self.assertRaises(CyclicRuleError):
            t.add_rule("r1", "x", ["x"])  # 直接自环
        t.add_rule("r2", "y", ["x"])
        with self.assertRaises(CyclicRuleError):
            t.add_rule("r3", "x", ["y"])  # x <-> y 闭环
        self.assertNotIn("r1", t.rules)
        self.assertNotIn("r3", t.rules)
        # 既有结论依然有效, 未被污染
        self.assertEqual(t.nodes["x"].status, "active")

    def test_loop_without_facts_cannot_make_conclusions_valid(self):
        t = TMS()
        with self.assertRaises(DanglingRuleError):
            t.add_rule("r1", "x", ["y"])  # 无事实落地 + 互相悬空
        self.assertEqual(t.list_rules(), [])

    def test_duplicate_and_empty_ids_rejected(self):
        t = TMS()
        t.add_fact("a")
        with self.assertRaises(DuplicateIdError):
            t.add_fact("a")
        t.add_rule("r1", "x", ["a"])
        with self.assertRaises(DuplicateIdError):
            t.add_rule("r1", "y", ["a"])
        with self.assertRaises(TMSError):
            t.add_fact("   ")

    def test_no_premise_rule_rejected(self):
        t = TMS()
        with self.assertRaises(TMSError):
            t.add_rule("r1", "x", [])

    def test_diamond_dependency_has_no_cycle(self):
        # 菱形共享不是环, 应被接受
        t = TMS()
        t.add_fact("a")
        t.add_rule("r1", "m", ["a"])
        t.add_rule("r2", "n", ["a"])
        t.add_rule("r3", "z", ["m", "n"])
        self.assertEqual(t.nodes["z"].status, "active")


if __name__ == "__main__":
    unittest.main(verbosity=2)
