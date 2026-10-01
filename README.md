# 职业辐射剂量与异常事件

合并监测读数，比较历史剂量并管理超限调查、医学随访与报告期限。支持**仪器校准证书 → 原始读数 → 剂量事件**的可追溯修订链：证书补发后按生效区间重算历史剂量，旧结论失效并生成待办。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量，以及调查/随访规则效果比对。
- `src/repository.py`：SQLite建表、事务、乐观锁、证书版本、修订链和审计链。
- `src/recalc.py`：重算批次编排（幂等、仪器认领、失败恢复）。
- `src/service.py`：权限检查、用例编排、证书补发触发重算、结论失效与待办。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间、ISO解析和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、失败、修订链和HTTP测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8312
```

默认端口为`8312`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份；重算批次可用`Idempotency-Key`请求头作为幂等键。

## 可追溯修订链

1. **证书版本**：校准证书按仪器和生效区间形成版本。同生效起点再次签发即为**补发**（新证书号、`version+1`、旧证`superseded`并保留），区间重叠或改变区间被拒绝。补发后自动提交受影响区间（`valid_from`~`valid_to`）的重算批次。
2. **读数换算**：剂量事件创建时可携带原始读数（仪器、测量时刻、原始值、证书号），剂量=原始值×证书系数，落地第1版`dose_revisions`。证书号必须覆盖测量时刻。
3. **区间重算**：重算按当前有效证书版本重新换算区间内读数，沿修订链追加记录（旧/新系数、旧/新证书、旧/新剂量、批次号）。换算剂量未变不追加修订。
4. **并发互不覆盖**：重算以仪器为最小认领单位，跨批次正在处理的仪器记为`skipped`；剂量更新带乐观锁。部分（partial）批次用同一请求重试即可补齐跳过的仪器。
5. **失败恢复**：单台仪器失败则批次置`failed`，已完成仪器不回滚、不重算；重试只从最后未完成（failed/skipped）的仪器恢复。
6. **请求幂等**：同一`request_id`重试沿用首次结果（completed后返回冻结摘要）。
7. **结论失效**：状态流转进入调查/随访/关闭时，按当时规则生成`findings`。重算后若调查/医学随访判定翻转或报告期限变化，旧结论置`superseded`（记录与审计仍可查），生成`todos`；已关闭事件叠加`reassess_required`。
8. **历史基线**：无证书号的旧读数自动升级为仪器的**历史基线证书**（系数1.0，`is_baseline=1`）。首张正式证书签发后基线区间自动收紧到正式证书之前，基线读数不参与补发重算。

## 主要接口

### 剂量事件
- `GET /api/items`、`POST /api/items`（body可含`reading`）
- `GET /api/items/{id}`、`POST /api/items/{id}/records`
- `POST /api/items/{id}/readings`：为存量事件补挂读数（无证书号升级基线），必须提交`expected_version`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/items/{id}/revisions`：剂量修订链
- `GET /api/readings?instrument_id=`

### 仪器与证书
- `POST /api/instruments`、`GET /api/instruments`、`GET /api/instruments/{id}`
- `POST /api/instruments/{id}/certificates`：签发/补发（radiation_officer），补发响应含`recalc_batch`
- `GET /api/instruments/{id}/certificates`、`GET /api/certificates?instrument_id=`

### 重算批次（dosimetrist / radiation_officer）
- `POST /api/recalc-batches`：`{request_id, instrument_ids?, window_from?, window_to?, reason?}`；省略仪器表示全部仪器
- `GET /api/recalc-batches`、`GET /api/recalc-batches/{id}`

### 结论、待办与审计
- `GET /api/findings?item_id=&status=active|superseded`
- `GET /api/todos?item_id=&status=open|closed`、`POST /api/todos/{id}/close`
- `GET /api/audit`（health_physicist / viewer）

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，剂量达到阈值必须进入调查、达到2倍必须医学随访；更正剂量不能覆盖已确认审计记录。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
