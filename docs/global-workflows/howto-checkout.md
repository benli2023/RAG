---
domain: global
type: how-to
version: v1.0
summary: 说明请求如何先完成 Token 校验，再把 user_id 传给订单中心。说明订单创建接口如何计算金额、生成订单号并扣减库存。说明下单成功后如何发起预支付，并校验支付金额和订单金额一致。说明支付成功后的微信回调、MQ 消息和订单状态流转闭环。
related_domains:
  - global
  - user-center
  - order-center
  - payment-gateway
keywords:
  - API Gateway
  - Token 校验
  - user_id
  - 订单创建
  - 订单中心
  - 锁库存
  - 支付网关
  - 收银台
  - 金额一致性
  - PaymentSuccessEvent
  - 消息回调
  - 闭环流程
sections:
  - title: API 网关鉴权与 user_id 透传
  - title: 订单中心创单、金额计算与锁库存
  - title: 支付网关唤起收银台与金额一致性校验
  - title: PaymentSuccessEvent 异步回调与订单状态闭环
acl:
  allow:
    - "*"
---

# 电商完整下单跨微服务链路说明：鉴权、创单、支付与回调闭环

当用户在 App 前端点击“提交订单”并拉起收银台时，系统横跨了多个微服务。研发人员联调时，请严格遵循以下跨模块协同顺序：

## API 网关鉴权与 user_id 透传
请求首先到达 API 网关。网关需校验 HTTP Header 中的 Token。
如果校验失败返回 `401`；如果成功，网关将解析出 `user_id`，并透传给下游的【订单中心】。具体 Token 续期机制请参考《用户认证系统架构说明》。

## 订单中心创单、金额计算与锁库存
请求到达【订单中心】的 `/api/v1/order/create` 接口。
订单中心需要计算金额、生成订单号，并将状态置为 `INIT`。落库成功后，订单中心需同步调用【库存服务】扣减库存。创单的具体代码实现规范，请参阅订单中心的指南文档。

## 支付网关唤起收银台与金额一致性校验
订单创建成功后，前端拿到 `order_no` 和 `total_amount`。
前端根据这两个参数，向【支付网关】发起预支付请求，获取微信/支付宝的唤起参数。
注意：支付网关在组装报文时，必须保证传输的金额与【订单中心】记录的 `total_amount` 绝对一致。

## PaymentSuccessEvent 异步回调与订单状态闭环
当用户支付成功后，【支付网关】会收到微信的异步回调。此时支付网关必须发出 `PaymentSuccessEvent` 的 MQ 消息。
【订单中心】监听到该消息后，负责将 `t_order` 表中的状态从 `INIT` 修改为 `PAID`，完成订单闭环。