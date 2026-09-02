# Domain Importers & Extensible Registry

## 1. Importer Contract (`BaseBackupImporter`)

All domain backup importers implement the uniform `BaseBackupImporter` contract defined in [`apps/core/services/backup/base_importer.py`](file:///c:/Users/chall/OneDrive/Desktop/SRKRCC-Main_APPLICATION_BACKEND/apps/core/services/backup/base_importer.py):

```python
class BaseBackupImporter(ABC):
    domain_key: str
    display_name: str
    required_fields: list[str]
    expected_fields: dict[str, list[str]]

    @abstractmethod
    def calculate_confidence(self, headers: list[str]) -> tuple[bool, Decimal, dict[str, str], list[str]]:
        ...

    @abstractmethod
    def build_normalized_mapping(self, custom_mapping: dict[str, str] | None, headers: list[str]) -> dict[str, str]:
        ...

    @abstractmethod
    def preview(self, backup_job: BackupJob, mapping: dict[str, str], context: dict | None) -> ImportAttempt:
        ...

    @abstractmethod
    def commit(self, import_attempt: ImportAttempt, user: Any, options: dict | None) -> dict[str, Any]:
        ...
```

---

## 2. Currently Registered Domains

| Domain Key | Importer Class | Application Target | Deduplication Key |
| :--- | :--- | :--- | :--- |
| `USERS` | `UserBackupImporter` | `accounts.User` | Existing: `club_id` OR `email`; New: `email` required. |
| `EVENTS` | `EventBackupImporter` | `events.Event` | `title` + `start_time` |
| `HACKATHONS` | `HackathonBackupImporter` | `hackathons.Hackathon` | `title` + `start_date` |
| `FORMS` | `FormSubmissionImporter` | `forms.Response` + `Answer` | Target published Form schema |
| `UNKNOWN_RAW` | `RawVaultImporter` | `core.RawBackupArchive` + `Row` | Schemaless / Zero restrictions |

---

## 3. How to Add a New Backup Domain (Step-by-Step Guide)

To add a new backup domain (e.g. `CERTIFICATIONS` or `ATTENDANCE`), follow this 6-step recipe without modifying the universal intake engine:

### Step 1: Create Domain Importer
Create `apps/core/services/backup/certification_importer.py`:
```python
from apps.core.services.backup.base_importer import BaseBackupImporter

class CertificationBackupImporter(BaseBackupImporter):
    domain_key = 'CERTIFICATIONS'
    display_name = 'Certifications & Badges'
    required_fields = ['student_email', 'certificate_title']
    expected_fields = {
        'student_email': ['email', 'student email'],
        'certificate_title': ['title', 'certificate', 'course'],
        'issued_date': ['date', 'issue date', 'issued at'],
        'credential_url': ['url', 'verification url'],
    }
    # Implement calculate_confidence, build_normalized_mapping, preview, and commit
```

### Step 2: Register in `BackupImporterRegistry`
Add the new importer to `apps/core/services/backup/registry.py`:
```python
cls.register(CertificationBackupImporter())
```

### Step 3: Verify Domain Schema API
The endpoint `GET /api/admin/backups/domains/` will automatically expose the new domain schema, expected fields, and required keys to the frontend dynamically.

### Step 4: Add Unit Tests
Add test cases in `apps/core/tests/test_universal_backup_engine.py` testing the 50% threshold, alias deduplication, preview, and atomic commit.

### Step 5: Update Documentation
Update `backup-data-contract.md` with the new domain's expected canonical fields and identity strategies.
