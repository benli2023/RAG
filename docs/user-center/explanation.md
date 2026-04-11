---
domain: user-center
type: explanation
version: v1.0
summary: 说明访问令牌与刷新令牌各自的职责，以及为什么要把短期访问与长期续期拆开。给出 Access Token 和 Refresh Token 的有效期与使用边界。说明网关如何统一校验 Access Token，并把用户身份透传给下游服务。
keywords:
  - 双 Token
  - Access Token
  - Refresh Token
  - 鉴权架构
  - Token 生命周期
  - 2小时
  - 30天
  - 网关鉴权
  - Authorization Bearer
  - X-User-Id
  - 统一身份透传
sections:
  - title: 双 Token 职责分层：Access Token 与 Refresh Token
  - title: Token 生命周期：2 小时访问令牌与 30 天刷新令牌
  - title: 网关统一鉴权：Authorization Bearer 与 X-User-Id 透传
acl:
  allow:
    - "$authenticated"
---

# 用户中心双 Token 鉴权、Token 生命周期与网关透传说明

## 双 Token 职责分层：Access Token 与 Refresh Token
为了兼顾安全性和用户体验，【用户中心】采用了双 Token 机制，即 Access Token（访问令牌）和 Refresh Token（刷新令牌）。
传统的单 Token 设计一旦泄露，攻击者在有效期内可以随意调用接口。引入 Refresh Token 后，Access Token 的有效期可以设置得很短，从而大幅降低泄露风险。

## Token 生命周期：2 小时访问令牌与 30 天刷新令牌
* **Access Token**: 有效期为 2 小时。用于请求各个微服务的业务接口。
* **Refresh Token**: 有效期为 30 天。仅用于向【用户中心】换取新的 Access Token。

## 网关统一鉴权：Authorization Bearer 与 X-User-Id 透传
在微服务架构下，网关层（API Gateway）负责统一解析 Access Token。下游业务子系统（如订单中心、支付中心）不需要自己校验 Token，只需读取请求头中的 `X-User-Id` 即可识别当前用户身份。