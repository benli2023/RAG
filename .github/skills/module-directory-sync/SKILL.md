---
name: module-directory-sync
description: 'Use when syncing docs/module-directory.yaml from docs/*.md so file summaries come from each Markdown file''s top-level frontmatter summary, file sections mirror the Markdown section titles, and module/file metadata stays aligned with the latest docs tree.'
argument-hint: 'docs/module-directory.yaml or docs folder'
user-invocable: true
disable-model-invocation: false
---

# Module Directory Sync

## When to Use
- Syncing `docs/module-directory.yaml` with the current Markdown documents under `docs/**/*.md`
- Copying Markdown frontmatter summaries into module-directory file summaries
- Copying Markdown section titles into module-directory file section lists
- Removing derived `keywords` from module and file entries when they are present, unless the user explicitly wants to keep them
- Verifying the YAML structure still parses cleanly after sync

## Required Rules
1. Read each source Markdown file referenced by `docs/module-directory.yaml` before editing the directory file.
2. Copy the Markdown file's top-level frontmatter `summary` into the matching `files[].summary` entry.
3. Copy the Markdown file's `sections[].title` values into the matching `files[].sections` entry as title-only items.
4. Preserve `domain`, `name`, file paths, and other non-derived module-directory fields unless the user asks for a wider schema change.
5. Keep the module-directory structure aligned with the source docs tree and the latest Markdown frontmatter.
6. Re-validate the YAML structure after edits so each file entry contains only the fields required by the current schema.

## Sync Flow
1. Open the source Markdown file for each `files[].name` entry.
2. Read the Markdown document's top-level frontmatter and section titles.
3. Update the matching `files[].summary` from the frontmatter summary.
4. Update the matching `files[].sections` from the Markdown `sections[].title` values.
5. Remove `keywords` fields from module and file entries when present; if none exist, leave the YAML unchanged.
6. Validate the resulting YAML parses successfully and still matches the docs tree.

## Checklist
Before saving a synced module directory, confirm:
- Each `files[].summary` matches the corresponding Markdown document's top-level frontmatter summary
- Each `files[].sections` contains only the section titles from the corresponding Markdown document
- No `keywords` remain at the module or file level unless explicitly requested
- The YAML still parses cleanly

## Example Pattern
```yaml
modules:
  - domain: example
    files:
      - name: example/topic.md
        summary: Top-level frontmatter summary from example/topic.md
        sections:
          - title: Section A
          - title: Section B
```