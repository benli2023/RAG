---
domain: payment-gateway
type: faq
version: v1.0
description: 支付常见问题排查手册，覆盖支付成功未回调、签名错误和退款串行控制。
keywords:
  - 支付网关
  - 统一下单
  - SIGN_ERROR
  - MD5 签名
  - notify_status
  - SUCCESS
  - 微信回调
  - 退款
  - Redis 分布式锁
  - 支付未回调
  - 签名错误
  - 串行控制
  - 故障排查
  - FAQ
acl:
  allow:
    - "$authenticated"
---

# 支付网关常见问题解答

### Q: 为什么订单明明支付成功了，但业务系统一直没有收到回调？
**A:** 请按以下步骤排查：
1. 检查 `t_pay_record` 表中的 `notify_status` 是否为 `SUCCESS`。
2. 确认支付网关的出口 IP 是否被你们业务模块的防火墙拦截。
3. 微信支付的回调存在延迟，请检查是否在 15s、30s、3m 的重试阶梯中。

### Q: 调用统一下单接口时，报 `SIGN_ERROR` 签名错误怎么办？
**A:** 签名错误 90% 是因为参数排序不对。请确保传入的 JSON 参数在生成 MD5 之前，已经按照 ASCII 码从小到大进行了字典序排序，并且最后拼接了 `&key=商户秘钥`。

### Q: 退款接口可以并发调用吗？
**A:** 绝对不行。同一个 `order_no` 的退款请求必须串行。并发调用会导致微信底层重复扣款，引发极高的客诉风险。请在调用前加上 Redis 分布式锁。