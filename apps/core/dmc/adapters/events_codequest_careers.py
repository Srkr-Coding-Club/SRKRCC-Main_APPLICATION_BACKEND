"""
dmc/adapters/events.py - Event Registrations adapter (via linked form responses)
dmc/adapters/codequest.py - CodeQuest submissions adapter
dmc/adapters/careers.py - Career applications & job listings adapters
"""

from __future__ import annotations
from typing import Any, Generator

from django.db.models import Q

from apps.core.dmc.adapters.base import BaseDatasetAdapter
from apps.core.dmc.contracts import CanonicalValue, ColumnDefinition, FilterClause, FilterDefinition, FilterOption, QueryRequest, QueryResult
from django.utils import timezone

from apps.core.dmc.filter_engine import (
    Spec, apply_queryset, choices, distinct_values, person_specs, specs_by_key, to_definitions,
)
from apps.events.models import Event, EventStatus
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


LIVE_LABELS = {"LIVE": "Open (Live)"}

EVENT_STATUS_COLUMN = ColumnDefinition(
    key="event_status", label="Event Status", type="badge", category="common", sortable=False, filterable=True,
    visible_by_default=True, renderer="badge", source="events.Event.status",
    description="Live (open for registration) or Closed.",
)


def _on_event(qs, q):
    """Apply a condition on the Event behind each registration."""
    form_ids = Event.objects.exclude(registration_form=None).filter(q).values("registration_form_id")
    return qs.filter(form_id__in=form_ids)


def _event_timing(op, value):
    now = timezone.now()
    return Q(start_time__gt=now) if value == "upcoming" else Q(start_time__lte=now)


def _event_options():
    return [FilterOption(t, str(pk)) for pk, t in Event.objects.exclude(registration_form=None).order_by("title").values_list("pk", "title")]


EVENT_SPECS = specs_by_key(
    Spec("event_title", "Event", "select", ("pk",), value_type="int", options=_event_options, wrap=_on_event),
    Spec("event_status", "Event Status", "select", ("status",), options=lambda: choices(EventStatus, LIVE_LABELS), wrap=_on_event),
    Spec("event_category", "Event Category", "select", ("category",), options=lambda: distinct_values(Event.objects.all(), "category"), wrap=_on_event),
    Spec("event_timing", "Upcoming / Past", "select", options=[FilterOption("Upcoming", "upcoming"), FilterOption("Past", "past")],
         operators=("eq",), custom=_event_timing, wrap=_on_event),
    Spec("venue", "Venue", "text", ("venue",), wrap=_on_event),
    Spec("event_date", "Event Date", "date", ("start_time",), wrap=_on_event),
    *person_specs("user__"),
    Spec("submitted_at", "Registered At", "date", ("submitted_at",)),
)


class EventRegistrationsAdapter(BaseDatasetAdapter):
    """1 row = 1 event registration response."""

    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        columns = list(EVENT_COLS)
        columns.insert(2, EVENT_STATUS_COLUMN)
        return columns, to_definitions(EVENT_SPECS)

    def _base_qs(self):
        event_form_ids = Event.objects.exclude(registration_form=None).values_list("registration_form_id", flat=True)
        return (
            Response.objects.filter(form_id__in=event_form_ids, is_test_submission=False)
            .select_related("user", "form")
        )

    def filter_specs(self):
        return EVENT_SPECS

    def _apply_filters(self, qs, filters: list[FilterClause]):
        return apply_queryset(qs, filters, EVENT_SPECS)

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
            "event_status": self._val(event.get_status_display() if event else None,                       "badge",    "events.Event.status"),
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


CQ_SPECS = specs_by_key(
    *person_specs("user__")[:2],
    Spec("problem_title", "Problem", "text", ("problem__title",)),
    Spec("difficulty", "Difficulty", "select", ("problem__difficulty",), options=lambda: choices(CQDifficulty)),
    Spec("language", "Language", "select", ("language",), options=lambda: distinct_values(CQSubmission.objects.all(), "language")),
    Spec("is_correct", "Passed", "boolean", ("is_correct",)),
    Spec("created_at", "Submitted At", "date", ("created_at",)),
)


class CodequestSubmissionsAdapter(BaseDatasetAdapter):

    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        return list(CQ_COLS), to_definitions(CQ_SPECS)

    def filter_specs(self):
        return CQ_SPECS

    def _apply_filters(self, qs, filters: list[FilterClause]):
        return apply_queryset(qs, filters, CQ_SPECS)

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


JOB_STATUS_COLUMN = ColumnDefinition(
    key="job_status", label="Job Status", type="badge", category="career", sortable=False, filterable=True,
    visible_by_default=True, renderer="badge", source="career.JobListing.deadline",
    description="Open until the application deadline, then Expired.",
)
JOB_STATUS_OPTIONS = [FilterOption("Open", "open"), FilterOption("Expired", "expired")]


def _job_status(op, value):
    now = timezone.now()
    return Q(deadline__gte=now) if value == "open" else Q(deadline__lt=now)


def _job_status_label(job) -> str | None:
    if job is None or job.deadline is None:
        return None
    return "Open" if job.deadline >= timezone.now() else "Expired"


def _on_job(qs, q):
    """Apply a condition on the JobListing behind each application."""
    form_ids = JobListing.objects.exclude(application_form=None).filter(q).values("application_form_id")
    return qs.filter(form_id__in=form_ids)


def _job_options():
    return [FilterOption(t, str(pk)) for pk, t in JobListing.objects.order_by("title").values_list("pk", "title")]


def _job_specs(wrap=None) -> list[Spec]:
    return [
        Spec("job_title", "Position", "text", ("title",), wrap=wrap),
        Spec("company", "Company", "text", ("company_name",), wrap=wrap),
        Spec("job_type", "Type", "select", ("job_type",), options=lambda: choices(JobType), wrap=wrap),
        Spec("job_status", "Job Status", "select", options=JOB_STATUS_OPTIONS, operators=("eq",), custom=_job_status, wrap=wrap),
    ]


CAREER_APP_SPECS = specs_by_key(
    Spec("job", "Job", "select", ("pk",), value_type="int", options=_job_options, wrap=_on_job),
    *_job_specs(_on_job),
    *person_specs("user__"),
    Spec("submitted_at", "Applied At", "date", ("submitted_at",)),
)

CAREER_JOB_SPECS = specs_by_key(
    *_job_specs(),
    Spec("location", "Location", "text", ("location",)),
    Spec("deadline", "Deadline", "date", ("deadline",)),
    Spec("created_at", "Posted At", "date", ("created_at",)),
)


class CareerApplicationsAdapter(BaseDatasetAdapter):
    """1 row = 1 career application (Response linked to a JobListing's form)."""

    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        columns = list(CAREER_APP_COLS)
        columns.insert(3, JOB_STATUS_COLUMN)
        return columns, to_definitions(CAREER_APP_SPECS)

    def _base_qs(self):
        job_form_ids = JobListing.objects.exclude(application_form=None).values_list("application_form_id", flat=True)
        return Response.objects.filter(form_id__in=job_form_ids, is_test_submission=False).select_related("user", "form")

    def filter_specs(self):
        return CAREER_APP_SPECS

    def _apply_filters(self, qs, filters: list[FilterClause]):
        return apply_queryset(qs, filters, CAREER_APP_SPECS)

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
            "job_status":   self._val(_job_status_label(job),                                "badge",    "career.JobListing.deadline"),
            "name":         self._val(name,                                                  "text",     "accounts.User.first_name+last_name"),
            "email":        self._val(u.email if u else None,                                "email",    "accounts.User.email"),
            "roll_number":  self._val(u.roll_number if u else None,                          "text",     "accounts.User.roll_number"),
            "submitted_at": self._val(r.submitted_at.isoformat() if r.submitted_at else None,"datetime", "forms.Response.submitted_at"),
        }


class CareerJobsAdapter(BaseDatasetAdapter):
    """1 row = 1 JobListing."""

    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        columns = list(CAREER_JOB_COLS)
        columns.insert(3, JOB_STATUS_COLUMN)
        return columns, to_definitions(CAREER_JOB_SPECS)

    def filter_specs(self):
        return CAREER_JOB_SPECS

    def _apply_filters(self, qs, filters: list[FilterClause]):
        return apply_queryset(qs, filters, CAREER_JOB_SPECS)

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
            "job_status": self._val(_job_status_label(j),                                    "badge",    "career.JobListing.deadline"),
            "location":   self._val(j.location,                                             "text",     "career.JobListing.location"),
            "deadline":   self._val(j.deadline.isoformat() if j.deadline else None,         "datetime", "career.JobListing.deadline"),
            "created_at": self._val(j.created_at.isoformat() if j.created_at else None,    "datetime", "career.JobListing.created_at"),
        }
