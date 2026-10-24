# Architecture: Dynamic Form Validation Engine

## Club ID field

`CLUB_ID` is a scalar text field with submission-time member verification. The
response serializer finds the first active Club ID field when legacy automation
mapping is absent, trims and compares the submitted value case-insensitively,
and rejects unknown IDs before persistence. Legacy mapped verification remains
compatible, including optional name, email, phone, branch, and roll-number
comparisons.

> Where it lives: `apps/forms/validation/`
> What it guarantees: the **stored form definition is the source of truth**. Every
> rule an admin configures in the builder is checked when the form is saved and
> enforced again on every response — no matter what the browser did or didn't do.

## The pipeline

```
Question Type -> Validation Rules -> Response Type -> Conditional Rules
             -> Dependencies -> Cross-Field Rules -> Final Submission Validity
```

`validate_submission(form, raw_answers, *, mode, is_edit, existing_answers)` runs
these steps in order; each feeds the next:

| # | Step | Module | Rejects |
|---|---|---|---|
| 1 | **Structural** | `engine.py` | answer for an unknown / other-form / soft-deleted field; a field answered twice; an answer for a SECTION header or PAGE_BREAK |
| 2 | **Coerce** | `values.py` | value that can't be read as the field's type (`"abc"` for a number, a bare string for a checkbox, a bad date) |
| 3 | **Conditional layout** | `conditional.py` | — (computes visibility + required overrides; **answers for hidden fields are dropped, not errored**) |
| 4 | **Required** | `engine.py` | visible + effectively-required + empty (empty = `None` / `""` / whitespace / `[]` / `{}`) |
| 5 | **Type** | `field_types.py` | bad email/url/phone format; a choice value not in `options`; a matrix cell outside `rows`/`options`; a rating outside `min_value`..`max_value` |
| 6 | **Rules** | `rules.py` | any configured `validation_rules` constraint (length, regex, numeric bounds, selection counts, date bounds, file type/size/count, …) |
| 7 | **Cross-field** | `cross_field.py` | `crossField` comparisons against another answer (password confirm, date order, `required_if`) |

The result is a `ValidationReport`:

```python
report.ok               # -> bool (no errors)
report.errors           # -> list[FieldError]
report.warnings         # -> list[FieldError]  (partial mode / soft findings)
report.cleaned_answers  # -> {field_id: value}  the exact values to persist
```

`cleaned_answers` is coerced and normalized (numbers stored as numbers, dates as
ISO strings, emails lowercased when `normalizeCase` is set, hidden-field answers
removed). **The `Answer.value` JSON shapes are unchanged** from before this engine
existed — string / number / list / dict — so every downstream consumer (CSV
dedupe, the DMC forms adapter, `FormAutomationService`, response export) keeps
working.

## Before the engine: profile auto-fill and round gating

Two steps run in `ResponseSerializer.validate()` / `apps/forms/views.py` around the pipeline above:

- **Profile auto-fill (strict mode only).** For every active field with `profile_field` set, the client-sent answer is discarded and the submitter's profile value is injected before step 1, so it then passes through coercion, type and rule checks like any other answer. Unresolvable fields produce `PROFILE_FIELD_REQUIRES_LOGIN` (anonymous) or `PROFILE_FIELD_MISSING` (required and blank on the profile); these replace any generic error the engine raised for the same field id.
- **Hackathon round gate.** If the form is a hackathon round's `details_form`, `apps.hackathons.services.check_round_form_access` rejects anyone but the leader of a team shortlisted in that published round (`403 ROUND_FORM_RESTRICTED`) before validation runs.

## Strict vs. partial mode

| Mode | Used by | Behaviour |
|---|---|---|
| `strict` (default) | public `POST /api/forms/submissions/`, response edit | every failure is an error → `400` |
| `partial` | admin `manual-entry`, CSV `bulk-ingest`, backup `form_importer` | structural / type / option-membership failures stay errors; `REQUIRED` and constraint failures are **downgraded to warnings** so partial legacy data can still be stored. `?force=true` on manual-entry stores rows with hard errors too (audit-logged). |

## Hidden and calculated questions, option limits, uniqueness

- **HIDDEN** is an answerable text question nobody types into. The form page fills it from `validation_rules.queryParam`
  (a link parameter) or its `defaultValue`, and submits it like any text answer (500 characters at most, same character
  checks). It is never "required". A link can be edited by anyone, so it is a hint, not proof.
- **CALCULATED** is computed by the **server** (`validation/formula.py`) from the settled answers and stored as the answer; whatever
  the client sends for it is ignored. Formulas are `{id}` references, numbers, `+ - * /` and brackets only (no names or calls). A
  blank or hidden reference counts as 0; a reference that is not a number, division by zero or a result of 10^15 or more leaves the
  question without an answer; results round half away from zero to `decimals`. Calculated questions may build on each other (cycles are
  refused at publish). `src/lib/formula.ts` is the browser's copy and the shared vectors (`formulas`) keep both identical.
- **Custom messages** (`validation_rules.messages`) are applied to the error a rule produces (`engine._retext`), so codes, rule names
  and context stay the same for the API; only the text changes.
- **`unique`** and **`optionLimits`** are checked in `ResponseSerializer.validate` after the engine, under the form-row lock that
  serializes submissions (so two people cannot both take the last place). Both count only non-test responses and exclude the response
  being edited.
- **Publish checks:** option limits must name real options with whole numbers from 1; defaults must fit the question; a HIDDEN question needs
  a valid link parameter (warning if it has neither parameter nor default); formulas must parse, reference other number / rating / scale /
  calculated questions on the form, and not form a cycle; `{{id}}` in a label or description that points at an unknown question or one answered
  later warns (`WARN_PIPE_UNKNOWN_FIELD`).

## Conditional-logic engine

Canonical `conditional_logic` shape (stored on the dependent field):

```jsonc
{
  "logic": "AND" | "OR",
  "rules": [
    { "field": 12, "operator": "equals", "value": "Yes" },
    { "logic": "OR", "rules": [ ... ] }          // nested groups allowed
  ],
  "action": "show" | "hide" | "require" | "optional" | "set_value" | "restrict_options" | "display_message",  // default "show"
  "action_value": ...                                   // what set_value / restrict_options / display_message use
}
```

A rule normally compares the watched question with a fixed `value`. A rule with `"value_field": <question id>`
compares it with that other question's current answer instead (for example "End date is before Start date").
It stays dormant until both questions are answered, like any rule on a blank question.

- **Operators** (`schema.CONDITIONAL_OPERATORS`): `equals`, `not_equals`, `gt`,
  `gte`, `lt`, `lte`, `between`, `not_between`, `contains`, `not_contains`,
  `starts_with`, `ends_with`, `matches_regex`, `not_matches_regex`, `is_empty`,
  `is_not_empty`, `selected`, `not_selected`, `includes`, `not_includes`,
  `includes_any`, `includes_all`, `before`, `after`, `on`, `before_or_equal`,
  `after_or_equal`, `date_between`.
- **Actions that take effect**: `show`, `hide`, `require`, `optional`, and three that read `action_value` once the
  layout has settled:
  - `set_value` — the **server** stores `action_value` as the answer (coerced to the question's type), replacing
    anything the client sent, and it satisfies a required question. The form page fills it in as well.
  - `restrict_options` — a choice question may only be answered with the options listed in `action_value`
    (`OPTION_NOT_ALLOWED` otherwise). The form page hides the other options and clears a now-disallowed choice.
  - `display_message` — shows `action_value` (up to 500 characters) as a note on the question or section. No server effect.
- **Page routing** (`skip_to_section`, `end_form`) lives on a `PAGE_BREAK`: the break that starts a page decides,
  from answers on earlier pages, whether to jump ahead to a later break (`action_value` = its id) or end the form. The
  layout gains `pages` (question ids per page) and `skipped_pages`; everything on a skipped page is hidden, and because
  that changes which answers the other conditions can see, routing and the visibility fixpoint settle together. A route
  to a page that is not later, or a rule that cannot be evaluated, never routes.
- **Legacy**: `skip` is accepted and stored but does nothing (publishing warns with `WARN_ACTION_NOT_ACTIVE`).
- The Python engine (`conditional.py`) and the TypeScript engine (`src/lib/formConditional.ts`) must agree.
  `apps/forms/tests/fixtures/conditional_vectors.json` holds the shared operator and layout cases; the frontend keeps a
  byte-identical copy at `src/lib/__tests__/fixtures/conditional-vectors.json` and both suites run it. Change one
  copy → change both.
- **Layout** is a bounded fixpoint (`len(fields) + 1` passes) so cyclic
  dependencies terminate.
- `normalize_conditional_logic` upgrades the two legacy shapes
  (`{"if": id, "equals": v}` and `{"logic", "rules": [{"if": ...}]}`) and the
  frontend's broken `{"if": "parent", ...}` placeholder — the placeholder becomes
  an *unevaluable* rule that never hides or requires a field (it can't block a
  submission), and is surfaced as a `data_health` / definition warning.

## Form-definition validation (the publish gate)

`validate_form_definition(form_or_payload)` -> `DefinitionReport(publishable, errors, warnings)`.

Runs on both a saved `Form` and the raw serializer payload. Wired into:

- `FormSerializer.validate` — blocks a save that sets `status` to `PUBLISHED` /
  `SCHEDULED` while the definition has structural errors. DRAFT saves stay lenient.
- `FormViewSet.publish` — same gate; returns the error list on `400`, the warning
  list alongside `200`.
- `GET /api/forms/{slug}/validate/` — the report on demand, for pre-publish
  diagnostics in the builder.

**Structural (block):** unknown field type; choice field with `< 2` options /
blank / duplicate options; matrix with no rows/columns; RATING/LINEAR_SCALE with
`min >= max`; `validation_rules` key incompatible with the field type;
`minLength > maxLength` (and the value/selection/date analogues); negative
length/count; empty or uncompilable regex; unknown `format`; conditional rule
referencing a missing/deleted field, itself, or an unknown operator; cross-field
rule referencing a missing/incompatible field.

**Also blocked (production hardening):** a custom regex longer than 500
characters or shaped for catastrophic backtracking (a repeated group that already
repeats, e.g. `(a+)+`); a date/time bound that cannot be parsed; rule combinations
no answer can satisfy (`exactLength` outside min/max, `minSelected` above the option
count, `futureOnly` with a past `maxDate`, allowed file types that are all blocked,
`maxFileSizeMB` above the platform limit, ...); more than 200 questions per form,
200 options per question or 100 matrix rows; non-text or over-long (255) options;
blank/duplicate matrix rows and columns; a scale spanning more than 100 steps; a
`profile_field` mapped to a question type that cannot hold it; circular conditional
rule chains (A depends on B depends on A).

**Conditions are checked too:** the operator must suit the watched question (`before` only on dates, `includes_any`
only on choices, `gt`/`between` on numbers, ratings, dates, times or numeric choices, `contains` on text), a rule needs
a value (`DEF_CONDITION_MISSING_VALUE`) of the right shape (`between` takes exactly two numbers or dates, regexes must
compile and not nest repeats, dates must be `YYYY-MM-DD`; `DEF_CONDITION_BAD_VALUE`), `value_field` must name another
question on the form, and `action_value` must make sense for its action (`DEF_CONDITION_BAD_ACTION_VALUE`: a
`set_value` the question cannot hold, a `restrict_options` on a non-choice question or with unknown options, an empty or
over-long note). A `value_field` link counts for circular-rule detection.

**Pages are checked too:** routing actions only on a page break (`DEF_ROUTING_ON_QUESTION`); a page break only routes
(`DEF_PAGE_BREAK_ACTION`); `skip_to_section` must name a later page break (`DEF_ROUTE_TARGET_INVALID`); a routing rule
cannot watch questions on its own page or later (`DEF_ROUTE_WATCHES_LATER_PAGE`). Empty pages warn (`WARN_EMPTY_PAGE`).

**Soft (warn):** a rule comparing with an option the watched question does not have (it can never be true);
an action that needs pages (above); no required fields; the `{"if": "parent"}` placeholder;
unreachable conditions; duplicate display order; two questions with the same label
(ambiguous export columns); regex syntax the browser does not support (`(?P<name>`,
`\A`, `\Z`, inline flags).

## Safety limits (production hardening)

`validation/limits.py` holds hard ceilings that apply **regardless of what an admin
configures** - rules can only be tighter. `values.coerce_value` enforces them before
any rule runs, so nothing oversized or unstorable reaches the database.

| Area | Behaviour |
|---|---|
| Text | Control characters (except tab/LF/CR) and lone surrogates are rejected (`INVALID_CHARACTERS`) - PostgreSQL JSON cannot store them. Ceilings per type (`VALUE_TOO_LONG`): TEXT 1000, PARAGRAPH 10000, EMAIL 254, PHONE 32, URL 2048, CLUB_ID 32, RADIO/DROPDOWN 500. |
| Numbers | `NaN`, `Infinity` and magnitudes `>= 1e31` are `INVALID_NUMBER`; fractions are limited to 15 digits so they round-trip exactly; commas must be well-formed thousands groups (`1,234`), so `1,2,3` is rejected. |
| Collections | At most 200 checkbox selections / matrix rows / files per answer; nested values inside a matrix cell are `EXPECTED_SCALAR`. |
| Files | The browser's `size` and `type` are **not trusted**: for an inline `data:` URL the size is recomputed from the base64 payload, the type is taken from the data URL, and the first bytes must match the extension for png/jpg/gif/pdf/zip/docx/xlsx/pptx (`FILE_CONTENT_MISMATCH`). Max 10 MB per file, 25 MB per answer; only `data:` or `http(s)` URLs are stored. `FILE` takes one file, `MULTI_FILE` defaults to 10. Executables and active content (`.exe .msi .bat .cmd .com .scr .dll .vbs .ps1 .html .htm .xhtml .svg`) are blocked unless the field's `allowedFileTypes` lists the extension. |
| Signature | Must be a PNG data URL (or http(s) link) up to 512 KB (`INVALID_SIGNATURE`). |
| Dates | Outside 1900-01-01 .. 50 years ahead is `DATE_OUT_OF_BOUNDS`. "Today" uses the server's local date (`Asia/Kolkata`). |
| Phone | Default 7-15 digits (E.164) unless the admin supplies a `pattern`. |
| Club ID | Format `[A-Za-z0-9_-]{3,32}`. Verification failures (unknown ID *or* a mismatched detail) return the single generic `CLUB_ID_VERIFICATION_FAILED` on the Club ID field so the endpoint cannot be used to enumerate IDs or probe member details; the specific cause is logged. |

**Fail closed.** A rule that raises while evaluating returns
`RULE_EVALUATION_FAILED` (and is logged) instead of silently passing the value.

**Canonical storage.** RADIO / DROPDOWN / CHECKBOX / MATRIX answers are stored with
the admin's spelling of the matched option, not the casing the client sent, so
analytics and exports do not split "python" from "Python".

**Concurrency.** The submission endpoint takes a row lock on the `Form` for the
duration of the create transaction, so the per-user cap, the form-wide
`max_total_responses` cap (re-checked under the lock, `MAX_TOTAL_RESPONSES_REACHED`)
and the duplicate-email check cannot be raced by simultaneous requests. The
duplicate-email check is case-insensitive.

## Error contract

Submission `400` body (returned verbatim — `FormValidationError` is a plain
`APIException` so DRF does not re-wrap it):

```json
{
  "detail": "Validation failed for 2 field(s).",
  "code": "VALIDATION_FAILED",
  "errors": [
    { "field_id": 12, "label": "Your Name", "code": "INVALID_FORMAT",
      "rule": "format", "message": "Only letters and spaces are allowed.",
      "context": { "format": "alpha" } }
  ],
  "warnings": []
}
```

All applicable errors are returned at once — the engine never stops at the first.
Codes are defined in `apps/forms/validation/codes.py`.

## Adding a new rule or field type

- **New `validation_rules` key** — add it to the relevant set in
  `schema.RULE_COMPAT`, register a `@_register("yourKey")` function in `rules.py`,
  add a code to `codes.py`. Done — the engine picks it up.
- **New field type** — add the enum value, register a validator in
  `field_types.FIELD_VALIDATORS`, add a `coerce_value` branch in `values.py`, and
  extend `schema.RULE_COMPAT`. The pipeline itself doesn't change.

## Related

- `docs/reference/form-validation-rules.md` — every supported rule key and
  operator, per field type.
- `docs/features/dynamic-form-builder.md` — the admin-facing feature.
- `docs/architecture/data-model-dynamic-forms.md` — how forms are stored.
