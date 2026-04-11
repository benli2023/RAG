---
domain: user-center
type: reference
version: v1.0
summary: 描述登录接口的功能、请求参数、响应参数和错误码。列出手机号和密码两个请求字段及其校验要求。说明 user_id、access_token 和 refresh_token 的返回含义。收录登录失败时的业务错误码与前端提示含义。
keywords:
  - /api/v1/user/login
  - 账号密码登录接口
  - API 参考
  - 请求参数
  - Request Body
  - 响应参数
  - Response Data
  - 业务错误码字典
  - 10001
  - 10002
  - 10003
  - 10004
sections:
  - title: POST /api/v1/user/login 账号密码登录接口
  - title: Request Body 请求参数
  - title: Response Data 响应参数
  - title: 业务错误码字典
acl:
  groups:
    - iam-admin
  users:
    - zhangsan
---

# 用户中心 /api/v1/user/login 接口参考手册

## 账号密码登录接口：POST /api/v1/user/login
**接口路径**: `POST /api/v1/user/login`
**功能描述**: 验证用户手机号与密码，成功后发放鉴权 Token。

### 请求参数 (Request Body)
| 字段名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| phone | String | 是 | 11位手机号 |
| password | String | 是 | 明文密码（前端需做基础校验） |

### 响应参数 (Response Data)
| 字段名 | 类型 | 说明 |
|---|---|---|
| user_id | Long | 全局唯一的用户ID |
| access_token | String | 业务访问令牌 |
| refresh_token | String | 用于续期的刷新令牌 |

### 业务错误码字典
* `10001`: 手机号格式错误
* `10002`: 用户不存在或已注销
* `10003`: 密码错误
* `10004`: 账号因安全原因被冻结