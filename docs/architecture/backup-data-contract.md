# Authoritative Backup Data Contract

This document provides the authoritative schema contracts, identity strategies, and confidence rules for all supported backup domains across the SRKR Coding Club platform.

---

## 1. Mathematical 50% Schema Confidence Rule

$$\text{Confidence (\%)} = \left( \frac{\text{Unique Recognized Canonical Fields Matched}}{\text{Total Expected Canonical Fields}} \right) \times 100$$

### Invariants:
1. **Alias Deduplication**: Multiple source columns matching the same canonical field (e.g. `Email`, `E-mail`, `Email Address`) count as **exactly 1 match**.
2. **Eligibility**:
   $$\text{Structured Import Eligible} \iff \text{Required Fields Satisfied} = \text{True} \land \text{Confidence} \ge 50.0\%$$
3. **Unmapped Columns**: Unrecognized columns are **never** rejected as errors; they are preserved in `ImportRow.raw_data` with zero legacy data loss.

---

## 2. Domain Data Contracts

### A. Domain `USERS` (Club Member Directory)
- **Total Expected Canonical Fields**: 8
  - `full_name`, `email`, `phone_number`, `branch`, `club_id`, `referred_by`, `registered_at`, `membership_status`
- **Required Fields**:
  - Existing user update: `club_id` OR `email`.
  - New user creation: `email` strictly required.
- **Deduplication Strategy**:
  - Match by Club ID first; if not found, match by canonical lowercase email.
- **Watermark Synchronization**:
  - Automatically advances `ClubIDSequence.next_sequence` beyond the highest imported Club ID sequence (e.g. `25SCC277` $\rightarrow$ next is `278`).

### B. Domain `EVENTS` (Events & Workshops)
- **Total Expected Canonical Fields**: 7
  - `title`, `category`, `venue`, `start_time`, `end_time`, `capacity`, `description`
- **Required Fields**: `title`
- **Deduplication Strategy**: Match by `external_id` if present; otherwise match unique compound key `title` + `start_time`.

### C. Domain `HACKATHONS` (Hackathons & Sprints)
- **Total Expected Canonical Fields**: 7
  - `title`, `theme`, `prize_pool`, `start_date`, `end_date`, `team_size`, `is_flagship`
- **Required Fields**: `title`
- **Deduplication Strategy**: Match by `external_id` if present; otherwise match unique compound key `title` + `start_date`.

### D. Domain `FORMS` (Dynamic Form Submissions)
- **Schema**: Dynamic questions of selected published `Form`.
- **Required**: Explicit selection of target Form. *Form backups never create a new Form automatically.*

### E. Domain `UNKNOWN_RAW` (Schemaless Raw Vault)
- **Schema**: Arbitrary tabular data.
- **Required**: None. Always 100% eligible.
- **Target**: `core.RawBackupArchive` + `core.RawBackupRow`.
