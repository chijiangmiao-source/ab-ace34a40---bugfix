#!/usr/bin/env python3
"""API/HTTP 冒烟验证。

默认自起一个真实 HTTP 服务 (子进程, 临时 SQLite), 经真实网络接口验证:

1. 健康检查反映接口可用性;
2. 编辑规程 (唯一事实标识 + 无变量正向规则), 非法规则 (悬空引用/闭环) 被拒绝;
3. 撤回一条原始事实后, 结论凭剩余完整依据保持有效, 下游结论的当前依据
   同步换成替代路径;
4. 在两次撤回之间重启服务: 查询到的有效结论仍展示刷新后的当前依据;
5. 撤回最后一条支持事实后, 该结论及仅依赖它的下游结论失效, 返回的传播链
   每一步都带失效时刻实际有效的完整依据;
6. 重复撤回幂等返回既有裁决;
7. 重启服务后结论与依据状态仍保留。

设置 BASE_URL 时只对既有服务做在线冒烟 (不做重启项)。
退出码: 0 全部通过, 1 有断言失败。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Failure(AssertionError):
    pass


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)
    print(f"  ✓ {msg}")


def request(base: str, method: str, path: str, payload: Optional[dict] = None,
            expect_error: Optional[str] = None) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if expect_error:
                raise Failure(f"{path} 应被拒绝 ({expect_error}), 但成功了")
            return body
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode("utf-8"))
        if expect_error:
            check(body.get("error") == expect_error,
                  f"{path} 返回预期错误码 {expect_error} (实际 {body.get('error')})")
            return body
        raise Failure(f"{path} 意外失败 HTTP {e.code}: {body}") from None


def wait_ready(base: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as resp:
                if json.loads(resp.read())["status"] == "ok":
                    return
        except Exception as e:  # noqa: BLE001
            last = str(e)
            time.sleep(0.3)
    raise Failure(f"服务在 {timeout}s 内未就绪: {last}")


def conclusions_map(state: dict) -> Dict[str, dict]:
    return {c["id"]: c for c in state["conclusions"]}


# --------------------------------------------------------------------------- #
# 场景步骤 (在线模式与自起服务模式共用)
# --------------------------------------------------------------------------- #
def step_health(base: str) -> None:
    print("[smoke] 1) 健康检查")
    h = request(base, "GET", "/api/health")
    check(h["status"] == "ok" and h["database"] == "ok", "健康检查报告接口与持久层可用")


def step_build_procedure(base: str) -> None:
    print("[smoke] 2) 编辑规程: 两条独立支持路径 + 一个仅依赖结论的下游")
    for f in ("f1", "f2", "f3"):
        request(base, "POST", "/api/facts", {"id": f})
    request(base, "POST", "/api/rules",
            {"id": "r1", "conclusion": "c", "antecedents": ["f1"]})
    request(base, "POST", "/api/rules",
            {"id": "r2", "conclusion": "c", "antecedents": ["f2", "f3"]})
    request(base, "POST", "/api/rules",
            {"id": "r3", "conclusion": "d", "antecedents": ["c"]})

    state = request(base, "GET", "/api/state")
    cs = conclusions_map(state)
    check(cs["c"]["status"] == "active", "初始: 结论 c 有效")
    check({s["rule_id"] for s in cs["c"]["supports"]} == {"r1", "r2"},
          "c 的每次规则触发都保存了完整前提集合 (两条支持)")
    check(cs["d"]["status"] == "active", "初始: 下游 d 有效")

    print("[smoke] 2b) 非法规则必须被拒绝且不污染规程")
    request(base, "POST", "/api/rules",
            {"id": "bad1", "conclusion": "q", "antecedents": ["f1", "ghost"]},
            expect_error="dangling_reference")
    request(base, "POST", "/api/rules",
            {"id": "bad2", "conclusion": "f1", "antecedents": ["f1"]},
            expect_error="invalid_procedure")  # 结论与事实同名
    request(base, "POST", "/api/rules",
            {"id": "r2", "conclusion": "x", "antecedents": ["f1"]},
            expect_error="duplicate_id")
    state = request(base, "GET", "/api/state")
    check(all(c["id"] != "q" for c in state["conclusions"]),
          "被拒绝的悬空规则未污染规程")
    request(base, "POST", "/api/retract", {"fact_id": "ghost"},
            expect_error="unknown_fact")
    # 闭环: e 已由事实 f3 直接/间接定义后再制造环
    request(base, "POST", "/api/rules",
            {"id": "r4", "conclusion": "e", "antecedents": ["f3"]})
    request(base, "POST", "/api/rules",
            {"id": "r5", "conclusion": "g", "antecedents": ["e"]})
    request(base, "POST", "/api/rules",
            {"id": "r6", "conclusion": "e", "antecedents": ["g"]},
            expect_error="cyclic_rule")


def step_first_retraction(base: str) -> None:
    print("[smoke] 3) 撤回一条原始事实 -> 结论凭替代依据保留, 下游依据同步刷新")
    out = request(base, "POST", "/api/retract", {"fact_id": "f1"})
    v = out["verdict"]
    cs = conclusions_map(out["state"])
    check(cs["c"]["status"] == "active", "撤回 f1 后 c 仍有效 (替代依据)")
    check(cs["d"]["status"] == "active", "撤回 f1 后下游 d 仍有效")
    check(v["survived"] == ["c"], "裁决列出靠替代依据保留的结论 c")
    remaining = cs["c"]["supports"]
    check(len(remaining) == 1 and remaining[0]["rule_id"] == "r2",
          "页面/接口列出剩余的完整支持 r2")
    check(remaining[0]["basis"] == ["f2", "f3"],
          "剩余依据完整可复算: {f2, f3}")
    d_supports = cs["d"]["supports"]
    check(len(d_supports) == 1 and d_supports[0]["basis"] == ["f2", "f3"],
          "下游 d 的当前完整依据同步刷新为 {f2, f3} (不再含已撤回的 f1)")


def check_state_after_mid_restart(base: str) -> None:
    """两次撤回之间重启后的查询结果: 有效结论不得展示过期依据。"""
    print("[smoke] 4) 两次撤回之间重启 -> 有效结论仍展示刷新后的当前依据")
    state = request(base, "GET", "/api/state")
    cs = conclusions_map(state)
    facts = {f["id"]: f["active"] for f in state["facts"]}
    check(facts["f1"] is False and facts["f2"] is True and facts["f3"] is True,
          "重启后事实撤回状态保留 (f1 已撤回, f2/f3 有效)")
    check(cs["c"]["status"] == "active" and cs["d"]["status"] == "active",
          "重启后 c/d 仍凭替代依据保持有效")
    check([s["basis"] for s in cs["c"]["supports"]] == [["f2", "f3"]],
          "重启后 c 的当前依据仍为 {f2, f3}")
    check([s["basis"] for s in cs["d"]["supports"]] == [["f2", "f3"]],
          "重启后 d 的当前依据仍为 {f2, f3} (无过期依据)")
    check([s["rule_id"] for s in cs["c"]["retired_supports"]] == ["r1"],
          "重启后 c 的已耗尽历史依据 (r1) 保留")
    check(cs["d"]["retired_supports"] == [],
          "重启后 d 没有把已撤回事实误记为历史依据")


def step_second_retraction(base: str) -> None:
    print("[smoke] 5) 撤回最后一条支持事实 -> 结论与唯一下游失效 + 完整传播链")
    out = request(base, "POST", "/api/retract", {"fact_id": "f2"})
    v = out["verdict"]
    cs = conclusions_map(out["state"])
    check(cs["c"]["status"] == "inactive", "撤回 f2 后 c 失效 (支持耗尽)")
    check(cs["d"]["status"] == "inactive", "仅依赖 c 的下游 d 同步失效")
    check([a["node_id"] for a in v["affected"]] == ["f2", "c", "d"],
          "裁决依次列出被撤回事实与受影响结论")
    affected_c = next(a for a in v["affected"] if a["node_id"] == "c")
    check(affected_c["complete_basis"] == ["f2", "f3"],
          "受影响结论 c 附带失效前的完整依据")
    affected_d = next(a for a in v["affected"] if a["node_id"] == "d")
    check(affected_d["complete_basis"] == ["f2", "f3"],
          "受影响结论 d 附带失效前实际有效的依据 {f2, f3}")
    chain = [(s["node_id"], s["triggered_by"], s["rule_id"])
             for s in v["propagation_chain"]]
    check(chain == [("c", "f2", "r2"), ("d", "c", "r3")],
          "展示支持耗尽形成的传播链 f2→c→d")
    check([s["exhausted_basis"] for s in v["propagation_chain"]]
          == [["f2", "f3"], ["f2", "f3"]],
          "传播链每一步的耗尽依据都是失效时刻实际有效的 {f2, f3}")
    check(not cs["c"]["supports"] and not cs["d"]["supports"],
          "失效结论不再有任何当前完整支持")
    check({s["rule_id"] for s in cs["c"]["retired_supports"]} == {"r1", "r2"},
          "两条历史依据均留痕可复算")
    check([s["basis"] for s in cs["d"]["retired_supports"]] == [["f2", "f3"]],
          "d 的历史依据与失效时刻实际有效的支持一致")


def step_repeat_retraction(base: str) -> None:
    print("[smoke] 6) 重复撤回已失效事实 -> 稳定返回既有裁决")
    again = request(base, "POST", "/api/retract", {"fact_id": "f2"})
    check(again["verdict"]["already_retracted"] is True, "标记为重复撤回 (幂等)")
    chain2 = [(s["node_id"], s["triggered_by"])
              for s in again["verdict"]["propagation_chain"]]
    check(chain2 == [("c", "f2"), ("d", "c")], "稳定返回既有传播链裁决")
    check([s["exhausted_basis"] for s in again["verdict"]["propagation_chain"]]
          == [["f2", "f3"], ["f2", "f3"]],
          "既有裁决中的耗尽依据保持稳定")


def step_page_and_detail(base: str) -> None:
    print("[smoke] 7) 页面与单项结论依据接口可经 HTTP 访问")
    with urllib.request.urlopen(base + "/", timeout=5) as resp:
        html = resp.read().decode("utf-8")
    check("安全规程" in html, "GET / 返回页面 HTML")
    detail = request(base, "GET", "/api/conclusions/c")
    check(detail["status"] == "inactive" and not detail["supports"],
          "GET /api/conclusions/c 返回该结论的完整依据状态")
    detail_d = request(base, "GET", "/api/conclusions/d")
    check([s["basis"] for s in detail_d["retired_supports"]] == [["f2", "f3"]],
          "GET /api/conclusions/d 的历史依据与实际失效支持一致")


def check_final_state_after_restart(base: str) -> None:
    print("[smoke] 8) 重启后查询仍保留结论与依据状态")
    state = request(base, "GET", "/api/state")
    cs = conclusions_map(state)
    check(cs["c"]["status"] == "inactive" and cs["d"]["status"] == "inactive",
          "重启后 c/d 仍为失效")
    check({s["rule_id"] for s in cs["c"]["retired_supports"]} == {"r1", "r2"},
          "重启后历史完整依据仍保留")
    check([s["basis"] for s in cs["d"]["retired_supports"]] == [["f2", "f3"]],
          "重启后 d 的历史依据仍与失效时刻实际支持一致")
    facts = {f["id"]: f["active"] for f in state["facts"]}
    check(facts["f1"] is False and facts["f2"] is False and facts["f3"] is True,
          "重启后事实撤回状态保留, f3 仍有效")
    again = request(base, "POST", "/api/retract", {"fact_id": "f1"})
    check(again["verdict"]["already_retracted"] is True,
          "重启后重复撤回仍稳定返回既有裁决")


# --------------------------------------------------------------------------- #
# 服务进程管理 (自起服务模式)
# --------------------------------------------------------------------------- #
def start_server(port: int, db_path: str):
    env = dict(os.environ, PORT=str(port), HOST="127.0.0.1",
               DB_PATH=db_path, PYTHONPATH=ROOT, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.server"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc, f"http://127.0.0.1:{port}"


def stop_server(proc) -> None:
    proc.terminate()
    proc.wait(timeout=10)


def run_online(base_url: str) -> None:
    print(f"[smoke] 在线模式: {base_url} (要求服务为干净初始状态)")
    wait_ready(base_url)
    step_health(base_url)
    step_build_procedure(base_url)
    step_first_retraction(base_url)
    step_second_retraction(base_url)
    step_repeat_retraction(base_url)
    step_page_and_detail(base_url)


def run_self_started() -> None:
    tmp = tempfile.TemporaryDirectory()
    try:
        db_path = os.path.join(tmp.name, "smoke.db")
        port = int(os.environ.get("SMOKE_PORT", "8099"))

        proc, base = start_server(port, db_path)
        try:
            wait_ready(base)
            step_health(base)
            step_build_procedure(base)
            step_first_retraction(base)
        finally:
            stop_server(proc)

        # 在两次撤回之间重启服务 (同一数据文件), 经健康入口确认就绪后复查
        proc, base = start_server(port + 1, db_path)
        try:
            wait_ready(base)
            check_state_after_mid_restart(base)
            step_second_retraction(base)
            step_repeat_retraction(base)
            step_page_and_detail(base)
        finally:
            stop_server(proc)

        # 再次重启: 结论与依据状态 (含历史依据) 均应保留
        proc, base = start_server(port + 2, db_path)
        try:
            wait_ready(base)
            check_final_state_after_restart(base)
        finally:
            stop_server(proc)
    finally:
        tmp.cleanup()


def main() -> int:
    base_url = os.environ.get("BASE_URL")
    try:
        if base_url:
            run_online(base_url)
        else:
            run_self_started()
    except Failure as e:
        print(f"\n[smoke] 失败: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\n[smoke] 异常: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print("\n[smoke] 全部冒烟断言通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
