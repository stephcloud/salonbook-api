---
name: commit
description: Review staged changes and create a Conventional Commit message.
disable-model-invocation: true
allowed-tools: Bash(git status:*), Bash(git diff:*), Bash(git add:*), Bash(git commit:*)
---

# Commit

1. `git status` and `git diff --staged` (if nothing is staged, show the unstaged diff and ask what to include).
2. Warn if the diff contains secrets, `.env` content, or large unrelated changes.
3. Write a Conventional Commit message: `type(scope): short summary` (types: feat, fix, docs, refactor, test, chore). Add a short body if the change is non-obvious.
4. Show the message and commit after the user approves.

Never push.
