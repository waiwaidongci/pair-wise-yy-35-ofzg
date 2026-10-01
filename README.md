# 职业辐射剂量与异常事件

合并监测读数，比较历史剂量并管理超限调查、医学随访与报告期限。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8312
```

默认端口为`8312`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`（可带`readings`原始读数，剂量按证书系数计算）
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/items/{id}/revisions`：剂量事件修订链
- `GET /api/audit`
- `GET/POST /api/instruments`：仪器
- `GET/POST /api/instruments/{id}/certificates`：按仪器与生效区间形成证书版本，补发即新版本
- `GET/POST /api/instruments/{id}/readings`：原始读数
- `POST /api/recalc-batches`：补发证书后重算受影响区间，须带`request_key`（幂等）
- `GET /api/recalc-batches/{id}`、`POST /api/recalc-batches/{id}/retry`：失败后从最后完成的仪器恢复
- `GET /api/todos`、`POST /api/todos/{id}/done`：重算生成的待办
- `POST /api/baselines/upgrade`：无证书号的旧事件升级为历史基线

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。

## 修订链与重算

- 证书按仪器和生效区间版本化：补发证书生成新版本，旧版本保留为历史，读数按测量时间适用的证书系数重算剂量事件。
- 剂量事件保留修订链（`event_revisions`）：初始、读数追加、证书重算、历史基线，每次重算生成新修订，旧值可查。
- 两批并发重算时，仪器被原子认领（`instruments.active_batch_id`），后到的批次跳过该仪器，互不覆盖。
- 批次按仪器设置检查点：失败后重试跳过已完成仪器，从最后完成的仪器继续；同一`request_key`重试沿用首次结果。
- 重算改变调查升级、医学随访或报告期限时，旧的已关闭结论标记失效并生成待办（调查/随访/期限/结论失效），审计记录只增不改。
- 无证书号的旧数据事件可升级为历史基线（系数1.0），不参与证书系数重算。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
