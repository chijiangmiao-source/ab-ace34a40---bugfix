"""持久化层测试: 同一事务传播、重启后保留结论与依据状态。"""

import os
import tempfile
import unittest

from app.store import Store
from app.tms import UnknownFactError


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self, store: Store):
        for f in ("f1", "f2", "f3"):
            store.add_fact(f)
        store.add_rule("r1", "c", ["f1"])
        store.add_rule("r2", "c", ["f2", "f3"])
        store.add_rule("r3", "d", ["c"])

    def test_retraction_and_propagation_committed_together(self):
        store = Store(self.db)
        self._build(store)
        rec = store.retract_fact("f1")
        self.assertEqual(rec.survived, ["c"])

        # 重新打开 (模拟重启): 结论与依据状态保留
        store.close()
        store2 = Store(self.db)
        self.assertTrue(store2.tms.fact("f1").status == "inactive")
        self.assertEqual(store2.tms.nodes["c"].status, "active")
        self.assertEqual([s.rule_id for s in store2.tms.explain("c")], ["r2"])
        # 历史耗尽依据也保留
        self.assertEqual([s.rule_id for s in store2.tms.nodes["c"].retired_supports],
                         ["r1"])
        store2.close()

        # 撤回最后一条依据后再次重启: c 与仅依赖它的 d 均失效
        store3 = Store(self.db)
        rec2 = store3.retract_fact("f2")
        self.assertEqual([a.node_id for a in rec2.affected], ["f2", "c", "d"])
        store3.close()

        store4 = Store(self.db)
        self.assertEqual(store4.tms.nodes["c"].status, "inactive")
        self.assertEqual(store4.tms.nodes["d"].status, "inactive")
        self.assertEqual(store4.tms.explain("c"), [])
        # 重复撤回稳定返回既有裁决 (f1 首次撤回时 c 尚靠替代依据存活)
        again = store4.retract_fact("f1")
        self.assertTrue(again.already_retracted)
        self.assertEqual([a.node_id for a in again.affected], ["f1"])
        store4.close()

    def test_invalid_rule_does_not_persist_or_pollute(self):
        store = Store(self.db)
        store.add_fact("a")
        with self.assertRaises(Exception):
            store.add_rule("r1", "q", ["a", "ghost"])
        store.close()

        store2 = Store(self.db)
        self.assertEqual(store2.tms.list_rules(), [])
        with self.assertRaises(UnknownFactError):
            store2.retract_fact("ghost")
        store2.close()

    def test_health_ping(self):
        store = Store(self.db)
        self.assertTrue(store.ping())
        store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
