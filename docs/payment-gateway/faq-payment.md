---
domain: payment-gateway
type: faq
version: v1.0
summary: 说明微信回调未到达时，如何通过 notify_status 和网络出口排查问题。说明统一下单签名错误最常见的原因，以及如何按字典序生成签名。说明同一订单的退款请求必须串行，避免重复扣款风险。
keywords:
  - 退款
  - 支付未回调
  - notify_status
  - 微信回调
  - SIGN_ERROR
  - MD5 签名
  - 统一下单
  - 串行控制
  - Redis 分布式锁
sections:
  - title: 支付成功但业务系统未收到回调：notify_status 排查
  - title: SIGN_ERROR 签名错误：参数排序与 MD5 拼接
  - title: 退款并发控制：Redis 分布式锁与串行调用
acl:
  allow:
    - "$authenticated"
---

# 支付网关 FAQ：回调、SIGN_ERROR 与退款并发控制

## 支付成功但业务系统未收到回调：notify_status 怎么排查？
**A:** 请按以下步骤排查：
1. 检查 `t_pay_record` 表中的 `notify_status` 是否为 `SUCCESS`。
2. 确认支付网关的出口 IP 是否被你们业务模块的防火墙拦截。
3. 微信支付的回调存在延迟，请检查是否在 15s、30s、3m 的重试阶梯中。

## SIGN_ERROR 签名错误：参数排序与 MD5 拼接怎么修？
**A:** 签名错误 90% 是因为参数排序不对。请确保传入的 JSON 参数在生成 MD5 之前，已经按照 ASCII 码从小到大进行了字典序排序，并且最后拼接了 `&key=商户秘钥`。

## 退款并发控制：同一个 order_no 可以并发退款吗？
**A:** 绝对不行。同一个 `order_no` 的退款请求必须串行。并发调用会导致微信底层重复扣款，引发极高的客诉风险。请在调用前加上 Redis 分布式锁。