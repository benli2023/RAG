---
name: python-install-env-check
description: 'Use when running python installs, pip install, pip uninstall, or debugging package installation. Always check the active virtual environment and interpreter before changing Python packages.'
argument-hint: 'Optional package or environment target'
user-invocable: true
disable-model-invocation: false
---

# Python Install Environment Check

## When to Use
- Running any Python package install, upgrade, or uninstall
- Using pip, pip3, or python -m pip
- Debugging environment mismatches between global Python and a virtual environment

## Required Procedure
1. Check the active environment before touching packages.
2. Always prefer the project virtual environment when one exists.
3. Avoid global installs unless the user explicitly asks for them.

## Environment Check
Run these checks first:

    echo "VIRTUAL_ENV=${VIRTUAL_ENV:-}"
    which python3
    python3 -c 'import sys; print(sys.executable)'

If VIRTUAL_ENV is empty and the repository has a local .venv, activate it before installing packages.
If no virtual environment is available, ask the user which interpreter or environment to use before installing anything.

## Installation Rules
- Use python3 -m pip install instead of bare pip install.
- Use python3 -m pip uninstall instead of bare pip uninstall.
- Do not install packages into the global interpreter unless the user explicitly approves it.
- If the command must run outside a virtual environment, state that clearly before proceeding.

## Verification
After installation, verify the result with one of these:
- python3 -m pip show <package>
- python3 -c 'import <package>; print(<package>.__file__)'

## Failure Handling
If the active interpreter is not the intended one, stop and correct the environment first.
If package installation fails because dependencies are missing, report the missing package names and the interpreter path you used.