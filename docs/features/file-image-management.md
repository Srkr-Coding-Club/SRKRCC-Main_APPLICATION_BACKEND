# Feature: File & Image Management

## What It Is
Secure upload and storage for every file the platform handles — event posters, hackathon submissions, resumes, blog cover images, sponsor logos — backed by Cloudflare R2 and delivered through a CDN for fast loading anywhere.

## Why It Exists
A club platform generates a lot of files: posters for every event, resumes for every career applicant, project submissions for every hackathon team, images for every blog post. These need to be stored reliably, served quickly (a slow-loading poster looks bad), and access-controlled (a resume shouldn't be publicly listable; a hackathon submission shouldn't be downloadable by other teams).

## How It Works
1. A user or admin uploads a file through a form field (see [Dynamic Form Builder](dynamic-form-builder.md) — "File Upload" and "Multi File Upload" field types) or a dedicated upload screen (e.g. blog cover image).
2. The file is stored in **Cloudflare R2** (not the database — the database only stores a reference/link to it).
3. Files are served through a **CDN**, so images load fast for users regardless of their location.
4. Access rules apply per file type — public files (posters, blog images) are openly viewable; private files (resumes, hackathon submissions) are only accessible to users with the right role (see [Role Based Access](role-based-access.md)).

## Where It's Used

| Module | File types stored |
|---|---|
| [Events](../modules/events.md) | Posters, banner images |
| [Hackathons](../modules/hackathon.md) / [IconCoders](../modules/iconcoders.md) | Project submissions, sponsor logos, Hall of Fame photos |
| [Career](../modules/career.md) | Resumes, resource PDFs/templates |
| [Blog](../modules/blogs.md) | Cover images, embedded media |
| Home | Hero banner images |

## Related Docs
- [../architecture/tech-stack.md](../architecture/tech-stack.md) — Cloudflare R2 details
- [../architecture/backup-security.md](../architecture/backup-security.md) — how uploaded files are backed up
- [dynamic-form-builder.md](dynamic-form-builder.md)
