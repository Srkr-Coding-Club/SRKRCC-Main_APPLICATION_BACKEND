# Celebrations

## Overview
The `celebrations` application (`apps/celebrations`) marks happy moments for a person: a welcome after signing up, a team being registered, a teammate joining, a shortlisting, a winning, a streak. Each moment becomes one `Celebration` row for that person, kept on their **account**, so it is shown once on whatever device they open next. It is separate from notifications: clearing the bell does not clear a celebration, and a celebration is a one-time card with confetti, not an inbox entry.

This page covers the backend. The site-wide card and confetti are the frontend's `CelebrationHost`.

## Data model (`apps/celebrations/models.py`)
### `Celebration`
- `user` (`CASCADE`), `kind` (see `kinds.py`), `key` (names this exact moment, e.g. `team-registered:12`)
- `size` (`SMALL`, `MEDIUM`, `BIG`), `title` (120), `message` (300), `link_url`, `payload` (plain facts only, never private data)
- `expires_at` (14 days after creation), `seen_at`
- Unique on `(user, key)`: a moment is celebrated **once per person**, even if the code that awards it runs twice.

### `CelebrationSetting`
- `user` (one to one), `enabled` (default on). No row means on. While off, nothing is created for that person.

## Kinds (`apps/celebrations/kinds.py`)
Every kind is declared with its size and default text, with `{placeholders}` filled from the context given when awarding (a missing one is left empty, never an error). Kinds: `account.welcome`, `account.email_verified`, `account.profile_completed`, `account.club_member`, `event.registered`, `event.checked_in`, `event.badge_earned`, `hackathon.team_registered`, `hackathon.waitlist_promoted`, `hackathon.member_joined`, `hackathon.joined_team`, `hackathon.idea_approved`, `hackathon.project_submitted`, `hackathon.shortlisted`, `hackathon.winner`, `hackathon.certificate_ready`, `hackathon.checked_in`, `codequest.streak`, `codequest.first_solve`, and `custom` (the caller gives the title and message). Add a kind there before awarding it.

## Service (`apps/celebrations/services.py`)
| Function | What it does |
|---|---|
| `award(user, kind, key, link_url='', size=None, **context)` | Creates the celebration unless the person has them off, cannot sign in, or already has this `key`. An unknown kind raises (a programming error). |
| `award_after_commit(users, kind, key, **kwargs)` | What other apps call: celebrates each person once the surrounding transaction commits, so a rolled-back action celebrates nothing. A failure is logged and swallowed. |
| `retract(key, users=None)` | Removes celebrations nobody has seen yet, when the moment is undone (a team withdrawn before its members saw the card). |
| `pending(user)` | Unseen, unexpired, biggest first then oldest, at most 10; empty while the person has them off. |
| `mark_seen(user, ids)` | Marks the person's own; others' ids are ignored. |
| `sweep()` | Deletes unseen ones past their expiry and seen ones older than 30 days. |

## API (`/api/celebrations/`, signed-in users; exempt from the email-verification block so the welcome shows before verifying)
| Method | Path | Description |
|---|---|---|
| `GET` | `/pending/` | `{enabled, results}`: what to celebrate now. Each result: `id, kind, size, title, message, link_url, payload, created_at`. |
| `POST` | `/seen/` | `{ids: [..]}` (up to 50): the site showed these. Returns `{marked}`. |
| `GET`, `PATCH` | `/settings/` | `{enabled}` read or set (a real boolean). |

## Where celebrations are raised
Each hook calls `award_after_commit`, so a rolled-back action celebrates nothing, and a failure never breaks the action.

| Moment | Raised from | Key | Who |
|---|---|---|---|
| Welcome | `accounts.views.RegisterView.perform_create` (sign-up, and an admin creating a member) | `welcome` | The new member |
| Email verified | `email_verification.mark_verified` and `confirm` (the first time only) | `email-verified` | The member |
| Event registered | `apps/events/signals.py`: a signed-in person's own registration form response for an event (not a preview, not an admin's manual entry) | `event-registered:<event>` | The registrant |
| Event check-in | `attendance.services.celebrate_check_in` on the first scan at any session of an event | `event-check-in:<event>` | The registrant |
| Team registered | `hackathons.services._recompute_status` the first time a team is placed and meets its minimum size (payment verified, member joined, or at creation) | `team-registered:<team>` | Every member |
| Place opened | `WaitlistService.promote` | `waitlist-promoted:<team>` | Every member |
| Someone joined | `hackathons.services._notify_member_joined` (invite accepted, join request accepted, organizer added) | `joined-team:<team>:<user>`, `member-joined:<team>:<user>` | The newcomer, and the others |
| Idea approved | `IdeaService.review` (approved only) | `idea-approved:<idea>` | Every member |
| Project submitted | `SubmissionService.save` (first save only) | `project-submitted:<team>` | Every member |
| Shortlisted | `RoundService.publish_results`, and `decide` after publishing | `shortlisted:<round>:<team>` | Every member of a shortlisted team |
| Winner | `ResultsService.publish` | `winner:<award>` | Every member of an awarded team |
| Certificate ready | The hackathon update that releases certificates | `certificate-ready:<hackathon>` | Everyone who earned one |
| Hackathon check-in | `CheckInService._record`, main (arrival) session only | `hackathon-check-in:<hackathon>` | The person checked in |
| First solve, streak | `codequest.services.celebrate_progress` after a correct verdict; a streak only on the day it reaches 7, 30 or 100 | `first-solve`, `streak:<days>` | The member |

**Taken back.** `hackathons.celebrate.team_gone` retracts the unseen registered, waitlist, shortlist and winner cards of a team that is withdrawn or disqualified; unpublishing round results or the hackathon's results retracts those unseen cards. Cards already seen stay.

**Never celebrated.** Payment rejected, not shortlisted, rejected ideas, withdrawals.

**Declared but not yet raised:** `account.profile_completed`, `account.club_member`, `event.badge_earned`, `custom`.

## Operations
`manage.py sweep_celebrations` deletes expired and old seen rows; run it daily from cron.

## Tests
`apps/celebrations/tests/test_celebrations.py`: kinds and rendering, awarding once per moment, the commit and rollback rules, ordering and the cap, seen, retract, sweep, the preference, cascading delete, and the API including a person blocked for not verifying their email.
