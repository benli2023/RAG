---
domain: global
type: catalog
version: v1.0
acl:
  allow:
    - "*"
---

# 模块功能目录

本文档用于描述当前知识库中各个业务模块的职责边界、常见关键词和文档分布，供路由器和研发同学快速判断问题应该落到哪个模块。

## global（目录：global-workflows）

### 主要功能
- 描述跨模块的业务全链路，包括用户鉴权、订单创建、支付拉起和支付成功后的异步状态回写。
- 提供私有代码 RAG 方案的演示材料、系统架构图和时序图，帮助理解整体工作方式。

### 关键字
- 跨模块流程
- 下单链路
- API Gateway
- Token 校验
- 支付回调
- PaymentSuccessEvent
- Agentic RAG
- Mermaid 架构图

### 文件列表
| 文件 | 主要内容 |
| --- | --- |
| global-workflows/demo.md | RAG 系统演示稿，介绍企业私有 RAG 的痛点、架构、时序图、知识治理方式和 ROI。 |
| global-workflows/howto-checkout.md | 电商完整下单链路说明，串联用户中心、订单中心、支付网关和消息回调闭环。 |

## order-center

### 主要功能
- 管理订单创建、金额计算、订单号生成、订单状态流转和超时未支付自动取消。
- 覆盖订单主表设计、状态枚举、FAQ 和后端创单落库实施规范。

### 关键字
- 订单中心
- 下单
- 创单
- t_order
- order_status
- INIT
- PAID
- CANCELED
- 雪花 ID
- RocketMQ
- 超时取消
- 库存释放

### 文件列表
| 文件 | 主要内容 |
| --- | --- |
| order-center/explanation.md | 解释订单生命周期、乐观锁状态机和待支付订单 15 分钟超时取消机制。 |
| order-center/faq.md | 回答未支付订单处理、订单号生成方式、允许取消的状态等高频问题。 |
| order-center/howto-create.md | 指导后端如何实现订单落库，包括重新计算金额、事务插入和发送延迟 MQ。 |
| order-center/reference-db.md | 订单主表 t_order 字段说明与 order_status 状态枚举字典。 |

## payment-gateway

### 主要功能
- 负责支付网关侧的统一下单、签名、异步回调处理和退款调用约束。
- 聚焦支付链路故障排查，特别是回调收不到、签名错误和退款并发风险。

### 关键字
- 支付网关
- 统一下单
- SIGN_ERROR
- MD5 签名
- notify_status
- SUCCESS
- 微信回调
- 退款
- Redis 分布式锁

### 文件列表
| 文件 | 主要内容 |
| --- | --- |
| payment-gateway/faq-payment.md | 支付常见问题排查手册，覆盖支付成功未回调、签名错误和退款串行控制。 |

## user-center

### 主要功能
- 管理登录鉴权、Access Token / Refresh Token 双 Token 机制和统一身份透传。
- 覆盖前端登录流程、Token 存储规范、401 无感刷新和用户中心登录接口参考。

### 关键字
- 用户中心
- 登录
- Access Token
- Refresh Token
- 401 Unauthorized
- 无感刷新
- Authorization Bearer
- X-User-Id
- /api/v1/user/login
- /api/v1/user/refresh

### 文件列表
| 文件 | 主要内容 |
| --- | --- |
| user-center/explanation.md | 说明双 Token 鉴权架构、Token 生命周期和网关统一鉴权方式。 |
| user-center/howto-login.md | 规范前端登录接入流程，包括登录、存储 Token、鉴权请求和过期刷新。 |
| user-center/reference-api.md | 用户登录接口的请求参数、返回字段和业务错误码参考。 |