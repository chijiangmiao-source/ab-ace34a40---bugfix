"""SQLite 持久化层。

关键约束: 撤回事实与反向索引传播在 *同一个持久化事务* 中完成 —— 引擎在
事务函数内先完成内存传播, 再把事实状态、当前完整支持、耗尽的历史依据、
撤回裁决一次性写入, 随后统一 COMMIT; 任何异常都回滚, 不会出现 "事实已撤回
但结论仍旧有效" 的半截状态.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Dict, List, Optional

from . import tms as tms_mod
from .tms import (
    AffectedConclusion,
    PropagationStep,
    RetractionRecord,
    Rule,
    Support,
    TMS,
)

SCHEMA_VERSION = "1"


class Store:
    """持有一个 TMS 实例并把每次变更原子落盘。"""

    def __init__(self, path: str) -> None:
        self.path = path
        # check_same_thread=False + 互斥锁: 所有写操作串行化
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()
        self.tms = self._load()

    # ------------------------------------------------------------------ #
    # schema
    # ------------------------------------------------------------------ #
    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS facts (
                    id     TEXT PRIMARY KEY,
                    active INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rules (
                    id              TEXT PRIMARY KEY,
                    conclusion      TEXT NOT NULL,
                    antecedents     TEXT NOT NULL
                );
                -- 每次规则触发保存的完整前提集合; active=1 当前完整支持,
                -- active=0 已耗尽的历史依据 (用于传播链与复算展示)
                CREATE TABLE IF NOT EXISTS supports (
                    node_id          TEXT NOT NULL,
                    rule_id          TEXT NOT NULL,
                    antecedents_json TEXT NOT NULL,
                    basis_json       TEXT NOT NULL,
                    active           INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS retractions (
                    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
                    fact_id TEXT NOT NULL
                );
                -- 每次撤回的完整裁决, 重复撤回时稳定返回既有裁决
                CREATE TABLE IF NOT EXISTS verdicts (
                    fact_id      TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL
                );
                """
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )

    # ------------------------------------------------------------------ #
    # 装载 (重启后恢复结论与依据状态)
    # ------------------------------------------------------------------ #
    def _load(self) -> TMS:
        t = TMS()
        rows = self._conn.execute("SELECT id, active FROM facts ORDER BY id").fetchall()
        for row in rows:
            t.add_fact(row["id"])
        rules = self._conn.execute(
            "SELECT id, conclusion, antecedents FROM rules ORDER BY id"
        ).fetchall()
        for row in rules:  # 加入时会重算当前支持
            ants: List[str] = json.loads(row["antecedents"])
            t.add_rule(row["id"], row["conclusion"], ants)
        # 恢复事实撤回状态后重算: 得到与事实/规则一致的当前状态
        for row in rows:
            if not row["active"]:
                t.nodes[row["id"]].fact_active = False
        t.refresh()

        # supports 表是单一事务内提交的权威依据状态. 重放重算只用于确定
        # 节点有效性; 具体的当前/历史依据 (含历史触发时刻的事实层展开)
        # 一律以落盘行准, 避免重放凭空生成 "失效过又切换走" 的幻影历史依据.
        current_rows = self._conn.execute(
            "SELECT node_id, rule_id, antecedents_json, basis_json "
            "FROM supports WHERE active = 1 ORDER BY rowid"
        ).fetchall()
        retired_rows = self._conn.execute(
            "SELECT node_id, rule_id, antecedents_json, basis_json "
            "FROM supports WHERE active = 0 ORDER BY rowid"
        ).fetchall()

        current_by_node: Dict[str, List[Support]] = {}
        retired_by_node: Dict[str, List[Support]] = {}
        for bucket, rows_ in ((current_by_node, current_rows),
                              (retired_by_node, retired_rows)):
            for row in rows_:
                bucket.setdefault(row["node_id"], []).append(Support(
                    rule_id=row["rule_id"],
                    antecedents=tuple(json.loads(row["antecedents_json"])),
                    basis=tuple(json.loads(row["basis_json"])),
                ))

        for node, st in t.nodes.items():
            if st.is_fact:
                continue
            live = current_by_node.get(node, [])
            retired = retired_by_node.get(node, [])
            live_keys = {(s.rule_id, s.antecedents) for s in live}
            # 历史依据去重, 且与当前支持同键/同值的历史行不再展示为 "已耗尽"
            seen: List[Support] = []
            cleaned: List[Support] = []
            for sup in retired:
                if (sup.rule_id, sup.antecedents) in live_keys:
                    continue
                if sup in live or sup in seen:
                    continue
                seen.append(sup)
                cleaned.append(sup)
            if st.status == "active" and live:
                # 落盘的当前依据为准 (与重算结果一致, 且保留精确事实层展开)
                st.supports = live
            else:
                st.supports = []
                if st.status == "active":
                    st.status = "inactive"
                    st.reason = "所有完整支持均已耗尽"
            st.retired_supports = cleaned

        # 恢复撤回顺序与历史裁决 (幂等重复撤回用)
        order = [r["fact_id"] for r in self._conn.execute(
            "SELECT fact_id FROM retractions ORDER BY seq").fetchall()]
        t.retraction_order = order
        for row in self._conn.execute("SELECT fact_id, payload_json FROM verdicts"):
            t._last_records[row["fact_id"]] = self._record_from_dict(
                json.loads(row["payload_json"]))
        return t

    # ------------------------------------------------------------------ #
    # 变更操作 (每个方法 = 一个持久化事务)
    # ------------------------------------------------------------------ #
    def add_fact(self, fact_id: str) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self.tms.add_fact(fact_id)   # 非法标识会在此抛错, 不落盘
                self._conn.execute(
                    "INSERT INTO facts(id, active) VALUES(?, 1)", (fact_id,))
                self._conn.commit()
            except Exception:
                self._rollback_and_reload()
                raise

    def add_rule(self, rule_id: str, conclusion: str, antecedents: List[str]) -> Rule:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # 引擎先做全部校验; 非法规则抛异常 => 事务回滚, 不污染规程
                rule = self.tms.add_rule(rule_id, conclusion, antecedents)
                self._conn.execute(
                    "INSERT INTO rules(id, conclusion, antecedents) VALUES(?, ?, ?)",
                    (rule_id, conclusion, json.dumps(list(rule.antecedents))),
                )
                self._rewrite_supports()
                self._conn.commit()
                return rule
            except Exception:
                self._rollback_and_reload()
                raise

    def retract_fact(self, fact_id: str) -> RetractionRecord:
        with self._lock:
            # 确保无遗留隐式事务, 再 BEGIN IMMEDIATE:
            # 撤回 + 反向索引传播 + 依据落盘同一事务
            self._conn.commit()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                record = self.tms.retract(fact_id)   # 内存中完成完整传播
                self._conn.execute(
                    "UPDATE facts SET active = 0 WHERE id = ?", (fact_id,))
                if not record.already_retracted:
                    self._conn.execute(
                        "INSERT INTO retractions(fact_id) VALUES(?)", (fact_id,))
                self._rewrite_supports()
                self._conn.execute(
                    "INSERT OR REPLACE INTO verdicts(fact_id, payload_json) "
                    "VALUES(?, ?)", (fact_id, json.dumps(self._record_to_dict(record))))
                self._conn.commit()
            except Exception:
                self._rollback_and_reload()
                raise
            return record

    def _rollback_and_reload(self) -> None:
        """数据库事务回滚后, 从磁盘状态重建内存, 保证二者一致。"""
        self._conn.rollback()
        self.tms = self._load()

    def ping(self) -> bool:
        """健康检查: 持久层可读写。"""
        with self._lock:
            try:
                self._conn.execute("SELECT 1").fetchone()
                return True
            except sqlite3.Error:
                return False

    def latest_verdict(self) -> Optional[dict]:
        """返回最近一次撤回的裁决 (重启后用于恢复页面展示), 无则 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT v.payload_json FROM verdicts v "
                "JOIN retractions r ON r.fact_id = v.fact_id "
                "ORDER BY r.seq DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def snapshot(self) -> dict:
        """线程安全地导出引擎完整状态。"""
        with self._lock:
            return self.tms.snapshot()

    def node(self, node_id: str):
        with self._lock:
            return self.tms.nodes.get(node_id)

    def support_dict(self, support: Support) -> dict:
        return TMS._support_dict(support)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    # 内部: 用引擎当前状态整体重写 supports 表 (在事务内调用)
    # ------------------------------------------------------------------ #
    def _rewrite_supports(self) -> None:
        self._conn.execute("DELETE FROM supports")
        rows = []
        for node_id, st in self.tms.nodes.items():
            if st.is_fact:
                continue
            for s in st.supports:
                rows.append((node_id, s.rule_id,
                             json.dumps(list(s.antecedents)),
                             json.dumps(list(s.basis)), 1))
            for s in st.retired_supports:
                rows.append((node_id, s.rule_id,
                             json.dumps(list(s.antecedents)),
                             json.dumps(list(s.basis)), 0))
        self._conn.executemany(
            "INSERT INTO supports(node_id, rule_id, antecedents_json, basis_json, active)"
            " VALUES(?, ?, ?, ?, ?)", rows)

    # ------------------------------------------------------------------ #
    # 裁决序列化
    # ------------------------------------------------------------------ #
    @staticmethod
    def _record_to_dict(rec: RetractionRecord) -> dict:
        return {
            "fact_id": rec.fact_id,
            "already_retracted": rec.already_retracted,
            "affected": [
                {"node_id": a.node_id, "rule_id": a.rule_id,
                 "complete_basis": list(a.complete_basis)}
                for a in rec.affected
            ],
            "propagation_chain": [
                {"node_id": c.node_id, "rule_id": c.rule_id,
                 "exhausted_antecedents": list(c.exhausted_antecedents),
                 "exhausted_basis": list(c.exhausted_basis),
                 "triggered_by": c.triggered_by}
                for c in rec.propagation_chain
            ],
            "survived": list(rec.survived),
        }

    @staticmethod
    def _record_from_dict(d: dict) -> RetractionRecord:
        return RetractionRecord(
            fact_id=d["fact_id"],
            already_retracted=d.get("already_retracted", False),
            affected=[AffectedConclusion(
                node_id=a["node_id"], rule_id=a.get("rule_id"),
                complete_basis=tuple(a["complete_basis"]))
                for a in d.get("affected", [])],
            propagation_chain=[PropagationStep(
                node_id=c["node_id"], rule_id=c["rule_id"],
                exhausted_antecedents=tuple(c["exhausted_antecedents"]),
                exhausted_basis=tuple(c["exhausted_basis"]),
                triggered_by=c["triggered_by"])
                for c in d.get("propagation_chain", [])],
            survived=list(d.get("survived", [])),
        )
