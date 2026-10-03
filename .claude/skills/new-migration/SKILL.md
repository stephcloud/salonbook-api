---
name: new-migration
description: Generate and review an Alembic migration after model changes.
disable-model-invocation: true
---

# New Migration

1. Ask for a short descriptive message (snake_case), e.g. `add_avatar_url_to_users`.
2. Run `alembic revision --autogenerate -m "<message>"`.
3. Open the generated file and review it. Check for:
   - Unintended drops or renames (autogenerate sees a rename as drop + add)
   - Missing indexes on foreign keys and frequently filtered columns
   - NOT NULL columns added to existing tables without a default
   - A working `downgrade()`
4. Fix issues in the new file only; never edit older migrations.
5. Show the user the summary and ask before running `alembic upgrade head`.
