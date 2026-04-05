---
name: python-run-with-venv-visible-output
description: 'Use when running Python commands in this repository. Always use .venv/bin/python, and make sure terminal output is brought back into the chat when commands run.'
argument-hint: 'Optional script or command target'
user-invocable: true
disable-model-invocation: false
---

# Python Run With Venv And Visible Output

## When to Use
- Running any Python script, module, or helper command in this repository
- Debugging or validating backend scripts, tests, or retrieval pipelines
- Capturing command output so it is visible in the chat instead of hidden in the terminal only

## Required Procedure
1. Prefer the repository virtual environment at .venv.
2. Run Python commands with .venv/bin/python.
3. If .venv does not exist, create it before running project Python commands.
4. When a command produces useful logs or runs for a while, fetch the terminal output and share the important result back in chat.

## Command Rules
- Use .venv/bin/python for scripts and modules.
- Use .venv/bin/python -m pip for package management inside the virtual environment.
- Avoid bare python3 for project commands unless the user explicitly asks for system Python.
- Do not switch to the global interpreter when the repo venv is available.

## Output Visibility
- Prefer running long or noisy commands in a terminal session.
- After the command starts, collect the terminal output with the terminal output tools before replying.
- Include the latest meaningful console lines in the chat so the user can see what happened.
- If the command fails, surface the failure output clearly instead of only summarizing it.

## Verification
- Confirm the interpreter with .venv/bin/python -c 'import sys; print(sys.executable)'
- Confirm the runtime result by reading the terminal output or awaiting the terminal to completion

## Failure Handling
- If .venv is missing and cannot be created, ask the user how they want Python commands executed.
- If the terminal output is truncated, retrieve the latest output again and report the last visible progress point.