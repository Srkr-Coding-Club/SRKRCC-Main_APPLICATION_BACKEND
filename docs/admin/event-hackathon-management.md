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

## Creating a Hackathon
1. **Admin → Hackathons → New Hackathon**.
2. Set theme, dates, tracks, prize pool, rounds/timeline.
3. Build the registration form via [Form Builder](form-builder-admin.md) — typically includes team details, track selection, file uploads.
4. Assign **Judges** and **Volunteers** in the hackathon's Team/Access section (see [user-role-management.md](user-role-management.md)).
5. Set registration open/close dates (see [Scheduling](../features/scheduling.md)).
6. During the event, monitor submissions under **Teams & Submissions**.
7. Judges score via **Judging Panel**.
8. Compile and **Publish Results** — this triggers result notification emails automatically (see [Custom Email & Notifications](../features/email-notifications.md)).

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
