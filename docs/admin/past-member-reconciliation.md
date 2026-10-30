# Past-member sheet reconciliation

`apps/accounts/services/member_reconciliation.py` compares an uploaded sheet (name, email, Club ID / Affiliate ID) with the accounts. **Reading a sheet creates and changes nothing.** Accounts and Club IDs change only through the explicit row actions below, and every change goes through the [Club ID registry](club-id-registry.md), so it is recorded with a reason that names the sheet and row. The admin page is `/admin/reconcile`.

## Reading the sheet

`.xlsx` (any sheet of the workbook) or `.csv` (UTF-8, with or without a BOM, or Latin-1); at most 10 MB, 10,000 rows and 50 columns. The first non-empty row is the header (unnamed columns get a name). `POST inspect/` returns the sheets, the headers, a suggested mapping (`MemberImportService.auto_detect_mapping` headings such as "E-mail", "Affiliate ID", "Student Name") and the first rows; nothing is saved. The mapping needs an email column, a Club ID column, or both.

## What each row is

Emails are trimmed and lower-cased, ids are made canonical (`25scc277` is `25SCC277`), names are compared loosely (order, case, punctuation and an extra initial do not matter).

| Outcome | Meaning | Choices |
|---|---|---|
| `MATCH` / `NAME_DIFFERS` | Email and id agree with one account (the name may differ) | none |
| `ID_MISSING_IN_APP` | The account exists, has no id, and the sheet's id is free | **link**, follow up, dismiss |
| `ID_MISMATCH` | The account has a different id | **use the sheet's id** (reason required, the old id is freed not retired), keep the app's, follow up, dismiss |
| `ID_HELD_BY_OTHER` | The account has no id and the sheet's id belongs to someone else | keep, follow up, dismiss |
| `EMAIL_MISMATCH` | No account with that email, but the id is held under another email (maybe a new address) | keep, follow up, dismiss |
| `ID_RESERVED` | The id is reserved for someone else | keep, follow up, dismiss |
| `ID_RETIRED` | The id was cleared from a member earlier | **link anyway** (confirmed), follow up, dismiss |
| `NO_ACCOUNT` | No account and the id is free (or already reserved for this email) | **invite** (needs email and id), follow up, dismiss |
| `NO_ACCOUNT_NO_ID` | No account and no id in the sheet | follow up, dismiss |
| `NO_ID_IN_SHEET` | The account exists, the sheet has no id | none |
| `INVALID` | Bad email, malformed id, or neither | fix the sheet |
| `DUPLICATE` | The email or id is already on an earlier row (named in the detail) | none |

Rows are re-read against the accounts when listed and on **recheck**, so a person who registered or an id linked elsewhere shows as it is now. An action refused because the row is out of date (`409 NOT_APPLICABLE`) stores the row's real state.

## API (`/api/auth/reconciliation/`)

Reads and exports: ADMIN or CLUB_LEAD. Upload and every action: ADMIN only.

| Route | Notes |
|---|---|
| `POST inspect/` (file, sheet?) | Nothing saved. |
| `GET runs/` / `POST runs/` (file, sheet?, mapping JSON) | Create stores the run and its rows. |
| `GET runs/{id}/` | Counts by outcome and by decision, and how many rows still need a decision. `DELETE` (Admin) removes the run and its rows; accounts and the id history are untouched. |
| `GET runs/{id}/rows/` | Filters `group` (matched, link, mismatch, no_account, sheet), `outcome`, `resolution`, `open=1`, `q`; 25 per page. |
| `GET runs/{id}/missing/` | App members holding an id who are in the sheet by neither email nor id. |
| `POST runs/{id}/recheck/` | Re-reads every row. |
| `POST runs/{id}/rows/{row}/link/` `{allow_retired?}` | Gives the account the sheet's id (`registry.assign`). |
| `POST .../use-sheet/` `{note}` | `registry.replace`. |
| `POST .../decide/` `{choice: KEEP_APP\|FOLLOW_UP\|DISMISS, note?}` | Changes no account. |
| `POST .../invite/` | Reserves the id for the email (`source=RECONCILIATION`) and emails a sign-up link once (`409 ALREADY_INVITED`). The person gets the id when they register with that email. |
| `POST runs/{id}/bulk/` `{action: link\|invite, outcome, expected}` | Every open row of that outcome; `expected` is the count the person saw (`409 STALE` otherwise). One failing row is reported (`failed: [{row_number, error}]`) and the rest go ahead. |
| `GET runs/{id}/export/` | `kind=report` (optional `group`, `outcome`, ...) or `kind=missing`; `export_format=csv\|xlsx\|json`. |

## Data

Migration `0014_member_reconciliation`: `ReconciliationRun` and `ReconciliationRow` (the sheet's values and the decision kept per row). The sheet's personal data stays in the database until a run is deleted. Tests: `apps/accounts/tests/test_member_reconciliation.py`.
