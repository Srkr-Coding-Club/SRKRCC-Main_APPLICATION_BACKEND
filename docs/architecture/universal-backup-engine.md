# Universal Backup Engine & Lifecycle Architecture

## 1. Core Principle: Universal Backup ≠ Universal Import

Every uploaded backup file is **first and foremost an immutable backup artifact**, not an assumed application model. 

```text
UPLOAD SPREADSHEET (.csv / .xlsx)
              │
              ▼
┌───────────────────────────┐
│ Backup Intake & Hashing   │
│ SHA-256 + Disk Storage    │
└─────────────┬─────────────┘
              │
              ▼
          BackupJob
              │
              ▼
Admin Selects Target Domain
(or accepts AI/heuristic suggestion)
              │
  ┌───────────┼───────────┬─────────────┬─────────────┐
  ▼           ▼           ▼             ▼             ▼
USERS       FORMS       EVENTS     HACKATHONS    UNKNOWN_RAW
  │           │           │             │             │
  └───────────┴───────────┴─────────────┘             │
              │                                       │
              ▼                                       │
     Column Match & 50% Rule                          │
              │                                       │
              ▼                                       │
       Preview Snapshot                               │
              │                                       │
              ▼                                       ▼
     Confirmed Commit                        Raw Backup Vault
              │                        (RawBackupArchive + Rows)
              ▼
    Domain Application Models
   (User, Event, Hackathon)
```

---

## 2. Decoupled Lifecycle: `BackupJob` vs `ImportAttempt`

One `BackupJob` can have multiple `ImportAttempt` records.
- If an admin uploads `backup_2025.xlsx`, it creates a `BackupJob(status='AWAITING_DOMAIN_SELECTION')`.
- Attempt #1 as `USERS` might be rejected due to missing columns.
- Attempt #2 as `EVENTS` might also be rejected.
- Attempt #3 as `UNKNOWN_RAW` safely archives the backup in the schemaless Raw Vault without re-uploading the file.

### Decoupled Statuses:
- **`BackupJob`**: `PENDING`, `PARSED`, `AWAITING_DOMAIN_SELECTION`, `READY`, `PARTIALLY_IMPORTED`, `ARCHIVED_RAW`, `FAILED`, `EXPIRED`.
- **`ImportAttempt`**: `PREVIEWED`, `COMMITTED`, `REJECTED`, `FAILED`.

---

## 3. The 4-Stage Ingestion Protocol

| Stage | Action | Backend Component | Invariants |
| :--- | :--- | :--- | :--- |
| **Stage 1: Intake** | Upload, compute SHA-256, store pristine file, parse headers & row count. | `UniversalBackupService.intake_backup_file` | **No Implicit Mutation**: Never creates application records. |
| **Stage 2: Analysis** | Schema-level matching of uploaded headers against domain expected fields. | `UniversalBackupService.analyze_domain` | Deduplicates aliases; evaluates exact 50% rule. |
| **Stage 3: Preview** | Row-level inspection, collision detection, and row-level provenance. | `UniversalBackupService.generate_preview` | Saves `ImportRow` records with unmapped columns preserved in `raw_data`. |
| **Stage 4: Commit** | Atomic persistence into target application models under savepoints. | `UniversalBackupService.commit_import` | Idempotent via `idempotency_key`; syncs Club ID watermarks. |

---

## 4. Security & Storage Policies

1. **Storage Isolation**: Uploaded files are stored in `media/backups/<uuid>.<ext>` with sanitized filenames.
2. **Access Control**: All backup endpoints require `ADMIN` or `CLUB_LEAD` authorization.
3. **No Public URLs**: Direct file access is blocked; downloads must pass through authenticated `GET /api/admin/backups/<id>/raw-download/`.
4. **Limits**: Max file size 10 MB, max rows 10,000, max columns 50, max cell length 10,000 characters.
5. **Retention**: Incomplete/pending backups expire after 24 hours; committed backups and Raw Vault archives are retained permanently.
