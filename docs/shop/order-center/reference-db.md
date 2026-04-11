---
domain: order-center
type: reference
version: v1.0
summary: 说明订单主表的核心字段、主键和分库分表策略。列出订单状态的标准枚举值及其业务含义。
keywords:
  - t_order
  - 订单主表
  - 数据库字段
  - order_status
  - 状态枚举
  - 枚举字典
sections:
  - title: t_order 核心订单主表
  - title: order_status 状态枚举字典
acl:
  groups:
    - order-admin
  users:
    - lisi
---

# t_order 订单主表与 order_status 枚举参考

## 核心订单主表 t_order
本表存储订单的主干信息。由于数据量庞大，按 `user_id` 进行分库分表。

| 字段名 | 数据类型 | 约束 | 业务说明 |
|---|---|---|---|
| order_no | VARCHAR(32) | 主键 | 订单号，使用雪花算法生成 |
| user_id | BIGINT | 非空, 索引 | 下单用户的全网唯一ID |
| total_amount | INT | 非空 | 订单最终支付总金额，**单位统一为分** |
| order_status | VARCHAR(16) | 非空 | 当前订单状态，见状态枚举字典 |
| created_at | DATETIME | 非空 | 订单落库时间 |

## order_status 状态枚举字典
* `INIT`: 初始状态（待支付）
* `PAID`: 支付成功（待发货）
* `SHIPPED`: 商家已发货（待收货）
* `CANCELED`: 订单已取消（含超时未支付、用户主动取消）
* `REFUNDED`: 订单已全额退款