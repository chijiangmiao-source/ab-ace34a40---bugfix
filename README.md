# 安全规程 · 事实撤回与依据传播系统 (Safety Procedure TMS)

一个面向安全员的规程管理系统：规程由**带唯一标识的事实**与**无变量正向规则**
组成。每条结论在规则触发时保存**完整前提集合**；撤回事实时，在**单一持久化
事务**内沿**反向索引**传播撤回——结论只有在**所有完整支持都耗尽**时才失效，
并继续传播到仅依赖它的下游结论。规则依赖图中的任何环（含自我支持闭环）都会被
拒绝，循环规则不可能凭空生成有效结论。

## 核心语义

| 场景 | 行为 |
| --- | --- |
| 结论有两条独立支持路径，撤回其中一条事实 | 结论**保持有效**，页面列出剩余路径的完整可复算依据 |
| 撤回最后一条支持事实 | 该结论及**仅依赖它**的下游结论失效，返回支持耗尽的传播链 |
| 重复撤回已失效事实 | **幂等**，稳定返回既有裁决，不产生新传播 |
| 撤回未知事实 | 返回 `unknown_fact`，规程不变 |
| 规则引用不存在的结论 | 返回 `dangling_reference`，规则不落库、不污染规程 |
| 规则形成环（如 `x :- x`、`x :- y, y :- x`） | 返回 `cyclic_rule`，规则不落库 |
| 服务重启 | 事实状态、结论有效性、当前/历史依据、裁决均保留 |

每个结论的每条完整支持都附带展开到事实层的**完整依据（basis）**，例如
`must_evacuate` 经 `fire_confirmed` 推出时，依据展示为
`{smoke_detector_ok, sprinkler_pressure_ok}`，可直接复算。

## 目录结构

```
app/
  tms.py      # 良基正向规则引擎: 校验/分层不动点/触发依据/反向索引传播
  store.py    # SQLite 持久化: 撤回+传播同一事务, 重启恢复
  server.py   # 纯标准库 HTTP 服务: REST API + 页面托管 + 健康检查
web/src/      # 页面源码 (原生 HTML/CSS/JS, 无构建依赖)
scripts/
  build_page.py  # 构建页面到 web/dist
  smoke_http.py  # 真实 API/HTTP 冒烟 (可自起服务并验证重启)
  verify.sh      # Compose verify 入口: 测试 -> 构建 -> 冒烟
tests/        # 规则引擎与持久化单元测试 (unittest)
Dockerfile
docker-compose.yml
```

## 本地运行（无需 Docker、无需第三方依赖，Python 3.11+）

```bash
# 1) 规则逻辑测试
python3 -m unittest discover -s tests -v

# 2) 构建页面
python3 scripts/build_page.py

# 3) 启动服务 (宿主机端口可配置)
PORT=8080 HOST=0.0.0.0 DB_PATH=./data/procedure.db SEED_DEMO=1 \
  python3 -m app.server
# 浏览器打开 http://localhost:8080

# 4) 完整 verify (一次执行: 单元测试 + 构建 + HTTP 冒烟, 退出码报告结果)
bash scripts/verify.sh
```

## Docker Compose

```bash
# 启动页面/接口服务, 宿主机端口经 HOST_PORT 配置 (默认 8080)
HOST_PORT=9090 docker compose up --build web

# 一次性验证: 运行规则逻辑测试、构建页面、API/HTTP 冒烟, 完成后退出
docker compose build verify
docker compose up verify          # 退出码 0 = 全部通过
```

`web` 服务带 `/api/health` 健康检查；数据存于命名卷 `tms-data`。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/api/health` | 接口与持久层可用性 |
| GET  | `/api/state` | 事实、规则、结论、每条结论的当前/历史依据、最近裁决 |
| POST | `/api/facts` | `{"id":"f1"}` 新建事实 |
| POST | `/api/rules` | `{"id","conclusion","antecedents":[...]}` 新建规则 |
| POST | `/api/retract` | `{"fact_id":"f1"}` 撤回事实（事务内传播，返回完整裁决） |
| GET  | `/api/conclusions/<id>` | 单个结论的完整依据 |

撤回响应中的裁决包含：

- `affected`：被撤回事实与本次失效的结论，每项附失效前完整依据；
- `propagation_chain`：支持耗尽形成的传播链
  （`triggered_by → node`，含耗尽前提与事实层依据）；
- `survived`：靠替代依据保留的结论。

## 设计要点

- **良基语义**：加入规则前做依赖闭包与 DFS 环检测；求值按拓扑分层取最小
  不动点，无事实落地的循环无法推出任何结论。
- **每次触发保存完整前提集合**：`supports` 表区分当前支持（`active=1`）与
  已耗尽的历史依据（`active=0`），支撑页面复算与传播链展示。
- **反向索引传播**：维护 `前提 → (规则, 结论)` 索引，撤回只重算受影响的
  下游；节点支持集变化但结论存活时，继续向下游刷新展开依据。
- **事务原子性**：事实置为撤回、内存传播、依据/裁决落盘在同一个
  SQLite 事务中提交，异常整体回滚。
