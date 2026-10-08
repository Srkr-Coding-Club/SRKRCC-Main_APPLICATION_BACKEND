"""
dmc/adapters/hackathons.py
--------------------------
Granular DMC adapters for the Hackathons module.

datasets:
  hackathon_participants  → 1 row = 1 team member (or team leader)
  hackathon_teams         → 1 row = 1 team + project submission summary
  hackathon_submissions   → 1 row = 1 project submission
  hackathon_round_entries → 1 row = 1 team in 1 round (shortlisting outcome)
"""

from __future__ import annotations
from typing import Any, Generator

from django.db.models import Q

from apps.core.dmc.adapters.base import BaseDatasetAdapter
from apps.core.dmc.contracts import CanonicalValue, ColumnDefinition, FilterDefinition, FilterOption, QueryRequest, QueryResult
from apps.hackathons.models import Hackathon, RoundEntry, Team, Submission


# ---------------------------------------------------------------------------
# Shared hackathon columns
# ---------------------------------------------------------------------------

def _hackathon_col(key, label, type="text", cat="hackathon", renderer="text", sortable=True, visible=True, source="", filterable=False):
    return ColumnDefinition(key=key, label=label, type=type, category=cat, sortable=sortable,
                             filterable=filterable, visible_by_default=visible, renderer=renderer, source=source)


def _hackathon_title_filter() -> FilterDefinition:
    """Dynamic select filter listing every hackathon that has at least one team."""
    options = [
        FilterOption(title, str(hid))
        for hid, title in Hackathon.objects.filter(teams__isnull=False).distinct().order_by("title").values_list("id", "title")
    ]
    return FilterDefinition(key="hackathon_title", label="Hackathon", type="select", operators=["eq"], options=options)

def _problem_id(team: Team) -> str | None:
    if team.problem_statement_id:
        return team.problem_statement.code
    return team.open_innovation_code if team.is_open_innovation else None


def _problem_title(team: Team) -> str | None:
    if team.problem_statement_id:
        return team.problem_statement.title
    return team.custom_problem_title or None


def _problem_domain(team: Team) -> str | None:
    if team.problem_statement_id:
        return team.problem_statement.domain or None
    return team.custom_problem_domain or None


PARTICIPANT_COLS: list[ColumnDefinition] = [
    _hackathon_col("id",               "ID",            "number",   "meta",       visible=False,  source="hackathons.Team.id+member"),
    _hackathon_col("name",             "Full Name",      "text",                   renderer="text",  source="accounts.User.first_name+last_name"),
    _hackathon_col("email",            "Email",          "email",    renderer="email", source="accounts.User.email"),
    _hackathon_col("hackathon_title",  "Hackathon",      "text",     renderer="text",  source="hackathons.Hackathon.title", filterable=True),
    _hackathon_col("team_name",        "Team",           "text",     renderer="text",  source="hackathons.Team.name"),
    _hackathon_col("team_role",        "Team Role",      "badge",    renderer="badge", source="derived"),
    _hackathon_col("roll_number",      "Roll Number",    "text",     "academic",   renderer="text", source="accounts.User.roll_number"),
    _hackathon_col("created_at",       "Registered At",  "datetime", "meta",       renderer="date",  source="hackathons.Team.created_at"),
]

TEAMS_COLS: list[ColumnDefinition] = [
    _hackathon_col("id",                "Team ID",           "number", "meta",     visible=False, source="hackathons.Team.id"),
    _hackathon_col("team_name",         "Team Name",         "text",               renderer="text", source="hackathons.Team.name"),
    _hackathon_col("hackathon_title",   "Hackathon",         "text",               renderer="text", source="hackathons.Hackathon.title", filterable=True),
    _hackathon_col("leader_name",       "Leader",            "text",               renderer="text", source="hackathons.Team.leader"),
    _hackathon_col("leader_email",      "Leader Email",      "email",              renderer="email", source="hackathons.Team.leader.email"),
    _hackathon_col("member_count",      "Members",           "number",             renderer="text", source="hackathons.Team.members.count"),
    _hackathon_col("problem_id",        "Problem ID",        "text",               renderer="text", source="hackathons.ProblemStatement.code"),
    _hackathon_col("problem_title",     "Problem Title",     "text",               renderer="text", source="hackathons.Team.problem"),
    _hackathon_col("problem_domain",    "Problem Domain",    "text",               renderer="text", source="hackathons.Team.problem"),
    _hackathon_col("open_innovation",   "Open Innovation",   "boolean",            renderer="boolean", source="hackathons.Team.is_open_innovation"),
    _hackathon_col("project_title",     "Project",           "text",               renderer="text", source="hackathons.Submission.project_title"),
    _hackathon_col("submission_score",  "Score",             "number",             renderer="text", source="hackathons.Submission.score"),
    _hackathon_col("created_at",        "Created At",        "datetime", "meta",   renderer="date", source="hackathons.Team.created_at"),
]

SUBMISSIONS_COLS: list[ColumnDefinition] = [
    _hackathon_col("id",              "Sub ID",       "number",  "meta",     visible=False, source="hackathons.Submission.id"),
    _hackathon_col("project_title",   "Project",      "text",               renderer="text",  source="hackathons.Submission.project_title"),
    _hackathon_col("team_name",       "Team",         "text",               renderer="text",  source="hackathons.Team.name"),
    _hackathon_col("hackathon_title", "Hackathon",    "text",               renderer="text",  source="hackathons.Hackathon.title", filterable=True),
    _hackathon_col("repo_url",        "Repo",         "url",                renderer="link",  source="hackathons.Submission.repo_url"),
    _hackathon_col("demo_url",        "Demo",         "url",                renderer="link",  source="hackathons.Submission.demo_url"),
    _hackathon_col("score",           "Score",        "number",             renderer="text",  source="hackathons.Submission.score"),
    _hackathon_col("created_at",      "Submitted At", "datetime", "meta",   renderer="date",  source="hackathons.Submission.created_at"),
]

ALLOWED_SORT_PARTICIPANT = {"created_at", "email"}
ALLOWED_SORT_TEAMS       = {"id", "team_name", "submission_score", "created_at"}
ALLOWED_SORT_SUBMISSIONS = {"id", "score", "created_at"}


# ---------------------------------------------------------------------------
# HackathonParticipantsAdapter
# ---------------------------------------------------------------------------

class HackathonParticipantsAdapter(BaseDatasetAdapter):
    """1 row = 1 participant (leader or member) in a hackathon team."""

    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        return list(PARTICIPANT_COLS), [_hackathon_title_filter()]

    def _apply_row_filters(self, rows: list[dict], filters: list[FilterClause]) -> list[dict]:
        for f in filters:
            field = f.field
            op = f.operator
            val = str(f.value).lower().strip()
            if field == "hackathon_title" and str(f.value).isdigit():
                continue
            if op == "eq":
                rows = [r for r in rows if str(r.get(field, "") or "").lower() == val]
            elif op == "neq":
                rows = [r for r in rows if str(r.get(field, "") or "").lower() != val]
            elif op == "contains":
                rows = [r for r in rows if val in str(r.get(field, "") or "").lower()]
            elif op == "starts_with":
                rows = [r for r in rows if str(r.get(field, "") or "").lower().startswith(val)]
            elif op == "empty":
                rows = [r for r in rows if not r.get(field)]
            elif op == "not_empty":
                rows = [r for r in rows if r.get(field)]
        return rows

    def query(self, query_req: QueryRequest, user: Any) -> QueryResult:
        teams = Team.objects.select_related("hackathon", "leader").prefetch_related("memberships__user")
        for f in query_req.filters:
            if f.field == "hackathon_title" and str(f.value).isdigit():
                teams = teams.filter(hackathon_id=int(f.value))
        rows = []
        for team in teams:
            rows.extend(self._expand_team(team))

        if query_req.search:
            q = query_req.search.lower()
            rows = [r for r in rows if q in (r.get("email", "").lower() + r.get("name", "").lower())]

        rows = self._apply_row_filters(rows, query_req.filters)

        sort_field = query_req.sort.field if query_req.sort.field in ALLOWED_SORT_PARTICIPANT else "created_at"
        reverse = query_req.sort.direction == "desc"
        rows.sort(key=lambda r: r.get(sort_field, "") or "", reverse=reverse)

        total = len(rows)
        offset = (query_req.page - 1) * query_req.page_size
        page_rows = rows[offset: offset + query_req.page_size]
        return QueryResult(records=[self._to_record(r) for r in page_rows], total=total, page=query_req.page, page_size=query_req.page_size, dataset_id="hackathon_participants")

    def get_record(self, record_id: str, user: Any) -> dict[str, CanonicalValue] | None:
        return None  # Composite rows don't have a single DB primary key to look up

    def stream_records(self, query_req: QueryRequest, user: Any, selected_ids: list[str] | None = None) -> Generator[dict[str, CanonicalValue], None, None]:
        teams = Team.objects.select_related("hackathon", "leader").prefetch_related("memberships__user")
        for f in query_req.filters:
            if f.field == "hackathon_title" and str(f.value).isdigit():
                teams = teams.filter(hackathon_id=int(f.value))
        rows = []
        for team in teams.iterator(chunk_size=100):
            rows.extend(self._expand_team(team))
        rows = self._apply_row_filters(rows, query_req.filters)
        for row in rows:
            if selected_ids is None or row["id"] in selected_ids:
                yield self._to_record(row)

    def _expand_team(self, team: Team) -> list[dict]:
        rows = []
        for ms in team.memberships.all():
            m = ms.user
            rows.append({"id": f"t{team.id}_u{m.id}", "name": f"{m.first_name} {m.last_name}".strip(), "email": m.email, "hackathon_title": team.hackathon.title, "team_name": team.name, "team_role": "Leader" if ms.role == "LEADER" else "Member", "roll_number": getattr(m, "roll_number", None), "created_at": team.created_at.isoformat() if team.created_at else None})
        return rows

    def _to_record(self, row: dict) -> dict[str, CanonicalValue]:
        return {
            "id":              self._val(row["id"],               "text",     "derived"),
            "name":            self._val(row["name"],             "text",     "accounts.User.first_name+last_name"),
            "email":           self._val(row["email"],            "email",    "accounts.User.email"),
            "hackathon_title": self._val(row["hackathon_title"],  "text",     "hackathons.Hackathon.title"),
            "team_name":       self._val(row["team_name"],        "text",     "hackathons.Team.name"),
            "team_role":       self._val(row["team_role"],        "badge",    "derived"),
            "roll_number":     self._val(row["roll_number"],      "text",     "accounts.User.roll_number"),
            "created_at":      self._val(row["created_at"],       "datetime", "hackathons.Team.created_at"),
        }


# ---------------------------------------------------------------------------
# HackathonTeamsAdapter
# ---------------------------------------------------------------------------

class HackathonTeamsAdapter(BaseDatasetAdapter):
    """1 row = 1 Hackathon team."""

    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        return list(TEAMS_COLS), [_hackathon_title_filter()]

    def _apply_filters(self, qs, filters: list[FilterClause]):
        for f in filters:
            field = f.field
            op = f.operator
            val = f.value
            if field == "hackathon_title":
                if str(val).isdigit():
                    qs = qs.filter(hackathon_id=int(val))
                elif op == "contains":
                    qs = qs.filter(hackathon__title__icontains=str(val))
                else:
                    qs = qs.filter(hackathon__title__iexact=str(val))
            elif field == "team_name":
                if op == "contains": qs = qs.filter(name__icontains=str(val))
                elif op == "eq": qs = qs.filter(name__iexact=str(val))
            elif field == "leader_name":
                if op == "contains": qs = qs.filter(Q(leader__first_name__icontains=str(val)) | Q(leader__last_name__icontains=str(val)))
                elif op == "eq": qs = qs.filter(Q(leader__first_name__iexact=str(val)) | Q(leader__last_name__iexact=str(val)))
            elif field == "leader_email":
                if op == "contains": qs = qs.filter(leader__email__icontains=str(val))
                elif op == "eq": qs = qs.filter(leader__email__iexact=str(val))
            elif field == "open_innovation":
                if op == "eq":
                    qs = qs.filter(is_open_innovation=(str(val).lower() in ("true", "1", "yes")))
            elif field in ("submission_score", "score"):
                if op in ("eq", "gte", "lte", "gt", "lt"):
                    try:
                        num = float(val)
                        if op == "eq": qs = qs.filter(submission__score=num)
                        elif op == "gte": qs = qs.filter(submission__score__gte=num)
                        elif op == "lte": qs = qs.filter(submission__score__lte=num)
                        elif op == "gt": qs = qs.filter(submission__score__gt=num)
                        elif op == "lt": qs = qs.filter(submission__score__lt=num)
                    except (ValueError, TypeError):
                        pass
        return qs

    def query(self, query_req: QueryRequest, user: Any) -> QueryResult:
        qs = Team.objects.select_related("hackathon", "leader", "problem_statement").prefetch_related("memberships", "submission")
        if query_req.search:
            q = query_req.search.strip()
            qs = qs.filter(Q(name__icontains=q) | Q(leader__email__icontains=q) | Q(hackathon__title__icontains=q))
        
        qs = self._apply_filters(qs, query_req.filters)

        sort_field = query_req.sort.field if query_req.sort.field in ALLOWED_SORT_TEAMS else "created_at"
        prefix = "-" if query_req.sort.direction == "desc" else ""
        qs = qs.order_by(f"{prefix}{sort_field}")

        total = qs.count()
        offset = (query_req.page - 1) * query_req.page_size
        page = list(qs[offset: offset + query_req.page_size])
        return QueryResult(records=[self._normalize(t) for t in page], total=total, page=query_req.page, page_size=query_req.page_size, dataset_id="hackathon_teams")

    def get_record(self, record_id: str, user: Any) -> dict[str, CanonicalValue] | None:
        try:
            t = Team.objects.select_related("hackathon", "leader", "problem_statement").prefetch_related("memberships", "submission").get(pk=record_id)
        except (Team.DoesNotExist, ValueError):
            return None
        return self._normalize(t)

    def stream_records(self, query_req: QueryRequest, user: Any, selected_ids: list[str] | None = None) -> Generator[dict[str, CanonicalValue], None, None]:
        qs = Team.objects.select_related("hackathon", "leader", "problem_statement").prefetch_related("memberships", "submission")
        if selected_ids:
            qs = qs.filter(pk__in=selected_ids)
        else:
            if query_req.search:
                q = query_req.search.strip()
                qs = qs.filter(Q(name__icontains=q) | Q(leader__email__icontains=q) | Q(hackathon__title__icontains=q))
            qs = self._apply_filters(qs, query_req.filters)
        for t in qs.iterator(chunk_size=200):
            yield self._normalize(t)

    def _normalize(self, t: Team) -> dict[str, CanonicalValue]:
        sub = getattr(t, "submission", None)
        leader = t.leader
        return {
            "id":               self._val(str(t.pk),                                     "number",   "hackathons.Team.id"),
            "team_name":        self._val(t.name,                                         "text",     "hackathons.Team.name"),
            "hackathon_title":  self._val(t.hackathon.title,                              "text",     "hackathons.Hackathon.title"),
            "leader_name":      self._val(f"{leader.first_name} {leader.last_name}".strip() if leader else None, "text", "hackathons.Team.leader"),
            "leader_email":     self._val(leader.email if leader else None,               "email",    "hackathons.Team.leader.email"),
            "member_count":     self._val(t.memberships.count(),                              "number",   "hackathons.Team.members.count"),
            "problem_id":       self._val(_problem_id(t),                                     "text",     "hackathons.ProblemStatement.code"),
            "problem_title":    self._val(_problem_title(t),                                  "text",     "hackathons.Team.problem"),
            "problem_domain":   self._val(_problem_domain(t),                                 "text",     "hackathons.Team.problem"),
            "open_innovation":  self._val(t.is_open_innovation,                               "boolean",  "hackathons.Team.is_open_innovation"),
            "project_title":    self._val(sub.project_title if sub else None,             "text",     "hackathons.Submission.project_title"),
            "submission_score": self._val(sub.score if sub else None,                     "number",   "hackathons.Submission.score"),
            "created_at":       self._val(t.created_at.isoformat() if t.created_at else None, "datetime", "hackathons.Team.created_at"),
        }


# ---------------------------------------------------------------------------
# HackathonSubmissionsAdapter
# ---------------------------------------------------------------------------

class HackathonSubmissionsAdapter(BaseDatasetAdapter):
    """1 row = 1 Project submission."""

    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        return list(SUBMISSIONS_COLS), [_hackathon_title_filter()]

    def _apply_filters(self, qs, filters: list[FilterClause]):
        for f in filters:
            field = f.field
            op = f.operator
            val = f.value
            if field == "hackathon_title":
                if str(val).isdigit():
                    qs = qs.filter(team__hackathon_id=int(val))
                elif op == "contains":
                    qs = qs.filter(team__hackathon__title__icontains=str(val))
                else:
                    qs = qs.filter(team__hackathon__title__iexact=str(val))
            elif field == "project_title":
                if op == "contains": qs = qs.filter(project_title__icontains=str(val))
                elif op == "eq": qs = qs.filter(project_title__iexact=str(val))
            elif field == "team_name":
                if op == "contains": qs = qs.filter(team__name__icontains=str(val))
                elif op == "eq": qs = qs.filter(team__name__iexact=str(val))
            elif field == "score":
                if op in ("eq", "gte", "lte", "gt", "lt"):
                    try:
                        num = float(val)
                        if op == "eq": qs = qs.filter(score=num)
                        elif op == "gte": qs = qs.filter(score__gte=num)
                        elif op == "lte": qs = qs.filter(score__lte=num)
                    except (ValueError, TypeError):
                        pass
        return qs

    def query(self, query_req: QueryRequest, user: Any) -> QueryResult:
        qs = Submission.objects.select_related("team__hackathon", "team__leader")
        if query_req.search:
            q = query_req.search.strip()
            qs = qs.filter(Q(project_title__icontains=q) | Q(team__name__icontains=q))
        
        qs = self._apply_filters(qs, query_req.filters)

        sort_field = query_req.sort.field if query_req.sort.field in ALLOWED_SORT_SUBMISSIONS else "created_at"
        prefix = "-" if query_req.sort.direction == "desc" else ""
        qs = qs.order_by(f"{prefix}{sort_field}")

        total = qs.count()
        offset = (query_req.page - 1) * query_req.page_size
        page = list(qs[offset: offset + query_req.page_size])
        return QueryResult(records=[self._normalize(s) for s in page], total=total, page=query_req.page, page_size=query_req.page_size, dataset_id="hackathon_submissions")

    def get_record(self, record_id: str, user: Any) -> dict[str, CanonicalValue] | None:
        try:
            s = Submission.objects.select_related("team__hackathon").get(pk=record_id)
        except (Submission.DoesNotExist, ValueError):
            return None
        return self._normalize(s)

    def stream_records(self, query_req: QueryRequest, user: Any, selected_ids: list[str] | None = None) -> Generator[dict[str, CanonicalValue], None, None]:
        qs = Submission.objects.select_related("team__hackathon", "team__leader")
        if selected_ids:
            qs = qs.filter(pk__in=selected_ids)
        else:
            if query_req.search:
                q = query_req.search.strip()
                qs = qs.filter(Q(project_title__icontains=q) | Q(team__name__icontains=q))
            qs = self._apply_filters(qs, query_req.filters)
        for s in qs.iterator(chunk_size=200):
            yield self._normalize(s)

    def _normalize(self, s: Submission) -> dict[str, CanonicalValue]:
        return {
            "id":              self._val(str(s.pk),             "number",   "hackathons.Submission.id"),
            "project_title":   self._val(s.project_title,       "text",     "hackathons.Submission.project_title"),
            "team_name":       self._val(s.team.name,           "text",     "hackathons.Team.name"),
            "hackathon_title": self._val(s.team.hackathon.title,"text",     "hackathons.Hackathon.title"),
            "repo_url":        self._val(s.repo_url,            "url",      "hackathons.Submission.repo_url"),
            "demo_url":        self._val(s.demo_url,            "url",      "hackathons.Submission.demo_url"),
            "score":           self._val(s.score,               "number",   "hackathons.Submission.score"),
            "created_at":      self._val(s.created_at.isoformat() if s.created_at else None, "datetime", "hackathons.Submission.created_at"),
        }


# ---------------------------------------------------------------------------
# HackathonRoundEntriesAdapter
# ---------------------------------------------------------------------------

ROUND_ENTRY_COLS: list[ColumnDefinition] = [
    _hackathon_col("id",                "Entry ID",          "number",   "meta",   visible=False, source="hackathons.RoundEntry.id"),
    _hackathon_col("hackathon_title",   "Hackathon",         "text",               renderer="text",    source="hackathons.Hackathon.title", filterable=True),
    _hackathon_col("round_order",       "Round #",           "number",             renderer="text",    source="hackathons.Round.order"),
    _hackathon_col("round_name",        "Round",             "text",               renderer="text",    source="hackathons.Round.name"),
    _hackathon_col("team_name",         "Team",              "text",               renderer="text",    source="hackathons.Team.name"),
    _hackathon_col("leader_email",      "Leader Email",      "email",              renderer="email",   source="hackathons.Team.leader.email"),
    _hackathon_col("problem_statement", "Problem Statement", "text",               renderer="text",    source="hackathons.ProblemStatement.code"),
    _hackathon_col("status",            "Result",            "badge",              renderer="badge",   source="hackathons.RoundEntry.status"),
    _hackathon_col("results_published", "Published",         "boolean",            renderer="boolean", source="hackathons.Round.results_published"),
    _hackathon_col("details_submitted", "Details Submitted", "boolean",            renderer="boolean", source="hackathons.RoundEntry.details_response"),
    _hackathon_col("feedback",          "Feedback",          "text",               renderer="text",    source="hackathons.RoundEntry.feedback", visible=False),
    _hackathon_col("decided_at",        "Decided At",        "datetime", "meta",   renderer="date",    source="hackathons.RoundEntry.decided_at"),
]

ALLOWED_SORT_ROUND_ENTRIES = {"id", "round_order", "team_name", "status", "decided_at"}
_ROUND_ENTRY_SORT_MAP = {
    "id": "id", "round_order": "round__order", "team_name": "team__name",
    "status": "status", "decided_at": "decided_at",
}


def _round_entry_hackathon_filter() -> FilterDefinition:
    options = [
        FilterOption(title, str(hid))
        for hid, title in Hackathon.objects.filter(rounds__isnull=False).distinct().order_by("title").values_list("id", "title")
    ]
    return FilterDefinition(key="hackathon_title", label="Hackathon", type="select", operators=["eq"], options=options)


class HackathonRoundEntriesAdapter(BaseDatasetAdapter):
    """1 row = 1 team in 1 hackathon round (shortlisting outcome)."""

    def _base_qs(self):
        return RoundEntry.objects.select_related("round__hackathon", "team__leader", "team__problem_statement")

    def _apply_filters(self, qs, filters: list[FilterClause]):
        for f in filters:
            field = f.field
            op = f.operator
            val = f.value
            if field == "hackathon_title":
                if str(val).isdigit():
                    qs = qs.filter(round__hackathon_id=int(val))
                elif op == "contains":
                    qs = qs.filter(round__hackathon__title__icontains=str(val))
                else:
                    qs = qs.filter(round__hackathon__title__iexact=str(val))
            elif field == "team_name":
                if op == "contains": qs = qs.filter(team__name__icontains=str(val))
                elif op == "starts_with": qs = qs.filter(team__name__istartswith=str(val))
                elif op == "eq": qs = qs.filter(team__name__iexact=str(val))
            elif field == "round_name":
                if op == "contains": qs = qs.filter(round__name__icontains=str(val))
                elif op == "eq": qs = qs.filter(round__name__iexact=str(val))
            elif field == "leader_email":
                if op == "contains": qs = qs.filter(team__leader__email__icontains=str(val))
                elif op == "eq": qs = qs.filter(team__leader__email__iexact=str(val))
            elif field == "status":
                if op == "eq": qs = qs.filter(status__iexact=str(val))
                elif op == "neq": qs = qs.exclude(status__iexact=str(val))
            elif field == "results_published":
                if op == "eq": qs = qs.filter(round__results_published=(str(val).lower() in ("true", "1", "yes")))
            elif field == "details_submitted":
                if op == "eq":
                    is_true = str(val).lower() in ("true", "1", "yes")
                    qs = qs.filter(details_response__isnull=not is_true)
            elif field == "decided_at":
                if op == "gte": qs = qs.filter(decided_at__gte=val)
                elif op == "lte": qs = qs.filter(decided_at__lte=val)
        return qs

    def _filtered(self, query_req: QueryRequest):
        qs = self._base_qs()
        if query_req.search:
            q = query_req.search.strip()
            qs = qs.filter(Q(team__name__icontains=q) | Q(team__leader__email__icontains=q) | Q(round__name__icontains=q) | Q(round__hackathon__title__icontains=q))
        qs = self._apply_filters(qs, query_req.filters)
        return qs

    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        return list(ROUND_ENTRY_COLS), [_round_entry_hackathon_filter()]

    def query(self, query_req: QueryRequest, user: Any) -> QueryResult:
        sort_field = query_req.sort.field if query_req.sort.field in ALLOWED_SORT_ROUND_ENTRIES else "round_order"
        prefix = "-" if query_req.sort.direction == "desc" else ""
        qs = self._filtered(query_req).order_by(f"{prefix}{_ROUND_ENTRY_SORT_MAP[sort_field]}", "team__name")

        total = qs.count()
        offset = (query_req.page - 1) * query_req.page_size
        page = list(qs[offset: offset + query_req.page_size])
        return QueryResult(records=[self._normalize(e) for e in page], total=total, page=query_req.page, page_size=query_req.page_size, dataset_id="hackathon_round_entries")

    def get_record(self, record_id: str, user: Any) -> dict[str, CanonicalValue] | None:
        try:
            return self._normalize(self._base_qs().get(pk=record_id))
        except (RoundEntry.DoesNotExist, ValueError):
            return None

    def stream_records(self, query_req: QueryRequest, user: Any, selected_ids: list[str] | None = None) -> Generator[dict[str, CanonicalValue], None, None]:
        qs = self._filtered(query_req)
        if selected_ids:
            qs = qs.filter(pk__in=selected_ids)
        for e in qs.order_by("round__hackathon_id", "round__order", "team__name").iterator(chunk_size=200):
            yield self._normalize(e)

    def _normalize(self, e: RoundEntry) -> dict[str, CanonicalValue]:
        team = e.team
        problem_id = _problem_id(team)
        return {
            "id":                self._val(str(e.pk),                                          "number",   "hackathons.RoundEntry.id"),
            "hackathon_title":   self._val(e.round.hackathon.title,                            "text",     "hackathons.Hackathon.title"),
            "round_order":       self._val(e.round.order,                                      "number",   "hackathons.Round.order"),
            "round_name":        self._val(e.round.name,                                       "text",     "hackathons.Round.name"),
            "team_name":         self._val(team.name,                                          "text",     "hackathons.Team.name"),
            "leader_email":      self._val(team.leader.email if team.leader else None,         "email",    "hackathons.Team.leader.email"),
            "problem_statement": self._val(f"{problem_id}: {_problem_title(team)}" if problem_id else None, "text", "hackathons.ProblemStatement.code"),
            "status":            self._val(e.get_status_display(),                             "badge",    "hackathons.RoundEntry.status"),
            "results_published": self._val(e.round.results_published,                          "boolean",  "hackathons.Round.results_published"),
            "details_submitted": self._val(e.details_response_id is not None,                  "boolean",  "hackathons.RoundEntry.details_response"),
            "feedback":          self._val(e.feedback or None,                                 "text",     "hackathons.RoundEntry.feedback"),
            "decided_at":        self._val(e.decided_at.isoformat() if e.decided_at else None, "datetime", "hackathons.RoundEntry.decided_at"),
        }
