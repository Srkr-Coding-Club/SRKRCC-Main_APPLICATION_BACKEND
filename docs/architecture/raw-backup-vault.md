# Raw Backup Vault Architecture

## 1. Purpose of the Schemaless Raw Vault

In university clubs, administrators frequently receive legacy spreadsheets, third-party competition exports, external workshop registrations, and raw data dumps that do not conform to any single application domain schema.

Rather than forcing these spreadsheets into rigid schemas or throwing validation errors, the **Raw Backup Vault** (`UNKNOWN_RAW`) provides a **true schemaless, forensic repository**.

---

## 2. Data Model: `RawBackupArchive` & `RawBackupRow`

```python
class RawBackupArchive(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    backup_job = models.OneToOneField(BackupJob, on_delete=models.CASCADE, related_name='raw_archive')
    title = models.CharField(max_length=255)
    category = models.CharField(max_length=50, default='UNKNOWN_RAW')
    headers = models.JSONField(default=list)
    total_rows = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

class RawBackupRow(TimeStampedModel):
    archive = models.ForeignKey(RawBackupArchive, on_delete=models.CASCADE, related_name='rows')
    row_number = models.PositiveIntegerField()
    row_data = models.JSONField(default=dict)
```

### Why Row-Level Provenance?
Instead of storing a monolithic multi-megabyte JSON blob in a single column:
1. **Queryable**: Individual rows can be indexed and searched via PostgreSQL JSONB operators (`row_data->>'Student ID'`).
2. **Streaming & Pagination**: Large 10,000-row archives can be paged efficiently without loading the entire dataset into server memory.
3. **Future Domain Promotion**: Raw archives can later be mapped and promoted to structured models (`User`, `Event`) when a new domain is introduced.

---

## 3. The "Save as Raw Backup" Escape Hatch

Even when an administrator selects a structured domain (e.g. `USERS` or `EVENTS`), if validation fails because:
- Essential keys are missing (e.g. no Email column found), or
- Column matching confidence is below the 50% threshold,

The UI immediately offers the **"Save as Raw Backup Vault"** escape hatch. The file and all its rows are stored without losing any information.
