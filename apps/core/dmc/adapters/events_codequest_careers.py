"""
dmc/adapters/events.py - Event Registrations adapter (via linked form responses)
dmc/adapters/codequest.py - CodeQuest submissions adapter
dmc/adapters/careers.py - Career applications & job listings adapters
"""

from __future__ import annotations
from typing import Any, Generator

from django.db.models import Q

from apps.core.dmc.adapters.base import BaseDatasetAdapter
from apps.core.dmc.contracts import CanonicalValue, ColumnDefinition, FilterDefinition, FilterOption, QueryRequest, QueryResult
from apps.events.models import Event
from apps.forms.models import Response
from apps.codequest.models import Submission as CQSubmission, Difficulty as CQDifficulty
from apps.career.models import JobListing, JobType

# ============================================================================
# Events - EventRegistrationsAdapter
# 1 row = 1 event registration (Response tied to an event's registration_form)
# ============================================================================

EVENT_COLS: list[ColumnDefinition] = [
    ColumnDefinition(key="id",           label="Response ID",   type="number",   category="meta",   sortable=True,  filterable=False, visible_by_default=False, renderer="text",  source="forms.Response.id"),
    ColumnDefinition(key="event_title",  label="Event",         type="text",     category="common", sortable=True,  filterable=False, visible_by_default=True,  renderer="text",  source="events.Event.title"),
    ColumnDefinition(key="name",         label="Full Name",     type="text",     category="common", sortable=False, filterable=False, visible_by_default=True,  renderer="text",  source="accounts.User.first_name+last_name"),
    ColumnDefinition(key="email",        label="Email",         type="email",    category="common", sortable=False, filterable=False, visible_by_default=True,  renderer="email", source="accounts.User.email"),
    ColumnDefinition(key="roll_number",  label="Roll Number",   type="text",     category="academic",sortable=False,filterable=False, visible_by_default=False, renderer="text",  source="accounts.User.roll_number"),
    ColumnDefinition(key="venue",        label="Venue",         type="text",     category="common", sortable=False, filterable=False, visible_by_default=True,  renderer="text",  source="events.Event.venue"),
    ColumnDefinition(key="event_date",   label="Event Date",    type="datetime", category="meta",   sortable=True,  filterable=False, visible_by_default=True,  renderer="date",  source="events.Event.start_time"),
    ColumnDefinition(key="submitted_at", label="Registered At", type="datetime", category="meta",   sortable=True,  filterable=False, visible_by_default=True,  renderer="date",  source="forms.Response.submitted_at"),
]


class EventRegistrationsAdapter(BaseDatasetAdapter):
    """1 row = 1 event registration response."""

    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        return list(EVENT_COLS), []

    def _base_qs(self):
        event_form_ids = Event.objects.exclude(registration_form=None).values_list("registration_form_id", flat=True)
        return (
            Response.objects.filter(form_id__in=event_form_ids, is_test_submission=False)
            .select_related("user", "form")
        )

    def _apply_filters(self, qs, filters: list[FilterClause]):
        for f in filters:
            field = f.field
            op = f.operator
            val = f.value
            if field == "event_title":
                if op == "contains": form_ids = Event.objects.filter(title__icontains=str(val)).values_list("registration_form_id", flat=True)
                elif op == "starts_with": form_ids = Event.objects.filter(title__istartswith=str(val)).values_list("registration_form_id", flat=True)
                elif op == "eq": form_ids = Event.objects.filter(title__iexact=str(val)).values_list("registration_form_id", flat=True)
                elif op == "neq": form_ids = Event.objects.exclude(title__iexact=str(val)).values_list("registration_form_id", flat=True)
                else: form_ids = Event.objects.filter(title__icontains=str(val)).values_list("registration_form_id", flat=True)
                qs = qs.filter(form_id__in=form_ids)
            elif field == "venue":
                if op == "contains": form_ids = Event.objects.filter(venue__icontains=str(val)).values_list("registration_form_id", flat=True)
                elif op == "starts_with": form_ids = Event.objects.filter(venue__istartswith=str(val)).values_list("registration_form_id", flat=True)
                elif op == "eq": form_ids = Event.objects.filter(venue__iexact=str(val)).values_list("registration_form_id", flat=True)
                elif op == "neq": form_ids = Event.objects.exclude(venue__iexact=str(val)).values_list("registration_form_id", flat=True)
                elif op == "empty": form_ids = Event.objects.filter(Q(venue__isnull=True) | Q(venue="")).values_list("registration_form_id", flat=True)
                elif op == "not_empty": form_ids = Event.objects.exclude(Q(venue__isnull=True) | Q(venue="")).values_list("registration_form_id", flat=True)
                else: form_ids = Event.objects.filter(venue__icontains=str(val)).values_list("registration_form_id", flat=True)
                qs = qs.filter(form_id__in=form_ids)
            elif field == "event_date":
                if op == "gte": form_ids = Event.objects.filter(start_time__gte=val).values_list("registration_form_id", flat=True)
                elif op == "lte": form_ids = Event.objects.filter(start_time__lte=val).values_list("registration_form_id", flat=True)
                elif op == "eq": form_ids = Event.objects.filter(start_time__date=val).values_list("registration_form_id", flat=True)
                else: form_ids = Event.objects.filter(start_time__gte=val).values_list("registration_form_id", flat=True)
                qs = qs.filter(form_id__in=form_ids)
            elif field == "name":
                if op == "contains": qs = qs.filter(Q(user__first_name__icontains=str(val)) | Q(user__last_name__icontains=str(val)))
                elif op == "starts_with": qs = qs.filter(Q(user__first_name__istartswith=str(val)) | Q(user__last_name__istartswith=str(val)))
                elif op == "eq": qs = qs.filter(Q(user__first_name__iexact=str(val)) | Q(user__last_name__iexact=str(val)))
            elif field == "email":
                if op == "contains": qs = qs.filter(user__email__icontains=str(val))
                elif op == "starts_with": qs = qs.filter(user__email__istartswith=str(val))
                elif op == "eq": qs = qs.filter(user__email__iexact=str(val))
            elif field == "roll_number":
                if op == "contains": qs = qs.filter(user__roll_number__icontains=str(val))
                elif op == "starts_with": qs = qs.filter(user__roll_number__istartswith=str(val))
                elif op == "eq": qs = qs.filter(user__roll_number__iexact=str(val))
            elif field == "submitted_at":
                if op == "gte": qs = qs.filter(submitted_at__gte=val)
                elif op == "lte": qs = qs.filter(submitted_at__lte=val)
                elif op == "eq": qs = qs.filter(submitted_at__date=val)
        return qs

    def query(self, query_req: QueryRequest, user: Any) -> QueryResult:
        qs = self._base_qs()
        if query_req.search:
            q = query_req.search.strip()
            matching_form_ids = Event.objects.filter(Q(title__icontains=q) | Q(venue__icontains=q)).values_list("registration_form_id", flat=True)
            qs = qs.filter(
                Q(user__email__icontains=q) |
                Q(user__first_name__icontains=q) |
                Q(user__last_name__icontains=q) |
                Q(form__title__icontains=q) |
                Q(form_id__in=matching_form_ids)
            ).distinct()

        qs = self._apply_filters(qs, query_req.filters)

        sort_field = "submitted_at" if query_req.sort.field not in {"id", "submitted_at"} else query_req.sort.field
        prefix = "-" if query_req.sort.direction == "desc" else ""
        qs = qs.order_by(f"{prefix}{sort_field}")

        total = qs.count()
        offset = (query_req.page - 1) * query_req.page_size
        page = list(qs[offset: offset + query_req.page_size])

        # Fetch event for each response's form
        event_map: dict[int, Event] = {
            e.registration_form_id: e
            for e in Event.objects.select_related().filter(registration_form_id__in=[r.form_id for r in page])
        }
        return QueryResult(records=[self._normalize(r, event_map) for r in page], total=total, page=query_req.page, page_size=query_req.page_size, dataset_id="event_registrations")

    def get_record(self, record_id: str, user: Any) -> dict[str, CanonicalValue] | None:
        try:
            r = Response.objects.select_related("user", "form").get(pk=record_id)
            event = Event.objects.filter(registration_form=r.form).first()
        except (Response.DoesNotExist, ValueError):
            return None
        return self._normalize(r, {r.form_id: event} if event else {})

    def stream_records(self, query_req: QueryRequest, user: Any, selected_ids: list[str] | None = None) -> Generator[dict[str, CanonicalValue], None, None]:
        qs = self._base_qs()
        if selected_ids:
            qs = qs.filter(pk__in=selected_ids)
        else:
            if query_req.search:
                q = query_req.search.strip()
                matching_form_ids = Event.objects.filter(Q(title__icontains=q) | Q(venue__icontains=q)).values_list("registration_form_id", flat=True)
                qs = qs.filter(
                    Q(user__email__icontains=q) |
                    Q(user__first_name__icontains=q) |
                    Q(user__last_name__icontains=q) |
                    Q(form__title__icontains=q) |
                    Q(form_id__in=matching_form_ids)
                ).distinct()
            qs = self._apply_filters(qs, query_req.filters)
        event_form_map = {e.registration_form_id: e for e in Event.objects.exclude(registration_form=None)}
        for r in qs.iterator(chunk_size=500):
            yield self._normalize(r, event_form_map)

    def _normalize(self, r: Response, event_map: dict) -> dict[str, CanonicalValue]:
        u = r.user
        event = event_map.get(r.form_id)
        name = f"{u.first_name} {u.last_name}".strip() if u else None
        return {
            "id":           self._val(str(r.pk),                                                           "number",   "forms.Response.id"),
            "event_title":  self._val(event.title if event else None,                                      "text",     "events.Event.title"),
            "name":         self._val(name,                                                                "text",     "accounts.User.first_name+last_name"),
            "email":        self._val(u.email if u else None,                                              "email",    "accounts.User.email"),
            "roll_number":  self._val(u.roll_number if u else None,                                        "text",     "accounts.User.roll_number"),
            "venue":        self._val(event.venue if event else None,                                      "text",     "events.Event.venue"),
            "event_date":   self._val(event.start_time.isoformat() if event and event.start_time else None,"datetime", "events.Event.start_time"),
            "submitted_at": self._val(r.submitted_at.isoformat() if r.submitted_at else None,             "datetime", "forms.Response.submitted_at"),
        }


# ============================================================================
# CodeQuest - CodequestSubmissionsAdapter
# 1 row = 1 codequest.Submission
# ============================================================================

CQ_COLS: list[ColumnDefinition] = [
    ColumnDefinition(key="id",            label="Sub ID",        type="number",   category="meta",      sortable=True,  filterable=False, visible_by_default=False, renderer="text",  source="codequest.Submission.id"),
    ColumnDefinition(key="name",          label="Full Name",     type="text",     category="common",    sortable=False, filterable=False, visible_by_default=True,  renderer="text",  source="accounts.User.first_name+last_name"),
    ColumnDefinition(key="email",         label="Email",         type="email",    category="common",    sortable=True,  filterable=False, visible_by_default=True,  renderer="email", source="accounts.User.email"),
    ColumnDefinition(key="problem_title", label="Problem",       type="text",     category="codequest", sortable=False, filterable=False, visible_by_default=True,  renderer="text",  source="codequest.Problem.title"),
    ColumnDefinition(key="difficulty",    label="Difficulty",    type="badge",    category="codequest", sortable=False, filterable=True,  visible_by_default=True,  renderer="badge", source="codequest.Problem.difficulty"),
    ColumnDefinition(key="language",      label="Language",      type="badge",    category="codequest", sortable=False, filterable=False, visible_by_default=True,  renderer="badge", source="codequest.Submission.language"),
    ColumnDefinition(key="is_correct",    label="Passed",        type="boolean",  category="codequest", sortable=True,  filterable=True,  visible_by_default=True,  renderer="boolean", source="codequest.Submission.is_correct"),
    ColumnDefinition(key="created_at",    label="Submitted At",  type="datetime", category="meta",      sortable=True,  filterable=False, visible_by_default=True,  renderer="date",  source="codequest.Submission.created_at"),
]


CQ_FILTERS: list[FilterDefinition] = [
    FilterDefinition(key="is_correct", label="Passed", type="boolean", operators=["eq"]),
    FilterDefinition(key="difficulty", label="Difficulty", type="select", operators=["eq"],
                      options=[FilterOption(label, value) for value, label in CQDifficulty.choices]),
]


class CodequestSubmissionsAdapter(BaseDatasetAdapter):

    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        return list(CQ_COLS), list(CQ_FILTERS)

    def _apply_filters(self, qs, filters: list[FilterClause]):
        for f in filters:
            field = f.field
            op = f.operator
            val = f.value
            if field == "is_correct":
                qs = qs.filter(is_correct=(str(val).lower() in ("true", "1", "yes")))
            elif field == "difficulty":
                qs = qs.filter(problem__difficulty=f.value)
            elif field == "language":
                if op == "eq": qs = qs.filter(language__iexact=str(val))
                elif op == "contains": qs = qs.filter(language__icontains=str(val))
            elif field == "problem_title":
                if op == "contains": qs = qs.filter(problem__title__icontains=str(val))
                elif op == "eq": qs = qs.filter(problem__title__iexact=str(val))
            elif field == "name":
                if op == "contains": qs = qs.filter(Q(user__first_name__icontains=str(val)) | Q(user__last_name__icontains=str(val)))
                elif op == "eq": qs = qs.filter(Q(user__first_name__iexact=str(val)) | Q(user__last_name__iexact=str(val)))
            elif field == "email":
                if op == "contains": qs = qs.filter(user__email__icontains=str(val))
                elif op == "eq": qs = qs.filter(user__email__iexact=str(val))
        return qs

    def query(self, query_req: QueryRequest, user: Any) -> QueryResult:
        qs = CQSubmission.objects.select_related("user", "problem")
        if query_req.search:
            q = query_req.search.strip()
            qs = qs.filter(Q(user__email__icontains=q) | Q(problem__title__icontains=q)).distinct()
        
        qs = self._apply_filters(qs, query_req.filters)

        sort_field = query_req.sort.field if query_req.sort.field in {"id", "is_correct", "created_at"} else "created_at"
        prefix = "-" if query_req.sort.direction == "desc" else ""
        qs = qs.order_by(f"{prefix}{sort_field}")
        total = qs.count()
        offset = (query_req.page - 1) * query_req.page_size
        page = list(qs[offset: offset + query_req.page_size])
        return QueryResult(records=[self._normalize(s) for s in page], total=total, page=query_req.page, page_size=query_req.page_size, dataset_id="codequest_submissions")

    def get_record(self, record_id: str, user: Any) -> dict[str, CanonicalValue] | None:
        try:
            s = CQSubmission.objects.select_related("user", "problem").get(pk=record_id)
        except (CQSubmission.DoesNotExist, ValueError):
            return None
        return self._normalize(s)

    def stream_records(self, query_req: QueryRequest, user: Any, selected_ids: list[str] | None = None) -> Generator[dict[str, CanonicalValue], None, None]:
        qs = CQSubmission.objects.select_related("user", "problem")
        if selected_ids:
            qs = qs.filter(pk__in=selected_ids)
        else:
            if query_req.search:
                q = query_req.search.strip()
                qs = qs.filter(Q(user__email__icontains=q) | Q(problem__title__icontains=q)).distinct()
            qs = self._apply_filters(qs, query_req.filters)
        for s in qs.iterator(chunk_size=500):
            yield self._normalize(s)

    def _normalize(self, s: CQSubmission) -> dict[str, CanonicalValue]:
        u = s.user
        return {
            "id":            self._val(str(s.pk),                                                "number",  "codequest.Submission.id"),
            "name":          self._val(f"{u.first_name} {u.last_name}".strip() if u else None,   "text",    "accounts.User.first_name+last_name"),
            "email":         self._val(u.email if u else None,                                   "email",   "accounts.User.email"),
            "problem_title": self._val(s.problem.title,                                          "text",    "codequest.Problem.title"),
            "difficulty":    self._val(s.problem.difficulty,                                     "badge",   "codequest.Problem.difficulty"),
            "language":      self._val(s.language,                                               "badge",   "codequest.Submission.language"),
            "is_correct":    self._val(s.is_correct,                                             "boolean", "codequest.Submission.is_correct"),
            "created_at":    self._val(s.created_at.isoformat() if s.created_at else None,       "datetime","codequest.Submission.created_at"),
        }


# ============================================================================
# Careers - CareerApplicationsAdapter & CareerJobsAdapter
# ============================================================================

CAREER_APP_COLS: list[ColumnDefinition] = [
    ColumnDefinition(key="id",           label="Response ID",  type="number",   category="meta",   sortable=True,  filterable=False, visible_by_default=False, renderer="text",  source="forms.Response.id"),
    ColumnDefinition(key="job_title",    label="Position",     type="text",     category="career", sortable=False, filterable=False, visible_by_default=True,  renderer="text",  source="career.JobListing.title"),
    ColumnDefinition(key="company",      label="Company",      type="text",     category="career", sortable=False, filterable=False, visible_by_default=True,  renderer="text",  source="career.JobListing.company_name"),
    ColumnDefinition(key="job_type",     label="Type",         type="badge",    category="career", sortable=False, filterable=True,  visible_by_default=True,  renderer="badge", source="career.JobListing.job_type"),
    ColumnDefinition(key="name",         label="Full Name",    type="text",     category="common", sortable=False, filterable=False, visible_by_default=True,  renderer="text",  source="accounts.User.first_name+last_name"),
    ColumnDefinition(key="email",        label="Email",        type="email",    category="common", sortable=False, filterable=False, visible_by_default=True,  renderer="email", source="accounts.User.email"),
    ColumnDefinition(key="roll_number",  label="Roll Number",  type="text",     category="academic",sortable=False,filterable=False, visible_by_default=False, renderer="text",  source="accounts.User.roll_number"),
    ColumnDefinition(key="submitted_at", label="Applied At",   type="datetime", category="meta",   sortable=True,  filterable=False, visible_by_default=True,  renderer="date",  source="forms.Response.submitted_at"),
]

CAREER_JOB_COLS: list[ColumnDefinition] = [
    ColumnDefinition(key="id",           label="Job ID",       type="number",   category="meta",   sortable=True,  filterable=False, visible_by_default=False, renderer="text",  source="career.JobListing.id"),
    ColumnDefinition(key="job_title",    label="Title",         type="text",     category="career", sortable=True,  filterable=False, visible_by_default=True,  renderer="text",  source="career.JobListing.title"),
    ColumnDefinition(key="company",      label="Company",       type="text",     category="career", sortable=True,  filterable=False, visible_by_default=True,  renderer="text",  source="career.JobListing.company_name"),
    ColumnDefinition(key="job_type",     label="Type",          type="badge",    category="career", sortable=False, filterable=True,  visible_by_default=True,  renderer="badge", source="career.JobListing.job_type"),
    ColumnDefinition(key="location",     label="Location",      type="text",     category="career", sortable=False, filterable=False, visible_by_default=True,  renderer="text",  source="career.JobListing.location"),
    ColumnDefinition(key="deadline",     label="Deadline",      type="datetime", category="meta",   sortable=True,  filterable=False, visible_by_default=True,  renderer="date",  source="career.JobListing.deadline"),
    ColumnDefinition(key="created_at",   label="Posted At",     type="datetime", category="meta",   sortable=True,  filterable=False, visible_by_default=False, renderer="date",  source="career.JobListing.created_at"),
]


CAREER_FILTERS: list[FilterDefinition] = [
    FilterDefinition(key="job_type", label="Type", type="select", operators=["eq"],
                      options=[FilterOption(label, value) for value, label in JobType.choices]),
]


class CareerApplicationsAdapter(BaseDatasetAdapter):
    """1 row = 1 career application (Response linked to a JobListing's form)."""

    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        return list(CAREER_APP_COLS), list(CAREER_FILTERS)

    def _base_qs(self):
        job_form_ids = JobListing.objects.exclude(application_form=None).values_list("application_form_id", flat=True)
        return Response.objects.filter(form_id__in=job_form_ids, is_test_submission=False).select_related("user", "form")

    def _apply_filters(self, qs, filters: list[FilterClause]):
        for f in filters:
            field = f.field
            op = f.operator
            val = f.value
            if field == "job_type":
                job_form_ids = JobListing.objects.filter(job_type=val, application_form__isnull=False).values_list("application_form_id", flat=True)
                qs = qs.filter(form_id__in=job_form_ids)
            elif field == "job_title":
                job_form_ids = JobListing.objects.filter(title__icontains=str(val), application_form__isnull=False).values_list("application_form_id", flat=True)
                qs = qs.filter(form_id__in=job_form_ids)
            elif field == "company":
                job_form_ids = JobListing.objects.filter(company_name__icontains=str(val), application_form__isnull=False).values_list("application_form_id", flat=True)
                qs = qs.filter(form_id__in=job_form_ids)
            elif field == "name":
                if op == "contains": qs = qs.filter(Q(user__first_name__icontains=str(val)) | Q(user__last_name__icontains=str(val)))
                elif op == "eq": qs = qs.filter(Q(user__first_name__iexact=str(val)) | Q(user__last_name__iexact=str(val)))
            elif field == "email":
                if op == "contains": qs = qs.filter(user__email__icontains=str(val))
                elif op == "eq": qs = qs.filter(user__email__iexact=str(val))
        return qs

    def query(self, query_req: QueryRequest, user: Any) -> QueryResult:
        qs = self._base_qs()
        if query_req.search:
            q = query_req.search.strip()
            qs = qs.filter(Q(user__email__icontains=q) | Q(user__first_name__icontains=q)).distinct()
        
        qs = self._apply_filters(qs, query_req.filters)
        qs = qs.order_by("-submitted_at")
        total = qs.count()
        offset = (query_req.page - 1) * query_req.page_size
        page = list(qs[offset: offset + query_req.page_size])
        job_map = {j.application_form_id: j for j in JobListing.objects.filter(application_form_id__in=[r.form_id for r in page])}
        return QueryResult(records=[self._normalize(r, job_map) for r in page], total=total, page=query_req.page, page_size=query_req.page_size, dataset_id="career_applications")

    def get_record(self, record_id: str, user: Any) -> dict[str, CanonicalValue] | None:
        try:
            r = Response.objects.select_related("user", "form").get(pk=record_id)
            job = JobListing.objects.filter(application_form=r.form).first()
        except (Response.DoesNotExist, ValueError):
            return None
        return self._normalize(r, {r.form_id: job} if job else {})

    def stream_records(self, query_req: QueryRequest, user: Any, selected_ids: list[str] | None = None) -> Generator[dict[str, CanonicalValue], None, None]:
        qs = self._base_qs()
        if selected_ids:
            qs = qs.filter(pk__in=selected_ids)
        else:
            if query_req.search:
                q = query_req.search.strip()
                qs = qs.filter(Q(user__email__icontains=q) | Q(user__first_name__icontains=q)).distinct()
            qs = self._apply_filters(qs, query_req.filters)
        job_map = {j.application_form_id: j for j in JobListing.objects.exclude(application_form=None)}
        for r in qs.iterator(chunk_size=500):
            yield self._normalize(r, job_map)

    def _normalize(self, r: Response, job_map: dict) -> dict[str, CanonicalValue]:
        u = r.user
        job = job_map.get(r.form_id)
        name = f"{u.first_name} {u.last_name}".strip() if u else None
        return {
            "id":           self._val(str(r.pk),                                            "number",   "forms.Response.id"),
            "job_title":    self._val(job.title if job else None,                            "text",     "career.JobListing.title"),
            "company":      self._val(job.company_name if job else None,                     "text",     "career.JobListing.company_name"),
            "job_type":     self._val(job.job_type if job else None,                         "badge",    "career.JobListing.job_type"),
            "name":         self._val(name,                                                  "text",     "accounts.User.first_name+last_name"),
            "email":        self._val(u.email if u else None,                                "email",    "accounts.User.email"),
            "roll_number":  self._val(u.roll_number if u else None,                          "text",     "accounts.User.roll_number"),
            "submitted_at": self._val(r.submitted_at.isoformat() if r.submitted_at else None,"datetime", "forms.Response.submitted_at"),
        }


class CareerJobsAdapter(BaseDatasetAdapter):
    """1 row = 1 JobListing."""

    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        return list(CAREER_JOB_COLS), list(CAREER_FILTERS)

    def _apply_filters(self, qs, filters: list[FilterClause]):
        for f in filters:
            field = f.field
            op = f.operator
            val = f.value
            if field == "job_type":
                qs = qs.filter(job_type=val)
            elif field == "job_title":
                if op == "contains": qs = qs.filter(title__icontains=str(val))
                elif op == "eq": qs = qs.filter(title__iexact=str(val))
            elif field == "company":
                if op == "contains": qs = qs.filter(company_name__icontains=str(val))
                elif op == "eq": qs = qs.filter(company_name__iexact=str(val))
            elif field == "location":
                if op == "contains": qs = qs.filter(location__icontains=str(val))
                elif op == "eq": qs = qs.filter(location__iexact=str(val))
            elif field == "deadline":
                if op == "gte": qs = qs.filter(deadline__gte=val)
                elif op == "lte": qs = qs.filter(deadline__lte=val)
                elif op == "eq": qs = qs.filter(deadline__date=val)
        return qs

    def query(self, query_req: QueryRequest, user: Any) -> QueryResult:
        qs = JobListing.objects.all()
        if query_req.search:
            q = query_req.search.strip()
            qs = qs.filter(Q(title__icontains=q) | Q(company_name__icontains=q) | Q(location__icontains=q))
        
        qs = self._apply_filters(qs, query_req.filters)
        qs = qs.order_by("-created_at")
        total = qs.count()
        offset = (query_req.page - 1) * query_req.page_size
        page = list(qs[offset: offset + query_req.page_size])
        return QueryResult(records=[self._normalize(j) for j in page], total=total, page=query_req.page, page_size=query_req.page_size, dataset_id="career_jobs")

    def get_record(self, record_id: str, user: Any) -> dict[str, CanonicalValue] | None:
        try:
            j = JobListing.objects.get(pk=record_id)
        except (JobListing.DoesNotExist, ValueError):
            return None
        return self._normalize(j)

    def stream_records(self, query_req: QueryRequest, user: Any, selected_ids: list[str] | None = None) -> Generator[dict[str, CanonicalValue], None, None]:
        qs = JobListing.objects.all()
        if selected_ids:
            qs = qs.filter(pk__in=selected_ids)
        else:
            if query_req.search:
                q = query_req.search.strip()
                qs = qs.filter(Q(title__icontains=q) | Q(company_name__icontains=q) | Q(location__icontains=q))
            qs = self._apply_filters(qs, query_req.filters)
        for j in qs.iterator(chunk_size=500):
            yield self._normalize(j)

    def _normalize(self, j: JobListing) -> dict[str, CanonicalValue]:
        return {
            "id":         self._val(str(j.pk),                                              "number",   "career.JobListing.id"),
            "job_title":  self._val(j.title,                                                "text",     "career.JobListing.title"),
            "company":    self._val(j.company_name,                                         "text",     "career.JobListing.company_name"),
            "job_type":   self._val(j.job_type,                                             "badge",    "career.JobListing.job_type"),
            "location":   self._val(j.location,                                             "text",     "career.JobListing.location"),
            "deadline":   self._val(j.deadline.isoformat() if j.deadline else None,         "datetime", "career.JobListing.deadline"),
            "created_at": self._val(j.created_at.isoformat() if j.created_at else None,    "datetime", "career.JobListing.created_at"),
        }
