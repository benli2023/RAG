---
domain: user-center
type: how-to
version: v1.0
summary: 收集手机号和密码，调用用户中心登录接口完成首次鉴权。登录成功后把双 Token 放入安全存储，避免 LocalStorage 暴露风险。说明后续调用订单中心等接口时如何携带访问令牌。当 Access Token 过期时，前端通过 refresh 接口静默换取新令牌并重试请求。
keywords:
  - /api/v1/user/login
  - 前端登录
  - 登录请求
  - Token 存储
  - access_token
  - refresh_token
  - 安全存储
  - Authorization Bearer
  - 鉴权请求
  - 业务请求
  - 401 Unauthorized
  - 无感刷新
  - /api/v1/user/refresh
sections:
  - title: 调用 /api/v1/user/login 发起登录
  - title: 安全存储 access_token 与 refresh_token
  - title: 业务请求携带 Authorization Bearer
  - title: 401 无感刷新：调用 /api/v1/user/refresh 续期
acl:
  allow:
    - "$authenticated"
---

# 前端登录接入与无感刷新流程指南

当客户端（Web/App）需要接入用户认证时，必须严格按以下步骤操作：

## 调用 /api/v1/user/login 发起登录
收集用户输入的手机号和密码，调用【用户中心】的 `POST /api/v1/user/login` 接口。
如果接口返回错误码 `10003`，前端应提示“用户名或密码错误”，并清空密码输入框。

## 安全存储 access_token 与 refresh_token
登录成功后，前端必须将返回的 `access_token` 和 `refresh_token` 存储在本地安全的存储中（例如浏览器的 HttpOnly Cookie 或 App 的安全沙箱中，严禁存在 LocalStorage 以防 XSS 攻击）。

## 业务请求携带 Authorization Bearer
后续请求【订单中心】等任何业务接口时，必须在 HTTP Header 中携带 `Authorization: Bearer <access_token>`。

## 401 无感刷新：调用 /api/v1/user/refresh 续期
如果业务接口返回 HTTP 状态码 `401 Unauthorized`，说明 Access Token 已过期。前端需静默调用 `/api/v1/user/refresh` 接口换取新 Token，并重新发起刚才失败的业务请求。