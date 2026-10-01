# 开发步骤与 AI 实施清单

> 本文档用于把项目逐步交给编码 AI 实现。项目总体设计、边界和验收要求见 `README.md`。本文档只规定开发顺序、每一步的交付物和验收方式。

## 0. 使用方法

每次只让 AI 完成一个步骤，不要一次要求实现整个项目。

建议每次给 AI 的固定前置指令：

```text
请先完整阅读 README.md 和 DEVELOPMENT_PLAN.md，检查当前仓库状态和已有测试。
本次只实施指定步骤，不提前实现后续阶段，不删除或重写已经通过测试的功能。
完成后运行与本步骤相关的测试，并汇报：
1. 修改了哪些文件；
2. 实现了什么；
3. 运行了哪些测试及结果；
4. 还有什么限制；
5. 是否满足本步骤的验收条件。
如果发现设计冲突，先说明冲突，不要自行扩大范围。
```

每一步完成后：

1. 检查代码和测试结果。
2. 将本步骤前的 `[ ]` 修改为 `[x]`。
3. 在“实施记录”中写入日期和简短说明。
4. 确认验收通过后再做下一步。

## 全局开发规则

- 默认使用 Mock 模型，任何阶段都不应因为没有 API Key 或本地模型而无法测试。
- 所有 Agent 输出必须使用 Pydantic Schema 校验。
- Agent 不直接修改库存、订单最终状态或执行高风险运维动作。
- 权限、审批、幂等、状态机、重试、回滚和验证使用确定性代码实现。
- 不允许无限重试。
- 不允许模型创建虚假的证据 ID。
- 购物助手可以从只读目录工具返回的事实中选择 SKU，但不允许创建 SKU、可信价格、库存、属性或直接订单；商品事实必须来自商品目录。
- 自然语言选购必须经过候选校验和用户显式确认，确认服务再用服务端价格构造 `OrderRequest`。
- 不提交真实密钥、个人数据或机器相关绝对路径。
- Windows、Linux 下的路径处理使用 `pathlib.Path`。
- 时间统一存储为带时区的 ISO 8601 字符串；默认演示时区可以是 `Asia/Shanghai`。
- ID 使用稳定前缀，例如 `REQ-`、`ORD-`、`RUN-`、`TRACE-`、`INC-`、`E-`、`APPROVAL-`、`ACTION-`。
- 每个阶段至少包含与新增功能对应的测试。
- 先完成 API 和脚本，再考虑图形界面。
- 不在前期引入消息队列、容器编排或真实监控平台。

## 阶段总览

| 阶段 | 内容 | 完成状态 |
| --- | --- | --- |
| 1 | 项目骨架与测试入口 | [x] |
| 2 | 配置系统和领域 Schema | [x] |
| 3 | SQLite 存储和状态机 | [x] |
| 4 | 结构化日志与调用链基础 | [x] |
| 5 | ModelProvider 和 Mock 模型 | [x] |
| 6 | 模拟数据和业务数据适配器 | [x] |
| 7 | 三个业务 Agent | [x] |
| 8 | 订单规则引擎与执行服务 | [x] |
| 9 | 正常订单端到端 API | [x] |
| 9A | 自然语言选购、候选商品与用户确认 | [x] |
| 10 | 故障注入机制 | [x] |
| 11 | 指标聚合与异常检测器 | [x] |
| 12 | 五字段证据和两个只读运维工具 | [x] |
| 13 | 运维助手与诊断验证 | [x] |
| 14 | 审批和危险动作执行 | [x] |
| 15 | 处置后验证和订单重放 | [x] |
| 16 | 本地/云端模型切换 | [x] |
| 17 | 运维 API 和最小演示界面 | [x] |
| 18 | 完整实验脚本与测试 | [x] |
| 19 | 架构图、报告材料和最终清理 | [ ] |

---

## 阶段 1：项目骨架与测试入口

- [x] 完成阶段 1

### 目标

建立最小可运行的 Python 项目，应用可以启动，测试可以执行。

### 需要创建

```text
pyproject.toml
.gitignore
.env.example
config/settings.yaml
src/order_agent_ops/__init__.py
src/order_agent_ops/main.py
src/order_agent_ops/config.py
tests/test_health.py
```

### 实现要求

- Python 版本要求写入 `pyproject.toml`，建议 3.11 或更高。
- 添加 FastAPI、Pydantic、httpx、PyYAML、pytest 等最小依赖。
- `main.py` 暴露 FastAPI `app`。
- 实现 `GET /health`，返回应用名称、版本和 `ok` 状态。
- `config.py` 能读取 `config/settings.yaml`，但此阶段不需要复杂环境变量覆盖。
- `.env.example` 只放变量名和示例值，不包含真实密钥。
- `.gitignore` 忽略虚拟环境、缓存、数据库、日志、`.env` 和生成物。

### 验收条件

- `python -m pytest` 通过。
- FastAPI 应用可导入。
- `/health` 返回 200。
- 没有 API Key 时测试仍然通过。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 1：创建 Python 项目骨架、配置加载入口、FastAPI health 接口和首个测试。只做阶段 1，不实现订单、Agent 或数据库逻辑。完成后运行 python -m pytest。
```

---

## 阶段 2：配置系统和领域 Schema

- [x] 完成阶段 2

### 目标

定义项目所有核心输入输出契约，让后续模块围绕稳定 Schema 开发。

### 建议创建

```text
src/order_agent_ops/domain/enums.py
src/order_agent_ops/domain/orders.py
src/order_agent_ops/domain/agents.py
src/order_agent_ops/domain/evidence.py
src/order_agent_ops/domain/incidents.py
src/order_agent_ops/domain/approvals.py
tests/unit/test_domain_schemas.py
tests/unit/test_config.py
```

### 必须定义

- `OrderStatus`
- `IncidentStatus`
- `RiskLevel`
- `ActionRiskLevel`
- `OrderRequest`
- `OrderRecord`
- `InventoryCheckResult`
- `RiskCheckResult`
- `AgentRunRecord`
- `ToolCallRecord`
- `EvidenceRecord`
- `RootCauseCandidate`
- `DiagnosisResult`
- `ApprovalRequest`
- `RemediationAction`

### 关键规则

- 金额不得为负数。
- 商品数量必须大于 0。
- 置信度必须在 0 到 1 之间。
- 高风险动作必须设置 `requires_approval=true`。
- `EvidenceRecord` 必须含五个顶层字段：`evidence_id`、`time`、`source`、`object`、`fact`。
- 诊断结论必须能够关联 `evidence_ids`。
- 所有状态使用 Enum，不使用任意字符串。

### 验收条件

- 合法示例可以通过校验。
- 非法数量、金额、置信度和高风险动作会被拒绝。
- Evidence Schema 的五个顶层字段有测试锁定。
- 配置支持从环境变量覆盖模型 Provider、地址和模型名。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 2：定义配置系统和全部核心 Pydantic Schema。严格遵守 README 中的五字段证据结构和安全约束。不要实现数据库、Agent 或 API。补充完整单元测试并运行 python -m pytest。
```

---

## 阶段 3：SQLite 存储和状态机

- [x] 完成阶段 3

### 目标

实现可审计、可测试的本地状态存储和合法状态转换。

### 建议创建

```text
src/order_agent_ops/storage/database.py
src/order_agent_ops/storage/repositories.py
src/order_agent_ops/business/order_state_machine.py
src/order_agent_ops/ops/incident_state_machine.py
tests/unit/test_order_state_machine.py
tests/unit/test_incident_state_machine.py
tests/integration/test_repositories.py
```

### 实现要求

- 使用 SQLite；测试使用临时数据库。
- 初版可以使用标准库 `sqlite3`，不要为了简单项目引入复杂 ORM。
- 至少保存订单、库存、Agent 运行、工具调用、事件、证据、审批、处置和审计记录。
- Repository 封装 SQL，业务代码不直接写 SQL。
- 状态机拒绝非法转换。
- 所有写操作记录时间。
- 数据库初始化必须幂等。

### 验收条件

- 数据库可以重复初始化。
- Repository 可以完成基本增删查改。
- 正常状态转换通过，非法状态转换抛出明确业务异常。
- 测试结束不在项目目录留下测试数据库。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 3：实现 SQLite 初始化、Repository、订单状态机和事件状态机。使用临时数据库测试，禁止提前实现 Agent 和 API。运行全部测试。
```

---

## 阶段 4：结构化日志与调用链基础

- [x] 完成阶段 4

### 目标

任何一次订单运行都能通过 `trace_id` 还原 Agent、工具和状态转换过程。

### 建议创建

```text
src/order_agent_ops/telemetry/logging.py
src/order_agent_ops/telemetry/tracing.py
src/order_agent_ops/telemetry/metrics.py
tests/unit/test_tracing.py
tests/unit/test_metrics.py
```

### 实现要求

- 日志输出 JSON。
- 实现轻量 `TraceRecorder` 和 span context manager。
- 每个 span 至少包含 `trace_id`、`span_id`、`parent_span_id`、组件、动作、开始时间、结束时间、耗时和状态。
- 记录异常类型，但避免保存密钥和完整敏感输入。
- 指标至少支持 count、error count 和 duration 聚合。
- 暂不要求接入 OpenTelemetry。

### 验收条件

- 嵌套 span 的父子关系正确。
- 发生异常时 span 状态为 error，且异常继续向上抛出。
- 能按 `trace_id` 查询完整链路。
- 能计算成功率和 P95 延迟。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 4：实现本地结构化日志、轻量调用链和指标聚合。要求不依赖外部监控平台，并为异常、嵌套 span 和 P95 统计增加测试。
```

---

## 阶段 5：ModelProvider 和 Mock 模型

- [x] 完成阶段 5

### 目标

建立与具体模型厂商解耦的模型接口，并用 Mock 模型支持稳定测试。

### 建议创建

```text
src/order_agent_ops/models/provider.py
src/order_agent_ops/models/gateway.py
src/order_agent_ops/models/mock_provider.py
src/order_agent_ops/models/errors.py
tests/unit/test_model_gateway.py
```

### 实现要求

- 定义 `ModelProvider` 抽象接口。
- `ModelGateway` 统一处理超时、一次有限重试、结构化输出校验和遥测。
- `MockModelProvider` 根据任务类型返回固定、可配置结果。
- Mock 支持模拟超时、非法 JSON 和异常。
- Provider 错误统一映射为项目内部异常类型。
- 不接入真实云端或本地模型。

### 验收条件

- Mock 模型可以返回三个业务 Agent 和运维助手所需的结构化结果。
- 模拟一次失败后重试成功。
- 连续失败后不会无限重试。
- 模型调用记录 Provider、模型名、Prompt 版本、耗时和结果状态。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 5：实现 ModelProvider、ModelGateway 和可配置 MockModelProvider。只使用 Mock，不接入真实 API。加入超时、一次重试、非法 JSON 和 Pydantic 校验测试。
```

---

## 阶段 6：模拟数据和业务数据适配器

- [x] 完成阶段 6

### 目标

提供订单系统运行需要的确定性模拟数据和只读适配器。

### 建议创建

```text
data/products.json
data/users.json
data/service_profiles.json
data/deployments.json
data/historical_incidents.json
src/order_agent_ops/business/inventory_adapter.py
src/order_agent_ops/business/risk_profile_adapter.py
scripts/seed_demo_data.py
tests/unit/test_business_adapters.py
```

### 实现要求

- 至少准备两个商品、三个用户风险档案。
- 库存适配器提供查询，不直接扣减。
- 风险档案适配器只返回事实数据。
- 提供正常版本 `inventory-agent v2.0` 的服务档案和发布记录。
- 脚本可以重复运行，不产生重复数据。
- 路径相对于项目根目录或配置目录解析。

### 验收条件

- 查询存在和不存在的 SKU 都有明确结果。
- 查询用户档案返回稳定结构。
- 重复执行 seed 脚本结果一致。
- 测试不依赖网络。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 6：创建最小演示数据、幂等 seed 脚本、库存只读适配器和风险档案适配器。不要实现 Agent。增加无网络单元测试。
```

---

## 阶段 7：三个业务 Agent

- [x] 完成阶段 7

### 目标

实现订单协调、库存和风控三个 Agent，并保证输出结构化、可追踪。

### 建议创建

```text
src/order_agent_ops/agents/base.py
src/order_agent_ops/agents/order_coordinator.py
src/order_agent_ops/agents/inventory_agent.py
src/order_agent_ops/agents/risk_agent.py
tests/unit/test_business_agents.py
```

### 实现要求

- Agent 通过依赖注入接收 `ModelGateway` 和适配器。
- Agent 不直接创建新的模型客户端。
- 每个 Agent 有名称、版本、Prompt 版本和输出 Schema。
- 协调 Agent 可以并行调度库存和风控检查，或先用简单异步并发实现。
- 库存查询失败返回 `unknown`/`timeout`，不得默认放行。
- 风控不确定返回 `manual_review`。
- 每个 Agent 运行产生 span 和 `AgentRunRecord`。

### 验收条件

- 三个 Agent 在 Mock 模式下可以独立测试。
- 输出均通过 Pydantic 校验。
- 并行检查共享同一个 `trace_id`，但有不同 span。
- 任何 Agent 失败都会产生可定位记录。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 7：实现三个业务 Agent。使用依赖注入和 MockModelProvider，所有输出通过 Pydantic，所有运行进入 trace。不要实现订单写入和运维助手。
```

---

## 阶段 8：订单规则引擎与执行服务

- [x] 完成阶段 8

### 目标

用确定性代码将 Agent 的判断转换为安全订单状态。

### 建议创建

```text
src/order_agent_ops/business/policy_engine.py
src/order_agent_ops/business/order_executor.py
tests/unit/test_policy_engine.py
tests/integration/test_order_executor.py
```

### 实现要求

- 策略引擎不调用模型。
- 库存不足进入 `REJECTED`。
- 库存未知或超时进入 `PENDING`。
- 风险高或未知进入 `MANUAL_REVIEW`。
- 只有库存充足且风险通过时才能创建订单。
- 执行服务使用幂等键。
- 库存预留和订单创建应在一个可补偿流程中完成。
- 重复请求不能重复扣减库存。

### 验收条件

- 决策矩阵全部有测试。
- 重复幂等键只产生一个订单。
- 模拟订单写入失败时库存不会永久重复扣减。
- Agent 无法绕过策略引擎直接写最终状态。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 8：实现确定性的 OrderPolicyEngine 和 OrderExecutorService，包括幂等、库存预留、失败补偿和完整决策矩阵测试。不要新增模型调用。
```

---

## 阶段 9：正常订单端到端 API

- [x] 完成阶段 9

### 目标

打通第一条可演示的正常订单闭环。

### 建议创建或修改

```text
src/order_agent_ops/api/orders.py
src/order_agent_ops/services/order_workflow.py
src/order_agent_ops/main.py
scripts/run_happy_path.py
tests/integration/test_order_api.py
```

### 实现要求

- `POST /orders` 创建订单请求。
- `GET /orders/{order_id}` 查询订单。
- API 返回 `trace_id`。
- 工作流依次组织协调 Agent、库存/风控 Agent、策略引擎和执行服务。
- API 错误使用稳定错误码，不返回内部堆栈。
- 提供命令行演示脚本。

### 验收条件

- 正常订单达到 `COMPLETED`。
- 库存不足达到 `REJECTED`。
- 高风险用户达到 `MANUAL_REVIEW`。
- 根据响应中的 `trace_id` 可以查到完整链路。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 9：打通正常订单端到端 API 和命令行演示脚本。覆盖完成、库存不足和高风险三种路径，返回 trace_id，并运行集成测试。
```

---

## 阶段 9A：自然语言选购、候选商品与用户确认

- [x] 完成阶段 9A

### 目标

在已经稳定的结构化订单入口之前增加一个自然语言选购层。用户可以描述用途、预算和偏好；系统能够理解隐含需求、发现缺失信息、查询本地商品目录并解释候选。意图阶段不执行约束冲突检测，无法同时满足的商品偏好在候选理由中说明取舍。只有用户显式确认候选后，系统才使用服务端商品事实构造 `OrderRequest` 并进入阶段 9 的现有订单工作流。

本阶段只新增一个业务 Agent：`ShoppingAssistantAgent`。当前增强方案由该 Agent 主动调用只读 `ProductCatalogTool`，并负责商品语义检索、候选选择和排序；工具只读取目录事实，不做匹配。候选事实校验、价格计算、确认状态和订单转换仍由确定性代码负责。

### 建议创建或修改

```text
data/products.json
src/order_agent_ops/domain/shopping.py
src/order_agent_ops/agents/shopping_assistant.py
src/order_agent_ops/business/product_search_adapter.py
src/order_agent_ops/tools/product_catalog.py
src/order_agent_ops/services/shopping_workflow.py
src/order_agent_ops/api/shopping.py
src/order_agent_ops/models/provider.py
src/order_agent_ops/models/mock_provider.py
src/order_agent_ops/storage/database.py
src/order_agent_ops/storage/repositories.py
src/order_agent_ops/main.py
tests/unit/test_shopping_assistant.py
tests/unit/test_product_search.py
tests/integration/test_shopping_api.py
```

### 必须定义的结构化契约

- `PurchaseIntentStatus`：至少包含 `NEEDS_CLARIFICATION`、`SEARCHING`、`READY_FOR_CONFIRMATION`、`CONFIRMED`、`EXPIRED`。
- `PurchaseIntentRequest`：用户、自然语言消息和可选会话引用。
- `ParsedPurchaseIntent`：商品类别、数量、预算、用途、硬性约束、软偏好、推断需求和缺失信息；`constraint_conflicts` 为接口兼容保留，但当前固定为空列表。
- `ProductCandidate`：目录中的真实 SKU、名称、服务端价格、属性、匹配项和不满足项。
- `ProductRecommendation`：推荐候选、备选、取舍说明和引用的候选 SKU。
- `PurchaseIntentRecord`：原始输入、结构化意图、候选快照、状态、模型/Prompt 版本、trace 和时间。
- `PurchaseConfirmationRequest`：候选 SKU、数量和幂等键，不接受可信单价或总金额。

### ShoppingAssistantAgent 要求

- 通过依赖注入使用现有 `ModelGateway`，不得直接创建 Qwen 或其他厂商客户端。
- 同一个 Agent 支持两个主流程结构化任务：`analyze_purchase_intent` 和 `search_product_catalog`，分别记录 Prompt 版本、模型调用和 span。
- 能将隐含表达映射为可解释属性，例如“晚上不影响室友”推断为“低噪音”，“经常出差”推断为“轻便或便携”。
- 明确区分安全边界与商品特征偏好，不得把所有描述都降级成关键词。
- 信息不足时返回一个最关键的澄清问题；不得猜测预算、数量或关键用途。
- `constraint_conflicts` 仅为既有 Schema 兼容而保留并固定为空列表；不得因模型误报商品偏好冲突而阻断目录检索，无法同时满足的条件由推荐理由说明取舍。
- 检索阶段主动调用 `ProductCatalogTool`，同时读取用户原始表达、结构化意图和完整目录事实，并综合商品描述与异构扩展属性进行语义匹配，而不是只依赖固定属性键相等。
- Agent 输出中引用的 SKU 必须属于本次工具返回的目录快照；代码必须拒绝不存在、停用、库存不足、超预算或类别错误的 SKU，并用目录事实填充名称、价格、库存和属性。
- 模型生成不存在的 SKU、价格、库存或商品属性时，工作流必须拒绝结果或使用确定性事实覆盖，不能将其展示为真实商品事实。

### 商品目录工具与 Agent 检索要求

- 将本地商品目录扩充到至少 15 个商品，并为演示类别提供足够区分度；至少包含类别、价格、库存、启用状态和可比较属性。
- `ProductCatalogTool` 不调用模型，只返回版本化的本地目录快照，不执行语义匹配和评分。
- ShoppingAssistantAgent 负责根据原始用户表达、结构化需求、商品描述及所有扩展属性选择和排序最多五个候选。
- 原始用户表达是语义判断依据；若结构化意图中出现明显不合理的属性和值组合，检索任务应结合原文与商品事实纠正理解，而不是机械执行错误字段映射。
- 关键字匹配可以作为 Agent 判断依据之一，但不能作为唯一的需求理解能力。
- Agent 选择后由确定性代码执行事实与安全校验：SKU 存在、商品启用、类别正确、价格不超预算且库存满足数量；语义匹配本身不交给外部适配器。
- 模型输出保持适合本地小模型的最小结构：最多五个按推荐顺序排列的 `selected_skus` 和一个整体 `reason`，两项均为 JSON Schema 必填字段。
- 推荐 Prompt 必须声明只有 `messages` 是用户原话，并要求 `reason` 逐项解释预算、明确或隐含特征及近似候选的具体取舍；目录 SKU 不得被描述成用户输入。
- 若 `reason` 虚构“用户提供/指定了目录 SKU”，确定性校验应附带纠错上下文重试一次；再次违规则拒绝本次模型输出，不得把错误理由展示给用户。
- 面向小模型的目录上下文可采用不丢失候选的紧凑事实表；确定性代码只规范化可唯一对应的 SKU，并校验首选商品理由是否与目录事实一致。特征不完全满足时允许继续推荐；模型纠错后仍遗漏条件或取舍时，使用可信目录事实生成逐项说明，避免仅因解释质量导致工作流失败。
- 类别、预算、启用状态和库存不得放宽；机械、无线、静音、材质等商品特征允许近似匹配，完全满足者优先。没有通过安全边界的商品时必须明确返回无匹配原因。
- 原 `ProductSearchAdapter.search()` 为兼容旧调用与既有测试保留，但正常购物工作流不再调用；确认阶段仍重新读取当前商品事实。

### 多轮澄清与确认要求

- `POST /shopping/intents` 创建意图并返回澄清问题或候选。
- `GET /shopping/intents/{intent_id}` 查询当前意图、候选和状态。
- `POST /shopping/intents/{intent_id}/messages` 提交澄清回答，并保留此前用户条件。
- `POST /shopping/intents/{intent_id}/confirm` 只能确认已保存候选集合中的 SKU。
- 意图、候选快照和确认状态保存到 SQLite；数据库初始化继续保持幂等。
- 确认前不得创建订单或扣减库存。
- 确认时重新读取当前商品记录，由服务端计算 `unit_price` 和 `total_amount`，客户端价格字段不得参与可信计算。
- 意图不存在、已过期、仍需澄清、SKU 不属于候选、商品已停用或价格/库存已变化时，返回稳定错误码。
- 确认成功后调用阶段 9 已有 `OrderWorkflow.submit()`，不得复制订单规则、直接调用仓储或绕过库存与风险检查。
- 确认请求使用幂等键；重复确认返回原订单，不得重复扣减库存。

### 模型与可观测性要求

- 本阶段只使用扩展后的 `MockModelProvider`，测试不依赖网络、API Key 或本地模型。
- Qwen 的真实接入保留到阶段 16，通过统一 OpenAI 兼容 Provider 完成；不得在本阶段硬编码 Qwen SDK。
- 自然语言分析、商品查询、候选排序、用户确认和后续订单处理共享或关联可追踪的 `trace_id`。
- 每次购物助手运行产生 `AgentRunRecord`；失败、重试、结构化校验失败和澄清路径都可定位。
- 不记录不必要的完整敏感自然语言；日志只保留定位所需的摘要或引用。

### 验收条件

- “晚上在宿舍使用，别影响室友”能够产生带原因的“低噪音”需求，而不是只匹配原句关键词。
- “想买一个好一点的键盘”会要求补充预算或用途，不会直接推荐和下单。
- 预算与“旗舰、全铝、无线”等偏好无法同时满足时仍可搜索安全边界内的近似候选，并在推荐理由中说明取舍。
- 所有候选 SKU、价格和属性均能在本地目录中复核。
- Mock 故意返回候选集合外 SKU 时，结果被拒绝。
- 无匹配商品、已过期意图和未确认意图都不会创建订单或扣减库存。
- 用户确认后由服务端价格生成 `OrderRequest`，并复用阶段 9 流程达到对应订单状态。
- 重复确认只产生一个订单且只扣减一次库存。
- 完整 trace 能区分自然语言分析、Agent 目录检索、目录工具读取、确认和订单处理。
- 阶段 1 至 9 已通过的测试继续通过。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 9A：只新增一个 ShoppingAssistantAgent，在现有 OrderRequest 之前实现自然语言语义分析、多轮缺失信息澄清、本地商品候选查询、候选解释和用户显式确认。意图阶段不执行约束冲突检测，偏好无法同时满足时在推荐理由中说明取舍。商品事实、价格计算、确认校验和订单转换必须由确定性代码完成；确认后必须复用现有 OrderWorkflow。使用 MockModelProvider，禁止提前接入真实 Qwen、外部商城、支付或步骤 10 之后的功能。补充语义、幻觉拦截、未确认不下单、服务端价格和确认幂等测试。
```

---

## 阶段 10：故障注入机制

- [x] 完成阶段 10

### 目标

通过配置稳定复现故障，不手工修改业务代码。

### 建议创建

```text
config/fault_scenarios.yaml
src/order_agent_ops/faults/controller.py
tests/unit/test_fault_controller.py
```

### 实现要求

- 支持对指定 Agent 或适配器增加延迟、错误率、强制超时和非法输出。
- 提供 `inventory_v21_latency` 场景。
- 正常 v2.0 延迟约 100 至 200ms；故障 v2.1 延迟约 2300ms。
- 随机错误必须支持固定 seed，保证测试可重复。
- 测试结束自动恢复故障设置。

### 验收条件

- 可以通过配置开启和关闭故障。
- 开启故障后订单进入 `PENDING` 或明显变慢。
- 关闭故障后恢复正常。
- 不修改 Agent 实现代码即可切换场景。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 10：实现配置化 FaultController 和 inventory_v21_latency 故障场景。要求可重复、可关闭、测试后自动恢复，不修改 Agent 业务逻辑。
```

---

## 阶段 11：指标聚合与异常检测器

- [x] 完成阶段 11

### 目标

使用确定性规则发现订单系统和 Agent 系统异常。

### 建议创建

```text
src/order_agent_ops/ops/detector.py
src/order_agent_ops/ops/rules.py
tests/unit/test_anomaly_detector.py
```

### 初版规则

- 订单 P95 延迟超过 2000ms。
- InventoryAgent P95 延迟超过阈值。
- Agent 或工具连续失败 3 次。
- PENDING 数量超过阈值。
- 模型输出 Schema 校验失败。
- 交接缺少 `trace_id`。
- 达到最大重试次数。
- 未审批调用危险动作。

### 实现要求

- 规则产生结构化 `Incident`。
- 同一根因的重复告警要去重或合并。
- 每个告警引用触发它的指标或运行记录。
- 检测器本身不调用 LLM。

### 验收条件

- 正常场景不产生误报告警。
- 库存延迟故障产生一个可定位事件。
- 重复检测不会无限创建相同事件。
- 事件包含对象、规则、时间和原始引用。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 11：实现规则型 AnomalyDetector、事件去重和库存延迟告警。检测器禁止调用模型。补充正常和异常两类测试。
```

---

## 阶段 12：五字段证据和两个只读运维工具

- [x] 完成阶段 12

### 目标

为运维助手提供可复核的证据查询能力。

### 建议创建

```text
src/order_agent_ops/tools/query_observability.py
src/order_agent_ops/tools/query_service_context.py
src/order_agent_ops/ops/evidence_store.py
tests/integration/test_ops_query_tools.py
```

### 实现要求

- `query_observability` 查询指标、日志和链路。
- `query_service_context` 查询发布、配置、依赖、负责人和运行手册。
- 所有工具结果转换为 `EvidenceRecord`。
- 每条证据只声明五个顶层必填字段。
- 查询超时、部分失败和无数据有明确状态。
- `evidence_id` 由平台生成，模型不能指定。
- 查询结果写入 EvidenceStore，后续可以复核。

### 验收条件

- 可以查询到库存 Agent v2.1 的延迟、日志、链路和发布证据。
- EvidenceStore 可以按事件、对象和时间查询。
- 查询不到数据时不伪造证据。
- 五字段结构有自动化测试。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 12：实现 query_observability、query_service_context 和 EvidenceStore。严格使用 README 定义的五字段证据结构，不允许模型生成 evidence_id，并增加查询失败测试。
```

---

## 阶段 13：运维助手与诊断验证

- [x] 完成阶段 13

### 目标

让运维助手基于真实证据生成结构化诊断，并阻止无证据结论。

### 建议创建

```text
src/order_agent_ops/agents/ops_guardian.py
src/order_agent_ops/ops/evidence_validator.py
src/order_agent_ops/services/incident_workflow.py
tests/integration/test_ops_diagnosis.py
```

### 实现要求

- OpsGuardian 接收事件并生成调查计划。
- 只能使用阶段 12 的只读工具获取证据。
- 输出事实、根因候选、缺失信息和建议动作。
- 每个事实和根因必须引用存在的 `evidence_id`。
- EvidenceValidator 在结果持久化前校验引用。
- 缺证据时返回“待补充证据”或低置信度，而不是确定性结论。
- 初版使用 Mock 模型稳定复现库存 v2.1 根因。

### 验收条件

- 库存延迟事件可以得到正确根因候选。
- 诊断引用指标、链路和发布记录证据。
- Mock 模型故意输出不存在的证据 ID 时，结果被拒绝。
- 诊断过程有完整 trace。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 13：实现 OpsGuardianAgent、IncidentWorkflow 和 EvidenceValidator。诊断必须引用 EvidenceStore 中真实存在的证据；增加虚假和缺失证据被拒绝的测试。
```

---

## 阶段 14：审批和危险动作执行

- [x] 完成阶段 14

### 目标

实现真正由代码强制执行的人工确认，而不是依赖 Prompt。

### 建议创建

```text
src/order_agent_ops/ops/approval_service.py
src/order_agent_ops/ops/remediation_executor.py
src/order_agent_ops/tools/execute_remediation.py
tests/integration/test_approval_and_remediation.py
```

### 实现要求

- 高风险动作创建 `ApprovalRequest`。
- 审批记录目标、版本、影响范围、证据、申请时间和决定。
- 审批令牌单次有效、有限期、绑定动作和目标。
- `execute_remediation` 无有效审批必须返回 `HUMAN_APPROVAL_REQUIRED`。
- 回滚只修改模拟 `inventory-agent` 版本和故障配置。
- 所有尝试，包括被拒绝动作，都写入审计日志。

### 验收条件

- 未审批、过期、重复使用和目标不匹配的令牌都被拒绝。
- 审批通过后可以从 v2.1 模拟回滚至 v2.0。
- 不执行真实系统命令。
- 行为有完整测试和审计记录。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 14：实现 ApprovalService、单次审批令牌、execute_remediation 和模拟回滚。所有权限由确定性代码检查，禁止执行真实系统命令。覆盖未审批、过期、重放和成功路径。
```

---

## 阶段 15：处置后验证和订单重放

- [x] 完成阶段 15

### 目标

形成“诊断、处置、验证、恢复”的闭环。

### 建议创建

```text
src/order_agent_ops/ops/verification_service.py
src/order_agent_ops/business/replay_service.py
tests/integration/test_recovery_loop.py
```

### 实现要求

- 回滚后重新运行健康探针或测试订单。
- 重新计算库存 Agent 和订单延迟。
- 只有验证通过才能将事件标记为 `RESOLVED`。
- 验证失败时标记诊断未验证并转人工。
- 重放仅处理可重放的 `PENDING` 订单。
- 重放复用原幂等关系，不能创建重复订单。

### 验收条件

- v2.1 回滚到 v2.0 后验证成功。
- 验证失败不会错误关闭事件。
- pending 订单重放后完成或进入明确失败状态。
- 重复重放不会重复扣减库存。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 15：实现 VerificationService 和 ReplayService，打通回滚后指标验证与 pending 订单安全重放。覆盖验证成功、验证失败和重复重放测试。
```

---

## 阶段 16：本地/云端模型切换

- [x] 完成阶段 16

### 目标

在不修改 Agent 代码的情况下，通过配置切换 Mock、本地和云端 OpenAI 兼容模型，包括可提供 OpenAI 兼容接口的 Qwen 服务。

### 建议创建

```text
src/order_agent_ops/models/openai_compatible_provider.py
src/order_agent_ops/models/fallback_provider.py
tests/unit/test_provider_switching.py
tests/integration/test_openai_compatible_provider.py
```

### 实现要求

- 使用统一 OpenAI 兼容 Chat Completions 接口作为最低公共能力。
- `base_url`、API Key 环境变量名、模型名和超时全部配置化。
- 不在日志打印 API Key。
- 工具调度继续由 Python 完成，不依赖特定厂商 Function Calling。
- `ShoppingAssistantAgent` 与订单/运维 Agent 使用同一个 Provider 抽象；切换到 Qwen 时不得修改自然语言选购业务代码。
- 远程集成测试默认跳过，只有设置专用测试变量时才运行。
- 可选 fallback：本地失败后切云端，但必须记录降级事件。

### 验收条件

- Mock、本地和云端配置使用相同 Agent 代码路径。
- ShoppingAssistantAgent 在 Mock 与 Qwen/OpenAI 兼容配置下使用相同 Pydantic 输出契约。
- 没有 API Key 时普通测试仍通过。
- Provider 切换只修改配置。
- 本地失败切换云端时有日志和指标。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 16：实现 OpenAICompatibleProvider、配置切换和可选 FallbackProvider。核心测试不能依赖真实 API；远程测试必须可选。禁止修改业务 Agent 接口。
```

---

## 阶段 17：运维 API 和最小演示界面

- [x] 完成阶段 17

### 目标

提供课堂演示所需的事件、证据、审批和动作入口。

### 建议接口

```text
GET  /ops/health
GET  /ops/incidents
GET  /ops/incidents/{incident_id}
POST /ops/incidents/{incident_id}/diagnose
POST /ops/approvals
POST /ops/approvals/{approval_id}/decision
POST /ops/actions
GET  /ops/traces/{trace_id}
POST /orders/{order_id}/replay
```

### 实现要求

- API 返回结构化错误码。
- 事件详情展示事实、根因候选、证据和缺失信息。
- 审批接口明确显示动作、目标、风险、影响和证据。
- 最小界面可以使用 Jinja2/原生 HTML，不引入大型前端框架。
- 页面不是必需的业务逻辑载体，所有操作都应有 API。

### 验收条件

- 仅通过 API 可以完成完整故障演示。
- 页面可以查看订单、trace、事件、证据和审批。
- 用户可以确认或拒绝动作。
- 页面刷新不会改变业务状态。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 17：补充运维 API，并在不引入大型前端框架的前提下创建最小演示页面。所有核心能力必须先有 API，页面只作为展示层。运行 API 集成测试。
```

---

## 阶段 18：完整实验脚本与测试

- [x] 完成阶段 18

### 目标

让正常路径和故障路径可以一键、重复运行并产生实验材料。

### 建议创建

```text
scripts/run_happy_path.py
scripts/run_inventory_latency_incident.py
scripts/reset_demo.py
tests/scenarios/test_happy_path.py
tests/scenarios/test_inventory_latency_incident.py
artifacts/traces/.gitkeep
artifacts/reports/.gitkeep
```

### 正常脚本

应输出：

- 自然语言原始需求、结构化意图和候选商品
- 用户确认的 SKU 与服务端价格
- 订单 ID
- 最终状态
- trace ID
- Agent 执行顺序
- 总耗时

### 故障脚本

应自动完成：

1. 重置演示环境。
2. 切换到 inventory-agent v2.1。
3. 创建测试订单。
4. 触发延迟告警。
5. 运行运维诊断。
6. 展示证据链。
7. 演示无审批动作被拒绝。
8. 创建并批准审批。
9. 执行回滚。
10. 验证恢复。
11. 重放 pending 订单。
12. 导出 trace、事件和审计结果。

### 验收条件

- 两个脚本可以连续重复执行。
- 正常脚本可以从自然语言需求、候选确认一直运行到订单结果，且不需要手工拼装 `OrderRequest`。
- 故障脚本无需手工修改数据库。
- 输出中没有密钥和敏感数据。
- `python -m pytest` 全部通过。
- 失败时脚本给出明确阶段和错误。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 18：创建可重复运行的正常流程和库存延迟事件脚本，并补充端到端场景测试。故障脚本要覆盖告警、证据、无审批拒绝、审批、回滚、验证和重放完整闭环。
```

---

## 阶段 19：架构图、报告材料和最终清理

- [ ] 完成阶段 19

### 目标

整理可提交、可汇报、可复现的最终成果。

### 需要完成

- 更新 README 的实际启动命令。
- 更新所有完成状态和实施记录。
- 生成端到端架构图，清晰区分四类边界。
- 在架构图中展示自然语言选购、商品事实校验、用户确认与现有订单工作流的衔接。
- 导出一次正常实验和一次故障实验结果。
- 保存代表性的 trace、日志、证据、审批和恢复结果。
- 检查依赖和运行说明。
- 检查大模型使用声明所需信息。
- 删除无用临时文件，但保留可复现实验所需内容。

### 最终检查

- 新环境能按 README 启动。
- 默认 Mock 模式不需要 API Key。
- 所有测试通过。
- 架构图与实际代码一致。
- 自然语言推荐中展示的 SKU、价格和属性可以回查本地商品目录。
- 报告中的每个诊断结论能找到证据。
- 无审批不能执行危险动作。
- 处置后有验证，验证失败不会错误关闭事件。
- 模型、Prompt、Agent 和工具版本可查询。

### 可复制给 AI 的任务

```text
实施 DEVELOPMENT_PLAN.md 的阶段 19：进行最终文档、架构图、实验材料和仓库清理。不要改变已经通过测试的核心行为。验证 README 的启动步骤，运行全部测试，并列出最终验收结果。
```

---

## 实施记录

后续每完成一个阶段，在这里追加一行：

| 日期 | 阶段 | 结果 | 测试 | 备注 |
| --- | --- | --- | --- | --- |
| 2026-09-27 | 阶段 1 | 完成项目骨架、配置加载、FastAPI 健康检查 | `python -m pytest`：2 passed | 默认配置不需要 API Key |
| 2026-09-27 | 阶段 2 | 完成模型配置覆盖和核心领域 Schema | `python -m pytest`：15 passed | 五字段证据与高风险审批约束已锁定 |
| 2026-09-27 | 阶段 3 | 完成 SQLite、Repository 和双状态机 | `python -m pytest`：50 passed | 临时数据库测试，状态转换写入审计 |
| 2026-09-27 | 阶段 4 | 完成 JSON 日志、轻量 trace/span 和指标聚合 | `python -m pytest`：64 passed | 本地内存实现，无外部监控依赖 |
| 2026-09-27 | 阶段 5 | 完成 ModelProvider、ModelGateway 和可配置 Mock | `python -m pytest`：76 passed | 超时、一次重试、JSON/Pydantic 校验和遥测 |
| 2026-09-27 | 阶段 6 | 完成演示数据、只读业务适配器和幂等 seed | `python -m pytest`：82 passed | 两个商品、三个用户档案，全程无网络依赖 |
| 2026-09-27 | 阶段 7 | 完成协调、库存和风控三个业务 Agent | `python -m pytest`：91 passed | 并发检查共享 trace，失败安全降级并可定位 |
| 2026-09-27 | 阶段 8 | 完成确定性订单策略、幂等执行、库存预留和事务回滚 | `python -m pytest`：117 passed | 完整决策矩阵；重复请求不重复扣减；无新增模型调用 |
| 2026-09-27 | 阶段 9 | 打通订单工作流、创建/查询 API 和正常流程脚本 | `python -m pytest`：123 passed | 完成、拒绝、人工复核、幂等与稳定错误码均有集成测试 |
| 2026-09-27 | 阶段 9A | 完成自然语言语义分析、商品候选、澄清与确认订单衔接 | `python -m pytest`：135 passed | 一个购物助手 Agent；商品事实和服务端计价由确定性代码控制 |
| 2026-09-27 | 阶段 10 | 完成配置化故障控制器及 Agent/适配器透明注入层 | `python -m pytest`：144 passed | v2.0/v2.1 延迟、超时、固定 seed 随机错误、非法输出及自动恢复均有测试 |
| 2026-09-27 | 阶段 11 | 完成规则型异常检测、事件合并去重和应用装配 | `python -m pytest`：154 passed | 覆盖延迟、连续失败、PENDING、非法输出、重试、缺失 trace 和未审批动作；检测器不调用模型 |
| 2026-09-27 | 阶段 12 | 完成五字段 EvidenceStore 与两个只读运维查询工具 | `python -m pytest`：160 passed | 可查询并持久化指标、日志、链路、发布和服务档案证据；ID 由平台生成，失败状态明确 |
| 2026-09-27 | 阶段 13 | 完成 OpsGuardian、事件调查工作流和证据引用校验 | `python -m pytest`：165 passed | v2.1 延迟根因引用指标、链路与发布证据；虚假或不足证据由确定性代码拒绝 |
| 2026-09-27 | 阶段 14 | 完成高风险动作审批、单次限时令牌、模拟回滚和全尝试审计 | `python -m pytest`：170 passed | 未审批、过期、重放和绑定不匹配均拒绝；只切换模拟故障配置，不执行系统命令 |
| 2026-09-27 | 阶段 15 | 完成回滚后测试订单验证、延迟复算和 pending 订单安全重放 | `python -m pytest`：170 passed | 验证失败转人工；首次重放需独立审批，重复调用不重复创建订单或扣减库存 |
| 2026-09-27 | 阶段 16 | 完成 Mock、本地和云端 OpenAI 兼容 Provider 配置切换及可选故障降级 | `python -m pytest`：179 passed, 1 skipped | API Key 仅按环境变量名读取；真实端点测试默认跳过，fallback 记录日志和指标 |
| 2026-09-28 | 阶段 17 | 完成运维 API、故障演示控制入口和无前端框架的最小运维页面 | `python -m pytest`：181 passed, 1 skipped | 可仅通过 API 完成检测、诊断、审批、模拟回滚、验证和安全重放；刷新页面不改变状态 |
| 2026-09-28 | 阶段 18 | 完成自然语言正常路径、库存延迟事件、精确重置和实验材料导出脚本 | `python -m pytest`：183 passed, 1 skipped | 两个场景均可连续重复运行；故障实验覆盖无审批拒绝、审批回滚、验证和安全重放，报告不保存明文令牌 |
| 2026-09-28 | 阶段 16 兼容增强 | 完成 JSON Schema 约束、Agent v2 Prompt 和 Ollama 端到端验证 | `python -m pytest`：185 passed, 2 skipped；Ollama E2E：1 passed | 本机 qwen3:1.7b 完整自然语言选购到订单完成；不支持 JSON Schema 的兼容端点仅回退一次 |
| 2026-09-29 | 阶段 9A Agent 检索增强 | ShoppingAssistantAgent 主动调用只读 ProductCatalogTool，直接基于原始表达与目录事实完成语义检索和排序 | `python -m pytest`：194 passed, 2 skipped | 正常工作流不再调用 ProductSearchAdapter.search；事实校验、确认和订单安全边界保持不变 |
| 2026-09-29 | 阶段 9A Ollama 检索兼容 | 将 Agent 检索输出简化为必填的 selected_skus 与 reason，并允许商品特征近似匹配 | `python -m pytest`：196 passed, 2 skipped；Ollama E2E：1 passed | 类别、预算、启用状态和库存继续由代码校验；无效 SKU 自动丢弃 |
| 2026-09-30 | 阶段 9A 推荐理由可信增强 | 明确用户原话边界，加入逐项理由清单、紧凑目录事实表、SKU 规范化、事实取舍校验及一次纠错重试 | `python -m pytest -q`：202 passed, 2 skipped；qwen3:1.7b 只读目录推荐：成功 | 真实模型选择 SKU-028/SKU-009，首选满足预算、无线、机械、低噪音和编程条件；商品特征仍不作为硬过滤条件 |
| 2026-09-30 | 阶段 9A 取消冲突阻断 | 停用意图阶段约束冲突检测，忽略模型误报并仅对类别、数量和预算缺失进行澄清 | `python -m pytest -q`：202 passed, 2 skipped | `constraint_conflicts` 为兼容保留并固定为空；安全边界和候选取舍说明不变 |
| 2026-10-01 | 阶段 9A 理由降级增强 | 模型纠错后仍遗漏条件或取舍时，按首选 SKU 的可信目录事实生成逐项理由，不再因解释质量返回 502 | `python -m pytest -q`：203 passed, 2 skipped | Agent 选品保持不变；近似候选明确展示取舍；重复虚构用户提供 SKU 仍拒绝 |

## 遇到问题时的处理顺序

如果某一步失败，按以下顺序处理，不要直接跳过：

1. 先确认失败是代码、配置、依赖还是环境问题。
2. 保留失败日志和最小复现步骤。
3. 修复当前步骤对应的问题。
4. 重新运行当前步骤测试。
5. 运行此前阶段的回归测试。
6. 只有全部通过才进入下一阶段。

如果本地模型部署失败：

1. 不阻塞阶段 1 至 15，继续使用 Mock。
2. 保留统一 ModelProvider 接口。
3. 在阶段 16 尝试 OpenAI 兼容本地服务。
4. 本地服务仍不可用时切换云端 Provider。
5. 在报告中如实记录最终使用的模型、失败原因和人工验证过程。

