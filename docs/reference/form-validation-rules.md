# Reference: Form Validation Rules & Conditional Operators

Every key below is enforced by the backend on **every** submission path
(`apps/forms/validation/`). Keys are **camelCase** and live in
`FormField.validation_rules` (a JSON object). RATING / LINEAR_SCALE ranges use the
`FormField.min_value` / `max_value` columns.

An unknown key for a field type, or a self-contradicting value
(`minLength > maxLength`, bad regex, …), is rejected at **publish** time by
`validate_form_definition` — see `docs/architecture/form-validation-engine.md`.

---

## `validation_rules` by field type

### TEXT / PARAGRAPH (short & long answer)

| Key | Type | Meaning |
|---|---|---|
| `minLength` / `maxLength` / `exactLength` | int | character-count bounds |
| `minWords` / `maxWords` | int | *(PARAGRAPH only)* word-count bounds |
| `pattern` | string (regex) | value must match (`re.search`) |
| `patternError` | string | custom message shown for **any** failed rule on this field |
| `format` | enum | `alpha` (letters + spaces), `alphabetic`, `alphanumeric`, `numeric`, `integer`, `decimal`, `email`, `phone`, `url`, `username`, `slug`, `date`, `time` |
| `allowedChars` / `disallowedChars` | string | whitelist / blacklist of characters (whitespace always allowed) |
| `startsWith` / `endsWith` | string | prefix / suffix requirement |
| `contains` / `notContains` | string | substring must / must not appear |

**Examples**

| Question | Rule | Valid | Invalid |
|---|---|---|---|
| Your name | `{"format": "alpha"}` | `Ashok`, `John Doe` | `Ashok123`, `12345`, `Ashok@123` |
| Employee ID | `{"format": "alphanumeric"}` | `EMP123`, `A1024` | `EMP@123`, `EMP-123` |
| Employee ID | `{"pattern": "^EMP\\d{3,6}$"}` | `EMP1024` | `EMP12`, `emp1024` |

### NUMBER

| Key | Type | Meaning |
|---|---|---|
| `minValue` / `maxValue` / `exactValue` | number | inclusive bounds / exact match |
| `gt` / `gte` / `lt` / `lte` | number | strict / non-strict comparison |
| `integerOnly` | bool | reject decimals |
| `positiveOnly` | bool | value must be `> 0` |
| `allowNegative` | bool | when `false`, reject `< 0` |
| `step` | number | value must be `minValue + n * step` |

### EMAIL

| Key | Type | Meaning |
|---|---|---|
| `allowedDomains` / `blockedDomains` | list / comma-string | domain allow / block list |
| `allowMultiple` | bool | accept several comma/semicolon-separated addresses |
| `normalizeCase` | bool | store the address lower-cased |
| `minLength` / `maxLength` | int | length bounds on the raw string |

### PHONE

| Key | Type | Meaning |
|---|---|---|
| `pattern` | string (regex) | full custom format; overrides the default check |
| `minDigits` / `maxDigits` | int | count of `0-9` characters |
| `numericOnly` | bool | only digits and `+ - ( ) space` allowed |

Default (no rule): `+?[0-9][0-9\s\-().]{5,}`.

### URL

| Key | Type | Meaning |
|---|---|---|
| `pattern` | string (regex) | custom format |
| `requireHttps` | bool | must start with `https://` |
| `allowedDomains` / `blockedDomains` | list | host allow / block list |

### DATE

| Key | Type | Meaning |
|---|---|---|
| `minDate` / `maxDate` | `"YYYY-MM-DD"` or `"today"` | inclusive bounds |
| `notBefore` / `notAfter` | same | aliases of min/max |
| `pastOnly` / `futureOnly` | bool | strictly before / after today |
| `allowToday` | bool | when `false`, reject today's date |

### TIME

| Key | Type | Meaning |
|---|---|---|
| `minTime` / `maxTime` | `"HH:MM"` | inclusive bounds |

### RADIO / DROPDOWN

Option membership is **always** enforced against `FormField.options`.

| Key | Type | Meaning |
|---|---|---|
| `allowOther` | bool | accept a value outside `options` |

### CHECKBOX

Value is a list; every element must be in `options` (unless `allowOther`);
duplicates are rejected.

| Key | Type | Meaning |
|---|---|---|
| `minSelected` / `maxSelected` / `exactSelected` | int | selection-count bounds |

### FILE / MULTI_FILE

File payloads are metadata only — `{"name": ..., "size": <bytes>}`. Validation is
by **name / extension / size / count**; there is no binary upload pipeline, so
true MIME sniffing is out of scope.

| Key | Type | Meaning |
|---|---|---|
| `allowedFileTypes` / `blockedFileTypes` | comma-string (`.pdf,.docx`) | extension allow / block list |
| `maxFileSizeMB` | number | per-file size ceiling (binary MB) |
| `minFileSizeKB` | number | per-file size floor |
| `minFiles` / `maxFiles` | int | *(MULTI_FILE)* file-count bounds |

### RATING / LINEAR_SCALE

Range comes from `FormField.min_value` / `max_value` (defaults `1` / `5`).

| Key | Type | Meaning |
|---|---|---|
| `integerOnly` | bool | default `true`; reject fractional values |
| `step` | number | allowed increment |

### MATRIX_RADIO / MATRIX_CHECKBOX

Value is an object `{ "<row label>": "<column>" }` (or `[...]` for checkbox).
Row keys must be in `FormField.rows`; cell values must be in `FormField.options`.

| Key | Type | Meaning |
|---|---|---|
| `requiredRows` | list | these row labels must be answered |
| `allRowsRequired` | bool | every row in `rows` must be answered |
| `minPerRow` / `maxPerRow` | int | *(MATRIX_CHECKBOX)* selections per row |

---

## Cross-field rules

Stored on the field that owns the comparison, as
`validation_rules.crossField` — a list of:

```jsonc
{ "op": "eq" | "ne" | "lt" | "lte" | "gt" | "gte" | "required_if",
  "field": <other FormField id>,
  "equals": "<value>",       // required_if only (omit -> "other field is non-empty")
  "message": "<custom>" }     // optional
```

| Use case | Rule on | Config |
|---|---|---|
| Confirm password | *Confirm* field | `{"op": "eq", "field": <password id>}` |
| Start ≤ End date | *End Date* field | `{"op": "gte", "field": <start id>}` |
| Min ≤ Max salary | *Max Salary* field | `{"op": "gte", "field": <min id>}` |
| Company required if employed | *Company* field | `{"op": "required_if", "field": <status id>, "equals": "Employed"}` |

Only evaluated when **both** fields are currently visible.

---

## Conditional-logic operators

Used in `FormField.conditional_logic.rules[].operator`.

| Group | Operators |
|---|---|
| Equality | `equals`, `not_equals` |
| Numeric | `gt`, `gte`, `lt`, `lte`, `between`, `not_between` |
| Text | `contains`, `not_contains`, `starts_with`, `ends_with`, `matches_regex`, `not_matches_regex`, `is_empty`, `is_not_empty` |
| Selection | `selected`, `not_selected`, `includes`, `not_includes`, `includes_any`, `includes_all` |
| Date | `before`, `after`, `on`, `before_or_equal`, `after_or_equal`, `date_between` |

Legacy aliases (`greater_than`→`gt`, `eq`→`equals`, `>=`→`gte`, …) are accepted
and normalized on save.

### Actions

`conditional_logic.action` — default `show`.

| Action | Enforced? | Effect |
|---|---|---|
| `show` | yes | field visible only while the condition is true |
| `hide` | yes | field hidden while the condition is true |
| `require` | yes | field becomes required while the condition is true |
| `optional` | yes | field becomes optional while the condition is true |
| `skip`, `skip_to_section`, `end_form`, `set_value`, `restrict_options`, `display_message` | **stored, not yet enforced** | reserved for the frontend flow rebuild |

### Multiple / nested conditions

```jsonc
{
  "logic": "OR",
  "rules": [
    { "logic": "AND", "rules": [
        { "field": 1, "operator": "equals", "value": "Yes" },
        { "field": 2, "operator": "gt",     "value": "18" }
    ]},
    { "field": 3, "operator": "equals", "value": "Admin" }
  ],
  "action": "show"
}
```
→ `(Q1 = Yes AND Q2 > 18) OR (Q3 = Admin)`.

---

## Error codes

Machine-readable `code` values on each `FieldError` are the constants in
`apps/forms/validation/codes.py` — e.g. `REQUIRED`, `INVALID_EMAIL`, `MIN_LENGTH`,
`OPTION_NOT_ALLOWED`, `MIN_SELECTIONS`, `DATE_TOO_EARLY`, `FILE_TOO_LARGE`,
`CROSS_FIELD_MISMATCH`, `UNKNOWN_FIELD`, `DELETED_FIELD`, `DUPLICATE_ANSWER`.
