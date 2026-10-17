# Colleges

A searchable catalogue of ~38k Indian colleges. Every `User` has an optional `college`
foreign key; accounts created without one (and every pre-existing account) get the club's home
college, **SRKR Engineering College** (`C-24163`).

## Data

- `apps/colleges/models.py` - `College(code, name, search_name, university, college_type, state, district)`.
  `code` is the national catalogue ID (`C-xxxxx`) and is the upsert key.
- `search_name` is lowercase with punctuation stripped, so `S.R.K.R.` and `SRKR` match each other.
  `SEARCH_ALIASES` adds extra searchable names (the home college is also found by
  "Sagi Rama Krishnam Raju Engineering College").
- Migration `colleges/0002` seeds the default college; `accounts/0009` backfills existing users with it.

## Loading the catalogue

`database.csv` (raw export, Windows-1252, gitignored) is converted once into the bundled, cleaned UTF-8
`apps/colleges/data/colleges.csv`, which **is** committed so every environment can load it:

```bash
python manage.py import_colleges                       # bundled catalogue, idempotent upsert
python manage.py import_colleges --file database.csv   # raw export also accepted
python manage.py import_colleges --dry-run
python manage.py import_colleges --file database.csv --write-clean apps/colleges/data/colleges.csv
```

Run `import_colleges` once after `migrate` on every environment.

## API

| Endpoint | Notes |
|---|---|
| `GET /api/colleges/?q=&page=&page_size=` | Public, unauthenticated, throttled (`college_search`, 120/min). Every word of `q` must match; names starting with `q` rank first. With no `q`, the default college is pinned first. Standard pagination (default 50, max 200). |
| `GET /api/colleges/<id>/` | One college. |

Registration accepts optional `college_id`; an unknown ID is rejected on that field. `/auth/me/`
returns `college: {id, code, name, district, state}` and accepts `college_id` on PATCH. Form
questions can auto-fill the name via `ProfileField.COLLEGE`.

## Colleges not in the catalogue

`User.other_college_name` holds a typed-in name (3-150 chars, letters/numbers/spaces and `. , & ' ( ) / -`). A user has either `college` or `other_college_name`, never both: choosing a catalogue college clears the typed name, and sending `other_college_name` (with `college_id` null or omitted) clears the catalogue link. Clearing `college_id` without a name is a 400. Signup with a typed name does **not** receive the default college.
