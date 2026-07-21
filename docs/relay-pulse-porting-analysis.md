# relay-pulse 可移植性分析

分析基线：[`prehisle/relay-pulse`](https://github.com/prehisle/relay-pulse)
commit `0dfbb8e22cb2fbb4a31476e3662a2b42841f9277` (v2.68.0)。该项目以 MIT
许可证发布。Lexsond 没有复制 Go 源码或 UI 资产；下列实现是根据公开
行为和运维问题进行的 Python/React 独立设计。

## 定位和技术栈差异

| 维度 | relay-pulse | Lexsond |
| --- | --- | --- |
| 主要目标 | 公开中转通道的高频可用性看板 | API 协议、性能、质量和多模态证据平台 |
| 后端 | Go，长驻调度器与高并发 HTTP | Python 3.12+，FastAPI + 原生探针 + LangChain 边界 |
| 工作流 | 进程内定时调度 | 本地执行或 Temporal 持久工作流 |
| 存储 | SQLite/PostgreSQL 时序记录 | SQLite/PostgreSQL 控制面 + 不可变运行/证据 |
| 前端 | React 公开热力图/筛选看板 | React 运维控制台、CRUD、运行证据和热力图 |
| 密钥 | 通道级配置模型 | 本地请求临时 Key；Temporal 仅持久化 `credential_ref` |

Go 在单进程超高并发轮询、内存占用和单二进制部署上更有优势。
Python 版的优势是能直接复用 Lexsond 的六模态探针、Pydantic 契约、
LangChain 回调和 Temporal 生态。对当前项目而言，更换语言的收益低于复用
成熟的运维思路，所以保留 Python 主体。

## 已移植并上线的能力

| relay-pulse 启发点 | Lexsond 实现 | 有意的差异 |
| --- | --- | --- |
| 定时调度、错峰和并发上限 | 持久化 `monitor_policies`，确定性错峰，每轮最多 4 个 | 到期策略用 DB 租约抢占，不只依赖进程内堆 |
| 防重复触发 | 策略 ID + 计划时槽派生 UUID 幂等键 | 与已有运行索引和 Temporal Workflow ID 对齐 |
| 90m/24h/7d/30d 热力图 | API 服务端时间桶 + React 对齐矩阵 | 同一页还展示 TTFT/E2E 和错误分类 |
| 状态变化事件 | `UNKNOWN/UP/DEGRADED/DOWN` 状态机与 `RECOVERED` 事件 | 失败/恢复阈值可按策略设置，取消不改变健康状态 |
| 算术题防 mock/回显 | 按时槽确定性轮换的多题面挑战与 128 位 nonce | 预期答案绝不进入题面；结果与 nonce 必须精确匹配，套件仍尊重其不可变断言 |
| SSRF URL/DNS 保护 | 在实际 socket 建连前校验全部 DNS 结果 | 消除“预检后重解析”窗口；允许明确的本机数字回环开发目标 |
| 历史数据清理 | 每小时有界批量删除派生样本/事件 | 当前状态和审计运行不被自动删除 |

## 暂不直接移植

- Go 调度器、Gin 服务器和 SQL 实现：会破坏现有 Python/Temporal 边界，
  且无法复用六模态原生证据。
- 可任意配置的原始 HTTP 模板：容易扩大 SSRF、密钥泄露和未受控请求面；
  Lexsond 继续使用有类型的六模态适配器。
- 持久化明文 API Key：与 Lexsond 的临时 `SecretStr` / `credential_ref`
  安全边界冲突。
- 通知机器人、收藏、URL 筛选和公开榜单：对运营有价值，但不是探针
  正确性前置条件。后续应在事件与投递记录分离、用户鉴权和可重放
  传输契约完成后再做。
- 自动“移板”或商业排名：Lexsond 是证据和诊断工具，不在无人审核时
  将技术测量直接变为商业处置。

## 验收边界

1. 一个计划时槽最多创建一个运行，租约 token 不匹配时不能提交调度结果。
2. 进程重启不丢策略；逾期不补发整段历史请求。
3. Key 不出现在策略、运行、样本、事件、SSE 或 Temporal History。
4. 相同运行的重复结果投影不重复增加样本或事件。
5. 清理仅删除过期派生历史，不改写当前健康状态。
