---
domain: order-center
type: how-to
version: v1.0
summary: 说明后端创单时必须使用分布式 ID 生成服务，而不是数据库自增主键。说明后端要按商品单价重新计算最终金额，不能相信前端传值。说明订单落库必须放在本地事务中，并把 order_status 设为 INIT。说明事务提交后要发延迟消息，用于后续超时取消检查。
keywords:
  - 雪花 ID
  - IdGenerator
  - 订单号
  - 金额计算
  - total_amount
  - SKU
  - 事务
  - t_order
  - INIT
  - 延迟 MQ
  - RocketMQ
  - 超时取消
sections:
  - title: 生成雪花 ID 订单号
  - title: 重新计算 total_amount 金额
  - title: 通过 @Transactional 写入 t_order
  - title: 发送 15 分钟延迟的 RocketMQ 消息
acl:
  allow:
    - "$authenticated"
---

# 后端创单落库指南：雪花 ID、金额重算、事务与延迟 MQ

当服务端接收到用户的下单请求时，后端研发人员需遵循以下标准动作生成订单：

## 生成雪花 ID 订单号
调用基础设施层的分布式 ID 生成服务 `IdGenerator.nextSnowflakeId()` 获取一个 64 位的订单号。绝对不要使用数据库自增 ID 以防泄露商业数据。

## 重新计算 total_amount 金额
禁止相信前端传入的金额！必须根据用户传入的 `sku_id` 列表，重新从商品库中查询单价，并扣减优惠券后，计算出 `total_amount`（单位：分）。

## 通过 @Transactional 写入 t_order
开启数据库事务 `@Transactional`，向 `t_order` 表插入一条记录。此时 `order_status` 必须硬编码设置为 `INIT`。

## 发送 15 分钟延迟的 RocketMQ 消息
事务提交成功后，必须向 RocketMQ 的 `order_delay_topic` 发送一条延迟 15 分钟的消息。该消息用于触发“超时未支付自动取消”的死信检查逻辑。