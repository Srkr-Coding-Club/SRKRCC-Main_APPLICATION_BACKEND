# Admin: Managing Blog, Career & Codequest Content

## Blog
1. **Admin → Blog → Pending** — review posts drafted by Contributors (see [../features/role-based-access.md](../features/role-based-access.md)); approve, reject with feedback, or edit before publishing.
2. **Admin → Blog → Posts** — edit, feature, or unpublish any post directly.
3. To schedule a post for a future date, set its **Publish At** field — it goes live automatically (see [Scheduling](../features/scheduling.md)).
4. Manage **Categories/Tags** to keep the blog organized.

Full detail: [../modules/blogs.md](../modules/blogs.md).

## Career
1. **Admin → Career → Listings → New Listing** — add a job/internship: company, role, eligibility, deadline, and whether it links externally or uses a club-run application form (built via [Form Builder](form-builder-admin.md)).
2. Set the listing's deadline — it automatically moves to "Closed/Archive" once the deadline passes (see [Scheduling](../features/scheduling.md)).
3. **Admin → Career → Resources** — upload resume templates, guides, and other files (see [File & Image Management](../features/file-image-management.md)).
4. For club-run drives, view/export applicants under **Admin → Career → [listing] → Applicants**.

Full detail: [../modules/career.md](../modules/career.md).

## Codequest (Daily Problems)
1. **Admin → Codequest → Problems** — the problem bank: add/edit/delete problems and filter the list by upcoming/archived/all, with per-problem submission and solved counts.
2. **Admin → Codequest → mini-calendar** — schedule each problem by picking a future day on the right-side POTD calendar (days already holding a problem are highlighted, past days are locked). The system auto-publishes it as "today's problem" at midnight on that date, and auto-archives the previous day's problem — no manual action needed. Use the **Batch Schedule** action to queue up to five problems in one request.
3. **Admin → Codequest → Review** — for problems needing manual verification (not auto-graded), review submissions (pending/all queue) and record pass/fail verdicts.
4. Monitor engagement (streaks, top solvers) on **Admin → Codequest → Stats** (Overview, Members, Insights, Audit) and the club **Analytics** page; members see their own dashboard at `/codequest/stats`.

Full detail: [../modules/codequest.md](../modules/codequest.md).

## Related Docs
- [../features/scheduling.md](../features/scheduling.md)
- [form-builder-admin.md](form-builder-admin.md)
