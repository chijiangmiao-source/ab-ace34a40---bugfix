"""规则引擎 / 正当性维持内核 (Truth Maintenance).

概念
----
规程(procedure)由两类元素组成:

* 事实 (fact): 带唯一标识的基础前提, 可经接口撤回 (retract);
* 规则 (rule): 无变量正向规则 ``conclusion :- a, b, ...``, 前提只能引用
  事实或其它规则结论.

结论有效的条件: 存在至少一条 "完整支持" (complete support), 即该结论的某条
规则在某次触发时保存下来的完整前提集合, 且集合中每一项当前都有效.
撤回一个事实时, 依据反向索引 (结论 -> 支持该结论的触发记录) 在同一持久化
事务内自底向上传播: 某结论的所有完整支持均失效时, 该结论才失效, 进而传播
到仅依赖它的下游结论.

语义采用分层 (stratified) Datalog 的最小不动点: 规则依赖图中只要存在环
(直接或间接自我支持), 该规则即被拒绝 —— 循环规则不得凭空生成有效结论.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple


# --------------------------------------------------------------------------- #
# 错误类型
# --------------------------------------------------------------------------- #
class TMSError(ValueError):
    """所有规则引擎校验错误的基类。"""


class DuplicateIdError(TMSError):
    """事实/规则标识与既有元素冲突。"""


class UnknownFactError(TMSError):
    """撤回或引用了系统中不存在的事实标识。"""


class DanglingRuleError(TMSError):
    """规则引用了既不是事实也不是任何规则结论的节点。"""


class CyclicRuleError(TMSError):
    """规则依赖图存在环 (含自我支持闭环)。"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Rule:
    """一条无变量正向规则: conclusion :- antecedent, ..."""

    rule_id: str
    conclusion: str
    antecedents: Tuple[str, ...]


@dataclass
class Support:
    """一次规则触发保存下来的完整前提集合 (完整依据)。

    ``antecedents`` 是触发时刻的完整前提节点集合; 支持成立当且仅当其中
    每个节点当前有效. ``basis`` 是展开到事实层的完整可复算依据
    (只含事实标识), 用于页面展示与复算.
    """

    rule_id: str
    antecedents: Tuple[str, ...]
    basis: Tuple[str, ...]


@dataclass
class NodeState:
    """一个节点 (事实或结论) 的运行时状态。"""

    is_fact: bool = False
    fact_active: bool = True          # 仅事实使用: 是否尚未被撤回
    supporting_rules: List[str] = field(default_factory=list)  # 推出本节点的规则
    supports: List[Support] = field(default_factory=list)       # 每次触发保存的完整前提
    status: str = "unknown"           # active / inactive / unknown(事实外节点初始态)
    reason: str = ""                  # inactive 时的人类可读原因
    retired_supports: List[Support] = field(default_factory=list)  # 已失效的历史依据


@dataclass
class RetractionRecord:
    """一次撤回的裁决结果。"""

    fact_id: str
    already_retracted: bool
    # 本次 (或历史裁决中) 判定失效的结论, 含传播顺序
    affected: List["AffectedConclusion"]
    # 支持耗尽形成的传播链: [(失效节点, 耗尽的支持[规则+前提+依据], 由谁传播), ...]
    propagation_chain: List["PropagationStep"]
    # 当时仍被其它完整支持保住的下游结论 (替代依据)
    survived: List[str]


@dataclass
class AffectedConclusion:
    node_id: str
    rule_id: Optional[str]            # None 表示事实本身
    complete_basis: Tuple[str, ...]   # 失效前最后一条完整支持的事实层依据


@dataclass
class PropagationStep:
    node_id: str
    rule_id: str
    exhausted_antecedents: Tuple[str, ...]
    exhausted_basis: Tuple[str, ...]
    triggered_by: str                 # 哪个上游节点失效触发了本步检查


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
class TMS:
    """良基正向规则引擎, 持有规程的内存状态 (由持久层加载/落盘)。"""

    def __init__(self) -> None:
        self.rules: Dict[str, Rule] = {}
        self.nodes: Dict[str, NodeState] = {}
        self.retraction_order: List[str] = []   # 事实撤回的先后顺序
        self._last_records: Dict[str, RetractionRecord] = {}
        self._retraction_basis: Dict[str, Tuple[str, ...]] = {}

    # ------------------------------------------------------------------ #
    # 规程编辑
    # ------------------------------------------------------------------ #
    def add_fact(self, fact_id: str) -> None:
        fact_id = self._check_id(fact_id, kind="事实")
        if fact_id in self.nodes:
            raise DuplicateIdError(f"标识 {fact_id!r} 已存在于规程中")
        st = NodeState(is_fact=True, fact_active=True, status="active")
        self.nodes[fact_id] = st

    def add_rule(self, rule_id: str, conclusion: str, antecedents: Iterable[str]) -> Rule:
        """加入一条规则并立即做全量校验; 非法规则不进入规程 (不污染)。"""
        rule_id = self._check_id(rule_id, kind="规则")
        conclusion = self._check_id(conclusion, kind="结论")
        ants: Tuple[str, ...] = tuple(dict.fromkeys(
            self._check_id(a, kind="前提") for a in antecedents
        ))
        if rule_id in self.rules:
            raise DuplicateIdError(f"规则 {rule_id!r} 已存在")
        if conclusion in self.nodes and self.nodes[conclusion].is_fact:
            # 结论名与事实同名会让语义含混, 拒绝
            raise TMSError(f"结论 {conclusion!r} 与既有事实同名")
        if not ants:
            raise TMSError(f"规则 {rule_id!r} 必须至少包含一个前提, 不接受无前提规则")

        candidate = Rule(rule_id, conclusion, ants)
        # 先以候选规则做依赖闭包与环校验, 任何失败都不改动现有状态
        self._validate_candidate(candidate)

        self.rules[rule_id] = candidate
        st = self.nodes.setdefault(conclusion, NodeState())
        if st.is_fact:  # 理论上前面已挡, 双保险
            raise TMSError(f"结论 {conclusion!r} 与既有事实同名")
        st.supporting_rules.append(rule_id)
        for a in ants:
            self.nodes.setdefault(a, NodeState())
        self.refresh()
        return candidate

    def _validate_candidate(self, candidate: Rule) -> None:
        """对 "现有规则 + 候选规则" 做良基校验。"""
        rules = dict(self.rules)
        rules[candidate.rule_id] = candidate

        # 1) 依赖闭包: 规则前提最终必须能落到事实上 (或被某规则推出,
        #    递归地落到事实上). 引用不存在结论 => DanglingRuleError.
        rules_by_conclusion: Dict[str, List[Rule]] = {}
        for r in rules.values():
            rules_by_conclusion.setdefault(r.conclusion, []).append(r)

        facts = {n for n, st in self.nodes.items() if st.is_fact}
        # 候选自身的结论此刻可能尚未建节点, 补入已知结论集合
        known_conclusions = set(rules_by_conclusion)

        def grounded(node: str, stack: Set[str]) -> None:
            if node in facts:
                return
            if node in stack:
                # 闭包计算中遇到环 —— 与下面的环检查一致, 拒绝
                raise CyclicRuleError(
                    f"规则 {candidate.rule_id!r} 引入循环依赖: "
                    f"{' -> '.join(list(stack) + [node])}"
                )
            if node not in known_conclusions:
                raise DanglingRuleError(
                    f"规则 {candidate.rule_id!r} 引用了不存在的节点 {node!r}: "
                    "该标识既非事实也非任何规则的结论"
                )
            stack.add(node)
            for r in rules_by_conclusion[node]:
                for a in r.antecedents:
                    grounded(a, stack)
            stack.discard(node)

        for r in rules_by_conclusion[candidate.conclusion]:
            for a in r.antecedents:
                grounded(a, set())

        # 2) 显式 DFS 环检测 (含自环 a :- a), 覆盖整个规则依赖图
        color: Dict[str, int] = {}  # 0=未访问 1=在栈中 2=完成

        def dfs(node: str, path: List[str]) -> None:
            c = color.get(node, 0)
            if c == 1:
                cycle = path[path.index(node):] + [node]
                raise CyclicRuleError(
                    f"规则依赖存在循环, 形成自我支持闭环: {' -> '.join(cycle)}"
                )
            if c == 2:
                return
            color[node] = 1
            path.append(node)
            for r in rules_by_conclusion.get(node, []):
                for a in r.antecedents:
                    dfs(a, path)
            path.pop()
            color[node] = 2

        for n in list(rules_by_conclusion):
            dfs(n, [])

    # ------------------------------------------------------------------ #
    # 前向求值 (分层最小不动点) + 每次触发保存完整前提集合
    # ------------------------------------------------------------------ #
    def topological_layers(self) -> List[Set[str]]:
        """按规则依赖给出结论节点的拓扑层级 (校验通过后必无环)。"""
        deps: Dict[str, Set[str]] = {n: set() for n in self.nodes if not self.nodes[n].is_fact}
        for r in self.rules.values():
            deps.setdefault(r.conclusion, set())
            for a in r.antecedents:
                if not self.nodes.get(a, NodeState()).is_fact:
                    deps[r.conclusion].add(a)
        layers: List[Set[str]] = []
        resolved: Set[str] = set()
        remaining = set(deps)
        while remaining:
            layer = {n for n in remaining if deps[n] <= resolved}
            if not layer:  # 不应发生 (加入时已校验), 防御性报错
                raise CyclicRuleError(f"无法分层的依赖: {sorted(remaining)}")
            layers.append(layer)
            resolved |= layer
            remaining -= layer
        return layers

    def _active_facts(self) -> Set[str]:
        return {n for n, st in self.nodes.items() if st.is_fact and st.fact_active}

    def _recompute(self) -> Tuple[Set[str], Dict[str, List[Support]]]:
        """重算最小不动点, 返回 (有效节点集合, 每个结论的触发完整支持列表)。

        一个结论在一轮求值中, 对每条前提全部成立的规则产生一个 Support;
        Support.basis 展开为支撑该触发的事实集合 (完整可复算依据).
        """
        active = set(self._active_facts())
        supports_by_node: Dict[str, List[Support]] = {}

        for layer in self.topological_layers():
            for node in layer:
                found: List[Support] = []
                for rule_id in self.nodes[node].supporting_rules:
                    r = self.rules[rule_id]
                    if all(a in active for a in r.antecedents):
                        basis = self._expand_basis(r.antecedents, active, supports_by_node)
                        found.append(Support(
                            rule_id=rule_id,
                            antecedents=tuple(r.antecedents),
                            basis=tuple(sorted(basis)),
                        ))
                if found:
                    active.add(node)
                    supports_by_node[node] = found
        return active, supports_by_node

    def _expand_basis(
        self,
        antecedents: Tuple[str, ...],
        active: Set[str],
        supports_by_node: Dict[str, List[Support]],
    ) -> Set[str]:
        """把触发前提展开为事实层依据: 事实保留自身, 结论替换为其(任一)完整依据。"""
        basis: Set[str] = set()
        for a in antecedents:
            st = self.nodes[a]
            if st.is_fact:
                basis.add(a)
            else:
                # a 有效则必有完整支持; 取第一条用于展示 (所有支持均为完整依据)
                basis.update(supports_by_node[a][0].basis)
        return basis

    def refresh(self) -> None:
        """依据当前事实有效性重算所有节点状态, 保留失效历史。"""
        active, supports_by_node = self._recompute()
        for n, st in self.nodes.items():
            if st.is_fact:
                st.status = "active" if st.fact_active else "inactive"
                st.reason = "" if st.fact_active else "事实已被撤回"
                continue
            new_supports = supports_by_node.get(n, [])
            # 记录本次重算中不再成立的历史支持 (支持耗尽痕迹)
            live_keys = {(s.rule_id, s.antecedents) for s in new_supports}
            for old in st.supports:
                if (old.rule_id, old.antecedents) not in live_keys:
                    if old not in st.retired_supports:
                        st.retired_supports.append(old)
            st.supports = new_supports
            if n in active and new_supports:
                st.status = "active"
                st.reason = ""
            else:
                st.status = "inactive"
                if not self._has_any_potential_rule(n):
                    st.reason = "无任何支持规则"
                else:
                    st.reason = "所有完整支持均已耗尽"

    def _has_any_potential_rule(self, node: str) -> bool:
        return bool(self.nodes[node].supporting_rules)

    # ------------------------------------------------------------------ #
    # 撤回 + 反向索引传播 (同一事务由 store 层保证)
    # ------------------------------------------------------------------ #
    def retract(self, fact_id: str) -> RetractionRecord:
        """撤回一个当前有效 (或已失效) 的事实, 返回完整裁决。

        * 未知事实 => UnknownFactError;
        * 重复撤回已失效事实 => 稳定返回既有裁决 (幂等);
        * 仅在结论没有任何完整支持时才令其失效, 并沿反向索引继续传播.
        """
        if fact_id not in self.nodes or not self.nodes[fact_id].is_fact:
            raise UnknownFactError(f"未知事实 {fact_id!r}: 规程中不存在该事实标识")

        st = self.nodes[fact_id]
        if not st.fact_active:
            return self._stable_past_verdict(fact_id)

        self._retraction_basis = self._capture_retraction_basis()

        # 撤回前快照: 哪些结论有效、依据是什么 (supports 与 status 始终同步)
        before_active: Set[str] = set()
        before_supports: Dict[str, List[Support]] = {}
        for n, xst in self.nodes.items():
            if xst.is_fact:
                if xst.fact_active:
                    before_active.add(n)
            elif xst.status == "active" and xst.supports:
                before_active.add(n)
                before_supports[n] = list(xst.supports)

        st.fact_active = False
        st.status = "inactive"
        st.reason = "事实已被撤回"
        self.retraction_order.append(fact_id)

        affected: List[AffectedConclusion] = [
            AffectedConclusion(node_id=fact_id, rule_id=None, complete_basis=(fact_id,))
        ]
        chain: List[PropagationStep] = []
        survived: List[str] = []
        exhausted_by_node: Dict[str, List[Support]] = {}

        # 反向索引传播: antecedent -> [(rule_id, conclusion)];
        # 失效节点入队, 仅重算真正依赖它的规则, 这是标准的反向索引传播.
        reverse_index: Dict[str, List[Tuple[str, str]]] = {}
        for _r in self.rules.values():
            for _a in _r.antecedents:
                reverse_index.setdefault(_a, []).append((_r.rule_id, _r.conclusion))

        # 队列元素为发生变化的节点: 失效 (向下传播失效) 或依据集变化但存活
        # (向下刷新展开依据, 例如上游从路径一切换到路径二).
        queue: List[str] = [fact_id]
        deactivated: Set[str] = {fact_id}
        refreshed: Set[str] = set()
        while queue:
            changed = queue.pop(0)
            for rule_id, node in reverse_index.get(changed, []):
                nst = self.nodes[node]
                r = self.rules[rule_id]
                key = (rule_id, tuple(r.antecedents))
                existing = next((s for s in nst.supports
                                 if (s.rule_id, s.antecedents) == key), None)
                sup = self._current_support(r)
                node_lost_support = False
                node_basis_shifted = False
                if existing is not None and sup is None:
                    # 该规则的完整支持此刻耗尽 (撤回单调移除, 支持不可能新增)
                    nst.supports.remove(existing)
                    if existing not in nst.retired_supports:
                        nst.retired_supports.append(existing)
                    exhausted_by_node.setdefault(node, []).append(existing)
                    node_lost_support = True
                elif existing is not None and sup is not None and existing.basis != sup.basis:
                    # 规则仍成立, 但其事实层展开依据随上游切换而变化
                    idx = nst.supports.index(existing)
                    nst.supports[idx] = sup
                    node_basis_shifted = True

                if node not in before_active or node in deactivated:
                    continue

                if not nst.supports:
                    # 仅在没有任何完整支持时结论才失效, 然后继续向下游传播
                    nst.status = "inactive"
                    nst.reason = "所有完整支持均已耗尽"
                    picked = exhausted_by_node.get(node,
                                                   before_supports.get(node, [Support("?", (), ())]))[-1]
                    affected.append(AffectedConclusion(
                        node_id=node,
                        rule_id=picked.rule_id,
                        complete_basis=picked.basis,
                    ))
                    chain.append(PropagationStep(
                        node_id=node,
                        rule_id=picked.rule_id,
                        exhausted_antecedents=picked.antecedents,
                        exhausted_basis=picked.basis,
                        triggered_by=changed,
                    ))
                    deactivated.add(node)
                    refreshed.discard(node)
                    if node in survived:
                        survived.remove(node)
                    queue.append(node)
                elif node_lost_support:
                    # 丢失了部分支持但仍有完整支持 (替代路径) —— 结论保持有效
                    nst.status = "active"
                    if node not in survived:
                        survived.append(node)
                    if node not in refreshed:
                        refreshed.add(node)
                        queue.append(node)
                elif node_basis_shifted and node not in refreshed:
                    # 仅依据展开发生变化: 继续向下游刷新, 但不算替代依据幸存
                    refreshed.add(node)
                    queue.append(node)

        record = RetractionRecord(
            fact_id=fact_id,
            already_retracted=False,
            affected=affected,
            propagation_chain=chain,
            survived=survived,
        )
        self._last_records[fact_id] = record
        self._retraction_basis = {}
        return record

    def _capture_retraction_basis(self) -> Dict[str, Tuple[str, ...]]:
        basis_by_rule: Dict[str, Tuple[str, ...]] = {}
        for st in self.nodes.values():
            if st.is_fact or st.status != "active":
                continue
            for support in st.supports:
                basis_by_rule.setdefault(support.rule_id, support.basis)
        return basis_by_rule

    def _current_support(self, r: Rule) -> Optional[Support]:
        """若规则前提此刻全部有效, 构造完整支持 (含事实层展开依据), 否则 None。"""
        if not all(self._is_active_now(a) for a in r.antecedents):
            return None
        retained_basis = self._retraction_basis.get(r.rule_id)
        if retained_basis is not None:
            return Support(rule_id=r.rule_id,
                           antecedents=tuple(r.antecedents),
                           basis=retained_basis)
        basis: Set[str] = set()
        for a in r.antecedents:
            ast = self.nodes[a]
            if ast.is_fact:
                basis.add(a)
            else:
                if not ast.supports:
                    return None
                basis.update(ast.supports[0].basis)
        return Support(rule_id=r.rule_id,
                       antecedents=tuple(r.antecedents),
                       basis=tuple(sorted(basis)))

    def _is_active_now(self, node: str) -> bool:
        st = self.nodes.get(node)
        if st is None:
            return False
        if st.is_fact:
            return st.fact_active
        return st.status == "active" and bool(st.supports)

    def _stable_past_verdict(self, fact_id: str) -> RetractionRecord:
        past = self._last_records.get(fact_id)
        affected: List[AffectedConclusion] = []
        chain: List[PropagationStep] = []
        if past is not None:
            affected = [AffectedConclusion(a.node_id, a.rule_id, a.complete_basis)
                        for a in past.affected]
            chain = [PropagationStep(c.node_id, c.rule_id, c.exhausted_antecedents,
                                     c.exhausted_basis, c.triggered_by)
                     for c in past.propagation_chain]
        else:
            # 持久层加载来的历史撤回: 依据当前状态重建裁决
            for n, st in self.nodes.items():
                if n == fact_id or (not st.is_fact and st.status == "inactive"):
                    basis = st.retired_supports[-1].basis if st.retired_supports else ()
                    affected.append(AffectedConclusion(
                        node_id=n,
                        rule_id=st.retired_supports[-1].rule_id if st.retired_supports else None,
                        complete_basis=basis,
                    ))
        return RetractionRecord(
            fact_id=fact_id,
            already_retracted=True,
            affected=affected,
            propagation_chain=chain,
            survived=[],
        )

    # ------------------------------------------------------------------ #
    # 查询 / 导出
    # ------------------------------------------------------------------ #
    def fact(self, fact_id: str) -> NodeState:
        if fact_id not in self.nodes or not self.nodes[fact_id].is_fact:
            raise UnknownFactError(f"未知事实 {fact_id!r}")
        return self.nodes[fact_id]

    def list_facts(self) -> List[str]:
        return sorted(n for n, st in self.nodes.items() if st.is_fact)

    def list_rules(self) -> List[Rule]:
        return [self.rules[k] for k in sorted(self.rules)]

    def explain(self, node: str) -> List[Support]:
        """返回节点当前所有完整支持 (每项含完整可复算事实依据)。"""
        return list(self.nodes[node].supports) if node in self.nodes else []

    def snapshot(self) -> dict:
        """导出完整状态 (持久层/页面共用)。"""
        facts, conclusions = [], []
        for n in sorted(self.nodes):
            st = self.nodes[n]
            if st.is_fact:
                facts.append({"id": n, "active": st.fact_active})
            else:
                conclusions.append({
                    "id": n,
                    "status": st.status,
                    "reason": st.reason,
                    "supporting_rules": list(st.supporting_rules),
                    "supports": [self._support_dict(s) for s in st.supports],
                    "retired_supports": [self._support_dict(s) for s in st.retired_supports],
                })
        return {
            "facts": facts,
            "rules": [
                {"id": r.rule_id, "conclusion": r.conclusion,
                 "antecedents": list(r.antecedents)}
                for r in self.list_rules()
            ],
            "conclusions": conclusions,
            "retraction_order": list(self.retraction_order),
        }

    @staticmethod
    def _support_dict(s: Support) -> dict:
        return {"rule_id": s.rule_id, "antecedents": list(s.antecedents),
                "basis": list(s.basis)}

    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_id(value: str, *, kind: str) -> str:
        if not isinstance(value, str):
            raise TMSError(f"{kind}标识必须是字符串")
        v = value.strip()
        if not v:
            raise TMSError(f"{kind}标识不能为空")
        if any(ch.isspace() for ch in v):
            raise TMSError(f"{kind}标识 {value!r} 不得包含空白字符")
        if v in {":-", "->"}:
            raise TMSError(f"{kind}标识 {value!r} 非法")
        return v
