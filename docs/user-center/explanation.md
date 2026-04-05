---
domain: user-center
type: explanation
version: v1.0
description: 说明双 Token 鉴权架构、Token 生命周期和网关统一鉴权方式。
keywords:
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
  - 双 Token
  - Token 生命周期
  - 网关鉴权
  - 鉴权架构
acl:
  allow:
    - "$authenticated"
---

# 用户认证系统架构说明

## 1. 双 Token 鉴权机制
为了兼顾安全性和用户体验，【用户中心】采用了双 Token 机制，即 Access Token（访问令牌）和 Refresh Token（刷新令牌）。
传统的单 Token 设计一旦泄露，攻击者在有效期内可以随意调用接口。引入 Refresh Token 后，Access Token 的有效期可以设置得很短，从而大幅降低泄露风险。

## 2. Token 生命周期
* **Access Token**: 有效期为 2 小时。用于请求各个微服务的业务接口。
* **Refresh Token**: 有效期为 30 天。仅用于向【用户中心】换取新的 Access Token。

## 3. 跨域会话保持
在微服务架构下，网关层（API Gateway）负责统一解析 Access Token。下游业务子系统（如订单中心、支付中心）不需要自己校验 Token，只需读取请求头中的 `X-User-Id` 即可识别当前用户身份。