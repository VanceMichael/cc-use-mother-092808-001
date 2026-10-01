# 后端架构设计

对应焦点报告场景：研究主管过去只保存每周最终数字，无法回答"某机构何时改过判断""能源优惠结束
的理由在发布前是否可见"。本系统以**不可覆盖的修订链 + 发布水位快照 + 规则/发布双审批**为核心。

## 分层

| 层 | 文件 | 职责 |
|---|---|---|
| 存储 | `src/macro_survey/store.py` | SQLite schema、事务（含 SAVEPOINT 嵌套）、查询原语 |
| 领域计算 | `src/macro_survey/consensus.py` | 加权均值、（加权）中位数、IQR/MAD 异常值，纯函数 |
| 领域服务 | `src/macro_survey/service.py` | 征集、规则审批、发布水位、回测、提醒、审计 |
| HTTP | `src/macro_survey/api.py` | 标准库 WSGI，Bearer 鉴权，JSON |
| 入口 | `src/macro_survey/app.py` | 装配与 `python -m` 启动 |

仅依赖 Python 3.11 标准库。时间全部以带时区 ISO-8601 UTC 存储，字典序即时间序；
`SurveyService(store, clock=...)` 的时钟可注入，测试可任意推进时间。

## 角色与职责分离

- `CONTRIBUTOR`：必须归属一家机构，只能提交/修订/撤回本机构预测，只能看自己与已发布报告。
- `STATISTICIAN`：定义机构权重、起草共识规则、可申请发布。
- `APPROVER`：审批规则、审批发布单。**起草人≠审批人、申请人≠审批人**，服务层强制。
- `PUBLISHER`：申请发布、关闭轮次。
- `RESEARCH` / `AUDITOR`：研究主管视角与审计视角，可读全部提交链、审计日志、做完整性校验。

## 修订链与水位（核心不变量）

每次提交生成一条 `submissions` 行：

- `seq`：同机构同目标（轮次×指标×年份）严格递增；
- `kind`：`upsert` / `withdraw`；
- `status`：`active` / `superseded` / `withdrawn` / `late`；
- `prev_hash` + `row_hash`：以机构为链的 SHA-256 哈希链，覆盖数值、区间、置信度、
  理由、回执编号、时间。`verify_publication_integrity` 可重放校验。

关键规则：

1. **截止前修订**：旧版置 `superseded`/`withdrawn`，永不删除；新版生效。
2. **截止后到达（含撤回）**：落库为 `late`，**不改任何既有行**，因此
   迟到修订不能覆盖、迟到撤回不能抹除已发布水位。
3. **撤回**：以一条 `withdraw` 行留痕，旧 active 变 `withdrawn`——"当时参与过共识征集"
   的事实仍在，且在工作人员提交视图与解释视图中可见。
4. **发布水位**：申请发布时记录 `watermark_at`，把水位之前每机构最后一个 active 版本
   连同权重、理由、哈希**复制**进 `publication_samples`。之后的任何提交/修订都不影响
   已冻结快照；共识数字以快照为准，可随时用同一已批准规则重算复核。

## 幂等与隔离

- 客户端对每个写请求带 `client_ref`（回执编号），服务端保存规范化请求体指纹。
- 同编号 + 同指纹 → 返回**首次结果**（`replayed: true`），不产生新版本。
- 同编号 + 不同指纹 → 载荷进 `quarantined_messages`，返回 `409 quarantined`。
  隔离写入在独立事务中先提交，再抛业务异常，确保异常回滚不会吞掉隔离事实。
- 该编号在隔离未决期间的后续到达继续隔离。工作人员可 `discard` 或 `accepted`
  （受理时生成新内部编号、按当前时刻重放入库，迟到则仍按迟到处理）。

## 共识规则

`RuleSpec`（`consensus.py`）：

- `method`: `median`（默认，机构等权；启用置信度时为加权中位数）/ `mean`（权重×可选置信度）；
- `outlier`: `iqr`（k=1.5）或 `mad`（k=3.5）；小样本（<4）不剔除，避免误杀；
- `min_samples`: 发布所需的有效样本下限；置信度为 0 的样本不入数值计算但仍登记在册。

规则为**版本化**：`draft → approved | rejected`，只有最新 approved 版本可用于发布；
草案也允许回测，审批人可先看历史表现再决定。

## 匿名化

- 发布快照为每位贡献者生成当次随机匿名码（`C01…`，**每次发布重新洗牌**，无法跨报告对齐）。
- 机构视角的解释接口：自己可见匿名码归属与理由；他人只显示匿名码、数值、权重、
  是否入算及剔除原因，`rationale`/`institution` 一律为 `null`。
- 待批/未发布报告对机构不可见、不可列；偏离复核接口机构只能查到自己。

## 实际值、误差、回测与复核

- `actuals` 记录实际值后，对该指标×年份所有已发布报告：保存共识误差
  （`error/abs_error/pct_error`）与当时规则参数快照；按显式阈值或
  1.5×机构平均绝对误差自动标记 `contributor_deviations.needs_review`，
  供"下一轮开始前复核"。
- `run_backtest` 用**历史发布快照**对任意规则版本重算 MAE/RMSE/偏差/区间覆盖率，
  结果存 `rule_backtests`，支持规则变更前拿证据。

## 重启恢复

- 所有写操作走 SQLite 事务并即时提交到文件库（WAL）。
- 建轮次时即持久化三条提醒（截止前 24h、截止、计划发布时刻）；待批发布单再挂一条
  决定到期提醒。提醒状态机 `due → delivered | cancelled`，扫描投递幂等。
- 重启后调 `GET /resume` 即可看到：待批准发布单、未投递提醒、未决隔离数量；
  批准与提醒处理可无缝继续（见 `test_pending_publication_survives_restart`）。

## 审计

所有状态变更写 `audit_log`（动作、实体、操作者、时间、细节）。
