---
domain: order-center
type: faq
faq: true
version: v1.0
description: 回答未支付订单处理、订单号生成方式、允许取消的状态等高频问题。
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
  - 未支付订单
  - 订单号生成
  - 订单取消
  - 高频问题
  - FAQ
acl:
  allow:
    - "$authenticated"
---

# 订单中心 FAQ

## 下单后一直不支付，订单会怎么处理？
系统会先创建状态为 `INIT` 的订单，并发送一条延迟 15 分钟的 RocketMQ 消息。
如果 15 分钟内仍未收到支付成功回调，系统会触发“超时取消”机制，将订单自动关闭并释放库存，不会长期占用库存。

## 订单创建接口能不能直接使用数据库自增 ID 作为订单号？
不能。
订单号必须通过 `IdGenerator.nextSnowflakeId()` 生成 64 位雪花算法 ID，不能直接暴露数据库自增 ID，否则会泄露业务规模和订单量信息。

## 订单处于什么状态时才允许取消？
只有处于 `INIT`（初始化 / 待支付）状态的订单才允许取消。
取消成功后，订单状态会流转为 `CANCELED`。