# SalonBook API: Security Plan

Each item says **where** it fits in `BUILD_GUIDE.md` and gives a **prompt**.
Use the loop from the guide: `/clear`, prompt, check the diff, `/test`, `/commit`.

## Setup (do first, once)
1. Paste `claude-md-security-section.md` into `CLAUDE.md` (before "Rules for Claude").
2. Copy `security-reviewer.md` into `.claude/agents/`.
3. On GitHub: repo **Settings → Code security**, turn on **secret scanning** and **push protection**, and enable **Dependabot alerts**.
4. Commit: `chore: add security rules and security-reviewer subagent`.

---

## Tier 1: Do now (Days 3-9)

### 1. Argon2 + JWT with expiry + input validation (Guide Step 5)
```
Plan authentication following the Security rules in CLAUDE.md. Use pwdlib[argon2] and
PyJWT (access token expiry 30 minutes). Public register allows only client and owner.
Login returns the same generic error for wrong email or wrong password. Validate inputs
with Pydantic (EmailStr, password min length 8, name length limits). Response models must
not include password_hash. Show the plan and dependencies. Do not write code until I approve.
```
After approving, add: `Write tests for each rule, including: a client cannot register as stylist, password never appears in any response, expired token is rejected.`

### 2. Ownership checks and tests (Guide Steps 6 and 7+)
Add this sentence to **every** `/new-endpoint` prompt:
```
Add ownership checks per the Security rules, and tests where user A cannot read, edit,
or delete user B's data (expect 403 or 404).
```

### 3. Strict CORS (Guide Step 3 review)
```
Review CORS in app/main.py. It must use an explicit allow-list from CORS_ORIGINS, never "*",
with only the methods and headers we need. Show me the code and explain each setting.
```

### 4. Signature-verified idempotent webhooks (Guide Step 9)
Already in the guide. After building it:
```
use the security-reviewer subagent to review the payment and webhook code
```

### 5. Secrets in env, pinned dependencies (done), pip-audit
```
Add pip-audit to requirements.txt (pinned) and add it to the /test skill as a final step.
Run it and tell me about any vulnerable packages and how to fix them.
```

### 6. Pending-booking expiry (Guide Step 7.4)
A database constraint cannot use "now", so use two layers:
```
Implement pending-booking expiry: bookings in "pending" status older than 15 minutes
count as expired. (1) When creating a booking, inside the same transaction first cancel
expired pending bookings for that stylist so they stop blocking the slot. (2) Add a small
cleanup function that cancels all expired pending bookings, callable from a scheduled task.
Explain why a database constraint alone can't do this. Write tests: a pending booking
older than 15 minutes no longer blocks the slot; a recent one still does.
```

---

## Tier 2: Add if time allows (Day 8+)

### 7. Login rate limiting (needs Redis)
```
Add rate limiting to POST /auth/login and POST /auth/register using Redis: max 5 failed
logins per email and per IP in 15 minutes, then return 429. Explain the approach before
coding. Write tests.
```
**Note:** this keeps Redis in the project. Don't remove it if you do this.

### 8. Refresh tokens
```
Plan refresh tokens: short-lived access token (15-30 min) plus a refresh token that can
be revoked (store in Redis or the database). Add POST /auth/refresh and POST /auth/logout.
Show the plan first.
```

### 9. Security headers
```
Add middleware that sets X-Content-Type-Options, X-Frame-Options, Referrer-Policy and
Strict-Transport-Security. Explain what each header does.
```

---

## Tier 3: Before real launch (NOT part of the two-week MVP)

These take real time and some need decisions beyond code. Keep this list in your README under "Production roadmap" to show you know what's missing.

- [ ] Email verification (needs Resend with a verified domain)
- [ ] Password reset (single-use, short-lived tokens)
- [ ] MFA for salon owners
- [ ] Error monitoring (e.g. Sentry) and alerts
- [ ] Nigeria Data Protection Act 2023 compliance: privacy policy, consent, breach process, user data deletion (check requirements with a professional)
- [ ] Database backups with a tested restore
- [ ] Non-root containers, least-privilege DB user, `/docs` hidden in production
- [ ] httpOnly cookie auth on the frontend, with CSRF handling

---

## Review checkpoints
Run `use the security-reviewer subagent` after: auth (Step 5), salons and services (Step 6), bookings (Step 7), payments (Step 9), and before deploy (Step 13). Fix Critical and High findings before moving on.

## Viva answers
- **What's the top API risk?** Broken object-level authorization (OWASP API #1). My ownership checks and the tests where user A touches user B's data address it.
- **Why Argon2?** It is memory-hard, so brute-forcing hashes is expensive, and salting is automatic.
- **Why verify the webhook signature?** Anyone can POST to my webhook URL. The signature proves the request came from Paystack.
- **Why can't expiry live in the database constraint?** Constraints can't depend on the current time, so I expire pending bookings in the transaction and with a cleanup job.
