# 再保险合约与巨灾暴露管理

纯Python标准库实现的再保险合约与巨灾暴露管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、分层摊回、赔偿限额和恢复保费和冲突检查。
- `src/repository.py`：SQLite建表、事务、事件暴露台账（`events`/`occupancies`）和查询。
- `src/service.py`：用例编排、权限检查、事件台账核对、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、台账语义、并发抢占和失败场景测试。

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
- `GET /api/events`：事件暴露台账列表。
- `GET /api/events/{event_id}`：事件台账详情（容量、剩余恢复次数、占用与结算明细）。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 事件暴露台账语义

- **首案定账**：同`event_id`的首笔合约确定事件单层容量（`(limit-attachment)×cession_pct`）与
  恢复次数`reinstatements`（非负整数，缺省1），后续赔案共用该台账。
- **核定先占额度**：`calculate`只占用事件容量，不耗恢复次数、不计保费；占用超出容量直接拒绝，
  错误响应`details`返回剩余容量、剩余次数和占用明细。
- **结算才耗次数**：`settle`在同一`BEGIN IMMEDIATE`事务内核对剩余恢复次数，成功才
  `reinstatements_used+1`并累计`premium_received`，付款参考与保费在占用明细上留档。
- **释放**：未结算案件`reject`或`withdraw`（`cancel`为别名）释放`reserved`占用；
  已结算记录为终态，付款与保费保留。
- **并发抢占**：两笔赔案同时结算抢最后一次恢复时仅一笔成功，另一笔收到409，
  响应体含`reinstatements_remaining`和逐笔`occupancies`。
- **写入失败重开**：占用以`(event_id, record_id)`唯一，重试按同一事件同一赔案核对差额，
  不重复占用、不重复耗次数。
- **旧数据回填**：建表时对缺少关联的旧记录按原状态（`calculated`→reserved、
  `settled`→settled并计入次数/保费）回填，记录本身状态不变，回填后继续走原状态机。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、台账占用/释放/结算、并发抢占最后一次恢复、旧数据回填、
重开幂等、重复引用、权限拒绝和版本冲突。
