"""
Organizer downloads for one hackathon: teams, participants, ideas, projects, round results and check-in.

Every download is rendered by the shared export serializer (apps.core.dmc.export_service), the same one the form
responses and the Data Management Center use, so they all share one file contract: name pattern, UTF-8 BOM,
formula protection, readable headers, Yes/No, IST dates and CSV / Excel / JSON.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from django.db.models import Count, Prefetch
from django.utils.text import slugify

from apps.core.dmc.contracts import ColumnDefinition
from apps.core.dmc.export_service import serialize_records

from .errors import HackathonError
from . import check_in_sessions
from . import payments as payments_module
from .models import CheckIn, Hackathon, IdeaSubmission, PaymentStage, PaymentStatus, Submission, TeamMember, TeamStatus

EXPORT_FORMATS = ('csv', 'xlsx', 'json')
QuerySlice = dict[str, str]
Row = dict[str, Any]


def _col(key: str, label: str, type: str = 'text') -> ColumnDefinition:
    return ColumnDefinition(key=key, label=label, type=type)


def _full_name(user) -> str:
    return f'{user.first_name} {user.last_name}'.strip() or user.email


def _college(user) -> str:
    return user.college.name if user.college_id else (user.other_college_name or '')


def _ids(params: QuerySlice) -> set[int] | None:
    """``?ids=1,2,3`` limits an export to those rows (what the organizer sees after filtering); absent means all."""
    if 'ids' not in params:
        return None
    return {int(part) for part in params['ids'].split(',') if part.strip().isdigit()}


# ---------------------------------------------------------------------------
# Teams: one row per member, with the team's problem
# ---------------------------------------------------------------------------

TEAM_COLUMNS = [
    _col('team', 'Team'), _col('team_status', 'Team Status'), _col('problem_id', 'Problem ID'),
    _col('problem_title', 'Problem Title'), _col('problem_domain', 'Problem Domain'), _col('open_innovation', 'Open Innovation', 'boolean'),
    _col('role', 'Role'), _col('name', 'Name'), _col('email', 'Email', 'email'), _col('phone', 'Phone'), _col('college', 'College'),
    _col('branch', 'Branch'), _col('year', 'Year', 'number'), _col('roll_number', 'Roll Number'), _col('club_id', 'Club ID'),
]


PAYMENT_COLUMNS = [_col('payment_status', 'Payment Status'), _col('payment_amount', 'Payment Amount', 'number'), _col('transaction_id', 'Transaction ID')]


def team_columns(hackathon: Hackathon) -> list[ColumnDefinition]:
    """The payment columns appear only for hackathons whose teams pay when they register."""
    return TEAM_COLUMNS + (PAYMENT_COLUMNS if hackathon.payment_stage == PaymentStage.REGISTRATION else [])


def _problem(team) -> tuple[str, str, str]:
    if team.problem_statement_id:
        problem = team.problem_statement
        return problem.code, problem.title, problem.domain or ''
    if team.is_open_innovation:
        return team.open_innovation_code, team.custom_problem_title, team.custom_problem_domain
    return '', '', ''


def team_rows(hackathon: Hackathon, params: QuerySlice) -> Iterable[Row]:
    wanted = _ids(params)
    with_payment = hackathon.payment_stage == PaymentStage.REGISTRATION
    teams = hackathon.teams.select_related('problem_statement', 'payment').prefetch_related(
        Prefetch('memberships', queryset=TeamMember.objects.select_related('user', 'user__college')),
    ).order_by('-created_at', 'id')
    for team in teams:
        if wanted is not None and team.id not in wanted:
            continue
        code, title, domain = _problem(team)
        payment = getattr(team, 'payment', None) if with_payment else None
        for membership in team.memberships.all():
            user = membership.user
            yield {
                **({'payment_status': payment.get_status_display() if payment else 'Not required', 'payment_amount': payment.amount if payment else '',
                    'transaction_id': payment.transaction_id if payment else ''} if with_payment else {}),
                'team': team.name, 'team_status': 'WAITLISTED' if team.is_waitlisted else team.status, 'problem_id': code,
                'problem_title': title, 'problem_domain': domain, 'open_innovation': team.is_open_innovation,
                'role': membership.role, 'name': _full_name(user), 'email': user.email, 'phone': user.phone_number or '',
                'college': _college(user), 'branch': user.branch or '', 'year': user.year or '',
                'roll_number': user.roll_number or '', 'club_id': user.club_id or '',
            }


# ---------------------------------------------------------------------------
# Participants: one row per person with the organizers' own questions as extra columns
# ---------------------------------------------------------------------------

PARTICIPANT_COLUMNS = [
    _col('team', 'Team'), _col('team_status', 'Team Status'), _col('role', 'Role'), _col('name', 'Name'), _col('email', 'Email', 'email'),
    _col('phone', 'Phone'), _col('branch', 'Branch'), _col('year', 'Year', 'number'), _col('roll_number', 'Roll Number'),
    _col('college', 'College'), _col('rules_accepted', 'Rules Accepted', 'boolean'), _col('checked_in', 'Checked In', 'boolean'),
]


def _later_sessions(hackathon: Hackathon):
    """The sessions after arrival that are worth a column: ones that take scans, or kept because people were checked in."""
    return [
        session for session in hackathon.check_in_sessions.annotate(taken=Count('check_ins')).order_by('day_index', 'slot')
        if not session.is_main and (session.is_scannable or session.taken)
    ]


def participant_columns(hackathon: Hackathon) -> list[ColumnDefinition]:
    questions = [_col(f"answer_{q['key']}", q['label']) for q in (hackathon.registration_questions or [])]
    attended = [_col(f'attended_{session.id}', f'Attended: {session.label}', 'boolean') for session in _later_sessions(hackathon)]
    return PARTICIPANT_COLUMNS + attended + questions


def participant_rows(hackathon: Hackathon, params: QuerySlice) -> Iterable[Row]:
    arrived = set(check_in_sessions.main_check_ins(hackathon).values_list('user_id', flat=True))
    later = _later_sessions(hackathon)
    attended: dict[int, set[int]] = {session.id: set() for session in later}
    for session_id, user_id in CheckIn.objects.filter(hackathon=hackathon, session_id__in=attended).values_list('session_id', 'user_id'):
        attended[session_id].add(user_id)
    questions = hackathon.registration_questions or []
    memberships = hackathon.memberships.select_related('user', 'user__college', 'team').order_by('team__name', 'role', 'id')
    for membership in memberships:
        user = membership.user
        row = {
            'team': membership.team.name, 'team_status': 'WAITLISTED' if membership.team.is_waitlisted else membership.team.status,
            'role': membership.role, 'name': _full_name(user), 'email': user.email, 'phone': user.phone_number or '',
            'branch': user.branch or '', 'year': user.year or '', 'roll_number': user.roll_number or '', 'college': _college(user),
            'rules_accepted': membership.rules_accepted_at is not None, 'checked_in': user.id in arrived,
        }
        for session in later:
            row[f'attended_{session.id}'] = user.id in attended[session.id]
        for question in questions:
            row[f"answer_{question['key']}"] = (membership.answers or {}).get(question['key'], '')
        yield row


# ---------------------------------------------------------------------------
# Ideas and projects
# ---------------------------------------------------------------------------

IDEA_COLUMNS = [
    _col('team', 'Team'), _col('team_status', 'Team Status'), _col('status', 'Status'), _col('title', 'Title'), _col('problem', 'Problem', 'multiline'),
    _col('solution', 'Solution', 'multiline'), _col('plan', 'Plan', 'multiline'), _col('tech_stack', 'Tech Stack'), _col('deck_url', 'Slides Link', 'url'),
    _col('linkedin_url', 'LinkedIn Post', 'url'), _col('has_slides_file', 'Slides File Uploaded', 'boolean'),
    _col('has_payment_proof', 'Payment Screenshot Uploaded', 'boolean'), _col('transaction_id', 'Transaction ID'), _col('feedback', 'Feedback', 'multiline'),
    _col('score', 'Score', 'number'), _col('review_count', 'Reviews', 'number'), _col('created_at', 'Submitted At', 'datetime'),
    _col('updated_at', 'Last Updated', 'datetime'),
]


def idea_rows(hackathon: Hackathon, params: QuerySlice) -> Iterable[Row]:
    ideas = IdeaSubmission.objects.filter(team__hackathon=hackathon).select_related('team').order_by('status', 'team__name')
    for idea in ideas:
        yield {
            'team': idea.team.name, 'team_status': idea.team.status, 'status': idea.status, 'title': idea.title, 'problem': idea.problem,
            'solution': idea.solution, 'plan': idea.plan, 'tech_stack': idea.tech_stack, 'deck_url': idea.deck_url or '',
            'linkedin_url': idea.linkedin_url or '', 'has_slides_file': idea.deck_file_id is not None,
            'has_payment_proof': idea.payment_proof_id is not None, 'transaction_id': idea.transaction_id or '', 'feedback': idea.feedback,
            'score': idea.score if idea.review_count else '', 'review_count': idea.review_count,
            'created_at': idea.created_at, 'updated_at': idea.updated_at,
        }


SUBMISSION_COLUMNS = [
    _col('team', 'Team'), _col('team_status', 'Team Status'), _col('project_title', 'Project'), _col('tagline', 'Tagline'),
    _col('description', 'Description', 'multiline'), _col('repo_url', 'Repository', 'url'), _col('demo_url', 'Demo', 'url'),
    _col('video_url', 'Video', 'url'), _col('slides_url', 'Slides', 'url'), _col('tech_stack', 'Tech Stack'), _col('is_public', 'Public', 'boolean'),
    _col('score', 'Score', 'number'), _col('review_count', 'Reviews', 'number'), _col('submitted_at', 'Submitted At', 'datetime'),
    _col('updated_at', 'Last Updated', 'datetime'),
]


def submission_rows(hackathon: Hackathon, params: QuerySlice) -> Iterable[Row]:
    from . import analytics

    for row in analytics.submission_rows(hackathon):
        yield {key: value for key, value in row.items() if key not in ('id', 'team_id')}


# ---------------------------------------------------------------------------
# Round results
# ---------------------------------------------------------------------------

ROUND_COLUMNS = [
    _col('team_name', 'Team'), _col('team_status', 'Team Status'), _col('leader_email', 'Leader Email', 'email'), _col('member_count', 'Members', 'number'),
    _col('problem_code', 'Problem ID'), _col('status', 'Status'), _col('idea_status', 'Idea Status'), _col('idea_score', 'Idea Score', 'number'),
    _col('has_project', 'Project Submitted', 'boolean'), _col('project_score', 'Project Score', 'number'),
    _col('checked_in', 'Members Checked In', 'number'), _col('feedback', 'Feedback', 'multiline'), _col('admin_notes', 'Admin Notes', 'multiline'),
    _col('details_submitted', 'Details Submitted', 'boolean'), _col('decided_by_name', 'Decided By'), _col('decided_at', 'Decided At', 'datetime'),
]


def round_rows(hackathon: Hackathon, params: QuerySlice) -> Iterable[Row]:
    from .serializers import AdminRoundEntrySerializer
    from .views import round_entries_queryset

    round_id = params.get('round', '')
    if not round_id.isdigit():
        raise HackathonError('Choose which round to export.', 'ROUND_REQUIRED', field='round')
    round_obj = hackathon.rounds.filter(pk=int(round_id)).first()
    if round_obj is None:
        raise HackathonError('That round does not exist in this hackathon.', 'NOT_FOUND', field='round', status=404)
    for entry in AdminRoundEntrySerializer(round_entries_queryset(round_obj, params), many=True).data:
        yield {**entry, 'details_submitted': entry['details_response_id'] is not None}


# ---------------------------------------------------------------------------
# Check-in
# ---------------------------------------------------------------------------

CHECK_IN_COLUMNS = [
    _col('name', 'Name'), _col('email', 'Email', 'email'), _col('team', 'Team'), _col('role', 'Role'), _col('checked_in', 'Checked In', 'boolean'),
    _col('checked_in_at', 'Checked In At', 'datetime'), _col('checked_in_by', 'Checked In By'),
]


def check_in_rows(hackathon: Hackathon, params: QuerySlice) -> Iterable[Row]:
    session = check_in_sessions.resolve(hackathon, params.get('session'))
    arrived = {c.user_id: c for c in CheckIn.objects.filter(session=session).select_related('checked_in_by')}
    placed = hackathon.memberships.filter(team__status=TeamStatus.REGISTERED, team__is_waitlisted=False)
    for membership in placed.select_related('user', 'team').order_by('team__name', 'role', 'user__first_name', 'id'):
        record = arrived.get(membership.user_id)
        yield {
            'name': _full_name(membership.user), 'email': membership.user.email, 'team': membership.team.name, 'role': membership.role,
            'checked_in': record is not None, 'checked_in_at': record.checked_in_at if record else '',
            'checked_in_by': _full_name(record.checked_in_by) if record and record.checked_in_by else '',
        }


# ---------------------------------------------------------------------------
# Registration payments: every payment ever made, including those of teams that no longer exist
# ---------------------------------------------------------------------------

PAYMENT_EXPORT_COLUMNS = [
    _col('team', 'Team'), _col('team_status', 'Team Status'), _col('leader', 'Leader'), _col('leader_email', 'Leader Email', 'email'),
    _col('leader_phone', 'Leader Phone'), _col('amount', 'Amount', 'number'), _col('transaction_id', 'Transaction ID'),
    _col('status', 'Payment Status'), _col('submitted_at', 'Submitted At', 'datetime'), _col('reviewed_by', 'Reviewed By'),
    _col('reviewed_at', 'Reviewed At', 'datetime'), _col('reason', 'Reason', 'multiline'), _col('settle', 'Money To Settle', 'boolean'),
]

#: Teams that were placed and later dropped out also leave money behind.
_DROPPED_TEAM = (TeamStatus.WITHDRAWN, TeamStatus.DISQUALIFIED)


def payment_rows(hackathon: Hackathon, params: QuerySlice) -> Iterable[Row]:
    payments = hackathon.team_payments.select_related('team', 'reviewed_by').order_by('created_at', 'id')
    if params.get('status') in PaymentStatus.values:
        payments = payments.filter(status=params['status'])
    for payment in payments:
        team_status = payment.team.status if payment.team_id else 'DELETED'
        yield {
            'team': payment.team_name or (payment.team.name if payment.team_id else ''), 'team_status': team_status,
            'leader': payment.leader_name, 'leader_email': payment.leader_email, 'leader_phone': payment.leader_phone,
            'amount': payment.amount, 'transaction_id': payment.transaction_id, 'status': payment.get_status_display(),
            'submitted_at': payment.created_at, 'reviewed_by': _full_name(payment.reviewed_by) if payment.reviewed_by_id else '',
            'reviewed_at': payment.reviewed_at or '', 'reason': payment.reason,
            'settle': payment.status in (PaymentStatus.REJECTED, *payments_module.CLOSED_STATUSES) or team_status in _DROPPED_TEAM,
        }


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExportKind:
    key: str
    #: Goes into the file name: <hackathon>-<name>-<timestamp>.<ext>
    name: str
    #: Anyone holding any of these hackathon permissions may download it.
    permissions: tuple[str, ...]
    columns: Callable[[Hackathon], list[ColumnDefinition]]
    rows: Callable[[Hackathon, QuerySlice], Iterable[Row]]


def _fixed(columns: list[ColumnDefinition]) -> Callable[[Hackathon], list[ColumnDefinition]]:
    return lambda hackathon: columns


EXPORT_KINDS: dict[str, ExportKind] = {kind.key: kind for kind in (
    ExportKind('teams', 'teams', ('teams', 'rounds', 'results'), team_columns, team_rows),
    ExportKind('participants', 'participants', ('teams',), participant_columns, participant_rows),
    ExportKind('ideas', 'ideas', ('ideas',), _fixed(IDEA_COLUMNS), idea_rows),
    ExportKind('submissions', 'submissions', ('submissions',), _fixed(SUBMISSION_COLUMNS), submission_rows),
    ExportKind('rounds', 'round-results', ('rounds',), _fixed(ROUND_COLUMNS), round_rows),
    ExportKind('check-in', 'check-in', ('check_in',), _fixed(CHECK_IN_COLUMNS), check_in_rows),
    ExportKind('payments', 'payments', ('teams',), _fixed(PAYMENT_EXPORT_COLUMNS), payment_rows),
)}


def file_part(hackathon: Hackathon, kind: ExportKind, params: QuerySlice) -> str:
    """The kind's name in the file name; a check-in list of a session other than arrival says which one."""
    if kind.key == 'check-in':
        session = check_in_sessions.resolve(hackathon, params.get('session'))
        if not session.is_main:
            return f"{kind.name}-{slugify(session.label) or 'session'}"
    return kind.name


def render(hackathon: Hackathon, kind: ExportKind, params: QuerySlice, fmt: str) -> tuple[bytes, str, int]:
    """The file's bytes, its content type and how many rows it holds."""
    rows = list(kind.rows(hackathon, params))
    data, content_type = serialize_records(kind.columns(hackathon), iter(rows), fmt)
    return data, content_type, len(rows)
