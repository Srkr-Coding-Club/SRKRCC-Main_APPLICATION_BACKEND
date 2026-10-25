# Form File Uploads (direct to S3)

File and Multi-File questions can send their files straight to an S3 bucket instead of embedding them in the submission. This page covers the backend, the filler uploader and the admin Files tab. It also covers housekeeping (sweeper, malware scan, backfill) and the Terraform bucket.

## Switches
| Setting | Meaning |
|---|---|
| `FORM_UPLOADS_BACKEND` | `inline` (application default: local and Render, legacy base64) or `s3`. The dev Lambda stack sets `s3` via `enable_form_uploads`. Set that variable to `false` to roll Lambda back to base64. |
| `FORM_UPLOAD_BUCKET` | Bucket for uploads (falls back to `AWS_STORAGE_BUCKET_NAME`). Use a dedicated bucket. |
| `FORM_UPLOAD_DEFAULT_MB` | Limit for a question whose admin set none. Default **2**. |
| `FORM_UPLOAD_CEILING_MB` | Most any admin may set. Default 25. |
| `FORM_UPLOAD_PENDING_HOURS` | How long a picked-but-unsubmitted file is kept (default 168 = 7 days, enough for a draft). |
| `FORM_UPLOAD_MAX_PENDING_PER_FORM` | Files one person may have waiting on one form (default 50). |

Credentials come from the process role / default AWS chain, never from settings.

## Flow
1. `POST /api/forms/{slug}/uploads/` `{field_id, name, size, type}` (signed in). The server checks the form is open, the question takes files, the name, type rules (`allowedFileTypes`, `blockedFileTypes`, risky types such as .exe/.html/.svg unless allowed) and the size (`maxFileSizeMB`, else the default, never above the ceiling). It creates a `FormUpload` row (`PENDING`, key generated server-side) and returns a **presigned POST** with `content-length-range` and the content type signed in, so S3 itself refuses a wrong size or type.
2. The browser posts the file to S3.
3. `POST /api/forms/{slug}/uploads/{id}/complete/`. The server asks S3 for the real size, re-applies the limits (the admin may have changed them), and checks the first bytes against the extension. A failing file is deleted and the row is kept as `REJECTED`. Passing marks it `UPLOADED`. Calling twice is harmless.
4. `DELETE /api/forms/{slug}/uploads/{id}/` removes a file picked but not submitted (object deleted, row `DELETED`).

A single-file question keeps one file: a new pick replaces the earlier one. A multi-file question stops at `maxFiles`. Only the owner can complete or remove an upload, and only through its own form.

Throttle scope `form_upload` (120/min per account). Rows: `FormUpload` (states `PENDING → UPLOADED → ATTACHED`, or `REJECTED` / `DELETED`).

## Submitting
The answer for a file question carries references: `{"upload_id": "<uuid>"}` per file (the browser may also send name/size/type, but they are ignored).

1. **Before validation** (`upload_attach.resolve_references`): each reference is replaced by what the server holds (name, real size, type). A reference is refused (`UPLOAD_NOT_FOUND`) unless the upload is the caller's own, finished (`UPLOADED`), for this form and this question, or already attached to the response being edited. The same upload twice in one answer is refused. Anonymous callers cannot use uploads. Every normal file rule (types, `maxFiles`, `minFiles`, size) then runs on the real values; a question with no `maxFileSizeMB` gets the default (2 MB).
2. **On save** (`upload_attach.attach`, inside the same transaction as the response): ownership is checked again under a row lock, each object is copied from `form-uploads/tmp/...` to `form-uploads/files/<form>/<upload>`, and the row becomes `ATTACHED` to the response. If the copy fails nothing is saved and the person can submit again (`UPLOAD_STORAGE_ERROR`); their files stay `UPLOADED`.
3. **Edits**: files left out of the edited answer are marked `DELETED` and their objects removed after the transaction commits.
4. **Deletes**: deleting a response or a form removes its objects after commit (signals in `signals.py`). Object removal never fails a request; leftovers are for the sweeper (later phase).

The stored answer is `[{"name", "size", "type", "upload_id"}]`; no link is stored. Old inline (`data:`) answers still validate and display, so existing responses keep working until the backfill.

## Admin limits
Builder: "Max file size (MB)" shows the default as its placeholder and the platform maximum in a hint. Saving a form with a larger value is refused (`maxFileSizeMB exceeds the platform limit of 25 MB`). `GET /api/forms/upload-config/` (public) returns `{direct_upload, default_mb, max_mb}`; the builder and the filler page read it.

## Drafts
While direct upload is on, a draft keeps a file answer as references. On save and again on restore the server keeps only this person's own finished, unexpired uploads for that question (rewritten from the database, so the browser cannot smuggle in links or other people's files); anything else is dropped. With the feature off, files are never stored in a draft.

## Managing files (admins and club leads)
Per form, under `/api/forms/{slug}/`:

| Call | What it does |
|---|---|
| `GET files/` | Attached files, newest first. Filters: `field`, `kind` (images/documents), `search` (file name, person), `date_from`, `date_to`, `page`, `page_size`. Returns `total_bytes` over the filter. Storage keys are never returned. |
| `GET files/{id}/link/?inline=1` | A 60-second presigned link. Images and PDFs may open in the page; everything else (and anything not asked for inline) is forced to download. Admins: any file. Anyone else: only their own. |
| `POST files/delete/` `{ids}` | Delete up to 100 files: removed from the answer (an emptied answer is deleted and, for a required question, reported in `responses_missing_required_file`), marked `DELETED`, object removed after commit. Audit-logged. |
| `POST files/{id}/replace/` `{upload_id}` | Swap in a file the admin has just uploaded for the **same question** (through the normal upload flow, so type/size rules apply). The old file is deleted, the new one records `replaces`, the answer is updated, and it is audit-logged. A storage failure changes nothing. |

The admin UI is the **Files** switch next to **Responses** on `/admin/responses` (shown only for forms with file questions while direct upload is on): thumbnails, filters, preview, download, replace, delete, bulk delete, and the same files are clickable inside a response row or drawer.

## Testing against a real S3 API
`apps/forms/tests/test_uploads_localstack.py` runs the flow against LocalStack (skipped unless `LOCALSTACK_URL` is set):
```
docker run -d -p 4566:4566 -e SERVICES=s3 localstack/localstack:3.8
AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test LOCALSTACK_URL=http://127.0.0.1:4566 python manage.py test apps.forms.tests.test_uploads_localstack
```
It posts signed forms over HTTP exactly as a browser would, and checks the bucket itself refuses an oversize file, a changed key or a changed content type. `FORM_UPLOAD_ENDPOINT_URL` points the app at LocalStack/MinIO; leave it empty for AWS. The frontend repo's `pnpm test:e2e:live` also drives the whole thing in a real browser against LocalStack (bucket CORS included).

## Housekeeping commands
| Command | Run | What it does |
|---|---|---|
| `sweep_form_uploads` | hourly or daily | Removes uploads nobody attached in time (`FORM_UPLOAD_PENDING_HOURS`, default 7 days) and their objects; forgets finished rows older than 30 days. The bucket's lifecycle rule is the safety net if this stops running. |
| `scan_form_uploads` | every few minutes, with `FORM_UPLOAD_SCAN=guardduty` | Reads the verdict GuardDuty Malware Protection for S3 puts on each object (tag `GuardDutyMalwareScanStatus`). Clean -> `CLEAN`. Malware -> removed from the answer and the bucket, marked `INFECTED`, audit-logged ("Malware Found In Form File"). |
| `backfill_form_uploads [--form slug] [--limit n] [--dry-run]` | once, after switching to `s3` | Moves old base64 (`data:`) file answers into the bucket and rewrites them as references. Repeatable. Files that cannot be moved (unreadable, too large, content not matching the extension, storage failure) are left exactly as they were and counted as skipped. Old responses keep working without it. |

**Scanning.** With `FORM_UPLOAD_SCAN=guardduty` a file is served (admin link) only once its verdict is clean; until then the link call answers `423 SCAN_PENDING` ("still being checked") and re-reads the verdict itself. Enable GuardDuty Malware Protection for S3 on the form-uploads bucket first; with scanning `off` nothing is held back.

## The admin Maintenance page
There is no scheduler in this app, so these tasks (and `purge_form_drafts`) are run on demand from **Admin -> Data Center -> Maintenance** (`/admin/maintenance`, administrators only). For each task the page shows:

* a state: **Needs running** (work is waiting, e.g. 4 unused uploads), **Run soon** (nothing waiting but it has not run within its interval: uploads 24 h, scan 1 h, drafts 7 days), **Up to date**, or **Not in use** with the reason (uploads or scanning switched off);
* how much is waiting, when it last ran, who ran it (a person or "from the command line") and what it did, or why it failed;
* a **Run now** button (asks first; runs in the background and the page updates when it finishes; a task cannot be started twice at once).

The admin menu shows an amber badge on Data Center -> Maintenance with the number of tasks that need attention. Every run, from the page or the command line, is recorded (`MaintenanceRun`); a run stuck "running" for 15 minutes is treated as dead. Starting a task from the page is audit-logged.

API: `GET /api/admin/maintenance/` and `POST /api/admin/maintenance/<task>/run/` (both admin-only). To add a task, register a `maintenance.Task` (see `apps/forms/maintenance_tasks.py`).

## Infrastructure (`infra/terraform/s3_form_uploads.tf`)
A bucket separate from media: public access blocked, AES-256 encryption, TLS-only policy, versioning, a CORS rule for the site origin (`POST`/`GET`, from `cors_allowed_origins` and `csrf_trusted_origins`), and lifecycle rules: unattached uploads under `form-uploads/tmp/` expire after `form_upload_tmp_retention_days` (8, must stay above the app's pending window), and old versions of deleted or replaced files after `form_upload_noncurrent_days` (7). The Lambda role may read, write and delete objects in it. `enable_form_uploads` defaults to true, which sets `FORM_UPLOADS_BACKEND=s3`, the bucket, and the region on Lambda. After apply, set the frontend `UPLOAD_CONNECT_SRC` to the `form_uploads_bucket_origin` output (`https://<bucket>.s3.<region>.amazonaws.com`) and rebuild the site. The bucket CORS rule already allows `cors_allowed_origins` and `csrf_trusted_origins`.

## Not built yet
ZIP download of many files, and a GuardDuty plan itself (enabled in the AWS console / its own Terraform, not here).
