---
name: test
description: Run lint and the pytest suite, then summarize and fix failures.
allowed-tools: Bash(pytest:*), Bash(ruff check:*), Bash(ruff format:*)
---

# Test

1. `ruff check .` and `ruff format --check .`; fix simple issues.
2. `pytest -q`
3. If tests fail: read the failure, identify whether the bug is in the code or the test, fix the cause, and re-run.
4. Report: passed/failed counts and anything you changed.

Do not delete or weaken tests to make them pass.
