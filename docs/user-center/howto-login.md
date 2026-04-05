---
domain: user-center
type: how-to
version: v1.0
description: 规范前端登录接入流程，包括登录、存储 Token、鉴权请求和过期刷新。
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
  - 前端登录
  - Token 存储
  - 过期刷新
  - 鉴权请求
acl:
  allow:
    - "$authenticated"
---

# 指南：前端如何实现标准化登录流程

当客户端（Web/App）需要接入用户认证时，必须严格按以下步骤操作：

## 1. 发起登录请求
收集用户输入的手机号和密码，调用【用户中心】的 `POST /api/v1/user/login` 接口。
如果接口返回错误码 `10003`，前端应提示“用户名或密码错误”，并清空密码输入框。

## 2. 存储 Token 数据
登录成功后，前端必须将返回的 `access_token` 和 `refresh_token` 存储在本地安全的存储中（例如浏览器的 HttpOnly Cookie 或 App 的安全沙箱中，严禁存在 LocalStorage 以防 XSS 攻击）。

## 3. 携带 Token 发起业务请求
后续请求【订单中心】等任何业务接口时，必须在 HTTP Header 中携带 `Authorization: Bearer <access_token>`。

## 4. 处理 Token 过期 (无感刷新)
如果业务接口返回 HTTP 状态码 `401 Unauthorized`，说明 Access Token 已过期。前端需静默调用 `/api/v1/user/refresh` 接口换取新 Token，并重新发起刚才失败的业务请求。