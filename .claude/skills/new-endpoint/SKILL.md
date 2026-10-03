---
name: new-endpoint
description: Add a new API resource or endpoint (model, schemas, service, router, tests). Use when the user asks for a new route or CRUD resource.
---

# New Endpoint

Ask (if not given): resource name, fields, which operations (create/read/list/update/delete), auth required or not.

Follow the conventions in `CLAUDE.md`. Create or update, in this order:
1. `app/models/<resource>.py`: SQLAlchemy model (plural table name).
2. `app/schemas/<resource>.py`: `<Resource>Create`, `<Resource>Update`, `<Resource>Response`.
3. `app/services/<resource>_service.py`: business logic and DB access.
4. `app/api/v1/<resource>s.py`: thin router; register it in `app/main.py`.
5. `app/tests/api/test_<resource>s.py`: success and failure cases for each route.
6. Remind the user to run `/new-migration` for the model change.

Then run `/test` and fix failures. Summarize routes added.
