"""
The API for judging a round: what the organizer sets up and watches, what the judge sees and scores, and judge invitations.

Organizer routes need the ``judging`` permission (the invitations need ``staff``). Judge routes need only a signed-in judge who sits
on the round's panel; every query is limited to the teams that judge was given, so another team is simply "not found".
"""
from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import round_judging
from .access import scoped
from .errors import HackathonError
from .judge_invites import JudgeInviteService
from .round_judging import RoundJudgingService
from .views import _get_hackathon


def _round(request, slug, pk):
    """The hackathon (visible to this person) and one of its rounds, loaded fresh."""
    hackathon = _get_hackathon(request, slug)
    return hackathon, get_object_or_404(hackathon.rounds.select_related('hackathon'), pk=pk)


# ---------------------------------------------------------------------------------------------------------------------------
# Organizer
# ---------------------------------------------------------------------------------------------------------------------------

class RoundEvaluationView(APIView):
    """GET /rounds/{id}/evaluation/ - how the round is judged and whether it can start; PATCH changes the settings."""
    permission_classes = [scoped('judging')]

    def get(self, request, slug, pk):
        _, round_obj = _round(request, slug, pk)
        return Response(round_judging.evaluation_payload(round_obj))

    def patch(self, request, slug, pk):
        _, round_obj = _round(request, slug, pk)
        RoundJudgingService.configure(round_obj, request.user, request.data)
        round_obj.refresh_from_db()
        return Response(round_judging.evaluation_payload(round_obj))


class RoundEvaluationCriteriaView(APIView):
    """PUT /rounds/{id}/evaluation/criteria/ {criteria: [...]} - the round's own rubric; an empty list means the hackathon rubric."""
    permission_classes = [scoped('judging')]

    def put(self, request, slug, pk):
        _, round_obj = _round(request, slug, pk)
        RoundJudgingService.set_criteria(round_obj, request.user, request.data.get('criteria'))
        round_obj.refresh_from_db()
        return Response(round_judging.evaluation_payload(round_obj))


class RoundEvaluationPanelView(APIView):
    """PUT /rounds/{id}/evaluation/panel/ {judge_ids: [...]} - the judges of this round."""
    permission_classes = [scoped('judging')]

    def put(self, request, slug, pk):
        _, round_obj = _round(request, slug, pk)
        RoundJudgingService.set_panel(round_obj, request.user, request.data.get('judge_ids'))
        round_obj.refresh_from_db()
        return Response(round_judging.evaluation_payload(round_obj))


class RoundEvaluationAssignView(APIView):
    """POST /rounds/{id}/evaluation/assign/ {manual?: [{entry_id, judge_ids}]} - share the teams out."""
    permission_classes = [scoped('judging')]

    def post(self, request, slug, pk):
        _, round_obj = _round(request, slug, pk)
        result = RoundJudgingService.assign(round_obj, request.user, request.data.get('manual'))
        return Response({'result': result, 'progress': RoundJudgingService.progress(round_obj)})


class RoundEvaluationActionView(APIView):
    """POST /rounds/{id}/evaluation/{start|close|reopen}/ - open scoring, close it into a ranking, or open it again."""
    permission_classes = [scoped('judging')]

    def post(self, request, slug, pk, action):
        _, round_obj = _round(request, slug, pk)
        data = request.data
        result = {}
        if action == 'start':
            RoundJudgingService.start(round_obj, request.user)
        elif action == 'close':
            result = RoundJudgingService.close(round_obj, request.user, force=bool(data.get('force')), reason=data.get('reason', ''))
        elif action == 'reopen':
            RoundJudgingService.reopen(round_obj, request.user, data.get('reason', ''))
        else:
            raise HackathonError('Unknown action.', 'NOT_FOUND', status=404)
        round_obj.refresh_from_db()
        return Response({**result, 'evaluation': round_judging.evaluation_payload(round_obj)})


class RoundEvaluationProgressView(APIView):
    """GET /rounds/{id}/evaluation/progress/ - the judge-by-team grid."""
    permission_classes = [scoped('judging')]

    def get(self, request, slug, pk):
        _, round_obj = _round(request, slug, pk)
        return Response(RoundJudgingService.progress(round_obj))


class RoundEvaluationResultsView(APIView):
    """GET /rounds/{id}/evaluation/results/ - every team's score and rank with each judge's total (organizers only)."""
    permission_classes = [scoped('judging')]

    def get(self, request, slug, pk):
        _, round_obj = _round(request, slug, pk)
        return Response({'status': round_obj.evaluation_status, 'rows': RoundJudgingService.results(round_obj), 'panel': round_judging.evaluation_payload(round_obj)['panel']})


# ---------------------------------------------------------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------------------------------------------------------

class MyJudgingView(APIView):
    """GET /my-judging/ - the judged rounds this person is a judge of."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        return Response({'enabled': hackathon.feature('round_judging'), 'rounds': round_judging.judge_rounds(hackathon, request.user)})


class MyJudgingRoundView(APIView):
    """GET /my-judging/{round}/ - the rubric and the teams this judge was given."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug, pk):
        _, round_obj = _round(request, slug, pk)
        return Response(round_judging.judge_round(round_obj, request.user))


class MyJudgingTeamView(APIView):
    """GET /my-judging/{round}/teams/{entry}/ - read a team; PUT {scores: {criterion_id: n}, comment} scores it."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug, pk, entry_id):
        _, round_obj = _round(request, slug, pk)
        return Response(round_judging.judge_team(round_obj, entry_id, request.user))

    def put(self, request, slug, pk, entry_id):
        _, round_obj = _round(request, slug, pk)
        review = RoundJudgingService.submit_review(round_obj, entry_id, request.user, request.data.get('scores'), request.data.get('comment', ''))
        return Response({'scores': review.scores, 'comment': review.comment, 'total': review.total, 'submitted_at': review.submitted_at})


class MyJudgingRecuseView(APIView):
    """POST /my-judging/{round}/teams/{entry}/recuse/ {reason} - step aside from a team."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug, pk, entry_id):
        _, round_obj = _round(request, slug, pk)
        return Response(RoundJudgingService.recuse(round_obj, entry_id, request.user, request.data.get('reason', '')))


# ---------------------------------------------------------------------------------------------------------------------------
# Inviting judges
# ---------------------------------------------------------------------------------------------------------------------------

class JudgeInvitesView(APIView):
    """GET /judge-invites/ - every invitation; POST {name, email} or {judges: [{name, email}]} invites one or a list."""
    permission_classes = [scoped('staff')]

    def get(self, request, slug):
        return Response(JudgeInviteService.list(_get_hackathon(request, slug)))

    def post(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        if 'judges' in request.data:
            return Response({'results': JudgeInviteService.invite_many(hackathon, request.user, request.data.get('judges'))})
        invite = JudgeInviteService.invite(hackathon, request.user, request.data.get('name'), request.data.get('email'), request.data.get('permissions'))
        return Response(invite, status=status.HTTP_201_CREATED)


class JudgeInviteResendView(APIView):
    """POST /judge-invites/{id}/resend/ - a fresh set-password link for someone who has not set one up."""
    permission_classes = [scoped('staff')]

    def post(self, request, slug, pk):
        return Response(JudgeInviteService.resend(_get_hackathon(request, slug), request.user, pk))


class JudgeInviteDetailView(APIView):
    """DELETE /judge-invites/{id}/ - revoke: the judge is removed from this hackathon and any unused link stops working."""
    permission_classes = [scoped('staff')]

    def delete(self, request, slug, pk):
        return Response(JudgeInviteService.revoke(_get_hackathon(request, slug), request.user, pk))
