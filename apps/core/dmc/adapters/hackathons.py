"""
dmc/adapters/hackathons.py
--------------------------
Three granular DMC adapters for the Hackathons module.

datasets:
  hackathon_participants → 1 row = 1 team member (or team leader)
  hackathon_teams        → 1 row = 1 team + project submission summary
  hackathon_submissions  → 1 row = 1 project submission
"""

from __future__ import annotations
from typing import Any, Generator

from django.db.models import Q

from apps.core.dmc.adapters.base import BaseDatasetAdapter
from apps.core.dmc.contracts import CanonicalValue, ColumnDefinition, FilterDefinition, FilterOption, QueryRequest, QueryResult
from apps.hackathons.models import Hackathon, Team, Submission


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

    def query(self, query_req: QueryRequest, user: Any) -> QueryResult:
        teams = Team.objects.select_related("hackathon", "leader").prefetch_related("members")
        for f in query_req.filters:
            if f.field == "hackathon_title":
                teams = teams.filter(hackathon_id=f.value)
        rows = []
        for team in teams:
            rows.extend(self._expand_team(team))

        if query_req.search:
            q = query_req.search.lower()
            rows = [r for r in rows if q in (r.get("email", "").lower() + r.get("name", "").lower())]

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
        for team in Team.objects.select_related("hackathon", "leader").prefetch_related("members").iterator(chunk_size=100):
            for row in self._expand_team(team):
                yield self._to_record(row)

    def _expand_team(self, team: Team) -> list[dict]:
        rows = []
        leader = team.leader
        if leader is not None:
            rows.append({"id": f"t{team.id}_l{leader.id}", "name": f"{leader.first_name} {leader.last_name}".strip(), "email": leader.email, "hackathon_title": team.hackathon.title, "team_name": team.name, "team_role": "Leader", "roll_number": getattr(leader, "roll_number", None), "created_at": team.created_at.isoformat() if team.created_at else None})
        for m in team.members.all():
            rows.append({"id": f"t{team.id}_m{m.id}", "name": f"{m.first_name} {m.last_name}".strip(), "email": m.email, "hackathon_title": team.hackathon.title, "team_name": team.name, "team_role": "Member", "roll_number": getattr(m, "roll_number", None), "created_at": team.created_at.isoformat() if team.created_at else None})
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

    def query(self, query_req: QueryRequest, user: Any) -> QueryResult:
        qs = Team.objects.select_related("hackathon", "leader").prefetch_related("members", "submission")
        if query_req.search:
            q = query_req.search.strip()
            qs = qs.filter(Q(name__icontains=q) | Q(leader__email__icontains=q) | Q(hackathon__title__icontains=q))
        for f in query_req.filters:
            if f.field == "hackathon_title":
                qs = qs.filter(hackathon_id=f.value)

        sort_field = query_req.sort.field if query_req.sort.field in ALLOWED_SORT_TEAMS else "created_at"
        prefix = "-" if query_req.sort.direction == "desc" else ""
        qs = qs.order_by(f"{prefix}{sort_field}")

        total = qs.count()
        offset = (query_req.page - 1) * query_req.page_size
        page = list(qs[offset: offset + query_req.page_size])
        return QueryResult(records=[self._normalize(t) for t in page], total=total, page=query_req.page, page_size=query_req.page_size, dataset_id="hackathon_teams")

    def get_record(self, record_id: str, user: Any) -> dict[str, CanonicalValue] | None:
        try:
            t = Team.objects.select_related("hackathon", "leader").prefetch_related("members", "submission").get(pk=record_id)
        except (Team.DoesNotExist, ValueError):
            return None
        return self._normalize(t)

    def stream_records(self, query_req: QueryRequest, user: Any, selected_ids: list[str] | None = None) -> Generator[dict[str, CanonicalValue], None, None]:
        qs = Team.objects.select_related("hackathon", "leader").prefetch_related("members", "submission")
        if selected_ids:
            qs = qs.filter(pk__in=selected_ids)
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
            "member_count":     self._val(t.members.count(),                              "number",   "hackathons.Team.members.count"),
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

    def query(self, query_req: QueryRequest, user: Any) -> QueryResult:
        qs = Submission.objects.select_related("team__hackathon", "team__leader")
        if query_req.search:
            q = query_req.search.strip()
            qs = qs.filter(Q(project_title__icontains=q) | Q(team__name__icontains=q))
        for f in query_req.filters:
            if f.field == "hackathon_title":
                qs = qs.filter(team__hackathon_id=f.value)

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
