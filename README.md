# 简化订单多智能体系统与运维助手

> 本文档是项目的设计基线，主要供后续 Vibe Coding 和代码生成模型阅读。实现过程中应优先保持本文定义的系统边界、接口契约、证据链、安全控制和恢复流程；如果需要偏离，应先更新本文档并说明原因。

## 1. 项目目标

实现一个可运行的简化订单多智能体系统，并实现一个能够观测和运维该系统的运维助手。

系统需要完成两类目标：

1. 业务目标：用户可以用自然语言描述选购需求；购物助手 Agent 负责理解场景、澄清缺失条件并解释候选商品，用户确认后再由订单 Agent 完成订单校验、库存判断和风险判断，最终由确定性订单服务创建订单。
2. 运维目标：采集指标、日志、调用链、发布记录和对象档案，发现 Agent、工具或协作流程中的异常，输出诊断证据、根因候选和建议动作；高风险动作必须经过人工确认。

本项目不是完整电商平台。核心目的是用尽量小的业务范围展示多 Agent 协作、可观测性、证据驱动诊断、安全审批、故障恢复和组件可替换能力。

## 2. 作业验收目标

项目必须体现以下五个评价维度：

| 维度 | 本项目的实现方式 |
| --- | --- |
| 边界 | 明确区分 Agent、数据平台、模型服务和运维平台 |
| 证据 | 每个诊断结论都引用可查询的指标、日志或链路证据 |
| 安全 | 回滚、重启、切换后端和重放订单等高风险动作需要权限检查和人工确认 |
| 可恢复 | 支持超时、有限重试、降级、回滚、重放和事后复盘 |
| 可演进 | 模型、Prompt、知识数据和工具通过标准接口与版本号解耦 |

系统最终应能回答：

- 系统如何证明诊断结论有依据？
- 如果判断错误，哪个组件能够发现？
- 哪个组件能够阻止危险动作？
- 动作执行后如何验证效果？
- 验证失败时如何回滚或转人工？

## 3. 项目范围

### 3.1 包含范围

- 简化订单请求，不接入真实支付。
- 自然语言选购、需求澄清、本地商品候选查询和用户确认。
- 一个购物助手 Agent，以及订单协调、库存判断、风险判断三个订单处理 Agent，共四个业务 Agent。
- 确定性的订单规则引擎和订单执行服务。
- 一个独立运维助手 Agent。
- 三个正式运维工具定义。
- 指标、日志、链路、发布记录和对象档案的本地模拟数据平台。
- 人工审批、模拟回滚、订单重放和处置后验证。
- Mock、本地和云端三种模型 Provider 接口。
- 一个正常实验和至少一个故障注入实验。
- 自动化测试、演示脚本和实验结果留存。

### 3.2 不包含范围

- 真实支付、物流或第三方电商接口。
- 抓取第三方购物网站、跨平台比价或代表用户在外部平台自动下单；“真实商品”在本项目中指本地模拟目录中可校验、可查询的商品记录。
- 真实生产数据库、Prometheus、ELK、Jaeger 或 Kubernetes 集群。
- 真实生产服务重启与发布回滚。
- 大模型训练或微调。
- 复杂前端；先保证 API、命令行和最小演示页面可用。
- 多租户、真实身份认证和企业级 RBAC；只实现足以展示审批和权限边界的模拟版本。

## 4. 总体架构

```text
                              +------------------+
                              |   Model Gateway  |
                              | Mock/Local/Cloud |
                              +---------+--------+
                                        |
User natural language -> Shopping API -> ShoppingAssistantAgent
                                        | analyze / clarify
                                        | calls read-only tool
                                        v
                               ProductCatalogTool
                                        |
                              Local Product Catalog
                                        |
                       catalog facts -> Agent search / rank
                                        |
                           candidates + user confirmation
                                        v
User / confirmed intent -> Order API -> Order Coordinator Agent
                                      |                 |
                                      v                 v
                               Inventory Agent      Risk Agent
                                      |                 |
                                      +--------+--------+
                                               v
                                      Order Policy Engine
                                               v
                                      Order Executor Service
                                               v
                                 SQLite / Local State Storage
                                               |
                                   metrics, logs, traces
                                               v
                              Observability and Ops Data Layer
                                               v
                                  Detector -> Ops Guardian Agent
                                               v
                              evidence + diagnosis + recommendation
                                               v
                                    Approval and Permission Gate
                                               v
                              Remediation / Rollback / Replay
                                               v
                                  Post-action Verification
```

### 4.1 系统边界

#### Agent 层

负责自然语言理解、任务规划、证据分析和根因推理：

- `OrderCoordinatorAgent`
- `InventoryAgent`
- `RiskAgent`
- `ShoppingAssistantAgent`
- `OpsGuardianAgent`

#### 数据平台层

负责提供可查询事实，不负责自由推理：

- 订单和库存状态
- 商品目录、选购意图、候选商品和用户确认状态
- 指标
- 日志
- 调用链
- 发布记录
- 服务和 Agent 对象档案
- 历史事件与审计记录

初版可以使用 SQLite、JSON 和 JSONL 文件模拟。

#### 模型服务层

通过统一 `ModelProvider` 接口向 Agent 提供推理能力：

- `MockModelProvider`
- `LocalOpenAICompatibleProvider`
- `CloudOpenAICompatibleProvider`
- 可选 `FallbackModelProvider`

所有 Agent 可以共用同一个底层模型。Agent 的区别来自指令、上下文、工具权限和输出契约，而不是必须使用不同模型。

#### 运维平台层

使用确定性代码实现：

- 异常检测
- 权限检查
- 人工审批
- 动作执行
- 回滚
- 重放
- 处置后验证
- 审计记录

危险动作不能仅依赖 Prompt 约束。

## 5. 业务多智能体设计

### 5.1 ShoppingAssistantAgent

职责：

- 将自然语言拆分为使用场景、商品类别、数量、预算、硬性约束和软偏好。
- 从隐含表达中推断可解释的需求，例如“晚上不影响室友”对应“低噪音”。
- 识别类别、数量和预算等缺失信息；信息不足时生成一个明确的澄清问题。意图阶段不再执行约束冲突检测，商品偏好是否能同时满足由目录检索阶段通过候选和取舍说明表达。
- 主动调用只读 `ProductCatalogTool` 获取目录快照，结合用户原始表达、商品描述和异构扩展属性进行语义检索。
- 在同一次目录检索任务中选择真实候选、排序并说明取舍；不依赖外部系统按属性键做语义匹配。
- 使用同一个 Agent 身份完成 `analyze_purchase_intent` 和 `search_product_catalog` 两类结构化模型任务，每次调用均有独立 Prompt 版本和 span。

禁止事项：

- 不编造商品、SKU、价格、库存或商品属性。
- 不把模型输出直接当作商品事实；所有候选必须来自本地商品目录。
- 不直接创建订单、扣减库存或代表用户确认商品。
- 信息不足时不得进入商品确认；商品特征无法全部满足时可以返回有解释的近似候选，但仍必须由用户显式确认。

### 5.2 ProductCatalogTool、事实校验与确认边界

`ProductCatalogTool` 是 ShoppingAssistantAgent 的只读业务工具，不是独立 Agent，也不计入三个正式运维工具。

- 工具只读取版本化本地商品目录，把真实 `sku`、名称、描述、服务端价格、库存、启用状态和扩展属性交给 Agent；工具自身不执行语义匹配或候选评分。
- Agent 同时读取原始自然语言和结构化需求，可以综合不同商品的异构属性进行检索，避免因为属性键不统一而退化为硬编码字段相等匹配。
- 为适配本项目使用的本地小模型，Agent 只需结构化返回最多五个 `selected_skus` 和一个整体 `reason`；系统按返回顺序生成候选与推荐，不要求模型维护复杂的嵌套匹配状态。
- `messages` 是唯一的用户原话来源，目录中的 SKU 和属性只是系统提供的商品事实。推荐理由必须围绕预算、用户明确提出或由场景隐含的特征逐项解释；若候选只能近似满足条件，必须说明具体取舍。
- 如果模型声称用户提供、指定或举例了某个 SKU，而该 SKU 并未出现在原始 `messages` 中，确定性代码会拒绝该理由并附带纠错信息重试一次；第二次仍出现同类错误时，本次模型输出整体失败。
- 为避免小模型在长目录中遗忘前半段商品，Agent 将通过安全边界的完整候选投影为紧凑表格：保留 SKU、名称、价格、键盘类型、噪音、连接、适用场景和其余去重特征，原始目录事实仍用于最终回填。模型返回的唯一数字后缀或带名称的 SKU 可规范化为真实 SKU；无法唯一对应目录的值仍会丢弃。
- 确定性代码还会用首选商品的真实属性核对理由：近似候选不会因特征不完全满足而被删除，但把有线说成无线、把 `medium` 说成低噪音等结论会携带事实依据重试一次；重试后若只是遗漏条件或取舍，系统以可信目录事实生成逐项说明并继续。虚构“用户提供了某 SKU”等来源错误再次出现时仍拒绝输出。
- 类别、预算、启用状态和所需库存是确定性安全边界；机械、无线、静音、材质等商品特征优先满足，无法完全满足时允许展示近似候选。
- Agent 只能选择本次工具返回的 SKU。选择结果通过 Pydantic 校验，并由代码重新填充目录中的名称、价格、库存和属性；不存在、停用、库存不足、超预算或类别错误的 SKU 会被拒绝。
- 原 `ProductSearchAdapter.search()` 为兼容旧调用和单元测试保留，但正常购物工作流不再调用它；确认阶段仍通过只读查询重新获取商品事实。
- 用户必须显式确认一个当前意图中的候选 SKU。
- 确认时重新读取商品记录，由服务端计算单价和总金额，再构造现有 `OrderRequest`。
- 未确认、过期意图、候选集合外 SKU、价格被客户端修改等情况必须拒绝。
- 确认成功后复用现有 `OrderWorkflow`，不得直接调用仓储或跳过库存与风控。

### 5.3 OrderCoordinatorAgent

职责：

- 接收并理解订单请求。
- 检查必填字段。
- 生成 `request_id`、`order_id` 和 `trace_id`。
- 调度库存 Agent 和风控 Agent，条件允许时并行执行。
- 汇总结构化结果。
- 将结果交给确定性规则引擎。

禁止事项：

- 不直接扣减库存。
- 不直接写订单最终状态。
- 不跳过库存或风险检查。
- 不执行重启或回滚。

### 5.4 InventoryAgent

职责：

- 查询商品和库存快照。
- 判断库存是否充足。
- 返回库存判断及证据引用。
- 在数据源不可用时明确返回 `unknown` 或 `timeout`，不得猜测库存充足。

禁止事项：

- 不直接扣减库存。
- 不在查询失败时默认放行。
- 不修改其他 Agent 状态。

### 5.5 RiskAgent

职责：

- 查询用户风险档案和模拟交易历史。
- 根据规则与模型判断风险等级。
- 返回风险分数、风险等级、理由和证据引用。
- 不确定时进入人工审核，不得自动放行。

禁止事项：

- 不直接创建或拒绝订单。
- 不直接修改风险规则。

### 5.6 OrderPolicyEngine

这是普通代码，不是 Agent。

建议规则：

```text
库存充足 + 风险低/中 -> 允许创建
库存不足 -> 拒绝
库存未知或超时 -> pending
风险高或风险未知 -> manual_review
任何结构化输出校验失败 -> retry once, then pending/manual_review
```

### 5.7 OrderExecutorService

这是普通代码，不是 Agent。

职责：

- 使用幂等键预留库存。
- 创建订单。
- 防止重复提交导致重复扣减。
- 保存动作前状态，支持模拟回滚。
- 产生完整审计日志。

## 6. 订单状态机

```text
RECEIVED
   |
   v
CHECKING
   |--------------------|
   |                    |
   v                    v
APPROVED             PENDING
   |                    |
   v                    +--> REPLAYING --> CHECKING
CREATING
   |
   +--> COMPLETED
   +--> FAILED

CHECKING --> MANUAL_REVIEW --> APPROVED / REJECTED
CHECKING --> REJECTED
```

每次状态转换必须记录：

- `request_id`
- `order_id`
- `trace_id`
- 原状态和新状态
- 触发组件
- 原因
- 时间
- 相关证据或审批编号

## 7. 正常业务流程

### 7.1 自然语言选购与确认

```text
1. 用户提交自然语言需求，例如“想买一个五百元以内、晚上不影响室友的键盘”。
2. ShoppingAssistantAgent 输出经过 Pydantic 校验的结构化意图。
3. 如果缺少预算、数量或关键用途，API 返回澄清问题，不查询或创建订单。
4. ShoppingAssistantAgent 调用只读 ProductCatalogTool 获取目录事实；意图阶段不因模型声称“条件冲突”而阻断检索。
5. ShoppingAssistantAgent 结合原始表达、结构化意图、商品描述和扩展属性完成语义检索、候选选择、排序和取舍说明；所有 SKU 必须来自工具结果。
6. API 保存意图、候选集合、模型与 Prompt 版本，并返回候选商品。
7. 用户显式确认候选 SKU。
8. 确认服务重新读取商品目录中的单价，计算总金额并生成幂等 OrderRequest。
9. OrderRequest 进入已经实现的正常订单流程。
```

选购意图至少区分以下状态：

```text
NEEDS_CLARIFICATION -> SEARCHING -> READY_FOR_CONFIRMATION
READY_FOR_CONFIRMATION -> CONFIRMED
NEEDS_CLARIFICATION / READY_FOR_CONFIRMATION -> EXPIRED
```

建议链路结构：

```text
shopping.intent
  +-- shopping.analyze
  +-- shopping.catalog_search
  |     +-- catalog.read
  +-- shopping.confirm
        +-- order.request
```

### 7.2 已确认订单处理

```text
1. 用户提交订单请求。
2. API 创建 request_id、order_id、trace_id 和幂等键。
3. 协调 Agent 校验并规划任务。
4. 库存 Agent 与风控 Agent 执行检查。
5. 两个 Agent 返回通过 Pydantic 校验的结构化结果。
6. OrderPolicyEngine 做确定性决策。
7. OrderExecutorService 预留库存并创建订单。
8. 返回订单结果。
9. 全过程产生指标、日志和 span。
```

建议链路结构：

```text
order.request
  +-- coordinator.plan
  +-- inventory.check
  |     +-- inventory.query
  +-- risk.evaluate
  |     +-- profile.query
  +-- policy.decide
  +-- inventory.reserve
  +-- order.create
```

## 8. 运维系统设计

### 8.1 AnomalyDetector

使用普通代码和规则检测异常，不依赖 LLM 自由判断。

初版规则：

- 订单端到端 P95 延迟超过 2 秒。
- 任一 Agent P95 延迟超过配置阈值。
- 任一 Agent 或工具连续失败 3 次。
- 输出 JSON/Pydantic 校验失败。
- 订单 `PENDING` 数量超过阈值。
- Agent 达到最大重试次数。
- Agent 交接缺少 `trace_id` 或必要字段。
- 诊断结论没有引用 `evidence_id`。
- 危险动作没有有效审批令牌。

### 8.2 OpsGuardianAgent

职责：

- 接收异常检测器生成的告警。
- 制定调查计划。
- 调用只读运维工具查询证据。
- 关联指标、日志、链路、发布记录和对象档案。
- 输出根因候选、置信度、证据引用和建议动作。
- 明确区分客观事实与模型推断。

禁止事项：

- 不直接执行危险动作。
- 不生成不存在的证据 ID。
- 数据不完整时不得输出确定性根因。
- 不绕过审批服务。

### 8.3 ApprovalService

职责：

- 判断动作风险级别。
- 创建审批请求。
- 记录动作目标、影响范围、理由和证据。
- 接受确认或拒绝。
- 生成短期、单次使用的模拟 `approval_token`。
- 保存审批人、时间和决定。

### 8.4 RemediationExecutor

职责：

- 验证权限和审批令牌。
- 执行模拟回滚、重启、后端切换或订单重放。
- 使用幂等键防止重复执行。
- 保存动作前后状态。
- 记录执行结果和错误。

### 8.5 VerificationService

动作执行后必须重新查询指标或状态：

```text
指标恢复 -> 标记处置成功 -> 可重放 pending 订单
指标未恢复 -> 标记诊断未验证 -> 通知人工 -> 停止自动动作
指标恶化 -> 执行补偿/恢复动作 -> 通知人工
```

## 9. 三个正式运维工具

作业报告中重点定义以下三个工具。业务内部的库存读取、风险档案读取等视为内部服务适配器，不计入这三个正式运维工具。

### 9.1 query_observability

用途：统一查询指标、日志和调用链。

输入：

```json
{
  "object_id": "inventory-agent",
  "start_time": "2026-09-27T20:00:00+08:00",
  "end_time": "2026-09-27T21:00:00+08:00",
  "data_types": ["metrics", "logs", "traces"],
  "filters": {}
}
```

输出：

```json
{
  "status": "success",
  "evidence": []
}
```

失败情况：

- 参数错误
- 查询超时
- 数据源不可用
- 时间范围过大
- 部分数据源成功、部分失败

### 9.2 query_service_context

用途：查询发布记录、配置变更、对象档案、依赖关系、负责人和运行手册。

输入：

```json
{
  "object_id": "inventory-agent",
  "include": [
    "deployments",
    "configuration",
    "dependencies",
    "owner",
    "runbook"
  ]
}
```

失败情况：

- 对象不存在
- 档案版本过旧
- 发布记录缺失
- 依赖信息不完整

### 9.3 execute_remediation

用途：执行模拟回滚、重启、后端切换或订单重放。

输入：

```json
{
  "action": "rollback",
  "target": "inventory-agent",
  "target_version": "v2.0",
  "approval_token": "APPROVAL-001",
  "reason_evidence_ids": ["E-101", "E-102", "E-103"],
  "idempotency_key": "ACTION-001"
}
```

无有效审批时必须返回：

```json
{
  "status": "rejected",
  "error_code": "HUMAN_APPROVAL_REQUIRED"
}
```

## 10. 五个必须记录的证据字段

每条诊断证据必须包含且只把以下五项称为“必须证据字段”：

| 字段 | 含义 |
| --- | --- |
| `evidence_id` | 证据唯一编号 |
| `time` | 证据时间点或时间窗口 |
| `source` | 数据类型、数据源和查询引用 |
| `object` | 相关服务、Agent、工具及其版本 |
| `fact` | 客观观察值、基线、状态或错误 |

示例：

```json
{
  "evidence_id": "E-101",
  "time": "2026-09-27T20:15:00+08:00",
  "source": {
    "type": "trace",
    "query_ref": "trace-order-882"
  },
  "object": {
    "id": "inventory-agent",
    "version": "v2.1"
  },
  "fact": {
    "observation": "库存查询耗时占订单总耗时的 82%",
    "duration_ms": 2350,
    "baseline_ms": 180
  }
}
```

额外审计字段不计入上述五项，但建议记录：

- `run_id`
- `trace_id`
- `model_provider`
- `model_name`
- `prompt_version`
- `agent_version`
- `tool_version`
- `retry_count`
- `approval_id`

## 11. 诊断输出契约

诊断结果必须是结构化数据，并通过 Pydantic 校验：

```json
{
  "incident_id": "INC-001",
  "facts": [
    {
      "statement": "inventory-agent P95 延迟超过阈值",
      "evidence_ids": ["E-101"]
    }
  ],
  "root_cause_candidates": [
    {
      "cause": "inventory-agent v2.1 引入错误连接配置",
      "confidence": 0.82,
      "evidence_ids": ["E-101", "E-102", "E-103"]
    }
  ],
  "missing_information": [],
  "recommended_actions": [
    {
      "action": "rollback",
      "target": "inventory-agent",
      "risk_level": "high",
      "requires_approval": true,
      "evidence_ids": ["E-101", "E-102", "E-103"]
    }
  ]
}
```

验证规则：

- 每个事实必须有至少一个 `evidence_id`。
- 每个根因候选必须有证据和 0 到 1 的置信度。
- 证据 ID 必须真实存在于当前事件的证据集合中。
- 数据不足时填写 `missing_information`。
- 高风险动作必须设置 `requires_approval=true`。

## 12. 主故障实验

默认故障场景：

> `inventory-agent` 从 v2.0 发布到 v2.1 后，库存查询耗时由约 180ms 上升至约 2300ms，导致订单端到端延迟升高并产生大量 pending 订单。

证据链：

1. 指标：订单 P95 延迟超过阈值。
2. 链路：主要耗时集中在 `inventory.check` span。
3. 日志：库存数据源出现连接等待或超时。
4. 发布记录：异常前发布了 `inventory-agent v2.1`。
5. 对象档案：库存 Agent 是订单创建的同步依赖。

处置流程：

```text
Detector 发现异常
  -> OpsGuardian 查询证据
  -> 输出根因候选和回滚建议
  -> ApprovalService 创建审批
  -> 人工确认
  -> RemediationExecutor 模拟回滚到 v2.0
  -> VerificationService 重新查询指标
  -> 指标恢复后重放 pending 订单
```

必须同时演示一次拒绝路径：没有审批令牌时，`execute_remediation` 返回 `HUMAN_APPROVAL_REQUIRED`。

## 13. 次要故障场景

至少选择一个作为自动化测试，可选作为课堂演示：

### 13.1 模型输出格式错误

- Pydantic 校验失败。
- 自动重试一次。
- 再次失败后进入规则降级或 `PENDING`。

### 13.2 证据查询超时

- `query_observability` 超时。
- 有限重试一次。
- 使用缓存证据或输出低置信度结果。
- 不得假装已经获得完整证据。

### 13.3 风控 Agent 超时

- 订单进入 `MANUAL_REVIEW`。
- 不允许自动创建订单。

### 13.4 缺失证据引用

- 诊断 Agent 输出根因但没有 `evidence_ids`。
- 验证器拒绝结果并要求重新诊断。

### 13.5 重复订单请求

- 使用同一幂等键重复提交。
- 系统不得重复扣减库存或创建多份订单。

## 14. 模型抽象与切换

Agent 只能依赖统一接口，不允许在每个 Agent 内硬编码厂商 SDK。

建议接口：

```python
class ModelProvider:
    def generate(self, messages, response_schema=None):
        raise NotImplementedError
```

实现：

```text
MockModelProvider
OpenAICompatibleProvider
FallbackModelProvider
```

默认开发配置：

```yaml
model:
  provider: mock
  base_url: null
  api_key_env: null
  model_name: mock-order-model
  timeout_seconds: 10
```

本地配置示例：

```yaml
model:
  provider: openai_compatible
  base_url: http://localhost:8000/v1
  api_key_env: LOCAL_MODEL_API_KEY
  model_name: local-model
  timeout_seconds: 30
```

云端配置示例：

```yaml
model:
  provider: openai_compatible
  base_url: https://provider.example/v1
  api_key_env: CLOUD_MODEL_API_KEY
  model_name: cloud-model
  timeout_seconds: 30
```

切换本地与云端时，Agent 代码不应变化。核心流程只使用普通消息、JSON 输出和 Python 工具调度，避免依赖某一厂商的专属会话、文件检索或工具调用协议。商品目录读取同样由 Python 将只读工具结果交给 Agent，不要求模型端支持厂商专属 Function Calling。

步骤 16 已实现上述配置切换。`api_key_env` 保存的是环境变量名称，而不是密钥本身；本地服务不需要鉴权时可设置为 `null`。例如在 PowerShell 中连接本地 OpenAI 兼容服务：

```powershell
$env:MODEL_PROVIDER = "openai_compatible"
$env:MODEL_BASE_URL = "http://localhost:8000/v1"
$env:MODEL_API_KEY_ENV = ""
$env:MODEL_NAME = "local-model"
$env:MODEL_TIMEOUT_SECONDS = "30"
python -m uvicorn order_agent_ops.main:app
```

Ollama 使用其 OpenAI 兼容入口，不直接使用原生 `/api/chat`。例如本机已有 `qwen3:1.7b` 时：

```powershell
$env:MODEL_PROVIDER = "openai_compatible"
$env:MODEL_BASE_URL = "http://localhost:11434/v1"
$env:MODEL_API_KEY_ENV = ""
$env:MODEL_NAME = "qwen3:1.7b"
$env:MODEL_TIMEOUT_SECONDS = "180"
.\.venv\Scripts\python.exe -m uvicorn order_agent_ops.main:app --port 8000
```

真实模型结构化输出首先使用 OpenAI 兼容的 `response_format=json_schema`，并把同一份 Pydantic JSON Schema 加入系统消息。若兼容端点明确以 400、404、415 或 422 表示不支持该格式，Provider 只回退一次到 `json_object`；Schema 指令、ModelGateway 的一次有限重试和最终 Pydantic 校验仍然保留。各 Agent Prompt 同时声明字段语义、状态约束和合法示例，并按任务独立管理版本；当前购物目录检索 Prompt 为 v3。

Ollama 端到端测试默认跳过，避免普通测试依赖本地模型。显式验收可运行：

```powershell
$env:RUN_OLLAMA_E2E_TESTS = "1"
$env:OLLAMA_TEST_BASE_URL = "http://localhost:11434/v1"
$env:OLLAMA_TEST_MODEL = "qwen3:1.7b"
$env:OLLAMA_TEST_TIMEOUT_SECONDS = "180"
.\.venv\Scripts\python.exe -m pytest tests\integration\test_openai_compatible_provider.py -k ollama -v
```

可选降级使用 `MODEL_FALLBACK_ENABLED`、`MODEL_FALLBACK_BASE_URL`、`MODEL_FALLBACK_API_KEY_ENV`、`MODEL_FALLBACK_NAME` 和 `MODEL_FALLBACK_TIMEOUT_SECONDS` 配置。主端点调用失败时才会访问备用端点，并记录 `model.fallback.activation` 和 `model.fallback.result` 指标以及不含密钥的降级日志。

真实端点测试默认跳过。只有显式设置 `RUN_REMOTE_MODEL_TESTS=1` 以及 `TEST_MODEL_BASE_URL`、`TEST_MODEL_NAME` 和可选的 `TEST_MODEL_API_KEY` 后，才会发起网络请求。

`ShoppingAssistantAgent` 首先使用 Mock Provider 完成可重复测试。Qwen 等模型只有在阶段 16 通过统一的 OpenAI 兼容 Provider 接入，不在 Agent 中硬编码 Qwen SDK、地址或模型名。Python 负责把 ProductCatalogTool 的只读结果交给 Agent，Agent 负责语义检索和匹配；多轮澄清与工具调用不依赖厂商专属 Function Calling。

## 15. 数据与存储

建议初版使用：

- SQLite：选购意图、候选快照、用户确认、订单、库存、事件、审批、动作和审计状态。
- JSON：带结构化属性的本地商品目录、服务档案、发布记录、模拟用户风险资料。
- JSONL：结构化运行日志和 trace 导出。
- 内存聚合或 SQLite：简单指标。

建议数据文件：

```text
data/
  products.json
  users.json
  service_profiles.json
  deployments.json
  historical_incidents.json
```

建议数据库实体：

- `purchase_intents`
- `orders`
- `inventory`
- `agent_runs`
- `tool_calls`
- `incidents`
- `evidence`
- `approvals`
- `remediation_actions`
- `audit_events`

`products.json` 在自然语言选购阶段扩充为至少 15 个商品，并为演示涉及的类别提供可比较属性，例如：

```json
{
  "sku": "SKU-101",
  "category": "keyboard",
  "name": "Silent Office Keyboard",
  "unit_price": "329.00",
  "available_quantity": 8,
  "active": true,
  "attributes": {
    "noise_level": "silent",
    "connection": ["wired"],
    "suitable_for": ["office", "programming"],
    "weight_grams": 680
  }
}
```

模型可以推断查询条件和解释候选，但商品事实以目录记录为准。用户确认后必须重新读取当前商品记录，不能使用模型输出或客户端回传的价格创建订单。

## 16. 故障注入

通过配置控制故障，不修改业务代码：

```yaml
fault_injection:
  inventory_agent:
    enabled: true
    version: v2.1
    delay_ms: 2300
    error_rate: 0.10
  risk_agent:
    enabled: false
    delay_ms: 0
    error_rate: 0
```

模拟回滚的本质是将运行版本和故障配置从 v2.1 切回 v2.0，并记录变更前后状态。不要执行真实系统命令或修改本机服务。

## 17. 推荐技术栈

- Python 3.11+
- FastAPI：HTTP API
- Pydantic：输入、输出和模型结果校验
- SQLite：状态存储
- `httpx`：模型和内部 HTTP 调用
- `pytest`：自动化测试
- 标准 `logging` + JSON Formatter：结构化日志
- 可选 Jinja2/原生 HTML：最小演示界面
- 可选 OpenTelemetry：如果时间充足再接入，不作为首个里程碑的前置条件

初版优先保证命令行和 API 可运行，再增加页面。

## 18. 建议目录结构

```text
.
|-- README.md
|-- pyproject.toml
|-- .env.example
|-- config/
|   |-- settings.yaml
|   `-- fault_scenarios.yaml
|-- data/
|   |-- products.json
|   |-- users.json
|   |-- service_profiles.json
|   |-- deployments.json
|   `-- historical_incidents.json
|-- src/
|   `-- order_agent_ops/
|       |-- api/
|       |   |-- orders.py
|       |   `-- shopping.py
|       |-- agents/
|       |   |-- shopping_assistant.py
|       |   |-- order_coordinator.py
|       |   |-- inventory_agent.py
|       |   |-- risk_agent.py
|       |   `-- ops_guardian.py
|       |-- models/
|       |   |-- gateway.py
|       |   |-- providers.py
|       |   `-- schemas.py
|       |-- business/
|       |   |-- product_search_adapter.py
|       |   |-- policy_engine.py
|       |   `-- order_executor.py
|       |-- services/
|       |   |-- shopping_workflow.py
|       |   `-- order_workflow.py
|       |-- ops/
|       |   |-- detector.py
|       |   |-- evidence_validator.py
|       |   |-- approval_service.py
|       |   |-- remediation_executor.py
|       |   `-- verification_service.py
|       |-- tools/
|       |   |-- product_catalog.py
|       |   |-- query_observability.py
|       |   |-- query_service_context.py
|       |   `-- execute_remediation.py
|       |-- telemetry/
|       |   |-- logging.py
|       |   |-- metrics.py
|       |   `-- tracing.py
|       |-- storage/
|       |-- domain/
|       |   `-- shopping.py
|       `-- config.py
|-- tests/
|   |-- unit/
|   |-- integration/
|   `-- scenarios/
|-- scripts/
|   |-- seed_demo_data.py
|   |-- run_happy_path.py
|   `-- run_inventory_latency_incident.py
`-- artifacts/
    |-- traces/
    |-- screenshots/
    `-- reports/
```

## 19. API

启动应用后访问 `http://127.0.0.1:8000/` 会跳转到最小运维页面，也可以直接访问 `http://127.0.0.1:8000/ops/ui`。页面通过下列 API 读取数据和提交操作，刷新本身不会改变业务状态。

### 自然语言选购接口

- `POST /shopping/intents`：创建自然语言选购意图；返回澄清问题或候选商品。
- `GET /shopping/intents/{intent_id}`：查询意图、结构化需求、候选和确认状态。
- `POST /shopping/intents/{intent_id}/messages`：提交澄清回答并重新分析、查询候选。
- `POST /shopping/intents/{intent_id}/confirm`：显式确认候选 SKU，由服务端构造 `OrderRequest` 并进入现有订单流程。

确认接口只接受 `intent_id`、候选 `sku`、数量和幂等键；客户端不得提交可信单价或总金额。意图不存在、已过期、尚需澄清或 SKU 不在已保存候选集合中时，返回稳定错误码且不创建订单。

### 订单接口

- `POST /orders`：创建订单请求。
- `GET /orders/{order_id}`：查询订单状态。
- `POST /orders/{order_id}/replay`：经授权后重放 pending 订单。

### 运维接口

- `GET /ops/health`：查看组件健康状态。
- `GET /ops/orders`：列出订单。
- `GET /ops/incidents`：列出事件。
- `GET /ops/incidents/{incident_id}`：查看证据和诊断。
- `POST /ops/incidents/{incident_id}/diagnose`：触发诊断。
- `POST /ops/incidents/{incident_id}/verify`：执行处置后验证。
- `GET /ops/evidence`：按事件或对象查询证据。
- `GET /ops/approvals`：列出审批。
- `POST /ops/approvals`：创建审批。
- `POST /ops/approvals/{approval_id}/decision`：确认或拒绝。
- `GET /ops/actions`：列出处置动作。
- `POST /ops/actions`：使用审批令牌执行动作。
- `GET /ops/traces/{trace_id}`：查看调用链。
- `POST /ops/detect`：运行确定性异常检测。
- `GET /ops/faults`：列出配置化故障场景。
- `POST /ops/faults/{scenario_name}/activate`：启用演示故障。
- `POST /ops/faults/{scenario_name}/deactivate`：停用演示故障。
- `POST /orders/{order_id}/replay`：使用独立审批令牌重放 pending 订单。

API 错误统一返回 `error_code` 和 `message`，可选返回 `trace_id`。审批明文令牌只在批准响应中出现一次；列表和页面不展示数据库中的令牌哈希。

### 可重复实验脚本

阶段 18 提供两个默认使用 Mock Provider 的独立脚本，不需要先启动 Uvicorn，也不需要 API Key。每次默认先重置指定的演示数据库，因此可以连续重复执行：

```powershell
.\.venv\Scripts\python.exe scripts\run_shopping_workflow.py
.\.venv\Scripts\python.exe scripts\run_inventory_latency_incident.py
```

正常脚本从自然语言需求开始，输出结构化意图、Agent 读取目录后选择的候选、确认的服务端价格、订单结果、trace、Agent 执行顺序和总耗时。故障脚本自动执行 inventory-agent v2.1 延迟注入、告警、证据诊断、无审批拒绝、人工审批、模拟回滚、恢复验证和 pending 订单重放。

自然语言购物脚本默认输出分阶段的人类可读终端报告，逐项显示 ShoppingAssistantAgent 的需求分析与 Agent 检索、ProductCatalogTool 的目录读取、订单三个 Agent、确定性策略与订单执行结果。分析部分展示的是经过结构化输出整理的可审计摘要，不展示或伪造模型原始思维链。需要把标准输出交给其他程序处理时，可以使用 `--output-format json` 恢复完整 JSON 输出：

```powershell
.\.venv\Scripts\python.exe scripts\run_shopping_workflow.py `
  --message "我想买一个1000元以内的无线机械键盘" `
  --output-format json
```

自然语言购物工作流每次运行都会扫描已有结果并分配下一个编号，报告和 trace 使用相同编号，因此不会覆盖之前的运行结果。生成文件默认被 Git 忽略，仅保留目录占位文件：

```text
artifacts/traces/shopping_workflow_trace_001.json
artifacts/reports/shopping_workflow_report_001.json
artifacts/traces/shopping_workflow_trace_002.json
artifacts/reports/shopping_workflow_report_002.json
artifacts/traces/inventory_latency_incident_traces.json
artifacts/reports/inventory_latency_incident_report.json
```

`happy path` 是软件测试中表示“无异常的标准成功路径”的术语；导出的正式实验材料使用更明确的 `shopping_workflow` 名称。旧版 `happy_path_report.json` 和 `happy_path_trace.json` 不会被新脚本覆盖。
`scripts/run_happy_path.py` 继续作为兼容入口保留，正式演示建议使用 `scripts/run_shopping_workflow.py`。

只重置数据库，或同时清理已知的演示生成文件（包括所有编号的购物工作流结果）：

```powershell
.\.venv\Scripts\python.exe scripts\reset_demo.py
.\.venv\Scripts\python.exe scripts\reset_demo.py --clear-artifacts
```

重置脚本只删除明确指定的 SQLite 文件和上述已知 JSON 产物，不会修改商品目录、配置或源代码。脚本失败时返回非零退出码，并输出 `stage` 和错误信息。正常脚本还可使用 `--base-url http://127.0.0.1:8000` 调用已启动的 API；外部 API 请求默认等待 180 秒，可用 `--request-timeout` 调整。使用本地小模型时建议设置为 240 秒。故障脚本固定使用隔离的进程内 Mock 应用，以便导出完整审计材料。

## 20. 分阶段实现计划

### 阶段 0：项目骨架

- 创建 Python 项目和依赖。
- 创建配置加载、日志和测试框架。
- 创建 `.env.example`，禁止提交真实 API Key。
- 默认使用 Mock 模型，确保无 API Key 也能运行测试。

完成标准：`pytest` 能运行，应用能启动并返回健康检查。

### 阶段 1：领域模型和存储

- 定义订单、库存、Agent 运行、证据、事件、审批和动作的 Pydantic 模型。
- 实现 SQLite 初始化和 Repository。
- 实现订单与事件状态机。

完成标准：状态转换有单元测试，非法转换会被拒绝。

### 阶段 2：业务流程

- 实现 MockModelProvider。
- 实现三个订单处理 Agent。
- 实现 OrderPolicyEngine 和 OrderExecutorService。
- 实现幂等订单创建。
- 在订单闭环稳定后实现 ShoppingAssistantAgent、结构化意图、商品候选与显式确认，再复用已有订单入口。

完成标准：正常订单能够端到端完成；库存不足和高风险订单走正确分支；自然语言需求可以经过澄清和候选确认安全转换为同一个 `OrderRequest`。

### 阶段 3：可观测性

- 为每个 Agent、工具和状态转换生成结构化日志。
- 创建 trace/span 数据结构。
- 聚合延迟、成功率、失败率和重试次数。
- 记录模型、Prompt、Agent 和工具版本。

完成标准：给定 `trace_id` 能还原一次订单的完整执行路径。

### 阶段 4：运维查询工具

- 实现 `query_observability`。
- 实现 `query_service_context`。
- 实现五字段证据结构。
- 实现证据查询和存在性校验。

完成标准：能够根据时间范围和对象返回真实存在的证据。

### 阶段 5：异常检测与诊断

- 实现规则型 AnomalyDetector。
- 实现 OpsGuardianAgent。
- 实现诊断输出 Pydantic Schema。
- 实现 EvidenceValidator。

完成标准：缺少证据引用的诊断会被拒绝；库存延迟故障能够生成正确候选。

### 阶段 6：安全处置与恢复

- 实现 ApprovalService。
- 实现 `execute_remediation`。
- 实现 RemediationExecutor。
- 实现 VerificationService。
- 实现 pending 订单重放。

完成标准：无审批令牌的回滚被拒绝；批准后可回滚、验证并重放。

### 阶段 7：模型切换

- 实现 OpenAI 兼容 Provider。
- 通过配置支持本地和云端模型。
- 实现超时、有限重试和可选 fallback。
- 保证所有 Provider 返回统一结果或统一错误。

完成标准：切换 Provider 不修改 Agent 代码；Mock 模式始终可用于测试。

### 阶段 8：演示和报告材料

- 提供正常流程脚本。
- 提供库存 Agent 延迟故障脚本。
- 保存指标、日志、链路、审批和回滚结果。
- 生成端到端架构图。
- 准备报告截图和课堂演示步骤。

完成标准：演示可以重复运行，不依赖临时手工修改数据。

## 21. 测试清单

### 单元测试

- 自然语言意图 Schema、缺失信息验证，以及模型误报冲突不会阻断搜索的验证。
- 隐含表达到结构化属性的语义映射，例如“别影响室友”映射为“低噪音”。
- Agent 读取目录后的语义匹配、候选排序和无匹配结果。
- 商品目录工具不做语义匹配，且只能返回真实目录事实。
- Pydantic Schema 验证。
- 订单状态机。
- 策略引擎决策。
- 幂等键处理。
- 证据 ID 存在性校验。
- 风险动作识别。
- 审批令牌验证。
- 故障配置加载。

### 集成测试

- 模糊需求返回澄清问题，不创建订单。
- 候选商品全部来自 Agent 本次读取的本地目录；模型编造或不符合类别、预算、启用状态、库存边界的 SKU 会被丢弃，价格、库存和属性由目录事实覆盖。
- 用户确认前不扣库存；确认后使用服务端价格进入现有订单工作流。
- 同一个确认幂等键不会创建重复订单。
- 正常订单完成。
- 库存不足被拒绝。
- 风控高风险进入人工审核。
- 库存 Agent 超时后进入 pending。
- 模型输出格式错误时有限重试。
- 运维助手查询到指标、日志和链路证据。
- 根因候选引用真实证据。
- 无审批回滚被拒绝。
- 审批后回滚成功。
- 回滚后指标恢复。
- pending 订单可以安全重放。
- 相同幂等键不会重复创建订单。
- 切换 Mock/Local/Cloud Provider 不修改 Agent 代码。

## 22. 最终验收清单

- [ ] 四个职责清晰的业务 Agent：一个购物助手和三个订单处理 Agent。
- [ ] 自然语言需求能够进行语义分析、缺失信息澄清和候选解释；商品偏好冲突不在意图阶段阻断，而在候选理由中说明取舍。
- [ ] 候选商品来自可校验目录，用户确认前不创建订单，模型不能决定可信价格。
- [ ] Agent、数据平台、模型服务和运维平台边界清楚。
- [ ] 一张端到端架构图。
- [ ] 三个正式工具定义。
- [ ] 五个必须证据字段。
- [ ] 每个诊断结论都引用真实证据。
- [ ] 至少一个 Agent、工具或协作环节故障。
- [ ] 支持超时、有限重试和安全降级。
- [ ] 至少一个危险动作需要人工确认。
- [ ] 无审批时危险动作被系统强制拒绝。
- [ ] 支持模拟回滚和 pending 订单重放。
- [ ] 动作执行后有验证步骤。
- [ ] 记录模型、Prompt、Agent 和工具版本。
- [ ] 模型 Provider 可以通过配置切换。
- [ ] 有正常路径和故障路径自动化测试。
- [ ] 报告包含问题、设计实现、结果解释和大模型使用声明。

## 23. 给后续编码模型的开发约束

1. 先实现最小闭环，不要一开始增加微服务、消息队列或复杂前端。
2. 默认使用 `MockModelProvider`，保证没有本地模型和 API Key 时也能开发和测试。
3. 所有 Agent 输出必须结构化并经过 Pydantic 校验。
4. Agent 不能直接修改库存、订单最终状态或执行危险运维动作。
5. 权限、审批、幂等、状态机、重试和回滚必须由确定性代码实现。
6. 不允许模型生成不存在的证据；证据 ID 必须由平台产生并校验。
7. 不允许无限重试；每个重试点必须有明确上限和失败状态。
8. 故障注入必须配置化，并能一键恢复到正常状态。
9. 模拟回滚只修改项目内的模拟版本和状态，不执行真实系统管理命令。
10. 不在源代码、日志、测试数据或 README 中写入真实密钥。
11. 不依赖某个云端厂商的专属能力完成核心流程。
12. 每完成一个阶段，补充对应测试和 README 状态说明。
13. 优先使用清晰、可审计的代码，而不是追求过度自主的 Agent 行为。
14. 任何高风险动作都必须先经过 `ApprovalService`。
15. 不把“模型生成的解释”当成事实；事实必须来自数据平台和工具结果。
16. ShoppingAssistantAgent 可以从 ProductCatalogTool 返回的目录事实中选择 SKU 并生成候选解释，但不能创建 SKU，也不能生成可信价格、库存、属性或直接订单。
17. 商品确认必须绑定已保存的候选集合，由服务端重新读取价格并复用现有 `OrderWorkflow`。

## 24. 推荐演示顺序

```text
1. 展示架构和四类边界。
2. 输入一个包含隐含偏好或缺失条件的自然语言需求；偏好无法同时满足时展示候选取舍。
3. 展示 ShoppingAssistantAgent 的结构化理解、澄清问题和候选取舍说明。
4. 用户确认目录中的候选商品，进入同一个订单处理流程。
5. 展示四个业务 Agent 的协作 trace 和正常订单结果。
6. 开启 inventory-agent v2.1 延迟故障。
7. 再运行订单，展示延迟和 pending 状态。
8. 展示异常检测器生成告警。
9. 运维助手查询证据并生成根因候选。
10. 展示结论到 evidence_id 的映射。
11. 尝试无审批回滚，展示系统拒绝。
12. 人工确认后执行模拟回滚。
13. 重新查询指标，验证延迟恢复。
14. 重放 pending 订单并展示成功结果。
15. 展示模型、Prompt、Agent 和工具版本记录。
```

## 25. 报告建议结构

### 问题描述

- 自然语言选购与简化订单场景。
- 多智能体系统目标。
- 运维助手需要解决的问题。
- 选择该故障场景的原因。

### 设计和实现

- 端到端架构图。
- Agent 职责和协作方式。
- 自然语言语义理解、需求澄清、商品事实校验和用户确认边界。
- 数据、模型和运维平台边界。
- 三个工具定义。
- 五个证据字段。
- 人工审批、回滚和验证机制。

### 结果和解释

- 正常订单实验。
- 库存 Agent 延迟故障实验。
- 指标、日志和链路证据。
- 根因候选和处置结果。
- 系统不足与误判风险。

### 大模型使用声明

- 使用的模型或工具。
- 大模型辅助生成的内容。
- 人工修改和验证的内容。
- 如果未使用大模型，也要明确声明。

## 26. 当前默认决策

- 业务范围：自然语言选购、本地商品候选确认和简化订单处理；不包含真实支付或外部商城自动下单。
- 业务 Agent 数量：4，其中一个购物助手 Agent，三个订单处理 Agent。
- 运维助手 Agent 数量：1。
- 模型调用方式：统一 Model Gateway。
- 开发默认模型：Mock。
- 本地模型候选：OpenAI 兼容本地服务。
- 云端模型候选：OpenAI 兼容云端服务。
- Qwen 接入方式：阶段 16 通过 OpenAI 兼容 Provider 配置切换，阶段 9A 继续使用 Mock 保证离线测试。
- 商品来源：带结构化属性的本地模拟商品目录；ShoppingAssistantAgent 通过只读 ProductCatalogTool 读取并负责语义检索，工具不负责匹配。
- 商品确认：必须由用户显式确认，服务端重新读取价格并构造 `OrderRequest`。
- 数据存储：SQLite + JSON/JSONL。
- API：FastAPI。
- 前端：非首要，后续增加最小页面。
- 主故障：inventory-agent v2.1 延迟异常。
- 高风险动作：回滚 inventory-agent 到 v2.0。
- 恢复动作：验证指标后重放 pending 订单。

