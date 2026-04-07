---
name: api-reference-keyword-rules
description: 'Use when editing or creating API reference Markdown files such as docs/**/reference-api.md, especially when maintaining frontmatter keywords for interface documentation. Apply the keyword rules for module names, API reference titles, document-purpose terms, and exact heading text.'
argument-hint: 'Optional markdown file or keyword list target'
user-invocable: true
disable-model-invocation: false
---

# API Reference Keyword Rules

## When to Use
- Editing API reference Markdown files under `docs/**/reference-api.md`
- Reviewing or generating frontmatter `keywords` for interface, endpoint, request, or response documentation
- Standardizing keyword lists across related API reference docs

## Required Rules
1. Use only stable, document-level sources for keywords.
2. Include the module name or alias, the API reference title, and document-purpose terms.
3. Include the exact H1 and H2 heading text when those headings exist in the document.
4. Keep explicitly requested exceptions when a user asks to preserve a broader keyword.
5. Do not derive keywords from field names, response properties, token names, enum values, HTTP methods, or path fragments unless the user explicitly asks for them.
6. Avoid adding duplicate or near-duplicate keywords.

## Keyword Sources
Preferred sources, in order:
- Module name or alias, such as `用户中心` or `订单中心`
- API reference title, such as `用户中心 API 参考手册`
- Document-purpose terms, such as `请求参数`, `响应参数`, `业务错误码字典`, or `API 参考`
- Exact heading text from H1 and H2 sections
- Explicit user-approved exceptions

## What to Exclude by Default
- Request or response field names such as `phone`, `password`, `access_token`, or `refresh_token`
- Error code values such as `10001` or `401 Unauthorized`
- URL paths, HTTP methods, and route fragments unless they are part of the title or heading text
- Implementation details, infrastructure names, or operational notes

## Checklist
Before saving an API reference doc, confirm:
- The keywords describe the document, not the payload fields
- The H1 and H2 titles are represented if they are meaningful keywords
- The list stays short and readable
- Any special-case keyword was explicitly requested or justified

## Example Pattern
```markdown
keywords:
  - 用户中心
  - 用户中心 API 参考手册
  - 账号密码登录接口
  - 请求参数
  - 响应参数
  - 业务错误码字典
  - API 参考
```
