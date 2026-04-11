# Agentic RAG 全链路资产交接包 V2.0

## 1. 目标摘要

本版本将现有 RAG 从“固定检索流水线”升级为“意图感知 + 动态路由 + 动态执行”的 Agentic RAG。

核心变化：

- 前端 Router 不再只识别模块，还同时输出独立问题改写结果 `standalone_query` 与检索意图 `query_type`。
- 后端检索链路根据 `query_type` 动态调整向量召回、BM25 召回和 RRF 融合权重。
- 检索 trace 中保留动态决策信息，方便联调、观测和回归验证。

## 2. 端到端链路

### 2.1 控制面

1. VS Code 扩展读取当前问题和最近多轮对话。
2. 第一重 LLM 充当 Router，输出以下 JSON：

```json
{
  "standalone_query": "重写后的独立问题",
  "domains": ["order-center", "payment-gateway"],
  "query_type": "semantic"
}
```

3. 扩展将 `standalone_query`、`domains`、`query_type` 透传给后端 `/retrieve`。

### 2.2 执行面

1. 后端先按 ACL 和前端下发的 `domains` 缩小授权范围。
2. 若启用父子检索，则先做父文档双路召回，进一步缩小目标文档集合。
3. 子切片阶段根据 `query_type` 动态调整：

- `exact`: BM25 优先，扩大 BM25 召回，压低向量权重。
- `semantic`: 向量优先，维持默认召回深度，提高向量权重。
- `comparative`: 双路同时扩召，提升融合候选池覆盖。
- `factoid`: 偏高精度策略，BM25 略强于向量。

4. 动态加权 RRF 完成混合融合。
5. Reranker 与上下文压缩阶段保持兼容，不需要改动调用方。

## 3. 查询类型与策略映射

| query_type | 典型问题 | 策略重点 |
| --- | --- | --- |
| `exact` | 错误码、表名、字段名、枚举值 | BM25 提权，防止语义漂移 |
| `semantic` | 原因分析、流程说明、方案解释 | 向量提权，强调语义覆盖 |
| `comparative` | A/B 区别、方案对比 | 双路扩召，避免遗漏任一侧证据 |
| `factoid` | 次数、阈值、配置值、是否条件 | 中等向量 + 偏强 BM25，追求高精度 |

## 4. 本次代码落点

### 4.1 前端扩展

- `RAG_Extenstion/src/extension.ts`
  - 新增多轮历史摘要。
  - Router 从“模块数组”升级为“JSON 决策对象”。
  - 检索请求新增 `query_type`，查询内容切换为 `standalone_query`。
  - 聊天窗口展示检索范围、查询类型和问题改写结果。

### 4.2 后端服务

- `backend/api_models.py`
  - `QueryRequest` 新增 `domains` 与 `query_type`。
- `backend/server.py`
  - `/retrieve` 将动态路由参数传入 pipeline。
- `backend/retrieval_pipeline_service.py`
  - 增加 `query_type` 归一化与动态检索计划。
  - ACL 授权阶段接入前端模块路由范围。
  - 子切片检索阶段按 `query_type` 调整 `vector_k/bm25_k/fusion_top_k`。
  - trace 新增 `query_type`、`strategy`、动态权重信息。
- `backend/hybrid_retrieval_service.py`
  - RRF 支持 `vector_weight` 和 `bm25_weight`。

## 5. 观测与调试

建议重点观察以下日志字段：

- `query_type`
- `strategy`
- `vector_k`
- `bm25_k`
- `vector_weight`
- `bm25_weight`
- `target_domains`
- `two_stage_applied`

一个正常请求的关键日志应体现三层信息：

1. 前端 Router 做出了什么判断。
2. 后端是否按该判断缩小了检索范围。
3. 动态召回参数和融合权重是否生效。

## 6. 推荐验证集

建议至少覆盖以下四类：

1. `exact`: 错误码、表名、字段名、枚举状态。
2. `semantic`: 为什么、如何做、流程解释。
3. `comparative`: 两个概念或两个阶段的区别。
4. `factoid`: 最大次数、默认值、时长、阈值。

同时覆盖两类失败路径：

1. 文档不存在时，模型必须明确回答“知识库未找到此信息”。
2. ACL 禁止访问时，后端必须返回受限结果而不是泄露内容。

## 7. 当前能力边界

当前版本已经具备真正的“LLM as Router”控制面，但仍有几个自然延展点：

1. 将 `query_type` 扩展为可插拔枚举，例如 `sql_query`、`code_lookup`。
2. 将动态策略配置化，而不是写死在 Python 中。
3. 将 trace 输出给前端诊断面板，形成可观测化工作台。
4. 对 Router 结果做缓存，降低重复问题的首跳成本。

## 8. 结论

这次升级的本质，不是“又加了一个 Prompt”，而是把前端 LLM 从单纯问答入口，升级为检索系统的控制器。系统因此从固定 RAG Pipeline，转向可持续扩展的 Agentic Retrieval Runtime。