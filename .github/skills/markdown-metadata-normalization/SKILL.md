---
name: markdown-metadata-normalization
description: 'Use when normalizing Markdown frontmatter in docs/*.md so that document summaries and keywords are derived directly from section metadata, sections keep only title entries, and the document metadata mirrors the latest structure.'
argument-hint: 'Optional markdown file, docs folder, or docs/module-directory.yaml target'
user-invocable: true
disable-model-invocation: false
---

# Markdown Metadata Normalization

## When to Use
- Editing or reviewing Markdown files under `docs/**/*.md`
- Normalizing frontmatter that contains `summary`, `keywords`, and `sections`
- Converting section-level metadata into top-level document metadata
- Rewriting headings so each title summarizes the paragraph content while preserving key terms

## Required Rules
1. Rewrite the document's headings first so each title summarizes the surrounding paragraph content while preserving important keywords and stable domain terms, and collect each section's title, summary, and keywords in the same pass.
2. Build the top-level `summary` and `keywords` directly from the collected section metadata: concatenate summaries in document order and deduplicate keywords while preserving order.
3. Keep `sections` as an ordered list of title-only entries, and preserve the document's existing `domain`, `type`, `version`, `acl`, and other unrelated frontmatter fields.

## Normalization Flow
1. Rewrite the headings first and collect each section's `title`, `summary`, and `keywords` in the same pass.
2. Merge the collected summaries into the top-level `summary` and the collected keywords into the top-level `keywords`.
3. Rewrite `sections` as `{ title: ... }` only, then leave the body content unchanged unless headings were also rewritten.

## Checklist
Before saving a normalized doc, confirm:
- `summary` reads like a single document-level summary, not a section list
- `keywords` still cover the document's main topics and stable terms
- `sections` contains only titles
- No section summary or section keyword field remains

## Example Pattern
```markdown
---
summary: First section summary. Second section summary.
keywords:
  - topic-a
  - topic-b
sections:
  - title: Section One
  - title: Section Two
---
```