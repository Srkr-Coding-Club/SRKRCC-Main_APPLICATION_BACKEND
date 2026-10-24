"""
dmc/adapters/forms.py
---------------------
DMC Adapters for Dynamic Form Responses.

Two adapters:
  FormsAllAdapter      → dataset "forms_all": Cross-form view with COMMON columns only.
  FormIndividualAdapter → dataset "form_<id>": Full schema for a specific form with dynamic questions.

Performance strategy for large datasets:
  Step 1: Query and paginate Response IDs at DB level.
  Step 2: Prefetch Answer+Field relations ONLY for the paginated slice.
  Step 3: Build answer maps in Python.
  Step 4: Normalize to canonical records.
"""

from __future__ import annotations
import json
from typing import Any, Generator

from django.db.models import Q

from apps.core.dmc.adapters.base import BaseDatasetAdapter
from apps.core.dmc.contracts import (
    CanonicalValue,
    ColumnDefinition,
    FilterClause,
    FilterDefinition,
    FilterOption,
    QueryRequest,
    QueryResult,
)
from apps.core.dmc.filter_engine import (
    Spec, apply_queryset, choices, distinct_values, person_specs, specs_by_key, to_definitions,
)
from apps.forms.models import Form, FormField, FormStatus, Response, Answer

# ---------------------------------------------------------------------------
# Common columns shared across all form views
# ---------------------------------------------------------------------------

COMMON_COLUMNS: list[ColumnDefinition] = [
    ColumnDefinition(key="id",              label="Response ID",    type="number",   category="meta",   sortable=True,  filterable=False, visible_by_default=False, renderer="text",  source="forms.Response.id"),
    ColumnDefinition(key="form_title",      label="Form",           type="text",     category="common", sortable=True,  filterable=True,  visible_by_default=True,  renderer="text",  source="forms.Form.title"),
    ColumnDefinition(key="name",            label="Full Name",      type="text",     category="common", sortable=False, filterable=False, visible_by_default=True,  renderer="text",  source="forms.Response.user.first_name+last_name"),
    ColumnDefinition(key="email",           label="Email",          type="email",    category="common", sortable=False, filterable=False, visible_by_default=True,  renderer="email", source="forms.Response.user.email"),
    ColumnDefinition(key="roll_number",     label="Roll Number",    type="text",     category="academic",sortable=False,filterable=False, visible_by_default=False, renderer="text",  source="forms.Response.user.roll_number"),
    ColumnDefinition(key="is_manual_entry", label="Manual Entry",   type="boolean",  category="meta",   sortable=True,  filterable=True,  visible_by_default=False, renderer="boolean", source="forms.Response.is_manual_entry"),
    ColumnDefinition(key="submitted_at",    label="Submitted At",   type="datetime", category="meta",   sortable=True,  filterable=False, visible_by_default=True,  renderer="date",  source="forms.Response.submitted_at"),
]

FORM_STATUS_COLUMN = ColumnDefinition(
    key="form_status", label="Form Status", type="badge", category="common", sortable=False, filterable=True,
    visible_by_default=True, renderer="badge", source="forms.Form.status",
    description="Whether the form is open (published), closed, draft, scheduled or archived.",
)

# "Published" is what admins mean by an open form.
FORM_STATUS_LABELS = {"PUBLISHED": "Open (Published)"}


def _form_options():
    return [FilterOption(title, str(pk)) for pk, title in Form.objects.order_by("title").values_list("pk", "title")]


_RESPONSE_COMMON = [
    *person_specs("user__"),
    Spec("is_manual_entry", "Entry Type", "boolean", ("is_manual_entry",)),
    Spec("submitted_at", "Submitted At", "date", ("submitted_at",)),
]

FORMS_ALL_SPECS = specs_by_key(
    Spec("form_title", "Form", "select", ("form_id",), value_type="int", options=_form_options),
    Spec("form_status", "Form Status", "select", ("form__status",), options=lambda: choices(FormStatus, FORM_STATUS_LABELS)),
    Spec("form_category", "Form Category", "select", ("form__category",), options=lambda: distinct_values(Form.objects.all(), "category")),
    *_RESPONSE_COMMON,
)

SINGLE_FORM_SPECS = specs_by_key(*_RESPONSE_COMMON)

ALLOWED_SORT_FIELDS_FORMS = {"id", "submitted_at", "form_title", "is_manual_entry"}


# ---------------------------------------------------------------------------
# FormsAllAdapter - cross-form bounded common view
# ---------------------------------------------------------------------------

class FormsAllAdapter(BaseDatasetAdapter):
    """
    Exposes all form responses with common columns only.
    Does NOT generate a pivot of every dynamic question across all forms
    to avoid unbounded column explosion.
    """

    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        columns = list(COMMON_COLUMNS)
        columns.insert(2, FORM_STATUS_COLUMN)
        return columns, to_definitions(FORMS_ALL_SPECS)

    def filter_specs(self):
        return FORMS_ALL_SPECS

    def _apply_filters(self, qs, filters: list[FilterClause]):
        return apply_queryset(qs, filters, FORMS_ALL_SPECS)

    def query(self, query_req: QueryRequest, user: Any) -> QueryResult:
        qs = Response.objects.select_related("form", "user").prefetch_related("answers__field").filter(is_test_submission=False)

        if query_req.search:
            q = query_req.search.strip()
            qs = qs.filter(
                Q(user__email__icontains=q) |
                Q(user__first_name__icontains=q) |
                Q(user__last_name__icontains=q) |
                Q(form__title__icontains=q)
            ).distinct()

        qs = self._apply_filters(qs, query_req.filters)

        sort_field = query_req.sort.field if query_req.sort.field in ALLOWED_SORT_FIELDS_FORMS else "submitted_at"
        sort_db = "form__title" if sort_field == "form_title" else sort_field
        prefix = "-" if query_req.sort.direction == "desc" else ""
        qs = qs.order_by(f"{prefix}{sort_db}")

        total = qs.count()
        offset = (query_req.page - 1) * query_req.page_size
        page_responses = list(qs[offset: offset + query_req.page_size])

        records = [self._normalize_common(r) for r in page_responses]
        return QueryResult(records=records, total=total, page=query_req.page, page_size=query_req.page_size, dataset_id="forms_all")

    def get_record(self, record_id: str, user: Any) -> dict[str, CanonicalValue] | None:
        try:
            r = Response.objects.select_related("form", "user").prefetch_related("answers__field").get(pk=record_id, is_test_submission=False)
        except (Response.DoesNotExist, ValueError):
            return None
        record = self._normalize_common(r)
        # For detail view, include all answers as extra keys
        for answer in r.answers.all():
            key = f"q_{answer.field_id}"
            record[key] = self._val(answer.value, "text", f"forms.Answer[field_id={answer.field_id}]")
        return record

    def stream_records(self, query_req: QueryRequest, user: Any, selected_ids: list[str] | None = None) -> Generator[dict[str, CanonicalValue], None, None]:
        qs = Response.objects.select_related("form", "user").prefetch_related("answers__field").filter(is_test_submission=False)
        if selected_ids is not None:
            qs = qs.filter(pk__in=selected_ids)
        else:
            if query_req.search:
                q = query_req.search.strip()
                qs = qs.filter(Q(user__email__icontains=q) | Q(user__first_name__icontains=q) | Q(form__title__icontains=q)).distinct()
            qs = self._apply_filters(qs, query_req.filters)
        for r in qs.iterator(chunk_size=500):
            yield self._normalize_common(r)

    def _resolve_identity(self, r: Response) -> tuple[str | None, str | None]:
        """
        Resolves (name, email) with the same 3-tier fallback as
        ResponseDetailSerializer.get_user/get_user_name/get_user_email
        (apps/forms/serializers.py), so manual-entry and CSV-imported
        responses - which have no `r.user` - don't render blank:
          1. Real r.user.
          2. Manual entry created by an admin (r.created_by_admin).
          3. Search r.answers for a name/email-shaped field.
        """
        if r.user:
            name = f"{r.user.first_name} {r.user.last_name}".strip() or r.user.username or r.user.email
            return name, r.user.email
        if r.is_manual_entry and r.created_by_admin:
            return f"Admin: {r.created_by_admin.email}", r.created_by_admin.email

        name = None
        email = None
        for ans in r.answers.all():
            if not ans.field or not ans.value or not str(ans.value).strip():
                continue
            label = ans.field.label.lower()
            if name is None and any(k in label for k in ("name", "student", "candidate", "applicant")):
                name = str(ans.value).strip()
            if email is None and (ans.field.type == "EMAIL" or "email" in label):
                email = str(ans.value).strip()
        return name or "Anonymous Student", email or "offline@srkr.ac.in"

    def _resolve_roll_number(self, r: Response) -> str | None:
        """Same fallback tiers as _resolve_identity, applied to roll number."""
        if r.user:
            return r.user.roll_number
        if r.is_manual_entry and r.created_by_admin:
            return r.created_by_admin.roll_number
        for ans in r.answers.all():
            if not ans.field or not ans.value or not str(ans.value).strip():
                continue
            if "roll" in ans.field.label.lower():
                return str(ans.value).strip()
        return None

    def _normalize_common(self, r: Response) -> dict[str, CanonicalValue]:
        name, email = self._resolve_identity(r)
        roll_number = self._resolve_roll_number(r)
        return {
            "id":              self._val(str(r.pk), "number", "forms.Response.id"),
            "form_title":      self._val(r.form.title, "text", "forms.Form.title"),
            "form_status":     self._val(r.form.get_status_display(), "badge", "forms.Form.status"),
            "name":            self._val(name, "text", "forms.Response.user.first_name+last_name"),
            "email":           self._val(email, "email", "forms.Response.user.email"),
            "roll_number":     self._val(roll_number, "text", "forms.Response.user.roll_number"),
            "is_manual_entry": self._val(r.is_manual_entry, "boolean", "forms.Response.is_manual_entry"),
            "submitted_at":    self._val(r.submitted_at.isoformat() if r.submitted_at else None, "datetime", "forms.Response.submitted_at"),
        }


# ---------------------------------------------------------------------------
# FormIndividualAdapter - full dynamic schema for a specific form
# ---------------------------------------------------------------------------

def _answer_q(field_id: int):
    """
    Filter on what respondents answered for one question.

    Answers are stored as JSON, so a text answer reads as `"Hyderabad"` (quotes
    included). Matching on that JSON text is what makes "equals" and "starts with"
    work; a plain iexact/istartswith would never see the leading quote.
    """
    def build(op: str, value: str | None) -> Q:
        has_answer = Q(answers__field_id=field_id)
        if op == "empty":
            return ~(has_answer & Q(answers__value__isnull=False))
        if op == "not_empty":
            return has_answer & Q(answers__value__isnull=False)
        quoted = json.dumps(value, ensure_ascii=False)
        needle = {"contains": value, "starts_with": quoted[:-1], "eq": quoted, "neq": quoted}[op]
        match = has_answer & Q(answers__value__icontains=needle)
        return ~match if op == "neq" else match
    return build


class FormIndividualAdapter(BaseDatasetAdapter):
    """
    Exposes all responses for a single Form including all dynamic question answers.
    Uses 2-step pagination to avoid unbounded memory use:
      Step 1: Get page of Response IDs.
      Step 2: Prefetch Answer+Field for only those IDs.
    """

    def __init__(self, form_id: int):
        self._form_id = form_id
        self._form: Form | None = None
        self._fields: list[FormField] = []

    def _ensure_form(self) -> bool:
        if self._form is None:
            try:
                self._form = Form.objects.get(pk=self._form_id)
                self._fields = list(FormField.all_objects.filter(form=self._form, is_deleted=False).exclude(type__in=["SECTION", "PAGE_BREAK"]).order_by("order"))
            except Form.DoesNotExist:
                return False
        return True

    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        if not self._ensure_form():
            return [], []
        columns = list(COMMON_COLUMNS)
        for ff in self._fields:
            columns.append(ColumnDefinition(
                key=f"q_{ff.id}",
                label=ff.label,
                type="text",
                category="form_questions",
                source=f"forms.Answer[field_id={ff.id}]",
                sortable=False,
                filterable=False,
                visible_by_default=True,
                renderer="text",
                description=f"Form question: {ff.label} ({ff.type})",
            ))
        return columns, to_definitions(self.filter_specs())

    def filter_specs(self):
        specs = dict(SINGLE_FORM_SPECS)
        if self._ensure_form():
            for ff in self._fields:
                specs[f"q_{ff.id}"] = Spec(f"q_{ff.id}", ff.label, "text", custom=_answer_q(ff.id))
        return specs

    def _apply_filters(self, qs, filters: list[FilterClause]):
        return apply_queryset(qs, filters, self.filter_specs())

    def query(self, query_req: QueryRequest, user: Any) -> QueryResult:
        if not self._ensure_form():
            return QueryResult(records=[], total=0, page=1, page_size=query_req.page_size, dataset_id=f"form_{self._form_id}")

        qs = Response.objects.filter(form=self._form, is_test_submission=False).select_related("user")

        if query_req.search:
            q = query_req.search.strip()
            qs = qs.filter(
                Q(user__email__icontains=q) | Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q) |
                Q(answers__value__icontains=q)
            ).distinct()

        qs = self._apply_filters(qs, query_req.filters)

        sort_field = "submitted_at" if query_req.sort.field not in {"id", "submitted_at"} else query_req.sort.field
        prefix = "-" if query_req.sort.direction == "desc" else ""
        # pk breaks ties so OFFSET pages never overlap or skip rows that share a timestamp.
        ordering = (f"{prefix}{sort_field}", f"{prefix}pk")
        qs = qs.order_by(*ordering)

        total = qs.count()
        offset = (query_req.page - 1) * query_req.page_size
        page_ids = list(qs.values_list("pk", flat=True)[offset: offset + query_req.page_size])

        # Step 2: Fetch with answers only for this page slice
        page_responses = list(
            Response.objects.filter(pk__in=page_ids)
            .select_related("user", "form")
            .prefetch_related("answers__field")
            .order_by(*ordering)
        )

        field_set = {ff.id for ff in self._fields}
        records = [self._normalize_full(r, field_set) for r in page_responses]
        return QueryResult(records=records, total=total, page=query_req.page, page_size=query_req.page_size, dataset_id=f"form_{self._form_id}")

    def get_record(self, record_id: str, user: Any) -> dict[str, CanonicalValue] | None:
        self._ensure_form()
        try:
            r = Response.objects.select_related("user", "form").prefetch_related("answers__field").get(pk=record_id, form=self._form, is_test_submission=False)
        except (Response.DoesNotExist, ValueError):
            return None
        field_set = {ff.id for ff in self._fields}
        return self._normalize_full(r, field_set)

    def stream_records(self, query_req: QueryRequest, user: Any, selected_ids: list[str] | None = None) -> Generator[dict[str, CanonicalValue], None, None]:
        self._ensure_form()
        qs = Response.objects.filter(form=self._form, is_test_submission=False).select_related("user")
        if selected_ids is not None:
            qs = qs.filter(pk__in=selected_ids)
        else:
            if query_req.search:
                q = query_req.search.strip()
                qs = qs.filter(
                    Q(user__email__icontains=q) | Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q) |
                    Q(answers__value__icontains=q)
                ).distinct()
            qs = self._apply_filters(qs, query_req.filters)
        field_set = {ff.id for ff in self._fields}

        # Stream in chunks of 200 responses; for each chunk prefetch answers.
        # OFFSET chunking is only safe over a deterministic order, so pk is pinned.
        qs = qs.order_by("pk")
        CHUNK = 200
        offset = 0
        while True:
            chunk_ids = list(qs.values_list("pk", flat=True)[offset: offset + CHUNK])
            if not chunk_ids:
                break
            chunk = list(
                Response.objects.filter(pk__in=chunk_ids)
                .select_related("user", "form")
                .prefetch_related("answers__field")
                .order_by("pk")
            )
            for r in chunk:
                yield self._normalize_full(r, field_set)
            offset += CHUNK

    def _normalize_full(self, r: Response, field_ids: set[int]) -> dict[str, CanonicalValue]:
        record = FormsAllAdapter()._normalize_common(r)
        # Override form_title for this single-form adapter (always same)
        if self._form:
            record["form_title"] = self._val(self._form.title, "text", "forms.Form.title")

        # Build answer map for the response
        answer_map: dict[int, Any] = {a.field_id: a.value for a in r.answers.all()}

        for ff in self._fields:
            key = f"q_{ff.id}"
            if ff.id in answer_map:
                value = answer_map[ff.id]
                record[key] = self._val(value, "text", f"forms.Answer[field_id={ff.id}]")
            else:
                # Question exists in form but no answer from this respondent
                record[key] = CanonicalValue(state="empty", value=None, display_value="(empty)", type="text", source=f"forms.Answer[field_id={ff.id}]")
        return record
