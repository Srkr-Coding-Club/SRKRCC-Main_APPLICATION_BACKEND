# Mock & Sample Backup Datasets for SRKR Coding Club

This folder provides comprehensive, pre-formatted mock datasets in both **`.csv`** and **`.xlsx` (Excel)** formats.
Use these files to test and demonstrate all features of the **Universal Backup Engine**, **Schema Matching Gauge**, and **Password Setup Lifecycle**.

---

## Summary of Available Files

| File | Formats | Target Domain | Purpose / Recommended Workflow |
| :--- | :--- | :--- | :--- |
| `01_users_members_directory` | `.csv`, `.xlsx` | `USERS` | Test member ingestion, permanent Club ID mapping, and password setup link trigger. |
| `02_events_schedule` | `.csv`, `.xlsx` | `EVENTS` | Test event imports with dates, fees, venues, and types. |
| `03_hackathons_catalog` | `.csv`, `.xlsx` | `HACKATHONS` | Test hackathon competitions, team size constraints, and themes. |
| `04_forms_workshop_registrations` | `.csv`, `.xlsx` | `FORMS` | Test dynamic form builder submission ingestion with arbitrary questions. |
| `05_unknown_raw_legacy_inventory` | `.csv`, `.xlsx` | `UNKNOWN_RAW` | Test the **Schemaless Raw Vault** escape hatch (preserves 100% data with zero constraints). |

---

## Detailed Dataset Specifications

### `01_users_members_directory` (`.csv` & `.xlsx`)
- **Domain**: `USERS`
- **Description**: Student members directory for testing User domain restoration and password setup lifecycle.
- **Rows**: 10
- **Headers**: `Full Name, Email, Phone Number, Branch, Year of Study, Roll Number, Permanent Club ID, Membership Status`

### `02_events_schedule` (`.csv` & `.xlsx`)
- **Domain**: `EVENTS`
- **Description**: Club event schedule and workshop sessions for testing the Events domain import.
- **Rows**: 5
- **Headers**: `Event Name, Event Type, Status, Start Time, End Time, Venue, Description, Registration Fee`

### `03_hackathons_catalog` (`.csv` & `.xlsx`)
- **Domain**: `HACKATHONS`
- **Description**: Hackathon competition schedule and team parameters for testing the Hackathons domain.
- **Rows**: 3
- **Headers**: `Hackathon Title, Status, Start Date, End Date, Theme, Min Team Size, Max Team Size, Registration Fee`

### `04_forms_workshop_registrations` (`.csv` & `.xlsx`)
- **Domain**: `FORMS`
- **Description**: Responses and registration data for dynamic forms with custom fields.
- **Rows**: 5
- **Headers**: `Full Name, Email, Phone Number, Branch, T-Shirt Size, GitHub Profile, Dietary Preference, Prior Experience`

### `05_unknown_raw_legacy_inventory` (`.csv` & `.xlsx`)
- **Domain**: `UNKNOWN_RAW`
- **Description**: Schemaless legacy dataset (club hardware & lab inventory) to test the Raw Vault escape hatch.
- **Rows**: 6
- **Headers**: `Asset ID, Item Description, Category, Quantity, Assigned Room, Last Serviced Date, Custodian, Notes`
