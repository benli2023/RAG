---
name: database-reference-keyword-rules
description: 'Use when editing or creating database reference Markdown files such as docs/**/reference-*.md, especially when maintaining frontmatter keywords for table or field documentation. Apply the keyword rules for module names, table names, document purpose terms, and exact heading text.'
argument-hint: 'Optional markdown file or keyword list target'
user-invocable: true
disable-model-invocation: false
---

# Database Reference Keyword Rules

## When to Use
- Editing database reference Markdown files under `docs/**/reference-*.md`
- Reviewing or generating frontmatter `keywords` for schema, table, or field documentation
- Standardizing keyword lists across related database reference docs

## Required Rules
1. Use only stable, document-level sources for keywords.
2. Include the module name or alias, the table name, and document-purpose terms.
3. Include the exact H1 and H2 heading text when those headings exist in the document.
4. Keep explicitly requested exceptions, such as `数据库字段`, even if they are broader than the default rule.
5. Do not derive keywords from column names, enum values, API payload fields, or implementation details unless the user explicitly asks for them.
6. Avoid adding duplicate or near-duplicate keywords.

## Keyword Sources
Preferred sources, in order:
- Module name or alias, such as `订单中心` or `用户中心`
- Table name, such as `t_order`
- Document-purpose terms, such as `字段说明`, `状态枚举`, `枚举字典`, or `API 参考`
- Exact heading text from H1 and H2 sections
- Explicit user-approved exceptions

## What to Exclude by Default
- Column or field names such as `order_no`, `user_id`, or `access_token`
- Enum values such as `INIT`, `PAID`, or `CANCELED`
- Route-specific or payload-specific details unless they are part of the document title or heading
- Implementation details, infrastructure names, or operational notes

## Checklist
Before saving a reference doc, confirm:
- The keywords describe the document, not the internal fields
- The H1 and H2 titles are represented if they are meaningful keywords
- The list stays short and readable
- Any special-case keyword was explicitly requested or justified

## Example Pattern
```markdown
keywords:
  - 订单中心
  - 订单数据库设计规范
  - 订单主表
  - 核心订单主表
  - 核心订单主表 (t_order)
  - t_order
  - 字段说明
  - 数据库字段
  - 状态枚举
  - 枚举字典
  - order_status 状态枚举字典
```
