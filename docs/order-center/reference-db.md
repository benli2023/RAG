---
domain: order-center
type: reference
version: v1.0
description: 订单主表 t_order 字段说明与 order_status 状态枚举字典。
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
  - 数据库字段
  - 状态枚举
  - 字段说明
acl:
  groups:
    - order-admin
  users:
    - lisi
---

# 订单数据库设计规范

## 1. 核心订单主表 (t_order)
本表存储订单的主干信息。由于数据量庞大，按 `user_id` 进行分库分表。

| 字段名 | 数据类型 | 约束 | 业务说明 |
|---|---|---|---|
| order_no | VARCHAR(32) | 主键 | 订单号，使用雪花算法生成 |
| user_id | BIGINT | 非空, 索引 | 下单用户的全网唯一ID |
| total_amount | INT | 非空 | 订单最终支付总金额，**单位统一为分** |
| order_status | VARCHAR(16) | 非空 | 当前订单状态，见状态枚举字典 |
| created_at | DATETIME | 非空 | 订单落库时间 |

## 2. order_status 状态枚举字典
* `INIT`: 初始状态（待支付）
* `PAID`: 支付成功（待发货）
* `SHIPPED`: 商家已发货（待收货）
* `CANCELED`: 订单已取消（含超时未支付、用户主动取消）
* `REFUNDED`: 订单已全额退款