# HTTP API 参考

所有端点除 `/admin/bootstrap` 外需要请求头 `Authorization: Bearer <token>`，
请求与响应均为 JSON，时间为 ISO-8601（建议带时区）。

错误形态：`{"error": "<code>", "message": "..."}`，状态码 400/401/403/404/409/500。

## 引导与主数据

| 方法 路径 | 角色 | 说明 |
|---|---|---|
| `POST /admin/bootstrap` | 无（仅库中无用户时） | 创建首个管理员，body `{"confirm":true}`，返回 token |
| `POST /institutions` | ADMIN/RESEARCH | `{"name"}` |
| `POST /users` | ADMIN | `{"username","roles":[...],"institution_id"?}`，返回 token |
| `POST /indicators` | ADMIN/RESEARCH | `{"code","name","unit"}` |
| `POST /rounds` | ADMIN/RESEARCH | `{"label","opens_at","submission_deadline","publish_at","forecast_years":[2026]}` |
| `POST /rounds/close` | ADMIN/RESEARCH/PUBLISHER | `{"round_id"}` |
| `POST /weights` | STATISTICIAN | `{"indicator_id","institution_id","weight"}`，缺省权重为 1 |

## 征集（机构）

| 方法 路径 | 说明 |
|---|---|
| `POST /submissions` | `{"round","indicator","forecast_year","point_value","low"?,"high"?,"confidence"(0~1),"rationale","client_ref"}`。首次/修订 201，幂等重放 200 |
| `POST /submissions/withdraw` | `{"round","indicator","forecast_year","client_ref"}` |
| `GET /submissions/mine?round=&indicator=&year=` | 本机构完整修订链 |

- 区间可选，给就必须成对且 `low ≤ point ≤ high`；`rationale` 必填。
- 截止后到达：HTTP 201 但 `status="late"、eligible=false`，不影响水位。
- `client_ref` 重复但内容不同：`409 {"error":"quarantined"}`，进隔离区。

## 工作人员视图

| 方法 路径 | 角色 | 说明 |
|---|---|---|
| `GET /staff/submissions?round=` | 全体职员 | 含机构名、全部状态与理由的完整修订链 |

## 规则

| 方法 路径 | 角色 | 说明 |
|---|---|---|
| `POST /rules` | STATISTICIAN | `{"indicator_id","params":{...}}` → draft |
| `GET /rules?indicator_id=` | 职员 | 列出全部版本 |
| `POST /rules/decide` | APPROVER | `{"rule_id","approve":bool,"reason"?}`；起草人本人会被拒（403） |

`params`：`{"method":"median"|"mean","use_confidence_weight":true,"min_samples":3,"outlier":{"method":"iqr"|"mad","k":1.5}}`

## 发布

| 方法 路径 | 角色 | 说明 |
|---|---|---|
| `POST /publications/request` | PUBLISHER/STATISTICIAN | `{"round_id","indicator_id","forecast_year"}`，定格水位并生成待批单 |
| `POST /publications/decide` | APPROVER | `{"publication_id","approve":bool,"reason"?}`；申请人本人会被拒 |
| `GET /publications?status=` | 职员看全部；机构只看 published | 列表 |
| `GET /publications/explain?id=` | 职员/机构（已发布） | 规则、样本、剔除原因、`revision_timeline` 与完整 `revision_history` |
| `POST /publications/verify` | AUDITOR/RESEARCH/APPROVER/ADMIN | 快照重算与哈希链完整性 |

## 实际值 / 误差 / 回测 / 复核

| 方法 路径 | 角色 | 说明 |
|---|---|---|
| `POST /actuals` | RESEARCH/ADMIN | `{"indicator_id","forecast_year","actual_value","released_at","review_threshold"?}`，自动算误差并标记偏离 |
| `GET /errors?indicator_id=` | 职员 | 共识预测误差 |
| `POST /backtests` | 职员 | `{"rule_version_id","indicator_id","forecast_year"?}` → MAE/RMSE/偏差/覆盖率 |
| `GET /backtests` | 职员 | 历史回测 |
| `GET /reviews/next-round` | 职员 | 下一轮前需复核的机构偏离（含机构名） |
| `GET /reviews/mine` | CONTRIBUTOR | 仅本机构的偏离记录 |

## 隔离区

| 方法 路径 | 角色 | 说明 |
|---|---|---|
| `GET /quarantine?status=open` | 职员 | 隔离消息与原始载荷 |
| `POST /quarantine/resolve` | STATISTICIAN/PUBLISHER/RESEARCH/ADMIN | `{"quarantine_id","decision":"discarded"|"accepted","note"?}` |

## 提醒、恢复与审计

| 方法 路径 | 角色 | 说明 |
|---|---|---|
| `GET /reminders/due` | 职员 | 投递到期提醒并列出逾期未批的发布单（幂等） |
| `POST /reminders/cancel` | PUBLISHER/RESEARCH/ADMIN | `{"reminder_id"}` |
| `GET /resume` | 持有效令牌即可 | 重启后恢复：待批单、到期提醒、未决隔离数 |
| `GET /audit?limit=200` | AUDITOR/RESEARCH/ADMIN | 全量操作日志 |

## 快速体验

```bash
python -m src.macro_survey.app --db survey.db --port 8080
curl -s -X POST localhost:8080/admin/bootstrap \
  -H 'Content-Type: application/json' -d '{"confirm":true}'
```
