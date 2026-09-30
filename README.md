# 再保险合约与巨灾暴露管理

纯Python标准库实现的再保险合约与巨灾暴露管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、分层摊回、赔偿限额和恢复保费和冲突检查。
- `src/repository.py`：SQLite建表、事务、事件暴露台账与赔案占用明细、启动核对回填。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、事件台账并发与回填和失败场景测试。

## 事件暴露台账

同一巨灾事件（`event_id`）的容量与恢复次数以 `event_exposures` 台账为准，每笔赔案在
`event_occupancies` 留一条占用明细：

- **首案确定**：首笔赔案提交（`submit_claim`）时建账，容量=分层宽×分出比例，
  恢复次数取创建数据中的 `reinstatement_count`（缺省1次，非负整数）。
- **核定先占**：`calculate` 在同一 `BEGIN IMMEDIATE` 事务内预先占用摊回额度与一次
  恢复次数，此时不消耗次数、不计保费。次数或容量不足返回409，`details` 带
  `remaining_reinstatements`、`available_capacity` 与全部 `occupancies` 明细。
- **结算才耗次数并累计保费**：`settle` 将占用置为 `consumed`，恢复次数扣减，
  `settled_amount` 与 `received_premium` 累计入事件，付款参考与保费在赔案、占用明细
  双留档。
- **拒赔/撤销释放**：未结算案件执行 `reject` 或 `revoke`（新增，
  `claim_submitted`/`calculated` → `cancelled`），预占额度与次数归还事件。
- **并发**：两笔赔案同时核定争抢最后一次恢复时，事务串行化保证只有一笔成功，
  失败方收到剩余次数与占用明细。
- **重开核对与旧数据回填**：服务启动时按事件幂等核对（`reconcile_events`），修复写入
  中断残留的占用；旧版库（无台账表、无 `reinstatement_count`）按案件原状态回填事件
  关联，次数下限抬到与已耗/预占一致，回填后案件仍按原状态继续流转。


## 启动

```bash
python3 app.py --db ./data.db --port 8325
```

默认端口为`8325`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `GET /api/events`：事件暴露台账列表（含容量、恢复次数与逐笔占用明细）。
- `GET /api/events/{event_id}`：单个事件台账详情。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`；
  `data`可含`reinstatement_count`（恢复次数，缺省1）。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`；
  动作含`bind`/`submit_claim`/`calculate`/`settle`/`reject`/`revoke`。
  次数或容量冲突时409响应的`details`含剩余次数、可用额度与占用明细。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、事件台账并发争抢/释放/回填、重复引用、权限拒绝和版本冲突。
