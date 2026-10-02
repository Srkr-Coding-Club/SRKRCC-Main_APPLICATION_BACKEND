# Admin: Managing Events, Hackathons & IconCoders

## Creating an Event
1. **Admin → Events → New Event**.
2. Fill in title, description, date/time, venue (or online link), capacity.
3. Attach or build a registration form via [Form Builder](form-builder-admin.md).
4. Set the visibility window (see [module-management-feature-flags.md](module-management-feature-flags.md)) — e.g. visible from today until the event date.
5. Publish.
6. On event day, use **Attendees → Mark Attendance** (accessible to assigned Volunteers too).
7. After the event, it automatically moves to the "Past Events" archive per its visibility window.

Full detail on this module: [../modules/events.md](../modules/events.md).

## Running a Hackathon

### 1. Create it
**Admin → Modules → Events & Hackathons → New Hackathon**: title, slug, theme, dates, prize pool, banner, description. Then click **Manage** on its card (or go to **Modules → Hackathon Management**).

### 2. Registration tab
- **Registration opens / closes** — blank "opens" means immediately. After "closes", participants can no longer create teams, invite, accept invites, edit or leave.
- **Min / max team size** — teams below the minimum show as *Forming*; they become *Registered* automatically once they reach it. Pending invites count toward the maximum.
- **Required profile details** — e.g. phone number, branch, roll number. Members must have these on their SRKRCC profile before they can create or join a team, so nobody re-types them.
- **Lock team changes** — freezes all participant-side team changes (admins can still edit).
- **Close / Reopen hackathon** — closing stops registration regardless of the window but keeps the hackathon listed.

### 3. Problem Statements tab
Add statements with a code (e.g. `PS-01`), title, Markdown description, category, tags and an optional **max teams**. Leaders pick one when creating their team; full statements are disabled in the picker. Deactivate a statement to hide it — a statement teams have already picked cannot be deleted.

### 4. Teams tab
Search by team, member name, email or roll number; filter by status or problem statement; **Export CSV** (one row per member with contact details). Click a team to open its drawer: members' phone/branch/year/roll/Club ID, pending invites, round history, and admin overrides — **add member** by email (skips invites, size limit and registration window), remove a member, make someone leader, or change the team status (e.g. *Disqualified*).

### 5. Rounds & Shortlisting tab
1. **New round** — the first round includes every *Registered* team; each later round includes the teams shortlisted in the previous round. Use **Sync teams** if teams became eligible after the round was created.
2. Optionally attach a **details form** built in the [Form Builder](form-builder-admin.md) to re-collect information (repo link, deck, updated contacts — Profile Auto-fill fields work here). Only leaders of teams shortlisted in that round can submit it, once per team, after results are published. **View responses** opens it in the Responses viewer.
3. Tick teams → **Shortlist** / **Not shortlisted** / **Reset to pending**, with optional feedback (shown to the team) and an internal note.
4. **Publish results** — teams only see their outcome and feedback after this. Optionally post an announcement to the shortlisted teams in the same step. **Unpublish** hides results again.
5. Repeat with the next round. A round can't be deleted while later rounds exist.

### 6. Announcements tab
Post Markdown announcements to **Everyone (public)**, **All registered participants**, **All teams in a round**, **Shortlisted teams in a round** (visible only after that round is published) or **Specific teams**. Schedule with *Publish at*, auto-hide with *Expires at*, and tick **Also email the audience** (or use the mail button later) to send it through the email-job engine. Public announcements also appear on the hackathon's public page.

### What participants see
The public page `/hackathons/[slug]` shows problem statements, public announcements and a Register / Go-to-team button. `/hackathons/[slug]/dashboard` is their team workspace (create team, invites, members, rounds timeline with results and details-form links, announcements), and `/profile` lists their teams and pending invites.

Full detail on this module: [../modules/hackathon.md](../modules/hackathon.md).

## Creating an IconCoders Edition
IconCoders editions are created the same way as any hackathon (above), but flagged as "flagship" so they also appear on the permanent `/iconcoders` branded page and, after results, get added to the **Hall of Fame**.

1. **Admin → IconCoders → New Edition** (this creates a linked Hackathon entry).
2. Fill in the year's theme, sponsors, prize pool, dates.
3. Manage sponsor logos under **Sponsor Manager**.
4. Everything else (form, judging, results) works exactly like a regular hackathon — see above.
5. After results are published, curate what appears in **Hall of Fame** (winning team, project, photos).

Full detail on this module: [../modules/iconcoders.md](../modules/iconcoders.md).

## Related Docs
- [../modules/events.md](../modules/events.md), [../modules/hackathon.md](../modules/hackathon.md), [../modules/iconcoders.md](../modules/iconcoders.md)
- [form-builder-admin.md](form-builder-admin.md)
- [module-management-feature-flags.md](module-management-feature-flags.md)
