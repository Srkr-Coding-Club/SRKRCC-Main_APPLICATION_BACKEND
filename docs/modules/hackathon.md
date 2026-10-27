# Module: Hackathons

## Overview
The Hackathons module (`apps/hackathons/`) runs any hackathon on the platform: team registration, problem statements, any number of elimination rounds with admin shortlisting, re-collecting details from shortlisted teams, and public or targeted announcements. [IconCoders](iconcoders.md), the club's flagship hackathon, is a `Hackathon` with `is_flagship=True`.

All business rules live in `apps/hackathons/services.py` (`TeamService`, `RoundService`, `AnnouncementService`); views only parse input and serialize output. Every mutation is audit-logged via `apps.audit.utils.log_audit_event`.

## Who It's For
- **Participants** — logged-in members who create a team or accept an invite to one.
- **Team leaders** — the member who created the team (or was handed leadership). Only the leader edits the team.
- **Organizers (Admin / Club Lead)** — configure registration, problem statements and rounds, shortlist teams, publish results, post announcements.

## Lifecycle

```mermaid
flowchart TD
    A[Admin creates hackathon,\nsets registration window + team size,\nadds problem statements] --> B[Leader creates team,\npicks a statement or goes\nopen innovation]
    B --> C[Leader invites teammates by exact email]
    C --> D{Invitee accepts?}
    D -- yes --> E[Member joins; team is REGISTERED\nonce it reaches min_team_size]
    D -- no --> C
    E --> F[Admin creates Round 1:\nall REGISTERED teams enter]
    F --> G[Admin shortlists / rejects teams]
    G --> H[Admin publishes results\n(+ optional announcement)]
    H --> I[Shortlisted leaders submit the\nround's details form]
    I --> J[Admin creates Round N+1:\nteams shortlisted in Round N enter]
    J --> G
```

## Data Model

| Model | Purpose | Key rules |
|---|---|---|
| `Hackathon` | The event. Registration settings: `registration_opens_at`, `registration_closes_at`, `min_team_size`, `max_team_size`, `team_edits_locked`, `allow_open_innovation` (default on), `required_profile_fields` (list of `forms.ProfileField` keys every member must have on their profile). | `status` is `DRAFT` (created via the API; admin-only, nothing open), `LIVE` or `CLOSED`. `is_registration_open` = `status == LIVE` and now inside the (optional) window. `banner_crop` is `{x, y, zoom}` (focal point 0-100 %, zoom 1-3; `{}` = centred, no zoom) and only changes how the banner URL is framed on the public page; any other shape is a 400. The slug is immutable after creation and a few slugs are reserved (`my-teams`, `my-invites`, `teams`, `invites`, `submissions`). Date rules: end after start, `visible_until` after `visible_from`, registration closes before the event ends; `max_team_size` cannot drop below an existing team. Changing `min_team_size` re-evaluates every active team's FORMING/REGISTERED status. |
| `ProblemStatement` | Admin-defined problem: `title`, Markdown `description`, `domain`, `tags`, `max_teams` (blank = unlimited), `is_active`, `order`, plus a `code` the **application generates** (`PS-001`, `PS-002`, …; unique per hackathon, never typed or edited by an admin). | Title, description and domain are required. Statements are public (inactive ones are hidden from non-admins). A statement that teams have picked cannot be deleted (deactivate it instead). |
| `Team` | `name` (unique per hackathon, case-insensitive), `leader`, `problem_statement` (SET_NULL), `is_open_innovation` with `custom_problem_title` / `custom_problem_description` / `custom_problem_domain`, `status`: `FORMING` / `REGISTERED` / `DISQUALIFIED` / `WITHDRAWN`. | A team has **either** a statement **or** an open-innovation problem, never both. Open-innovation teams are known as `OI-<team id>` (`Team.open_innovation_code`). Also, `FORMING` ↔ `REGISTERED` is recomputed from member count vs `min_team_size` on every membership change. |
| `TeamMember` | Through-table for `Team.members`: `team`, `hackathon` (denormalized), `user`, `role` (`LEADER`/`MEMBER`), `joined_at`. | DB `UniqueConstraint(hackathon, user)` — one team per user per hackathon. |
| `TeamInvite` | `team`, `invited_user`, `invited_by`, `status`: `PENDING` / `ACCEPTED` / `DECLINED` / `CANCELLED`. | One pending invite per (team, user). Pending invites count toward `max_team_size`. |
| `Round` | `order` (unique per hackathon), `name`, Markdown `description`, `starts_at`/`ends_at`, `status` (`UPCOMING`/`ACTIVE`/`COMPLETED`), `details_form` (FK `forms.Form`), `results_published`. | Attaching a `details_form` forces `allow_multiple_responses=False` on it. |
| `RoundEntry` | One team in one round: `status` (`PENDING`/`SHORTLISTED`/`REJECTED`), `feedback` (shown to the team after publishing), `admin_notes` (internal), `decided_by/at`, `details_response` (FK `forms.Response`). | Unique `(round, team)`. |
| `HackathonAnnouncement` | `title`, Markdown `message`, `type` (reuses `announcements.AnnouncementType`), `audience`, `round`, `target_teams`, `is_active`, `publish_at`, `expires_at`, `send_email`. | Separate from the site-wide `apps.announcements` marquee. |
| `Submission` | Legacy one-per-team project submission (repo/demo/video, `score`). Not used by the round workflow; only the team leader or an admin can create one. |

Migrations `0004`–`0008`: `0008` renames `ProblemStatement.category` to `domain`, makes `code` application-generated, and adds the open-innovation fields. `0005` copies the old plain `Team.members` M2M (and `Team.leader`) into `TeamMember` rows and de-duplicates team names before `0006` swaps `members` onto the through-table and adds the name constraint.

## Business Rules (`services.py`)

**Team formation** — `TeamService`
- While `team_edits_locked` is on, participants cannot create teams either (admins can).
- `my-team` returns the round list only to members of a team.
- *Create team*: registration open; caller not already in a team for this hackathon; caller's profile has every `required_profile_fields` value; name not taken; a problem is chosen (see below). Creating a team cancels the caller's other pending invites for that hackathon.
- *Problem choice* (`_resolve_problem`, used by create and edit): the team picks one **active statement** (under its `max_teams`) **or** goes **open innovation** — never both (`PROBLEM_CHOICE_CONFLICT`). Open innovation requires `allow_open_innovation` (`OPEN_INNOVATION_DISABLED`) and a non-blank **title** (≤255), **description** (≤5000) and **domain** (≤100) — each missing or overlong value fails with `OPEN_INNOVATION_INCOMPLETE` / `OPEN_INNOVATION_TOO_LONG` naming the field. With neither, a problem is mandatory only once the hackathon has active statements (`PROBLEM_STATEMENT_REQUIRED`). Switching either way is validated before anything is saved; leaving a statement frees its slot, and open-innovation teams never use up statement slots.
- *Invite*: leader only; exact email of an existing account (`USER_NOT_FOUND` otherwise); invitee not already in any team of this hackathon; no duplicate pending invite; `members + pending invites < max_team_size`. After the transaction commits, `send_invite_email` emails the invitee a link to their dashboard on a background thread (a send failure never affects the invite).
- *Accept*: invitee only; re-validates everything under a row lock (registration open, edits not locked, team active, one-team rule, invitee's profile complete, team not full); cancels the user's other pending invites.
- *Remove member / transfer leadership / edit name or problem*: leader only (admins bypass). *Leave*: any member; the leader must transfer first unless they are the only member, in which case the team becomes `WITHDRAWN`.
- Participant-side changes require open registration and `team_edits_locked = False`, and the team must be `FORMING`/`REGISTERED`. Admin overrides (`admin_add_member`, `admin_set_status`) skip window/size checks but still enforce one team per user.

**Rounds** — `RoundService`
- Round 1 entries = every `REGISTERED` team. Round N entries = `REGISTERED` teams `SHORTLISTED` in round N−1. `populate` adds teams that became eligible after the round was created.
- `decide` sets status (and optionally feedback/notes) for many teams at once.
- Until `results_published`, participants see their entry as `PENDING` with no feedback. `publish` can also post a `ROUND_SHORTLISTED` announcement (`announce: true`) and a `ROUND_REJECTED` thank-you note (`announce_rejected: true`), and email either to those teams' members (`email: true`). Publishing an already-published round is a 409 (`ALREADY_PUBLISHED`); unpublish first. Once published, `decide` needs `confirm_published: true` (else 409 `RESULTS_PUBLISHED`), and every team whose result actually changes gets an in-app notification.
- A round cannot be deleted while later rounds exist (they depend on its shortlist). Deleting one keeps its announcements: `RoundService.delete_round` converts round-scoped announcements into `TEAMS` announcements for the same teams (all entries, or only the shortlisted ones).

**Details forms** — `check_round_form_access(form, user)` is called by `apps/forms/views.py` on submit and on edit. If the form is any round's `details_form`, only the **leader** of a `REGISTERED` team **shortlisted** in that round, after results are **published**, may submit (`403 ROUND_FORM_RESTRICTED` otherwise). The saved `Response` is linked to `RoundEntry.details_response`. Forms not attached to a round are unaffected.

**Announcements** — `AnnouncementService.visible_for(hackathon, user)`

| Audience | Who sees it |
|---|---|
| `PUBLIC` | Everyone, including logged-out visitors and the public hackathon page |
| `PARTICIPANTS` | Members of any `FORMING`/`REGISTERED` team |
| `ROUND_ALL` | Teams with an entry in the chosen round |
| `ROUND_SHORTLISTED` | Teams shortlisted in the chosen round, **only once that round's results are published** |
| `ROUND_REJECTED` | Teams not shortlisted in the chosen round, **only once that round's results are published** |
| `TEAMS` | Members of the selected teams |

Only active announcements with `publish_at <= now` and not past `expires_at` are shown. `send_email=True` (or the admin "notify" action) emails the audience through the shared email-job engine. A scheduled announcement (`publish_at` in the future) is emailed only once it publishes: the first read of the hackathon's announcements after that time, or `python manage.py send_due_hackathon_emails` from cron, dispatches it; `emailed_at` records the send so it happens once; see [Email & Notifications](../features/email-notifications.md).

## API (`/api/hackathons/`)

Participant / public:

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/{slug}/problem-statements/` | Anyone, no login (active only; admins see all) | List statements with `code`, `domain`, `team_count` / `slots_left` |
| GET | `/{slug}/announcements/` | Anyone (filtered by audience) | Announcements visible to the caller |
| GET | `/{slug}/my-team/` | Authenticated | Dashboard payload: hackathon, `profile_missing`, team, `is_leader`, pending invites, rounds (entry status/feedback hidden until published, details-form eligibility) |
| POST | `/{slug}/teams/` | Authenticated | Create a team `{name, problem_statement}` **or** `{name, open_innovation: {title, description, domain}}` |
| GET | `/{slug}/user-lookup/` | Team leader of the target team, or admin (403 otherwise); throttled `hackathon_lookup` 30/min; `?q=` needs 3+ characters and returns at most 5 matches | User lookup supporting exact `?email=` or instant autocomplete prefix/partial search `?q=` (searches name, email, club ID) with optional `&team_id=`. Returns minimal fields: `id, name, email, club_id, can_invite, reason, results` |
| GET | `/my-teams/`, `/my-invites/` | Authenticated | Caller's teams / pending invites across hackathons |
| POST | `/invites/{id}/accept/`, `/invites/{id}/decline/` | Invitee | Respond to an invite |
| GET, PATCH | `/teams/{id}/` | Members read; leader/admin write | Team detail (admins also get members' contact details and round history). PATCH accepts `problem_statement` or `open_innovation` to change the problem; a body without either leaves it untouched |
| POST | `/teams/{id}/{action}/` | See below | `invite {email}`, `cancel-invite {invite_id}`, `remove-member {user_id}`, `transfer-leadership {user_id}` (leader/admin); `leave` (member); `admin-add-member {email}`, `admin-set-status {status}` (admin) |

Non-members get `404` for another team, so team IDs can't be probed.

Admin (`IsAdminOrClubLead`):

| Method | Path | Purpose |
|---|---|---|
| PATCH | `/{slug}/` | Registration settings (validated: `max_team_size >= min_team_size >= 1`, closes after opens, known profile-field keys) |
| POST | `/{slug}/publish/` | DRAFT to LIVE. 400 `PUBLISH_CHECKLIST` (with `items`) unless it has a description, a problem option (an active statement or open innovation) and a future end date |
| POST | `/{slug}/close/`, `/{slug}/reopen/`, `/{slug}/hide/`, `/{slug}/show/` | Hackathon status / public visibility (`close` only from LIVE; `reopen` not from DRAFT). `hide` sets `hidden_at`, `show` clears it; the `visible_from`/`visible_until` window is never touched. |
| GET | `/{slug}/delete-impact/` | **Admin only.** Counts of teams, members, problem statements, rounds, round entries, announcements, stages and submissions a delete would remove |
| DELETE | `/{slug}/?confirm_slug={slug}` | **Admin only** (club leads get 403). Hard delete with DB cascade; the linked registration form and its responses are kept. 400 `CONFIRM_SLUG_MISMATCH` unless `confirm_slug` equals the slug. Audited as `Deleted Hackathon` with the impact counts |
| GET | `/{slug}/stats/` | Teams by status, participants, pending invites, per-statement uptake, per-round funnel |
| POST, PATCH, DELETE | `/{slug}/problem-statements/[{id}/]` | Manage statements. `POST {title, description, domain, tags?, max_teams?}` — the `code` is generated and any `code` sent is ignored. `DELETE ?force=true` force-unassigns any assigned teams before deletion |
| POST | `/{slug}/problem-statements/reorder/` | `{ids: [...]}`: every statement id of the hackathon, once, in the new order; sets `order` 1..n in one transaction (400 `REORDER_MISMATCH` otherwise) |
| POST | `/{slug}/problem-statements/upload/` | Multipart `file`: a CSV with `title`, `description`, `domain` columns (aliases such as `problem title`, `details`, `category`/`track` accepted; UTF-8, ≤1 MB, ≤500 rows). Generates IDs, skips rows repeating an existing title + domain, reports invalid rows by row number: `{created, skipped, errors, codes}` |
| GET | `/{slug}/teams/?status=&problem_statement=&round=&entry_status=&search=` | All teams with members' contact details (`problem_statement=open_innovation` lists open-innovation teams) |
| POST | `/{slug}/teams/` | Admin creation: accepts `leader_email` to register a team directly on behalf of a student, bypassing registration closed checks |
| DELETE | `/teams/{id}/` | Delete a team permanently with DB-level cascading of memberships, invites, round entries, and submissions |
| GET, POST, PATCH, DELETE | `/{slug}/rounds/[{id}/]` | Manage rounds (`POST` auto-populates entries unless `populate=false`) |
| GET | `/{slug}/rounds/{id}/entries/?status=&search=` | Teams in a round |
| POST | `/{slug}/rounds/{id}/decide/` | `{team_ids, status, feedback?, admin_notes?, confirm_published?}` |
| POST | `/{slug}/rounds/{id}/publish/`, `/unpublish/`, `/populate/` | Results visibility / sync eligible teams |
| POST | `/{slug}/rounds/{id}/advance/` | Advance all `SHORTLISTED` teams to the next round; automatically creates Round N+1 if it does not yet exist |
| POST | `/{slug}/rounds/{id}/add-team/` | `{team_id}`: wildcard promotion / manual addition of an eligible team to this round |
| POST | `/{slug}/rounds/{id}/remove-team/` | `{team_id}`: remove a team entry from this round |
| GET | `/{slug}/announcements/?all=true` | Every announcement with audience details |
| POST, PATCH, DELETE | `/{slug}/announcements/[{id}/]` | Manage announcements |
| POST | `/{slug}/announcements/{id}/notify/` | (Re)send the announcement email |

Errors from business rules are `400` with `{detail, code, field?}` — e.g. `REGISTRATION_CLOSED`, `ALREADY_IN_TEAM`, `PROFILE_INCOMPLETE`, `NAME_TAKEN`, `PROBLEM_STATEMENT_FULL`, `PROBLEM_CHOICE_CONFLICT`, `OPEN_INNOVATION_DISABLED`, `OPEN_INNOVATION_INCOMPLETE`, `OPEN_INNOVATION_TOO_LONG`, `CSV_MISSING_COLUMNS`, `CANNOT_INVITE`, `TEAM_FULL`, `INVITE_NOT_PENDING`, `LEADER_MUST_TRANSFER`, `TEAM_EDITS_LOCKED`, `ROUND_HAS_LATER`.

## Frontend Pages

| Page | Path | Visible to |
|---|---|---|
| Hackathons listing | `/hackathons` | Everyone (module flag + visibility window) |
| Hackathon details | `/hackathons/[slug]` | Everyone — facts, problem statements, public announcements, Register / Go-to-team CTA |
| Participant dashboard | `/hackathons/[slug]/dashboard` | Logged-in users — create team, invites, team management, rounds timeline, announcements |
| My Hackathons | `/profile` | Logged-in users — their teams and pending invites |
| Hackathon management | `/admin/hackathons`, `/admin/hackathons/[slug]` | Admin, Club Lead — Overview, Registration, Problem Statements, Teams, Rounds & Shortlisting, Announcements tabs |

## Visibility Rules
1. **Module flag** — the `hackathons` feature flag hides the whole module. See [Feature Flags](../features/feature-flags.md).
2. **Per-hackathon window** — non-admins only see hackathons inside `visible_from`/`visible_until` (`HackathonViewSet.get_queryset`); every `/{slug}/…` participant endpoint uses the same filter. Admins see everything. `close` stops registration but keeps the hackathon listed; `hide` (the `hidden_at` timestamp, independent of the window) removes it from public view. The serializer exposes `is_hidden` (hidden by either cause) and `is_manually_hidden` (hidden by an admin).

## Not Implemented
- Judge scoring / rubrics (`JUDGE` role exists but no scoring UI or model is wired to rounds). Shortlisting is a manual admin decision.
- A public results page / leaderboard.
- Invite expiry (pending invites stay open until accepted, declined, cancelled, or the invitee joins another team).

## Data Export
DMC datasets `hackathon_participants` (one row per `TeamMember`, role Leader/Member), `hackathon_teams`, `hackathon_submissions` and `hackathon_round_entries` ("Hackathon Round Results": one row per team per round with result, feedback, published flag and whether the details form was submitted) — see [DMC](dmc.md). The admin Teams tab also exports a per-member CSV client-side.

### Creating and withdrawing teams (`withdrawals.py`)

Stops people creating and withdrawing teams over and over (each cycle used up names, waitlist places and emails).

- **Limit:** a person may create `MAX_SELF_CREATED_TEAMS` (2) teams per hackathon (`Team.created_by`; teams an organizer creates do not count) and must wait `WITHDRAW_COOLDOWN` (10 minutes) after withdrawing one (`Team.withdrawn_at` / `withdrawn_by`). Refusals: `TEAM_LIMIT_REACHED` (with `limit`), `WITHDRAW_COOLDOWN` (with `retry_at`). Teams created before the limit existed are not counted.
- **Allowance:** an organizer with the `teams` permission lets one person create more: `POST /{slug}/team-allowances/ {user_id, extra, reason?}` (`TeamAllowance`, extra 1 to 10, audited).
- **Withdrawing** (`POST /teams/{id}/leave/ {reason}`, the only member): a reason is required (`REASON_REQUIRED`, kept in `status_reason`). A team that is forming or waitlisted is withdrawn at once. A team that holds a place (registered and not waitlisted, or in any round) becomes a `WithdrawalRequest`: the response is `{left: false, withdrawal_requested: true}`, the team stays untouched, `WITHDRAWAL_PENDING` blocks a second request, and the leader can take it back with `POST /teams/{id}/cancel-withdrawal/`. Organizers with the `teams` permission are notified.
- **Deciding:** `GET /{slug}/withdrawals/` (pending, the last 20 decisions, `repeaters` who created or withdrew two or more teams, and the limits) and `POST /{slug}/withdrawals/{id}/decide/ {approve: bool, note?}`. Approving withdraws the team (the waitlist moves up) and fails with `TEAM_NOT_EMPTY` if people joined meanwhile; deciding twice is `ALREADY_DECIDED`; an organizer changing the team's status themselves cancels a waiting request. The leader is notified either way.
- `GET /{slug}/my-team/` carries `withdrawal_request` (the latest, never a cancelled one) and `team_limits`.
- Members leaving a team are not limited; only creating and withdrawing a team is.

### Organizer downloads

`GET /api/hackathons/{slug}/export/{kind}/?export_format=csv|xlsx|json` (`apps/hackathons/exports.py`, rendered by the shared export serializer, so the file follows the contract in [dmc.md](dmc.md)). It replaces the browser-built CSVs and the old `/participants/` JSON endpoint, works for organizers with scoped permissions (the DMC is admin and club-lead only) and writes an audit entry per download.

| `kind` | Rows | Needs any of |
|---|---|---|
| `teams` | One per team member with the team's problem; `?ids=1,2` limits it to the teams on screen | `teams`, `rounds`, `results` |
| `participants` | One per person: profile, rules accepted, checked in, plus one column per registration question | `teams` |
| `ideas` | One per idea with status, payment and slides flags, score | `ideas` |
| `submissions` | One per project | `submissions` |
| `rounds` | One per team in `?round=<id>` (optionally `?status=`, `?search=`) | `rounds` |
| `check-in` | Everyone on a placed team and whether, when and by whom they were checked in | `check_in` (volunteers too) |

Errors: unknown kind 404, bad `export_format` 400 `INVALID`, `rounds` without a valid round 400 `ROUND_REQUIRED` / 404, no permission 403.

## Related Docs
- [../admin/event-hackathon-management.md](../admin/event-hackathon-management.md) — admin walkthrough
- [forms.md](forms.md) — details forms and profile auto-fill fields
- [iconcoders.md](iconcoders.md) — the flagship hackathon built on this module

Public hackathon payloads carry `registered_team_count` (teams that meet the minimum size) next to `team_count` (forming + registered). Public pages show the former. The in-app invite notification links to the team dashboard, and a failure creating it is logged without losing the invite.

## Notifications, email invites, reminders and FAQ

- **In-app notifications** (never block the action; own savepoint, errors logged): a removed member, a new leader, an admin-added member, every member when an admin changes the team's status, the leader when an invite is accepted or declined, and teams whose published round result changes.
- **Invite by email** - `POST /teams/{id}/invite-by-email/ {email}` (leader; throttled `hackathon_email_invite`, 20/hour). For an address with no account it stores a `TeamEmailInvite`, emails a sign-up link and counts as one team slot (`ACCOUNT_EXISTS` points an existing account to the normal invite). When an account with that address is created (a `post_save` signal in `signals.py`, so signup, admin creation and imports are all covered) pending email invites become normal `TeamInvite`s plus a notification, unless the team can no longer take them (then they are cancelled). Cancel with `POST /teams/{id}/cancel-email-invite/ {invite_id}`. `pending_email_invites` is part of the team payload.
- **Forming teams** - `/stats/` lists `forming_teams` (name, leader, members, `needed`); `POST /{slug}/remind-forming/` (admin) sends the leader of each a notification and an email.
- **Problem lock** - once a team has a round entry, only admins can change its problem (`PROBLEM_LOCKED`); renaming and resending the same problem still work.
- **FAQ** - `Hackathon.faq` is a list of `{question, answer}` (max 20; 200/2000 chars). Empty = the standard FAQ.
- Admin team members carry `college` (catalogue name or the typed one); participants never see it.
- The round entries list and round counts run a fixed number of queries (annotated member count and promotion flag; one aggregate for counts), pinned by tests.

## Team emails

Besides in-app notifications and invite emails, participants get emails as their team changes
(`apps/hackathons/team_emails.py`, hooked into `TeamService`):

| Email | When | To |
|---|---|---|
| `team_created` | Team created below the minimum size | Leader |
| `team_registered` | Team reaches the minimum size | Every member |
| `member_joined` | Invite accepted / admin adds a member | Every member |
| `member_left` | Member leaves or is removed | Remaining members |
| `member_removed` | Leader removes a member | The removed member |
| `status_changed` | Admin changes team status | Every member |

When a join is also the one that completes registration, only `team_registered` is sent.

**Customising:** `Hackathon.team_email_config` holds `{event: {enabled, subject, body}}`; blank
subject/body use the built-in wording. Texts use `{{placeholders}}` from the fixed list in
`PLACEHOLDERS` (team, hackathon, members, status, dashboard link...). Unknown placeholders are
rejected on save, values are HTML-escaped when rendered, and blank lines become paragraphs.
Admin API: `GET/PUT /api/hackathons/{slug}/team-emails/` and `POST .../team-emails/test/` (sends a
sample to the requesting admin). Frontend: the **Team Emails** tab on the admin hackathon page.

**Delivery:** one email per recipient, queued with `transaction.on_commit` and sent on a background
thread (`run_in_background`), so a slow mail server never blocks or rolls back a team change.
Failures are logged, not retried, and not stored as `EmailJob` rows.

## Public hackathon page content

Everything on the public page is admin-editable (Settings tab, "Public Page Presentation"):

| Field | Meaning |
|---|---|
| `mode` | `ONLINE` / `OFFLINE` / `HYBRID` (default `ONLINE`) |
| `venue` | Shown for offline/hybrid events (max 200 chars) |
| `organizer_name` | Line under the title; blank = "SRKR Coding Club, SRKR Engineering College" |
| `logo_image` | Square logo next to the title |
| `tags` | Up to 10 category labels (40 chars each, case-insensitive de-duplicated) |
| `prizes` | Up to 12 `{title, amount, description}` cards; blank = a single card for `prize_pool` |
| `faq` | Organizer FAQ (see above) |

The "Stages and Timelines" section has its own model, `HackathonStage`, independent of rounds so it can describe registration, idea submission, results or ceremonies. Per stage: `title`, Markdown `description`, `starts_at`/`ends_at`, a label chip (`badge`), `location`, colour `tone` (ORANGE, BLUE, GREEN, PURPLE, RED, SLATE), `marker` (a `DATE` badge or an `ICON` from a fixed set), an optional button (`link_label` + http(s) `link_url`), `is_visible` and `order`. Admin API (admin/club lead): `GET/POST /{slug}/timeline/`, `PATCH/DELETE /{slug}/timeline/{id}/`, `POST /{slug}/timeline/reorder/ {ids}` (every id once; atomic) and `POST /{slug}/timeline/import-rounds/` (copies each round that has no stage yet; safe to repeat). Public: `GET /{slug}/stages/` (no auth, visible stages only, 404 for drafts/hidden hackathons) returns just the display fields: never teams, results, forms or round entries.

## Problem statement configuration

- **IDs and order are automatic.** `PS-001`, `PS-002`... are generated by the app (never typed or edited); new statements are appended to the end of the order and admins reorder from the list (`POST /problem-statements/reorder/`).
- **Domains are configured per hackathon** (`Hackathon.problem_domains`, up to 30 names; edited with `PATCH /{slug}/` or the Domains panel in the admin Problem Statements tab). When the list is not empty a statement's `domain` must match one of them (case-insensitive; stored with the configured spelling). A domain used by any statement cannot be removed from the list, and the first list must cover every domain already in use. CSV imports add their new domains to the list automatically. The list is also the order of the public filter pills. Migration `0016` seeds the list from existing statements.
- **Tags** are free labels per statement: up to 10, 30 characters each, de-duplicated case-insensitively.
- Everything else is editable per statement: title, Markdown description, domain, tags, max teams (blank = unlimited) and `is_active` (hide from participants; teams already on it keep it).


## Event operations (admin-controlled modules)

Everything below is switched and configured by organizers per hackathon (create panel or **Registration controls**); nothing is hard-coded. Modules live in `Hackathon.features` (`submissions`, `judging`, `check_in`, `certificates`, `team_finder`, `showcase`; absent = off). Settings are validated by `apps/hackathons/config_validation.py`; business rules are in one service module per concern, and `views_ops.py` only parses, authorises and shapes responses.

| Concern | Module | Rules |
|---|---|---|
| Eligibility, capacity, rules, questions | `registration.py` + `TeamService` | `eligibility {colleges, branches, years}` is matched against the profile (case-insensitive; empty = open). `max_teams` / `max_participants` cap placed teams/people; with `waitlist_enabled` a new team queues (`Team.is_waitlisted`) and `WaitlistService.promote` places the oldest queued team when a spot opens (withdrawal, removal, delete, admin status change, a raised cap). Without a waitlist the API answers `HACKATHON_FULL`. `rules` + `require_rules_acceptance` and `registration_questions` are enforced on team creation and invite acceptance (`RULES_NOT_ACCEPTED`, `ANSWER_REQUIRED`, `ANSWER_INVALID`); the answers and `rules_accepted_at` live on `TeamMember`. `problem_statements_reveal_at` hides statements from non-admins until then. Waitlisted teams are excluded from rounds. Admins bypass these checks. |
| Submissions | `submissions.py` | One per team (`Submission`). Only the leader of a REGISTERED, non-waitlisted team may write, inside `submission_opens_at`..`submission_deadline`. `GET/PUT /{slug}/my-submission/`; organizers: `GET /{slug}/submissions/`, `POST /{slug}/submissions/remind/`. `manage.py send_hackathon_reminders` (run hourly from cron) reminds teams without a submission once, in the 24 h before the deadline. |
| Judging | `judging.py`, `access.py` | `JudgingCriterion` rubric (max score + weight); `HackathonStaff` gives a person JUDGE or VOLUNTEER for one hackathon only. A judge's `SubmissionReview` total is weighted out of 100; `Submission.score` is the average and is recalculated when the rubric changes. Judges cannot score their own team, participants cannot be made judges, and scores are locked once results are published (`RESULTS_PUBLISHED`). |
| Results | `judging.ResultsService` | `HackathonWinner` rows; `POST /{slug}/results/publish|unpublish/` sets `results_published_at`. Public `GET /{slug}/results/` shows winners (and every score only if `show_leaderboard`); `GET /{slug}/showcase/` lists public submissions once results are out and the `showcase` module is on. |
| Check-in | `checkin.py` | Each participant has a signed QR token (`/my-check-in/`); organizers and VOLUNTEER staff scan it or check people in by id (`POST /{slug}/check-in/`, idempotent). Only members of REGISTERED, non-waitlisted teams qualify. **Window:** `check_in_opens_at` / `check_in_closes_at` (empty = always) gate QR scans only (`CHECK_IN_NOT_OPEN`, `CHECK_IN_CLOSED`); manual check-in by id is always allowed, and `/my-check-in/` returns `window`, `opens_at`, `closes_at` and withholds the token unless the window is OPEN. **Offline replay:** `POST` accepts an optional `scanned_at` (clamped to the last 24h, never the future); the window is judged at that time and it becomes `checked_in_at`. The response includes `checked_in_by`. **Rotating codes:** the QR token carries a 60-second bucket and is accepted within 3 buckets of the scan time (`QR_EXPIRED` otherwise), so a forwarded screenshot soon dies; offline replays are judged at `scanned_at`. **Walk-ins:** `POST` with `{user_id, override: true, reason}` (needs the `teams` permission) lets in a member of a forming or waitlisted team, stored as `CheckIn.override_reason` and audited; removed teams (`TEAM_REMOVED`) never. **Roster:** `GET /check-in/?q=&missing=1&limit=&offset=` filters and pages on the server (limit max 500) and returns whole-event `total`/`checked_in`, the filtered `count`, and (organizers, with `q`) `held_back` people. **No-shows** (`teams` permission): `GET /check-in/no-shows/` lists teams with nobody here; `POST {action: 'remind'}` notifies missing participants; `POST {action: 'withdraw', team_ids}` withdraws chosen no-show teams through `TeamService.admin_set_status`, which promotes the waitlist (refused while a closing time is still in the future). **Stats** (`teams` permission): `GET /check-in/stats/` returns scans per volunteer (a deleted account is labelled `Removed account`), the override count and an arrivals timeline in 15-minute buckets (hourly once arrivals span more than 48h; empty slots between the first and last arrival are zero-filled). Roster search matches every typed word against name, email or team, so a full name finds its person. |
| Certificates | `certificates.py` | Wording and eligibility (`REGISTERED`, `SUBMITTED`, `CHECKED_IN`) are `certificate_config`; releasing it notifies participants. Winners get an award certificate. Every certificate carries a signed code checked by the public `GET /certificates/verify/{code}/`. |
| Team finder | `finder.py` | `TeamSeeker` listings and teams with `looking_for_members`. Emails are never exposed: leaders invite a listed person through the existing invite flow; seekers can ask a recruiting leader to add them (see **Join requests** below). |
| Insight, export, clone | `analytics.py`, `cloning.py` | `GET /{slug}/analytics/` (funnel, branch/year/college, per-day joins), `GET /{slug}/participants/` (rows incl. answers). `POST /{slug}/clone/` copies setup, stages, rounds (no teams), rubric and people to a new DRAFT with every date shifted. |

Public page content (`mentors`, `judges`, `sponsors`, `contacts`, `community_links`, `venue_map_url`) is admin-written JSON validated by `config_validation.py`.


## Per-hackathon roles and permissions

Site-wide `ADMIN` / `CLUB_LEAD` users hold every permission in every hackathon (only `ADMIN` can delete one; creating and cloning stay site-wide). Anyone else acts through a `HackathonStaff` row for **one** hackathon: a role (`ORGANIZER`, `JUDGE`, `VOLUNTEER`) with a stored `permissions` list that starts from the role's preset and can be edited per person. `apps/hackathons/access.py` is the single source: the `PERMISSIONS` catalog, `ROLE_PRESETS`, `my_permissions()` and the `scoped('<key>')` DRF permission used on every `/{slug}/...` endpoint. A row with an empty list falls back to its preset.

| Permission | Allows |
|---|---|
| `settings` | Edit the hackathon, publish/close/reopen, hide/show, modules, capacity, rules, public page |
| `content` | Problem statements, public timeline, announcements, team emails |
| `teams` | Every team and participant, team edits and status changes, participants export, remind forming teams |
| `rounds` | Rounds and shortlisting |
| `ideas` | Review ideas (approve / request changes / reject), remind teams |
| `submissions` | Project submissions list, window, reminders |
| `judging` | Rubric, leaderboard, every judge's scores |
| `judge` | Score projects (never one's own team) |
| `results` | Winners, publish results, certificates |
| `check_in` | Scan and check participants in |
| `insights` | Overview, funnel, breakdowns |
| `staff` | Add people and change permissions (only up to what the actor holds, never one's own, never someone who outranks them) |

Staff also see their hackathon while it is a draft or hidden. `GET /{slug}/my-permissions/` returns the caller's roles, permissions, the catalog and the presets; `GET/POST /{slug}/staff/`, `PATCH/DELETE /{slug}/staff/{id}/` manage people. Every change is audited.

## Two-phase submissions: idea, then project

`features.ideas` switches **phase 1 (plan)** on; `features.submissions` is **phase 2 (implement)**. Each has its own window (`idea_opens_at`/`idea_deadline`, `submission_opens_at`/`submission_deadline`; the idea deadline must not be after the project deadline). `IdeaSubmission` (one per team: title, problem, solution, plan, tech stack, optional slides link) is written by the team leader of a registered team inside the window. With `idea_review_required` an organizer holding `ideas` approves it, asks for changes (the team edits and resubmits) or rejects it, with a note the team is notified of; approved and rejected ideas are locked. Without review, ideas are approved on submission. `project_requires_approved_idea` makes the project phase available only to teams with an accepted idea (`IDEA_REQUIRED` otherwise; `my-submission` reports the `gate`). Endpoints: `GET/PUT /{slug}/my-idea/`, `GET /{slug}/ideas/`, `POST /{slug}/ideas/{id}/review/`, `POST /{slug}/ideas/remind/`. Cloning copies the phase settings with shifted dates.


## Idea submission: slides, LinkedIn post, payment and editing

On top of the written idea, organizers can ask for more (all set on the **Ideas** tab; every setting is optional and defaults to the old behaviour):

| Setting | Meaning |
|---|---|
| `idea_deck_required` | The leader uploads a `.ppt` / `.pptx` (up to 25 MB). |
| `idea_linkedin_required` | The link of the team's LinkedIn post (`linkedin.com` / `lnkd.in` only). |
| `idea_fee_amount` | Rupees to pay (0 = none, max 100000). Above 0 a **payment screenshot** (png/jpg/webp, up to 5 MB) is required; `transaction_id` stays optional. |
| `idea_payment_note` | How to pay (UPI id, QR instructions), shown with the form. |
| `idea_edits_enabled` | On (default): the leader can keep editing a submitted idea. Off: locked after the first submission; changes the organizers ask for are still allowed. |
| `idea_edits_until` | Optional. Editing closes at this time instead of at the deadline, and may be **later than the deadline** to reopen editing. Ignored while editing is off. It never lets a team that has not submitted start late. |

Accepted and rejected ideas stay locked whatever these say. Requiring a fee or a slides file needs file uploads switched on (`FORM_UPLOADS_BACKEND=s3`); the settings are refused otherwise.

**Files.** The slides and the screenshot are `IdeaFile` rows uploaded straight to the bucket like form uploads (presigned POST, size/type pinned, content checked on completion, copied out of the expiring `tmp/` area when the idea is saved, removed when replaced or when the team or hackathon is deleted; covered by the Maintenance sweep and scan tasks). Endpoints: `POST /{slug}/my-idea/files/ {kind: DECK|PAYMENT, name, size, type}`, `POST /{slug}/my-idea/files/{id}/` (confirm), `DELETE /{slug}/my-idea/files/{id}/`, and `GET /{slug}/ideas/{id}/files/{deck|payment}/link/?inline=1` (a 60-second link). `PUT /my-idea/` takes `deck_file_id` / `payment_file_id`: left out keeps the saved file, `null` removes it (refused if required), an id uses that upload (it must be the team's own, of the right kind).

**Payment QR code.** Organizers with the `settings` permission upload one image (png/jpg/webp, up to 5 MB) that teams scan to pay: `POST /{slug}/idea-qr/ {name, size, type}` (signed upload form), `POST /{slug}/idea-qr/{id}/` (the upload arrived: it is checked and becomes the hackathon's QR code, replacing and releasing the old one; saved at once, not with the settings Save button), `DELETE /{slug}/idea-qr/`. `GET /{slug}/idea-qr/link/` gives a 60-second link to the image to organizers and to members of a registered, non-waitlisted team of this hackathon only (judges, volunteers, outsiders and signed-out visitors get 404/401). The idea form's `config.payment_qr` says whether one exists (never its storage key). Set/remove are audit-logged; a cloned hackathon does not inherit the QR; it follows the same scanning and cleanup rules as other idea files (stored as an `IdeaFile` of kind `QR` with no team).

**Who sees what.** The payment screenshot and the transaction id are visible to organizers holding the `ideas` permission (site admins and club leads included) and to the team itself, **never to judges**; they are not in the judging lists. Judges can open the slides and the LinkedIn link. Opening a payment screenshot is audit-logged ("Viewed Idea Payment Proof"). Storage keys are never returned.

**Editing rules (`ideas.can_team_edit`).** First submission: window open. After that: locked ideas never; "changes requested" ideas while the window or the edit time is open; otherwise editing must be on, and then it lasts until `idea_edits_until` if set, else the deadline. `my-idea` reports `can_edit`, `edit_blocked_reason` and the `config` the form needs.

## Rounds: kinds, targets and rule-based shortlisting

- **Kind and target.** A round has a `kind` (General, Screening, Idea review, Project review, Pitch, Final) and an optional `shortlist_limit` (how many teams should go through). Shortlisting more than the target answers 409 `SHORTLIST_LIMIT` (with `limit` and `would_be`) until the caller repeats the request with `confirm_over_limit`.
- **Dates win.** `effective_status` is computed from `starts_at` / `ends_at` (upcoming, active, completed); the stored `status` is only used for rounds without dates. Participants see the effective status.
- **Pick teams by rule** (`POST /{slug}/rounds/{id}/auto-shortlist/ {rule, value?, reject_rest?, apply?}`, `rounds` permission). Rules live in `round_rules.py`: `PROJECT_SCORE_TOP|MIN`, `IDEA_SCORE_TOP|MIN`, `IDEA_APPROVED`, `HAS_PROJECT`, `CHECKED_IN` (any member), `ALL_CHECKED_IN` (every member). With `apply` false it only previews (ties at a "top N" cut-off are all kept); with `apply` true it records the decisions through the normal `decide` path, so the published-results and target confirmations still apply. `reject_rest` marks only still-pending teams as not shortlisted.
- **Order.** `POST /{slug}/rounds/reorder/ {ids}` renumbers rounds, but only while no team has been decided in any round (`ROUNDS_IN_USE`), because later rounds depend on earlier shortlists.
- **Evidence for decisions.** Round entries now carry the team's idea status and score, whether it submitted a project and its score, and how many members checked in.

## Team finder: join requests

A solo participant can ask a recruiting team to take them in (the other direction from an invite). `POST /{slug}/finder/teams/{id}/request/ {message, accept_rules, answers}` stores a `TeamJoinRequest` (PENDING) and notifies the team's leader. Everything accepting will need is checked at request time: registration open, finder on, team recruiting and not full, the person not already on a team, their profile complete, eligibility, and the rules acceptance / registration answers (stored with the request). Limits: one pending request per team, at most 5 pending per person (`429 TOO_MANY_REQUESTS`), and a declined request cannot be repeated to the same team for 24 hours (`409 DECLINED_RECENTLY`).

- **Answering.** `POST /{slug}/finder/requests/{id}/accept|decline/` by the team leader (or an organizer with the `teams` permission). Accepting runs the same checks as accepting an invite (`services._admit`: team active and not full, edits not locked, registration open, participant limit, not in another team) and adds the person with the stored answers; if any fails the request stays pending and the leader sees why (`blocked_reason`). Joining a team cancels the person's other open requests and invites. Declining tells the person. A request answered twice returns `409 REQUEST_NOT_PENDING` with the `outcome`.
- **Withdrawing.** The sender cancels with `.../cancel/`.
- **Lists.** `GET /{slug}/finder/requests/` returns `incoming` (pending requests to teams you lead: name, branch/year, skills, message, never an email) and `mine`. `GET /{slug}/finder/` also returns `my_requests`.
- **Notifications can carry an action.** `Notification` has `action_kind` (`JOIN_REQUEST`), `action_ref` (`<hackathon slug>:<request id>`) and `action_outcome`. The bell shows Accept / Decline buttons for an unanswered request; once it is answered anywhere (bell, dashboard card, cancelled by the sender) every notification about it shows the outcome instead (`NotificationService.resolve_action`).
- **UI.** The dashboard has a "Teams and people" card (teams that need people, people available, and the leader's pending requests); `/hackathons/{slug}/finder` shows everything with search.

## Idea scoring

`JudgingCriterion.target` is `PROJECT` (default) or `IDEA`: the two rubrics are independent (`GET/PUT/POST /{slug}/criteria/?target=`). Judges score ideas on `GET /{slug}/judging/ideas/` and `POST /{slug}/judging/ideas/{id}/` (same rules as projects: own team excluded, locked once results are published); `IdeaSubmission.score` / `review_count` hold the average. `GET /{slug}/leaderboard/?target=IDEA` ranks ideas (rejected ones excluded) and `GET /{slug}/ideas/{id}/reviews/` shows each judge's scores. Changing a rubric re-totals its existing reviews.
