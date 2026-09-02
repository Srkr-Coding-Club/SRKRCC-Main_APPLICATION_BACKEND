# Password Setup Lifecycle Architecture Guide

## 1. Overview & Credential Safety Invariant

The **Password Setup Lifecycle** governs how members restored from legacy spreadsheet backups establish secure authentication credentials.

```text
Spreadsheet Upload (USERS)
         │
         ▼
User Created (Club ID Assigned)
         │
         ▼
user.set_unusable_password()
password_status = 'NEEDS_SETUP'
         │
         ▼
Member Attempts Sign-in
         │
         ▼
403 Forbidden: PASSWORD_SETUP_REQUIRED
         │
         ▼
Rate-Limited Setup Request (5/hr/email)
         │
         ▼
One-Time Cryptographic Token Created (SHA-256 Hashed in DB)
Email Dispatched: /account/setup-password?token={raw_token}
         │
         ▼
Member Confirms New Password (select_for_update Row Lock)
         │
         ▼
user.set_password(new_pwd)
password_status = 'ACTIVE'
Remaining Tokens Invalidated
         │
         ▼
Normal Sign-In Enabled
```

---

## 2. Invariant Matrix

| Scenario | Stored Password | `password_status` | Can Login? |
| :--- | :--- | :--- | :--- |
| **New member from backup** | Unusable (`!`) | `NEEDS_SETUP` | ❌ (Prompts setup link) |
| **Existing member with password** | Existing hash (untouched) | `ACTIVE` | ✅ |
| **Existing member with unusable password** | Unusable (`!`) | `NEEDS_SETUP` | ❌ (Prompts setup link) |
| **User completes setup flow** | New bcrypt/pbkdf2 hash | `ACTIVE` | ✅ |
| **Re-import of same backup spreadsheet** | Untouched | `ACTIVE` | ✅ |

---

## 3. Security Specifications

1. **Zero Raw Token Logging**:
   - Raw tokens are never logged in application logs, audit logs, database tables, or exception traces.
   - Only SHA-256 hex digests (`len == 64`) are stored in `PasswordSetupToken.token_hash`.
2. **Concurrency & Replay Defense**:
   - `PasswordSetupService.confirm_password_setup()` executes with `select_for_update()` inside `transaction.atomic()`.
   - Replay attacks are immediately rejected with `InvalidSetupTokenError`.
3. **Anti-Enumeration vs. UX**:
   - `POST /api/auth/login/`: Returns `PASSWORD_SETUP_REQUIRED` without the email to provide immediate actionable UX.
   - `POST /api/auth/setup-password/request/`: Strictly non-enumerating. Returns identical generic 200 OK whether the email exists or not.
