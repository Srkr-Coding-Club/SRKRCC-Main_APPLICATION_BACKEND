# Architecture: Dynamic Form Validation Engine

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
| 1 | **Structural** | `engine.py` | answer for an unknown / other-form / soft-deleted field; a field answered twice; an answer for a SECTION header |
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

## Strict vs. partial mode

| Mode | Used by | Behaviour |
|---|---|---|
| `strict` (default) | public `POST /api/forms/submissions/`, response edit | every failure is an error → `400` |
| `partial` | admin `manual-entry`, CSV `bulk-ingest`, backup `form_importer` | structural / type / option-membership failures stay errors; `REQUIRED` and constraint failures are **downgraded to warnings** so partial legacy data can still be stored. `?force=true` on manual-entry stores rows with hard errors too (audit-logged). |

## Conditional-logic engine

Canonical `conditional_logic` shape (stored on the dependent field):

```jsonc
{
  "logic": "AND" | "OR",
  "rules": [
    { "field": 12, "operator": "equals", "value": "Yes" },
    { "logic": "OR", "rules": [ ... ] }          // nested groups allowed
  ],
  "action": "show" | "hide" | "require" | "optional"   // default "show"
}
```

- **Operators** (`schema.CONDITIONAL_OPERATORS`): `equals`, `not_equals`, `gt`,
  `gte`, `lt`, `lte`, `between`, `not_between`, `contains`, `not_contains`,
  `starts_with`, `ends_with`, `matches_regex`, `not_matches_regex`, `is_empty`,
  `is_not_empty`, `selected`, `not_selected`, `includes`, `not_includes`,
  `includes_any`, `includes_all`, `before`, `after`, `on`, `before_or_equal`,
  `after_or_equal`, `date_between`.
- **Enforced actions**: `show`, `hide`, `require`, `optional`.
- **Deferred actions** (accepted + stored, not yet enforced — no frontend flow):
  `skip`, `skip_to_section`, `end_form`, `set_value`, `restrict_options`,
  `display_message`.
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

**Soft (warn):** no required fields; the `{"if": "parent"}` placeholder;
unreachable conditions; duplicate display order.

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
