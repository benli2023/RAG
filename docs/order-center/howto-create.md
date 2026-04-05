---
domain: order-center
type: how-to
version: v1.0
description: 指导后端如何实现订单落库，包括重新计算金额、事务插入和发送延迟 MQ。
keywords:
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
  - 订单落库
  - 金额计算
  - 事务
  - 延迟 MQ
  - 创单实施
acl:
  allow:
    - "$authenticated"
---

# 指南：后端开发如何实现订单落库

当服务端接收到用户的下单请求时，后端研发人员需遵循以下标准动作生成订单：

## 1. 生成唯一订单号
调用基础设施层的分布式 ID 生成服务 `IdGenerator.nextSnowflakeId()` 获取一个 64 位的订单号。绝对不要使用数据库自增 ID 以防泄露商业数据。

## 2. 计算最终金额
禁止相信前端传入的金额！必须根据用户传入的 `sku_id` 列表，重新从商品库中查询单价，并扣减优惠券后，计算出 `total_amount`（单位：分）。

## 3. 执行本地事务落库
开启数据库事务 `@Transactional`，向 `t_order` 表插入一条记录。此时 `order_status` 必须硬编码设置为 `INIT`。

## 4. 发送延迟 MQ 消息
事务提交成功后，必须向 RocketMQ 的 `order_delay_topic` 发送一条延迟 15 分钟的消息。该消息用于触发“超时未支付自动取消”的死信检查逻辑。