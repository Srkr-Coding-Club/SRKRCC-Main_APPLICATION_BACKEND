# Club ID registry (control panel)

`apps/accounts/services/club_id_registry.py` is the single place that knows who holds which Club ID, what is held back, what has been retired and what happened to each id. The admin panel is `/admin/club-ids` (frontend); the API is under `/api/auth/club-ids/`.

## Rules

- **One holder, one id.** `User.club_id` stays unique and case-insensitive; `25scc277` is stored as `25SCC277`. Formats: `YYSCC###` (the club's own, allocated from a counter) and `YYCCA###` (accepted as given).
- **Every change is recorded.** `pre_save`/`post_save` handlers on `User` (connected in `AccountsConfig.ready`) write a `ClubIdEvent` whenever the stored id changes, from any code path: sign-up, the admin member form, an import, or this panel. A change made by the panel also carries who did it and why (`recording(...)` context). Saves that do not touch the id write nothing.
- **Reservations.** `ClubIdReservation` holds an id for an email (or for later). While `RESERVED`, nobody else can be given that id: sign-up, the admin member form, imports (`upsert_member`) and the panel all refuse it (`reserved for ...`). A person registering with the reserved email receives it (`attach_reserved`), or can be assigned it by an admin; the reservation then becomes `CLAIMED`. Releasing returns the id to the pool. One active reservation per id and per email (partial unique constraints).
- **Retirement.** Clearing an id (`CLEARED` event) retires it: it is never handed out again by the counter, and an admin can assign it again only with `allow_retired` (recorded). Moving an id between people does not retire it.
- **The counter skips.** `ClubIDService.allocate_next_club_id` skips a number that is held, reserved or retired (`next_free_sequence`), so a reserved id above the counter is never reused.
- **Affiliates always have an id.** Clearing or moving an id away from an `AFFILIATE` makes them `NON_AFFILIATE` (reported in the response).

## API

Reads: ADMIN or CLUB_LEAD. Writes: ADMIN only. Every write needs a `reason` (it is kept in the history); `reassign` and `clear` also need `confirm` = the id typed again. All refusals are `{detail, code}`.

| Route | Body / query | Notes |
|---|---|---|
| `GET overview/` | | Counters (`prefix`, `year`, `next_sequence`, `next_id`, `in_use`, `highest`, `gaps`, `editable`) and totals (holders, reserved, retired). |
| `GET lookup/?q=` | an id, a name or an email | An id: holder, active reservation, `retired`, history. Otherwise: matching people (max 10) and reservations. |
| `GET events/` | `club_id`, `q`, `kind` (comma list), `page`, `page_size` | The history, newest first. |
| `GET reservations/?status=` / `POST reservations/` | `{email, club_id?, reason, year?}` | Without `club_id` the next number is reserved. Codes: `ALREADY_HAS_ID`, `ALREADY_RESERVED`, `CLUB_ID_UNAVAILABLE`, `REASON_REQUIRED`. |
| `POST reservations/{id}/release/` | `{reason?}` | `NOT_ACTIVE` when already claimed or released. |
| `POST allocate/` | `{user_id, reason, year?}` | The next number for someone without an id. |
| `POST assign/` | `{user_id, club_id, reason, allow_retired?}` | `CLUB_ID_UNAVAILABLE` (held, reserved for someone else, retired), `ALREADY_HAS_ID`, `INVALID_CLUB_ID`. |
| `POST reassign/` | `{club_id, to_user_id, reason, confirm}` | `SAME_PERSON`, `ALREADY_HAS_ID`, `CONFIRM_MISMATCH`. |
| `POST clear/` | `{user_id, reason, confirm}` | Retires the id. |
| `POST counters/` | `{year, next_sequence, reason, prefix?}` | SCC only; forward only (`NOT_FORWARD`) and above every number in use (`IN_USE`). |

The registry list on the panel reuses `GET /auth/users/directory/?scope=members&ordering=club_id&club_id_prefix=25SCC`.

## Data

Migration `0013_club_id_registry`: `ClubIdReservation` and `ClubIdEvent`. Tests: `apps/accounts/tests/test_club_id_registry.py`.
