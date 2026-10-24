"""
Event-operations endpoints: submissions, judging, results, check-in, certificates, team finder,
analytics and cloning. Rules live in the service modules; these views parse, authorise and shape responses.
"""
from __future__ import annotations

from django.http import Http404
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response as DRFResponse
from rest_framework.views import APIView

from apps.core.permissions import IsAdminOrClubLead
from apps.core.throttling import ScopedRateThrottle

from . import access, analytics, certificates, idea_files, registration
from .access import can_check_in, can_judge, scoped
from .checkin import CheckInService, make_token
from .cloning import CloneService
from .ideas import IdeaService, can_team_edit, project_gate, window_state as idea_window_state
from .errors import HackathonError
from .finder import FinderService
from .judging import CriteriaService, JudgingService, ResultsService, StaffService, clean_target
from .models import HackathonStaff, IdeaReview, IdeaStatus, IdeaSubmission, Submission, SubmissionReview, Team, TeamMember, TeamStatus
from .submissions import SubmissionService, window_state
from .views import _get_hackathon


def _my_team(hackathon, user) -> Team | None:
    membership = TeamMember.objects.select_related('team').filter(hackathon=hackathon, user=user).first()
    return membership.team if membership else None


def _submission_payload(submission: Submission) -> dict:
    return {
        'id': submission.id, 'team_id': submission.team_id, 'team_name': submission.team.name,
        'project_title': submission.project_title, 'tagline': submission.tagline, 'description': submission.description,
        'repo_url': submission.repo_url, 'demo_url': submission.demo_url, 'video_url': submission.video_url,
        'slides_url': submission.slides_url, 'tech_stack': submission.tech_stack, 'is_public': submission.is_public,
        'created_at': submission.created_at, 'updated_at': submission.updated_at,
    }


def _criteria_payload(hackathon, target: str = 'PROJECT') -> list[dict]:
    return [
        {'id': c.id, 'name': c.name, 'description': c.description, 'max_score': c.max_score, 'weight': c.weight, 'order': c.order}
        for c in hackathon.criteria.filter(target=target)
    ]


def _require(hackathon, user, key: str):
    if not access.has_permission(hackathon, user, key):
        raise PermissionDenied('You do not have permission to do this in this hackathon.')


# ---------------------------------------------------------------------------
# Participant: registration details, submission, QR, certificate
# ---------------------------------------------------------------------------

class MySubmissionView(APIView):
    """GET / PUT /api/hackathons/{slug}/my-submission/ - the team's project (leader edits until the deadline)."""
    permission_classes = [permissions.IsAuthenticated]

    def _state(self, hackathon, team):
        submission = Submission.objects.select_related('team').filter(team=team).first() if team else None
        state = window_state(hackathon)
        gate = project_gate(hackathon, team)
        return {
            'state': state, 'opens_at': hackathon.submission_opens_at, 'deadline': hackathon.submission_deadline,
            'submission': _submission_payload(submission) if submission else None,
            'gate': gate,
            'can_edit': bool(team and team.leader_id == self.request.user.id and state == 'OPEN' and gate['satisfied']
                             and team.status == TeamStatus.REGISTERED and not team.is_waitlisted),
        }

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        return DRFResponse(self._state(hackathon, _my_team(hackathon, request.user)))

    def put(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        team = _my_team(hackathon, request.user)
        if team is None:
            raise HackathonError('Join or create a team before submitting.', 'NO_TEAM')
        SubmissionService.save(team, request.user, request.data)
        return DRFResponse(self._state(hackathon, team))


class MyRegistrationView(APIView):
    """PATCH /api/hackathons/{slug}/my-registration/ {accept_rules?, answers?} - fill in what the organizers asked for."""
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        membership = TeamMember.objects.filter(hackathon=hackathon, user=request.user).first()
        if membership is None:
            raise HackathonError('Join or create a team first.', 'NO_TEAM')
        accepted_at, answers = membership.rules_accepted_at, None
        if request.data.get('accept_rules') and not accepted_at:
            accepted_at = timezone.now()
        if 'answers' in request.data:
            answers = registration.clean_answers(hackathon, request.data.get('answers'))
        membership.rules_accepted_at = accepted_at
        if answers is not None:
            membership.answers = answers
        membership.save(update_fields=['rules_accepted_at', 'answers'])
        return DRFResponse({
            'rules_accepted_at': membership.rules_accepted_at, 'answers': membership.answers,
            'missing': registration.missing_registration_details(hackathon, membership),
        })


class MyCheckInView(APIView):
    """GET /api/hackathons/{slug}/my-check-in/ - the participant's signed QR code and whether they have arrived."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        if not hackathon.feature('check_in'):
            raise HackathonError('Check-in is not enabled for this hackathon.', 'FEATURE_OFF')
        team = _my_team(hackathon, request.user)
        if team is None or team.status != TeamStatus.REGISTERED or team.is_waitlisted:
            raise HackathonError('Check-in opens once your team is registered.', 'TEAM_NOT_REGISTERED')
        record = hackathon.check_ins.filter(user=request.user).first()
        return DRFResponse({
            'token': make_token(hackathon, request.user),
            'checked_in': record is not None, 'checked_in_at': record.checked_in_at if record else None,
        })


class MyCertificateView(APIView):
    """GET /api/hackathons/{slug}/my-certificate/"""
    permission_classes = [permissions.IsAuthenticated]
    verification_reads_need_verified = True  # a private feature even though it only reads

    def get(self, request, slug):
        return DRFResponse(certificates.certificate_for(_get_hackathon(request, slug), request.user))


class CertificateVerifyView(APIView):
    """GET /api/hackathons/certificates/verify/{code}/ - public: is this certificate genuine?"""
    permission_classes = [permissions.AllowAny]

    def get(self, request, code):
        return DRFResponse(certificates.verify(code))


# ---------------------------------------------------------------------------
# Public: results and showcase
# ---------------------------------------------------------------------------

class ResultsView(APIView):
    """GET /api/hackathons/{slug}/results/ - winners (and optionally every score) once the organizers publish."""
    permission_classes = [permissions.AllowAny]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        organizer = access.has_permission(hackathon, request.user, 'results')
        published = hackathon.results_published_at is not None
        if not published and not organizer:
            return DRFResponse({'published': False, 'winners': [], 'leaderboard': []})
        submissions = {s.team_id: s for s in Submission.objects.filter(team__hackathon=hackathon)}
        winners = [
            {
                'id': winner.id, 'team_id': winner.team_id, 'team_name': winner.team.name, 'title': winner.title,
                'rank': winner.rank, 'prize': winner.prize, 'note': winner.note,
                'project_title': submissions[winner.team_id].project_title if winner.team_id in submissions else '',
                'project_url': submissions[winner.team_id].demo_url or submissions[winner.team_id].repo_url
                if winner.team_id in submissions and submissions[winner.team_id].is_public else '',
            }
            for winner in hackathon.winners.select_related('team')
        ]
        show_scores = hackathon.show_leaderboard or organizer
        return DRFResponse({
            'published': published, 'published_at': hackathon.results_published_at, 'winners': winners,
            'leaderboard': JudgingService.leaderboard(hackathon) if show_scores else [],
        })


class ShowcaseView(APIView):
    """GET /api/hackathons/{slug}/showcase/ - public gallery of submitted projects after results are out."""
    permission_classes = [permissions.AllowAny]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        organizer = access.has_permission(hackathon, request.user, 'results')
        if not hackathon.feature('showcase') or (hackathon.results_published_at is None and not organizer):
            return DRFResponse({'enabled': False, 'projects': []})
        winners = {w.team_id: w.title for w in hackathon.winners.order_by('-rank')}
        submissions = (
            Submission.objects.filter(team__hackathon=hackathon, is_public=True, team__is_waitlisted=False)
            .exclude(team__status__in=(TeamStatus.DISQUALIFIED, TeamStatus.WITHDRAWN))
            .select_related('team').prefetch_related('team__memberships__user').order_by('team__name')
        )
        projects = [
            {
                'id': s.id, 'team_name': s.team.name, 'project_title': s.project_title, 'tagline': s.tagline,
                'description': s.description, 'repo_url': s.repo_url, 'demo_url': s.demo_url, 'video_url': s.video_url,
                'slides_url': s.slides_url, 'tech_stack': s.tech_stack, 'award': winners.get(s.team_id),
                'members': [f'{m.user.first_name} {m.user.last_name}'.strip() or m.user.email.split('@')[0] for m in s.team.memberships.all()],
            }
            for s in submissions
        ]
        projects.sort(key=lambda p: (p['award'] is None, p['team_name'].lower()))
        return DRFResponse({'enabled': True, 'projects': projects})


# ---------------------------------------------------------------------------
# Team finder
# ---------------------------------------------------------------------------

class FinderView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        return DRFResponse(FinderService.listing(_get_hackathon(request, slug), request.user))


class FinderMeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def put(self, request, slug):
        seeker = FinderService.save_profile(_get_hackathon(request, slug), request.user, request.data)
        return DRFResponse({'skills': seeker.skills, 'note': seeker.note})

    def delete(self, request, slug):
        FinderService.withdraw_profile(_get_hackathon(request, slug), request.user)
        return DRFResponse({'withdrawn': True})


class FinderInviteView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug, pk):
        FinderService.invite_seeker(_get_hackathon(request, slug), request.user, pk)
        return DRFResponse({'invited': True})


class FinderRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug, pk):
        FinderService.request_to_join(_get_hackathon(request, slug), request.user, pk, request.data.get('message', ''))
        return DRFResponse({'requested': True})


# ---------------------------------------------------------------------------
# Judges
# ---------------------------------------------------------------------------

class JudgingListView(APIView):
    """GET /api/hackathons/{slug}/judging/ - the rubric, every submission and this judge's own scores."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        if not can_judge(hackathon, request.user):
            raise PermissionDenied('You are not a judge for this hackathon.')
        mine = {r.submission_id: r for r in SubmissionReview.objects.filter(submission__team__hackathon=hackathon, judge=request.user)}
        own_team = _my_team(hackathon, request.user)
        rows = []
        for submission in (
            Submission.objects.filter(team__hackathon=hackathon, team__is_waitlisted=False)
            .exclude(team__status__in=(TeamStatus.DISQUALIFIED, TeamStatus.WITHDRAWN)).select_related('team').order_by('team__name')
        ):
            review = mine.get(submission.id)
            rows.append({
                **_submission_payload(submission), 'is_own_team': own_team is not None and own_team.id == submission.team_id,
                'my_review': {'scores': review.scores, 'comment': review.comment, 'total': review.total} if review else None,
            })
        return DRFResponse({
            'enabled': hackathon.feature('judging'), 'criteria': _criteria_payload(hackathon), 'submissions': rows,
            'reviewed': len(mine), 'total': len(rows),
            'ideas_scoring': hackathon.feature('ideas') and hackathon.criteria.filter(target='IDEA').exists(),
        })


class JudgingReviewView(APIView):
    """POST /api/hackathons/{slug}/judging/{submission_id}/ {scores: {criterion_id: n}, comment}"""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug, pk):
        hackathon = _get_hackathon(request, slug)
        submission = Submission.objects.select_related('team').filter(pk=pk, team__hackathon=hackathon).first()
        if submission is None:
            raise Http404
        review = JudgingService.review(submission, request.user, request.data.get('scores'), request.data.get('comment', ''))
        return DRFResponse({'scores': review.scores, 'comment': review.comment, 'total': review.total})


# ---------------------------------------------------------------------------
# Organizer: rubric, staff, scoreboard, winners, results
# ---------------------------------------------------------------------------

class CriteriaView(APIView):
    """GET (organizer or judge) / PUT (organizer) /api/hackathons/{slug}/criteria/"""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        if not access.has_any_permission(hackathon, request.user, ('judge', 'judging')):
            raise PermissionDenied('Not allowed.')
        return DRFResponse(_criteria_payload(hackathon, clean_target(request.query_params.get('target'))))

    def put(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        _require(hackathon, request.user, 'judging')
        target = clean_target(request.data.get('target') or request.query_params.get('target'))
        CriteriaService.replace(hackathon, request.user, request.data.get('criteria'), target)
        return DRFResponse(_criteria_payload(hackathon, target))

    def post(self, request, slug):
        """POST seeds a sensible default rubric (``?target=IDEA`` for the idea rubric) when none exists yet."""
        hackathon = _get_hackathon(request, slug)
        _require(hackathon, request.user, 'judging')
        target = clean_target(request.data.get('target') or request.query_params.get('target'))
        CriteriaService.seed_defaults(hackathon, request.user, target)
        return DRFResponse(_criteria_payload(hackathon, target), status=status.HTTP_201_CREATED)


def _staff_payload(staff: HackathonStaff) -> dict:
    user = staff.user
    return {
        'id': staff.id, 'role': staff.role, 'user_id': user.id, 'email': user.email,
        'name': f'{user.first_name} {user.last_name}'.strip() or user.email,
        'permissions': list(staff.permissions or access.preset_permissions(staff.role)),
    }


class StaffView(APIView):
    """GET / POST /api/hackathons/{slug}/staff/ {email, role} - judges and volunteers for this hackathon only."""
    permission_classes = [scoped('staff')]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        return DRFResponse([_staff_payload(s) for s in hackathon.staff.select_related('user')])

    def post(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        staff = StaffService.add(
            hackathon, request.user, request.data.get('email', ''), request.data.get('role', ''), request.data.get('permissions'),
        )
        return DRFResponse(_staff_payload(staff), status=status.HTTP_201_CREATED)


class StaffDetailView(APIView):
    permission_classes = [scoped('staff')]

    def patch(self, request, slug, pk):
        staff = StaffService.update_permissions(_get_hackathon(request, slug), request.user, pk, request.data.get('permissions'))
        return DRFResponse(_staff_payload(staff))

    def delete(self, request, slug, pk):
        StaffService.remove(_get_hackathon(request, slug), request.user, pk)
        return DRFResponse({'deleted': True, 'id': pk})


class LeaderboardView(APIView):
    """GET (organizer) /api/hackathons/{slug}/leaderboard/ - ranked submissions with review progress."""
    permission_classes = [scoped('judging')]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        judges = hackathon.staff.filter(role='JUDGE').count()
        if clean_target(request.query_params.get('target')) == 'IDEA':
            return DRFResponse({'judges': judges, 'rows': JudgingService.idea_leaderboard(hackathon), 'criteria': _criteria_payload(hackathon, 'IDEA')})
        return DRFResponse({'judges': judges, 'rows': JudgingService.leaderboard(hackathon), 'criteria': _criteria_payload(hackathon)})


class SubmissionReviewsView(APIView):
    """GET (organizer) /api/hackathons/{slug}/submissions/{id}/reviews/"""
    permission_classes = [scoped('judging')]

    def get(self, request, slug, pk):
        hackathon = _get_hackathon(request, slug)
        if not Submission.objects.filter(pk=pk, team__hackathon=hackathon).exists():
            raise Http404
        reviews = SubmissionReview.objects.filter(submission_id=pk).select_related('judge')
        return DRFResponse([
            {
                'id': r.id, 'judge': f'{r.judge.first_name} {r.judge.last_name}'.strip() or r.judge.email,
                'scores': r.scores, 'comment': r.comment, 'total': r.total, 'updated_at': r.updated_at,
            }
            for r in reviews
        ])


def _winner_payload(winner) -> dict:
    return {
        'id': winner.id, 'team_id': winner.team_id, 'team_name': winner.team.name, 'title': winner.title,
        'rank': winner.rank, 'prize': winner.prize, 'note': winner.note,
    }


class WinnersView(APIView):
    """GET / PUT (organizer) /api/hackathons/{slug}/winners/ - PUT replaces the full list of awards."""
    permission_classes = [scoped('results')]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        return DRFResponse([_winner_payload(w) for w in hackathon.winners.select_related('team')])

    def put(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        winners = ResultsService.set_winners(hackathon, request.user, request.data.get('winners'))
        return DRFResponse([_winner_payload(w) for w in winners])


class ResultsPublishView(APIView):
    """POST (organizer) /api/hackathons/{slug}/results/publish|unpublish/"""
    permission_classes = [scoped('results')]

    def post(self, request, slug, action):
        hackathon = _get_hackathon(request, slug)
        if action == 'publish':
            ResultsService.publish(hackathon, request.user, notify=bool(request.data.get('notify', True)))
        elif action == 'unpublish':
            ResultsService.unpublish(hackathon, request.user)
        else:
            raise Http404
        return DRFResponse({'published': hackathon.results_published_at is not None, 'published_at': hackathon.results_published_at})


# ---------------------------------------------------------------------------
# Organizer / volunteer: check-in
# ---------------------------------------------------------------------------

class CheckInView(APIView):
    """GET roster / POST {user_id | token} /api/hackathons/{slug}/check-in/ (organizers and volunteers)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        if not can_check_in(hackathon, request.user):
            raise PermissionDenied('You are not allowed to check participants in.')
        return DRFResponse(CheckInService.roster(hackathon))

    def post(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        record, created = CheckInService.check_in(
            hackathon, request.user, user_id=request.data.get('user_id'), token=request.data.get('token'),
        )
        user = record.user
        return DRFResponse({
            'created': created, 'user_id': user.id, 'name': f'{user.first_name} {user.last_name}'.strip() or user.email,
            'team_name': record.team.name, 'checked_in_at': record.checked_in_at,
        })


class CheckInDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, slug, user_id):
        CheckInService.undo(_get_hackathon(request, slug), request.user, user_id)
        return DRFResponse({'deleted': True, 'user_id': user_id})


# ---------------------------------------------------------------------------
# Organizer: submissions, certificates, analytics, exports, cloning
# ---------------------------------------------------------------------------

class AdminSubmissionsView(APIView):
    """GET (organizer) /api/hackathons/{slug}/submissions/ - every submission plus the registered teams that have none."""
    permission_classes = [scoped('submissions')]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        missing = SubmissionService.teams_without_submission(hackathon).values('id', 'name')
        return DRFResponse({
            'state': window_state(hackathon), 'rows': analytics.submission_rows(hackathon), 'missing': list(missing),
        })


class SubmissionRemindView(APIView):
    permission_classes = [scoped('submissions')]

    def post(self, request, slug):
        return DRFResponse({'reminded': SubmissionService.remind_missing(_get_hackathon(request, slug), request.user)})


class AdminCertificatesView(APIView):
    """GET (organizer) /api/hackathons/{slug}/certificates/ - how many are eligible and a preview of the wording."""
    permission_classes = [scoped('results')]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        return DRFResponse({
            'eligible': certificates.eligible_count(hackathon),
            'preview': certificates.preview(hackathon, request.user),
        })


class ParticipantsView(APIView):
    """GET (organizer) /api/hackathons/{slug}/participants/ - flat rows for CSV export."""
    permission_classes = [scoped('teams')]

    def get(self, request, slug):
        return DRFResponse(analytics.participants_rows(_get_hackathon(request, slug)))


class AnalyticsView(APIView):
    permission_classes = [scoped('insights')]

    def get(self, request, slug):
        return DRFResponse(analytics.overview(_get_hackathon(request, slug)))


class CloneView(APIView):
    """POST (organizer) /api/hackathons/{slug}/clone/ {title, slug?, start_date, copy_problem_statements?}"""
    permission_classes = [IsAdminOrClubLead]

    def post(self, request, slug):
        from django.utils.dateparse import parse_datetime

        source = _get_hackathon(request, slug)
        start = parse_datetime(str(request.data.get('start_date') or ''))
        if start is None:
            raise HackathonError('Give the new start date and time.', 'INVALID', field='start_date')
        if timezone.is_naive(start):
            start = timezone.make_aware(start)
        clone = CloneService.clone(
            source, request.user, title=request.data.get('title', ''), slug=request.data.get('slug', ''),
            start_date=start, copy_problem_statements=bool(request.data.get('copy_problem_statements')),
        )
        return DRFResponse({'slug': clone.slug, 'title': clone.title}, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Who am I here? (drives which tabs and buttons the UI shows)
# ---------------------------------------------------------------------------

class MyPermissionsView(APIView):
    """GET /api/hackathons/{slug}/my-permissions/ - my roles and permissions, plus the catalog for the roles editor."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        mine = access.my_permissions(hackathon, request.user)
        return DRFResponse({
            'permissions': mine,
            'is_site_organizer': access.is_organizer(request.user),
            'roles': sorted(access.staff_roles(hackathon, request.user)),
            'catalog': [{'key': key, 'label': label, 'description': text} for key, (label, text) in access.PERMISSIONS.items()],
            'presets': {role: access.preset_permissions(role) for role in access.ROLE_PRESETS},
        })


# ---------------------------------------------------------------------------
# Phase 1: ideas (plan) - participants
# ---------------------------------------------------------------------------

def _idea_payload(idea: IdeaSubmission, *, payment: bool = False) -> dict:
    """The idea as people see it. The payment screenshot and transaction id are added only when ``payment`` is set (the team, organizers)."""
    data = {
        'id': idea.id, 'team_id': idea.team_id, 'team_name': idea.team.name, 'title': idea.title, 'problem': idea.problem,
        'solution': idea.solution, 'plan': idea.plan, 'tech_stack': idea.tech_stack, 'deck_url': idea.deck_url,
        'linkedin_url': idea.linkedin_url, 'deck_file': idea_files.serialize(idea.deck_file),
        'status': idea.status, 'feedback': idea.feedback, 'reviewed_at': idea.reviewed_at,
        'score': idea.score, 'review_count': idea.review_count,
        'created_at': idea.created_at, 'updated_at': idea.updated_at,
    }
    if payment:
        data['payment_proof'] = idea_files.serialize(idea.payment_proof)
        data['transaction_id'] = idea.transaction_id
    return data


def _idea_config(hackathon) -> dict:
    """What the team's idea form needs to know about the organizers' choices."""
    return {
        'fee_amount': hackathon.idea_fee_amount, 'payment_note': hackathon.idea_payment_note,
        'deck_required': hackathon.idea_deck_required, 'linkedin_required': hackathon.idea_linkedin_required,
        'files_available': idea_files.available(), 'payment_qr': idea_files.serialize_qr(hackathon),
        'edits_enabled': hackathon.idea_edits_enabled, 'edits_until': hackathon.idea_edits_until,
    }


def _idea_related(qs):
    return qs.select_related('team', 'deck_file', 'payment_proof')


class MyIdeaView(APIView):
    """GET / PUT /api/hackathons/{slug}/my-idea/ - the team's idea (phase 1: plan)."""
    permission_classes = [permissions.IsAuthenticated]

    def _state(self, hackathon, team):
        idea = _idea_related(IdeaSubmission.objects).filter(team=team).first() if team else None
        state = idea_window_state(hackathon)
        editable, _, why_not = can_team_edit(hackathon, idea)
        return {
            'state': state, 'opens_at': hackathon.idea_opens_at, 'deadline': hackathon.idea_deadline,
            'review_required': hackathon.idea_review_required, 'project_requires_idea': hackathon.project_requires_approved_idea,
            'idea': _idea_payload(idea, payment=True) if idea else None, 'config': _idea_config(hackathon),
            'can_edit': bool(team and team.leader_id == self.request.user.id and editable
                             and team.status == TeamStatus.REGISTERED and not team.is_waitlisted),
            'edit_blocked_reason': why_not if (team and team.leader_id == self.request.user.id and idea) else '',
        }

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        return DRFResponse(self._state(hackathon, _my_team(hackathon, request.user)))

    def put(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        team = _my_team(hackathon, request.user)
        if team is None:
            raise HackathonError('Join or create a team before submitting an idea.', 'NO_TEAM')
        IdeaService.save(team, request.user, request.data)
        return DRFResponse(self._state(hackathon, team))


class MyIdeaFilesView(APIView):
    """POST /api/hackathons/{slug}/my-idea/files/ {kind: DECK|PAYMENT, name, size, type} - ask for a signed upload form."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'form_upload'

    def post(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        team = _my_team(hackathon, request.user)
        if team is None:
            raise HackathonError('Join or create a team before submitting an idea.', 'NO_TEAM')
        data = request.data
        granted = idea_files.request_upload(team, request.user, data.get('kind'), data.get('name'), data.get('size'), data.get('type'))
        return DRFResponse({
            'file': idea_files.serialize(granted['file']), 'post': granted['post'],
            'max_bytes': granted['max_bytes'], 'expires_in': granted['expires_in'],
        }, status=status.HTTP_201_CREATED)


class MyIdeaFileView(APIView):
    """POST .../my-idea/files/{id}/complete/ confirms a finished upload; DELETE .../my-idea/files/{id}/ removes one not yet saved."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'form_upload'

    def _team(self, request, slug):
        team = _my_team(_get_hackathon(request, slug), request.user)
        if team is None:
            raise HackathonError('Join or create a team before submitting an idea.', 'NO_TEAM')
        return team

    def post(self, request, slug, file_id):
        file = idea_files.complete_upload(self._team(request, slug), request.user, file_id)
        return DRFResponse({'file': idea_files.serialize(file)})

    def delete(self, request, slug, file_id):
        idea_files.discard_upload(self._team(request, slug), request.user, file_id)
        return DRFResponse(status=status.HTTP_204_NO_CONTENT)


class IdeaQrView(APIView):
    """
    The QR code teams scan to pay (organizers with the settings permission).
    POST /api/hackathons/{slug}/idea-qr/ {name, size, type} - ask for a signed upload form;
    DELETE /api/hackathons/{slug}/idea-qr/ - remove the current one.
    """
    permission_classes = [scoped('settings')]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'form_upload'

    def post(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        granted = idea_files.request_qr_upload(hackathon, request.user, request.data.get('name'), request.data.get('size'))
        return DRFResponse({
            'file': idea_files.serialize(granted['file']), 'post': granted['post'],
            'max_bytes': granted['max_bytes'], 'expires_in': granted['expires_in'],
        }, status=status.HTTP_201_CREATED)

    def delete(self, request, slug):
        idea_files.remove_qr(_get_hackathon(request, slug), request.user)
        return DRFResponse(status=status.HTTP_204_NO_CONTENT)


class IdeaQrFileView(APIView):
    """POST /api/hackathons/{slug}/idea-qr/{id}/ - the upload arrived: check it and make it the payment QR code."""
    permission_classes = [scoped('settings')]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'form_upload'

    def post(self, request, slug, file_id):
        hackathon = _get_hackathon(request, slug)
        file = idea_files.complete_qr_upload(hackathon, request.user, file_id)
        return DRFResponse({'file': idea_files.serialize(file), 'payment_qr': idea_files.serialize_qr(hackathon)})


class IdeaQrLinkView(APIView):
    """GET /api/hackathons/{slug}/idea-qr/link/ - a 60-second link to the QR image: organizers and registered team members only."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'form_upload'

    def get(self, request, slug):
        return DRFResponse(idea_files.qr_link(_get_hackathon(request, slug), request.user))


class IdeaFileLinkView(APIView):
    """GET /api/hackathons/{slug}/ideas/{id}/files/{deck|payment}/link/?inline=1 - a 60-second link. Payment proof: organizers and the team only."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'form_upload'

    def get(self, request, slug, pk, kind):
        hackathon = _get_hackathon(request, slug)
        idea = _idea_related(IdeaSubmission.objects).filter(pk=pk, team__hackathon=hackathon).first()
        if idea is None:
            raise Http404
        return DRFResponse(idea_files.link(hackathon, request.user, idea, kind.upper(), inline=request.query_params.get('inline') == '1'))


# ---------------------------------------------------------------------------
# Phase 1: ideas - organizers
# ---------------------------------------------------------------------------

class AdminIdeasView(APIView):
    """GET /api/hackathons/{slug}/ideas/ - every idea with its status, plus registered teams with none."""
    permission_classes = [scoped('ideas')]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        ideas = _idea_related(IdeaSubmission.objects).filter(team__hackathon=hackathon).order_by('status', 'team__name')
        rows = [{**_idea_payload(idea, payment=True), 'team_status': idea.team.status} for idea in ideas]
        missing = IdeaService.teams_without_idea(hackathon).values('id', 'name')
        counts = {value: 0 for value in IdeaStatus.values}
        for row in rows:
            counts[row['status']] += 1
        return DRFResponse({'state': idea_window_state(hackathon), 'rows': rows, 'missing': list(missing), 'counts': counts, 'config': _idea_config(hackathon)})


class IdeaReviewView(APIView):
    """POST /api/hackathons/{slug}/ideas/{id}/review/ {status: APPROVED|CHANGES_REQUESTED|REJECTED, feedback}"""
    permission_classes = [scoped('ideas')]

    def post(self, request, slug, pk):
        hackathon = _get_hackathon(request, slug)
        idea = _idea_related(IdeaSubmission.objects).filter(pk=pk, team__hackathon=hackathon).first()
        if idea is None:
            raise Http404
        IdeaService.review(idea, request.user, request.data.get('status', ''), request.data.get('feedback', ''))
        return DRFResponse(_idea_payload(idea, payment=True))


class IdeaRemindView(APIView):
    permission_classes = [scoped('ideas')]

    def post(self, request, slug):
        return DRFResponse({'reminded': IdeaService.remind_missing(_get_hackathon(request, slug), request.user)})


# ---------------------------------------------------------------------------
# Judging ideas (the idea rubric)
# ---------------------------------------------------------------------------

class JudgingIdeasListView(APIView):
    """GET /api/hackathons/{slug}/judging/ideas/ - the idea rubric, every idea and this judge's own scores."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        if not can_judge(hackathon, request.user):
            raise PermissionDenied('You are not a judge for this hackathon.')
        mine = {r.idea_id: r for r in IdeaReview.objects.filter(idea__team__hackathon=hackathon, judge=request.user)}
        own_team = _my_team(hackathon, request.user)
        rows = []
        for idea in (
            _idea_related(IdeaSubmission.objects).filter(team__hackathon=hackathon, team__is_waitlisted=False)
            .exclude(team__status__in=(TeamStatus.DISQUALIFIED, TeamStatus.WITHDRAWN)).order_by('team__name')
        ):
            review = mine.get(idea.id)
            rows.append({
                **_idea_payload(idea), 'is_own_team': own_team is not None and own_team.id == idea.team_id,
                'my_review': {'scores': review.scores, 'comment': review.comment, 'total': review.total} if review else None,
            })
        return DRFResponse({
            'enabled': hackathon.feature('judging') and hackathon.feature('ideas'),
            'criteria': _criteria_payload(hackathon, 'IDEA'), 'ideas': rows, 'reviewed': len(mine), 'total': len(rows),
        })


class JudgingIdeaReviewView(APIView):
    """POST /api/hackathons/{slug}/judging/ideas/{idea_id}/ {scores, comment}"""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug, pk):
        hackathon = _get_hackathon(request, slug)
        idea = IdeaSubmission.objects.select_related('team').filter(pk=pk, team__hackathon=hackathon).first()
        if idea is None:
            raise Http404
        review = JudgingService.review_idea(idea, request.user, request.data.get('scores'), request.data.get('comment', ''))
        return DRFResponse({'scores': review.scores, 'comment': review.comment, 'total': review.total})


class IdeaReviewsView(APIView):
    """GET (organizer) /api/hackathons/{slug}/ideas/{id}/reviews/ - every judge's scores for one idea."""
    permission_classes = [scoped('judging')]

    def get(self, request, slug, pk):
        hackathon = _get_hackathon(request, slug)
        if not IdeaSubmission.objects.filter(pk=pk, team__hackathon=hackathon).exists():
            raise Http404
        reviews = IdeaReview.objects.filter(idea_id=pk).select_related('judge')
        return DRFResponse([
            {
                'id': r.id, 'judge': f'{r.judge.first_name} {r.judge.last_name}'.strip() or r.judge.email,
                'scores': r.scores, 'comment': r.comment, 'total': r.total, 'updated_at': r.updated_at,
            }
            for r in reviews
        ])
