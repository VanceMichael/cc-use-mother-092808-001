# 宏观预测样本与修订水位治理

管理宏观预测调查样本、规则版本、发布水位和事后误差。

## 参与方与事实

主要参与方包括中央银行研究部门、市场调查机构、统计发布人员、规则审批人员、审计人员。领域资料记录以下已经确认的事实：

- 2026年通胀预期由4.92%上调至4.99%
- 经济增长预期连续第三周下调
- 电费优惠结束、油价和气候被视为价格风险

## 业务约束

- 调查样本与匿名贡献
- 修订水位
- 共识规则
- 发布冻结
- 误差回测

`contracts/context.schema.json` 描述资料结构，`fixtures/context.json` 提供不含真实身份信息的示例，`src/news_context_001.py` 负责读取和校验这些资料。

## 预测征集与发布后端

`src/forecast_backend/` 是完整的后端实现，仅依赖 Python 标准库：

| 模块 | 职责 |
| --- | --- |
| `models.py` | 角色、状态机、规则定义、匿名代号、领域错误 |
| `clock.py` | 时钟抽象（系统时钟 / 测试手动时钟） |
| `consensus.py` | 中位数、加权均值、IQR 异常值（纯函数） |
| `storage.py` | SQLite 持久化：提交、回执、隔离、规则、水位、误差、提醒、事件 |
| `services.py` | `ForecastSystem` 门面：全部业务规则 |
| `api.py` | HTTP JSON 适配层（`http.server`），头部 `X-Actor-Id` / `X-Actor-Role` 携带身份 |

核心行为：

- **提交与修订**：获准机构按（指标, 预测年份, 调查轮次）提交数值、区间、置信度与理由；再次提交构成新修订，完整修订链保留。
- **发布水位**：发布稿经统计人员计算、审批角色批准后冻结；迟到修订只进入下一版本，不得覆盖已发布水位。
- **撤回留痕**：撤回保留当时参与共识计算的事实，历史快照可解释。
- **双人审批**：规则与发布稿都要求定义者与批准者分离（四眼原则）。
- **匿名保护**：聚合查询要求机构数达到下限；统计视角只见按轮次生成的匿名代号；机构只能看自己的明细与误差。
- **回执幂等**：同一回执重复到达返回原结果；编号相同内容不同进入隔离区。
- **误差与回测**：实际值登记后保存共识与机构两级误差、超阈值偏离进入下一轮复核清单；候选规则可在历史轮次上回测。
- **重启恢复**：全部状态落盘，`recover()` 在重启后触发到期提醒并报告待批准事项。
- **可解释性**：`explain_publication` 给出任一报告数字的有效样本、权重、异常值标记与修订顺序。

### 运行 HTTP 服务

```bash
python3 -m src.forecast_backend.api --db forecast.db --port 8080
```

主要端点：`POST /submissions`、`POST /submissions/withdraw`、`GET /consensus`、`POST /rules`、`POST /rules/{id}/versions/{v}/approve`、`POST /publications/compute`、`POST /publications/{id}/publish`、`GET /publications/{id}/explain`、`POST /actuals`、`POST /backtests`、`GET /review-flags`、`POST /reminders/recover`、`GET /approvals/pending`。

## 开发命令

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

编译检查：

```bash
python3 -m compileall -q src
```

两条命令只读取仓库内文件，不需要连接外部业务系统。
