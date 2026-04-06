# Agentic RAG 动态路由验收表

## 1. 目的

本验收表用于验证以下改动是否真实生效：

- 前端 Router 是否能输出 `standalone_query`、`domains`、`query_type`
- 后端是否按 `query_type` 动态调整召回深度与融合权重
- 多轮对话改写是否生效
- 系统是否能在缺少知识时保持诚实，不发生幻觉

## 2. 建议观察点

每次提问后，建议同时观察以下三处：

1. VS Code 聊天窗口中的“检索范围”“查询类型”“问题改写”。
2. 后端日志中的 `query_type`、`strategy`、`vector_k`、`bm25_k`、`vector_weight`、`bm25_weight`、`target_domains`。
3. 最终回答是否符合知识库证据，而不是模型自由发挥。

## 3. 验收标准

- `exact` 问题应明显偏向 BM25，避免语义漂移。
- `semantic` 问题应偏向向量召回，回答应更完整、更像解释和方案说明。
- `comparative` 问题应同时覆盖比较双方，不能只答一边。
- `factoid` 问题应回答短、准、稳，优先给出明确值或明确缺失。
- 多轮代词问题应出现合理的 `standalone_query` 改写。
- 知识库没有答案时，应明确返回“知识库未找到此信息”或等价表达。

## 4. 正式验收表

| ID | 测试问题 | 场景类型 | 预期 standalone_query | 预期 domains | 预期 query_type | 预期日志特征 | 预期回答要点 / 验收标准 | 实际结果 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T01 | SIGN_ERROR 怎么解决？ | 精确检索 | 与原问题相同，或补全为“支付网关中的 SIGN_ERROR 怎么解决？” | `payment-gateway` | `exact` | `strategy=bm25-priority`；`bm25_weight` 大于 `vector_weight`；`bm25_k` 明显偏大 | 回答应直接围绕错误码处理，不应发散到泛化的“签名失败原因大全” |  |
| T02 | t_order 表里总金额字段叫什么？单位是什么？ | 精确检索 | 与原问题相同 | `order-center` | `exact` | `strategy=bm25-priority`；BM25 权重高于向量 | 回答应直接给出字段名、类型或单位；如果文档未写清，必须如实说明 |  |
| T03 | 这个错误码 10003 是什么意思？ | 精确检索 | 最好改写为“错误码 10003 是什么意思？” | `user-center` 或相关模块 | `exact` | `bm25_weight` 高于 `vector_weight`；命中内容应包含错误码字面量 | 回答应直接解释错误码含义，不应泛化成登录失败的各种可能原因 |  |
| T04 | 最大重试次数是多少？ | 事实查询 | 与原问题相同，必要时补全上下文对象 | 命中对应模块，若无明确模块则可全局 | `factoid` | `strategy=fact-high-precision`；`bm25_weight` 略高于 `vector_weight` | 回答应尽量短，直接给值；如果无证据，必须明确说未找到 |  |
| T05 | 登录接口里 email 是必填吗？ | 事实查询 / 防幻觉 | 与原问题相同 | `user-center` | `factoid` 或 `exact` | 应偏高精度召回，不应做大范围语义扩散 | 如果文档只写了 `phone` 和 `password`，回答应明确说未提及 `email`，不能脑补 |  |
| T06 | 为什么退款会失败？ | 语义检索 | 与原问题相同 | `payment-gateway` 或相关支付模块 | `semantic` | `strategy=semantic-priority`；`vector_weight` 大于 `bm25_weight` | 回答应体现原因分析或分类说明，而不是只摘一句原文 |  |
| T07 | 用户下单后一直不付款，系统会怎么处理？ | 语义检索 | 可改写为“用户下单后长期未支付时，系统会如何处理订单和库存？” | `order-center` | `semantic` | `vector_weight` 高于 `bm25_weight`；应能召回流程性文档 | 回答应覆盖超时取消、状态变化、库存释放、延迟消息等关键点 |  |
| T08 | Access Token 和 Refresh Token 的区别是什么？401 的时候前端应该怎么处理？ | 语义 / 对比 | 可改写为“Access Token 和 Refresh Token 的区别是什么，以及 401 时前端该如何处理？” | `user-center`，必要时联动业务模块 | `comparative` 或 `semantic` | 如果判为 `comparative`，应看到 `strategy=broad-dual-recall`；双路召回规模更大 | 回答至少要覆盖两个 Token 的职责差异，以及 401 后的 refresh 重试流程 |  |
| T09 | 等待期和犹豫期有什么区别？ | 对比查询 | 与原问题相同 | 命中对应模块，若无明确模块则可全局 | `comparative` | `strategy=broad-dual-recall`；`vector_k` 与 `bm25_k` 应高于默认精确检索场景 | 回答必须同时覆盖 A 和 B，且结构上有对比关系，而不是只解释一个术语 |  |
| T10 | 用户点击提交订单后，到最终支付成功，中间链路是怎样的？ | 跨模块语义检索 | 可改写为“用户提交订单到支付成功的跨系统调用链路和状态流转是怎样的？” | `global`、`order-center`、`payment-gateway` | `semantic` | `target_domains` 应体现跨模块；两阶段检索应尽量收敛到相关文档 | 回答应包含调用链路、关键系统、状态变化，不应只答支付或只答订单 |  |
| T11 | 商家发货后，怎么对接顺丰 API 获取物流单号？ | 防幻觉 | 与原问题相同 | `order-center` 或全局 | `semantic` | 可正常检索，但如果知识库无相关内容，不应伪造具体 API 字段 | 必须老实回答“知识库未找到此信息”或等价表达 |  |
| T12 | 它和刚才那个机制有什么区别？ | 多轮上下文改写 | 应显式改写为带实体名的完整问题，不能保留“它”“那个机制” | 跟随上一轮实体所属模块 | `comparative` 或 `semantic` | 前端展示中应看到 `问题改写`；后端收到的 query 应是改写后的完整句子 | 如果没有改写，说明历史上下文利用不完整；回答若跑偏，也视为不通过 |  |

## 5. 推荐执行顺序

建议按下面顺序执行，这样更容易定位问题：

1. 先跑 `T01` 到 `T05`，验证 `exact` 和 `factoid` 是否稳定。
2. 再跑 `T06` 到 `T10`，验证 `semantic` 和 `comparative` 的动态调参是否生效。
3. 最后跑 `T11` 和 `T12`，验证防幻觉与多轮改写。

## 6. 快速判定建议

如果出现以下现象，基本可以快速定位问题：

- `exact` 题回答明显发散：优先检查 `query_type` 是否被误判成 `semantic`。
- `comparative` 题只答了一边：优先检查 Router 是否把问题判成了 `semantic`，或双路扩召不够。
- 多轮代词问题仍然原样透传：优先检查前端历史对话摘要和 `standalone_query` 提取逻辑。
- 明明没文档却答得头头是道：优先检查生成 Prompt 的约束和最终回答兜底逻辑。

## 7. 记录模板

可直接复制下面的模板补充一次正式验收记录：

```md
### 验收批次

- 日期：
- 执行人：
- 后端版本：
- 扩展版本：

### 结论

- 通过项：
- 不通过项：
- 风险备注：

### 重点异常

- 异常 1：
- 异常 2：
- 后续动作：
```