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
| `Hackathon` | The event. Registration settings: `registration_opens_at`, `registration_closes_at`, `min_team_size`, `max_team_size`, `team_edits_locked`, `allow_open_innovation` (default on), `required_profile_fields` (list of `forms.ProfileField` keys every member must have on their profile). | `is_registration_open` = `status == LIVE` and now inside the (optional) window. |
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
- *Create team*: registration open; caller not already in a team for this hackathon; caller's profile has every `required_profile_fields` value; name not taken; a problem is chosen (see below). Creating a team cancels the caller's other pending invites for that hackathon.
- *Problem choice* (`_resolve_problem`, used by create and edit): the team picks one **active statement** (under its `max_teams`) **or** goes **open innovation** — never both (`PROBLEM_CHOICE_CONFLICT`). Open innovation requires `allow_open_innovation` (`OPEN_INNOVATION_DISABLED`) and a non-blank **title** (≤255), **description** (≤5000) and **domain** (≤100) — each missing or overlong value fails with `OPEN_INNOVATION_INCOMPLETE` / `OPEN_INNOVATION_TOO_LONG` naming the field. With neither, a problem is mandatory only once the hackathon has active statements (`PROBLEM_STATEMENT_REQUIRED`). Switching either way is validated before anything is saved; leaving a statement frees its slot, and open-innovation teams never use up statement slots.
- *Invite*: leader only; exact email of an existing account (`USER_NOT_FOUND` otherwise); invitee not already in any team of this hackathon; no duplicate pending invite; `members + pending invites < max_team_size`. After the transaction commits, `send_invite_email` emails the invitee a link to their dashboard on a background thread (a send failure never affects the invite).
- *Accept*: invitee only; re-validates everything under a row lock (registration open, edits not locked, team active, one-team rule, invitee's profile complete, team not full); cancels the user's other pending invites.
- *Remove member / transfer leadership / edit name or problem*: leader only (admins bypass). *Leave*: any member; the leader must transfer first unless they are the only member, in which case the team becomes `WITHDRAWN`.
- Participant-side changes require open registration and `team_edits_locked = False`, and the team must be `FORMING`/`REGISTERED`. Admin overrides (`admin_add_member`, `admin_set_status`) skip window/size checks but still enforce one team per user.

**Rounds** — `RoundService`
- Round 1 entries = every `REGISTERED` team. Round N entries = `REGISTERED` teams `SHORTLISTED` in round N−1. `populate` adds teams that became eligible after the round was created.
- `decide` sets status (and optionally feedback/notes) for many teams at once.
- Until `results_published`, participants see their entry as `PENDING` with no feedback. `publish` can also post a `ROUND_SHORTLISTED` announcement (`announce: true`) and email it to the shortlisted teams' members (`email: true`).
- A round cannot be deleted while later rounds exist (they depend on its shortlist).

**Details forms** — `check_round_form_access(form, user)` is called by `apps/forms/views.py` on submit and on edit. If the form is any round's `details_form`, only the **leader** of a `REGISTERED` team **shortlisted** in that round, after results are **published**, may submit (`403 ROUND_FORM_RESTRICTED` otherwise). The saved `Response` is linked to `RoundEntry.details_response`. Forms not attached to a round are unaffected.

**Announcements** — `AnnouncementService.visible_for(hackathon, user)`

| Audience | Who sees it |
|---|---|
| `PUBLIC` | Everyone, including logged-out visitors and the public hackathon page |
| `PARTICIPANTS` | Members of any `FORMING`/`REGISTERED` team |
| `ROUND_ALL` | Teams with an entry in the chosen round |
| `ROUND_SHORTLISTED` | Teams shortlisted in the chosen round, **only once that round's results are published** |
| `TEAMS` | Members of the selected teams |

Only active announcements with `publish_at <= now` and not past `expires_at` are shown. `send_email=True` (or the admin "notify" action) emails the audience through the shared email-job engine; see [Email & Notifications](../features/email-notifications.md).

## API (`/api/hackathons/`)

Participant / public:

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/{slug}/problem-statements/` | Anyone, no login (active only; admins see all) | List statements with `code`, `domain`, `team_count` / `slots_left` |
| GET | `/{slug}/announcements/` | Anyone (filtered by audience) | Announcements visible to the caller |
| GET | `/{slug}/my-team/` | Authenticated | Dashboard payload: hackathon, `profile_missing`, team, `is_leader`, pending invites, rounds (entry status/feedback hidden until published, details-form eligibility) |
| POST | `/{slug}/teams/` | Authenticated | Create a team `{name, problem_statement}` **or** `{name, open_innovation: {title, description, domain}}` |
| GET | `/{slug}/user-lookup/?email=` | Authenticated, throttled `hackathon_lookup` 30/min | Exact-email lookup returning only `id, name, email, club_id, can_invite, reason` |
| GET | `/my-teams/`, `/my-invites/` | Authenticated | Caller's teams / pending invites across hackathons |
| POST | `/invites/{id}/accept/`, `/invites/{id}/decline/` | Invitee | Respond to an invite |
| GET, PATCH | `/teams/{id}/` | Members read; leader/admin write | Team detail (admins also get members' contact details and round history). PATCH accepts `problem_statement` or `open_innovation` to change the problem; a body without either leaves it untouched |
| POST | `/teams/{id}/{action}/` | See below | `invite {email}`, `cancel-invite {invite_id}`, `remove-member {user_id}`, `transfer-leadership {user_id}` (leader/admin); `leave` (member); `admin-add-member {email}`, `admin-set-status {status}` (admin) |

Non-members get `404` for another team, so team IDs can't be probed.

Admin (`IsAdminOrClubLead`):

| Method | Path | Purpose |
|---|---|---|
| PATCH | `/{slug}/` | Registration settings (validated: `max_team_size >= min_team_size >= 1`, closes after opens, known profile-field keys) |
| POST | `/{slug}/close/`, `/{slug}/reopen/`, `/{slug}/hide/`, `/{slug}/show/` | Hackathon status / public visibility |
| GET | `/{slug}/stats/` | Teams by status, participants, pending invites, per-statement uptake, per-round funnel |
| POST, PATCH, DELETE | `/{slug}/problem-statements/[{id}/]` | Manage statements. `POST {title, description, domain, tags?, max_teams?}` — the `code` is generated and any `code` sent is ignored |
| POST | `/{slug}/problem-statements/upload/` | Multipart `file`: a CSV with `title`, `description`, `domain` columns (aliases such as `problem title`, `details`, `category`/`track` accepted; UTF-8, ≤1 MB, ≤500 rows). Generates IDs, skips rows repeating an existing title + domain, reports invalid rows by row number: `{created, skipped, errors, codes}` |
| GET | `/{slug}/teams/?status=&problem_statement=&round=&entry_status=&search=` | All teams with members' contact details (`problem_statement=open_innovation` lists open-innovation teams) |
| GET, POST, PATCH, DELETE | `/{slug}/rounds/[{id}/]` | Manage rounds (`POST` auto-populates entries unless `populate=false`) |
| GET | `/{slug}/rounds/{id}/entries/?status=&search=` | Teams in a round |
| POST | `/{slug}/rounds/{id}/decide/` | `{team_ids, status, feedback?, admin_notes?}` |
| POST | `/{slug}/rounds/{id}/publish/`, `/unpublish/`, `/populate/` | Results visibility / sync eligible teams |
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
2. **Per-hackathon window** — non-admins only see hackathons inside `visible_from`/`visible_until` (`HackathonViewSet.get_queryset`); every `/{slug}/…` participant endpoint uses the same filter. Admins see everything. `close` stops registration but keeps the hackathon listed; `hide` removes it from public view.

## Not Implemented
- Judge scoring / rubrics (`JUDGE` role exists but no scoring UI or model is wired to rounds). Shortlisting is a manual admin decision.
- A public results page / leaderboard.
- Invite expiry (pending invites stay open until accepted, declined, cancelled, or the invitee joins another team).

## Data Export
DMC datasets `hackathon_participants` (one row per `TeamMember`, role Leader/Member), `hackathon_teams`, `hackathon_submissions` and `hackathon_round_entries` ("Hackathon Round Results": one row per team per round with result, feedback, published flag and whether the details form was submitted) — see [DMC](dmc.md). The admin Teams tab also exports a per-member CSV client-side.

## Related Docs
- [../admin/event-hackathon-management.md](../admin/event-hackathon-management.md) — admin walkthrough
- [forms.md](forms.md) — details forms and profile auto-fill fields
- [iconcoders.md](iconcoders.md) — the flagship hackathon built on this module
