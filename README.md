为了全面检验你刚刚搭建的**“多模块智能路由 RAG 系统”**是否达到了生产级标准，我为你精心设计了 4 个维度的测试题（共 10 个问题）。

这些问题模拟了开发人员在日常工作中最真实的提问场景。你可以直接把它们复制到 VS Code 的 Copilot 聊天框中进行测试。

同时，我为你附上了**【预期路由命中】**和**【预期回答要点】**，方便你对照 Python 后端的日志来验证系统是否完美工作。
OAI Compatible
---

### 🟢 级别一：单模块精准检索（测试 Reference 的查表能力）

这类问题测试 RAG 系统能否精准过滤噪音，从特定模块的表格中提取客观参数。

**测试问 1：**
> “前端调登录接口的时候，需要传哪些参数？如果密码输错了会返回什么错误码？”
* **预期路由命中：** `["user-center"]`
* **预期回答要点：** 需要传 `phone`（11位）和 `password`（明文）。密码错误返回错误码 `10003`。

**测试问 2：**
> “订单表里的金额字段叫什么？它的数据类型和单位是什么？”
* **预期路由命中：** `["order-center"]`
* **预期回答要点：** 字段叫 `total_amount`，类型是 `INT`，单位统一为**分**。

**测试问 3：**
> “订单处于什么状态的时候，才允许被取消？”
* **预期路由命中：** `["order-center"]`
* **预期回答要点：** 只有处于 `INIT`（初始化/待支付）状态的订单才能被取消，状态变更为 `CANCELED`。

---

### 🟡 级别二：单模块逻辑推理（测试 Explanation 与 How-to）

这类问题测试 RAG 能否理解架构设计的原因，以及能否梳理出代码开发的先后步骤。

**测试问 4：**
> “为什么我们系统要搞 Access Token 和 Refresh Token 两个 Token？只用一个不行吗？”
* **预期路由命中：** `["user-center"]`
* **预期回答要点：** 为了兼顾安全和体验。单 Token 泄露风险大；双 Token 机制下 Access Token 有效期短（2小时），配合长期的 Refresh Token（30天）降低风险。

**测试问 5：**
> “后端在写创建订单接口的时候，订单号可以直接用数据库的自增 ID 吗？为什么？”
* **预期路由命中：** `["order-center"]`
* **预期回答要点：** **绝对不能**。必须调用分布式 ID 生成服务 `IdGenerator.nextSnowflakeId()` 获取 64 位雪花算法 ID。用自增 ID 会泄露商业数据。

**测试问 6：**
> “如果用户下单了但一直不付款，这笔订单会怎么处理？会不会一直占着库存？”
* **预期路由命中：** `["order-center"]`
* **预期回答要点：** 不会。落库时会发送一条延迟 15 分钟的 RocketMQ 消息。15分钟未支付会触发“超时取消”机制，自动关闭订单并释放库存。

---

### 🔴 级别三：跨模块全局推理（终极挑战：测试 LLM 路由器）

这类问题极其刁钻，问题中**没有明确说出模块名**，需要 Copilot 先推断出涉及多个系统，再进行联合检索。

**测试问 7：**
> “当用户在 App 里面点击提交订单后，到最终支付成功，这中间的系统调用链路是怎样的？状态是怎么变的？”
* **预期路由命中：** `["global", "order-center", "payment-gateway"]` （路由器必须推断出这是跨模块行为）
* **预期回答要点：**
    1. 网关鉴权，透传 `user_id`。
    2. 订单中心计算金额、生成订单，状态为 `INIT`，并同步扣减库存。
    3. 前端拿到 `order_no` 请求支付网关唤起收银台。
    4. 支付成功后，支付网关发出 MQ 消息。订单中心监听到后，将状态从 `INIT` 改为 `PAID`。

**测试问 8：**
> “如果前端请求下单接口的时候，报了 401 错误，前端应该怎么做？需要让用户重新输入账号密码吗？”
* **预期路由命中：** `["user-center", "order-center"]` 
* **预期回答要点：** **不需要重新输入密码**。401 说明 Access Token 过期。前端需静默调用 `/api/v1/user/refresh` 换取新 Token，然后**无感**重新发起刚才失败的下单请求。

---

### ⚫ 级别四：防幻觉测试（测试系统的“诚实度”）

如果大模型在这些问题上瞎编乱造，说明你的 Prompt 约束不够，或者检索池子里进了脏数据。

**测试问 9：**
> “登录接口里面，email 字段是必填的吗？”
* **预期路由命中：** `["user-center"]`
* **预期回答要点：** 接口参考手册中**未提及** `email` 字段，只有 `phone` 和 `password` 是必填的。（AI 绝不能根据它自己在网上的见识说 email 是可选的）。

**测试问 10：**
> “商家发货后，怎么对接顺丰快递的 API 获取物流单号？”
* **预期路由命中：** `["order-center"]` 或全局检索
* **预期回答要点：** **“内部知识库未找到此信息”** 或者 “当前文档没有提供对接快递 API 的相关说明”。（因为你提供的 7 个 MD 文件里根本没写物流对接的细节，AI 必须老实承认不知道）。

---

### 💡 如何观察测试结果？

当你问出问题后，请**紧盯你的 Python 后端控制台**。
一个完美的 RAG 系统工作流，在控制台的输出应该是这样的（以测试问 7 为例）：

```text
# 后端日志
🎯 接收到前端的大模型路由，锁定检索范围: ['global', 'order-center']
INFO: 执行 BGE-M3 向量检索，filter={'domain': {'$in': ['global', 'order-center']}}
INFO: 召回 Top 3: 
--- [模块: global | 章节: 全局流程：电商完整下单跨微服务调用链路] ---
--- [模块: order-center | 章节: 订单数据库设计规范 > order_status 状态枚举字典] ---
```
这说明你的双重大模型架构（LLM-as-a-Router -> 隔离检索 -> Copilot 生成）已经完全跑通，精准度达到了业界 T0 级别！




## TODO：
 >FAQ场景支持
 >用户权限
 >模块列表增强，增强路由功能）（api提供
 >知识库运维：“帮我写一个一键更新/删除指定文档（根据 source_file）的后端接口。”
 > 检索性能优化：“帮我引入 Reranker（如 BGE-Reranker）进行重排，进一步提升 Top-K 的准确率。
> 上下文记忆功能：“帮我在 extension.ts 或后端加入历史多轮对话的上下文管理机制。”
> 混合检索扩展：“请根据这个架构，帮我加一个接入 Elasticsearch 做 BM25 + 向量双路召回的功能。”

---

## 配置说明

Elasticsearch 版本要求：

- 最低可用版本建议为 `7.17`。
- 更稳妥的推荐版本为 `8.x`。
- 当前项目只使用基础索引、查询和删除接口，不依赖旧版 `2.x` 行为。

本地安装建议使用官方 tarball，并放到用户目录下，避免把大文件提交到仓库：

```bash
mkdir -p "$HOME/.local/elasticsearch"
curl -fL "https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-9.3.2-darwin-aarch64.tar.gz" \
    -o "$HOME/.local/elasticsearch/elasticsearch-9.3.2-darwin-aarch64.tar.gz"
tar -xzf "$HOME/.local/elasticsearch/elasticsearch-9.3.2-darwin-aarch64.tar.gz" \
    -C "$HOME/.local/elasticsearch"
rm "$HOME/.local/elasticsearch/elasticsearch-9.3.2-darwin-aarch64.tar.gz"
```

安装完成后，Elasticsearch 的根目录就是：

```bash
$HOME/.local/elasticsearch/elasticsearch-9.3.2
```

可以直接让启动脚本使用这个路径：

```bash
ES_HOME="$HOME/.local/elasticsearch/elasticsearch-9.3.2" ./scripts/start_elasticsearch.sh
```

这个脚本会复制一份干净的本地配置，然后以单节点、关闭安全认证的开发模式启动 Elasticsearch，方便后端直接用 `http://localhost:9200` 连接，也避免和第一次自动生成的安全配置冲突。

如果是第一次启动，建议把等待时间调长一点，避免 Elasticsearch 还在初始化时就被脚本判定失败：

```bash
ES_STARTUP_TIMEOUT=180 ES_HOME="$HOME/.local/elasticsearch/elasticsearch-9.3.2" ./scripts/start_elasticsearch.sh
```

如果你想手动启动，也可以直接执行：

```bash
"$HOME/.local/elasticsearch/elasticsearch-9.3.2/bin/elasticsearch"
```

布尔配置统一收口在 `backend/rag_config.py`，支持“文件内默认值 + 环境变量覆盖”：

```python
ENABLE_ACL = _get_bool_config("ENABLE_ACL", False)
RERANKER_ENABLED = _get_bool_config("RERANKER_ENABLED", True)
ENABLE_SUB_CHUNKING = _get_bool_config("ENABLE_SUB_CHUNKING", False)
```

这三个开关现在都遵循同一套规则：

- 直接修改 `backend/rag_config.py` 可以作为项目默认配置。
- 启动前设置同名环境变量，可以临时覆盖默认值。

Reranker 是否启用可以直接在 `backend/rag_config.py` 中配置：

```python
# 是否启用 BGE Reranker 精排
# 可直接修改这里；如需按环境覆盖，可设置 RERANKER_ENABLED=true/false
RERANKER_ENABLED = _get_bool_config("RERANKER_ENABLED", True)
```

默认行为：

- `RERANKER_ENABLED = True` 时，检索会先走向量召回，再走 CrossEncoder 精排。
- `RERANKER_ENABLED = False` 时，系统只保留向量召回排序，不加载 reranker 模型。
- 关闭 reranker 后，`RERANK_CANDIDATE_K` 会自动回退到 `RETRIEVAL_K`，避免无意义扩大候选集。

如果你想临时覆盖配置，也可以在启动前设置环境变量：

```bash
RERANKER_ENABLED=false python backend/server.py
```

ACL 和子分块的临时覆盖方式相同：

```bash
ENABLE_ACL=true ENABLE_SUB_CHUNKING=true python backend/server.py
```

另外可以通过只读接口查看当前生效配置：

```bash
GET /config
```

这个接口会返回当前布尔开关、检索参数、模型配置、路径配置，以及每项配置当前来自配置文件还是环境变量，便于前端展示和排查问题。