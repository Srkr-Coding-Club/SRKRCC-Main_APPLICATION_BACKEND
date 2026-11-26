"""
Idea review linked to a round (``Hackathon.idea_round``).

When a hackathon links a round, reviewing an idea sets the team's result in that round: approved = shortlisted,
rejected = not shortlisted, anything else = pending. An approval or rejection decided while the linked round is
unpublished is held back from the team (``IdeaSubmission.result_held``) until that round is published, so every result
reaches the teams together; "changes requested" is shown at once, because the team has to act on it. A result the team
was already told is never hidden again (linking a round later, or unpublishing it, does not take it back). Organizers
and judges always see the real status.

The link is one way: changing a result in the Rounds tab does not change the idea.
"""
from __future__ import annotations

from django.db import transaction

from . import cohorts
from .errors import HackathonError
from .models import EntryStatus, Hackathon, IdeaStatus, IdeaSubmission, Round, RoundEntry

ENTRY_FOR = {IdeaStatus.APPROVED: EntryStatus.SHORTLISTED, IdeaStatus.REJECTED: EntryStatus.REJECTED}
FINAL = tuple(ENTRY_FOR)
REVIEW_WORDING = {
    IdeaStatus.APPROVED: 'was approved. You can move on to the project phase.',
    IdeaStatus.CHANGES_REQUESTED: 'needs changes. Update it and resubmit before the deadline.',
    IdeaStatus.REJECTED: 'was not accepted.',
}


def linked_round(hackathon: Hackathon) -> Round | None:
    return hackathon.idea_round if hackathon.idea_round_id else None


def should_hold(idea: IdeaSubmission) -> bool:
    """Whether a decision made now has to wait: a final outcome while the linked round is unpublished."""
    round_obj = linked_round(idea.team.hackathon)
    return round_obj is not None and not round_obj.results_published and idea.status in FINAL


def is_held(idea: IdeaSubmission) -> bool:
    """The team must not see this outcome yet: it was decided while held and the round has not been published since."""
    return bool(idea.result_held) and idea.status in FINAL


def team_view(idea: IdeaSubmission) -> dict:
    """The status and feedback as the team sees them: a held outcome reads as still under review."""
    if is_held(idea):
        return {'status': IdeaStatus.SUBMITTED, 'feedback': '', 'reviewed_at': None, 'result_held': True}
    return {'status': idea.status, 'feedback': idea.feedback, 'reviewed_at': idea.reviewed_at, 'result_held': False}


def sync(idea: IdeaSubmission, actor, *, confirm_published=False, confirm_over_limit=False) -> None:
    """Set the team's result in the linked round from its idea's status, when it differs. Does nothing without a link."""
    round_obj = linked_round(idea.team.hackathon)
    if round_obj is None:
        return
    wanted = ENTRY_FOR.get(idea.status, EntryStatus.PENDING)
    current = round_obj.entries.filter(team_id=idea.team_id).values_list('status', flat=True).first()
    if current == wanted:
        return  # nothing changes: no "your result was updated" notice, no overwritten decision
    _decide(round_obj, actor, [idea.team_id], wanted, confirm_published=confirm_published, confirm_over_limit=confirm_over_limit)


def copy_plan(hackathon: Hackathon, round_obj: Round) -> dict[str, list[int]]:
    """Which teams linking would shortlist or mark not shortlisted: the competing teams whose idea is already decided."""
    plan: dict[str, list[int]] = {EntryStatus.SHORTLISTED: [], EntryStatus.REJECTED: []}
    ideas = IdeaSubmission.objects.filter(cohorts.competing_q('team__'), team__hackathon=hackathon, status__in=FINAL)
    for team_id, status in ideas.values_list('team_id', 'status'):
        plan[ENTRY_FOR[status]].append(team_id)
    return plan


@transaction.atomic
def link(hackathon: Hackathon, round_id, actor, *, preview=False, confirm_published=False, confirm_over_limit=False) -> dict:
    """Link (or, with ``round_id`` empty, unlink) a round, copying the ideas already decided into it.

    ``preview`` reports what would be copied without changing anything. Copying into a published round, or past the
    round's target, needs the same confirmations as deciding by hand. Results held for the previous link are released
    to the teams, since nothing would publish them any more.
    """
    from apps.audit.utils import log_audit_event

    if not round_id:
        if not preview and hackathon.idea_round_id:
            hackathon.idea_round = None
            hackathon.save(update_fields=['idea_round', 'updated_at'])
            release(hackathon, actor)
            log_audit_event(actor=actor, action='Unlinked Idea Review Round', target_model='Hackathon', target_id=hackathon.slug, details={})
        return {'round': None, 'shortlisted': 0, 'rejected': 0, 'published': False}
    round_obj = hackathon.rounds.filter(pk=round_id).first()
    if round_obj is None:
        raise HackathonError('Pick a round of this hackathon.', 'ROUND_INVALID', field='round')
    plan = copy_plan(hackathon, round_obj)
    result = {
        'round': round_obj.pk, 'shortlisted': len(plan[EntryStatus.SHORTLISTED]), 'rejected': len(plan[EntryStatus.REJECTED]),
        'published': round_obj.results_published,
    }
    if preview:
        return result
    if hackathon.idea_round_id and hackathon.idea_round_id != round_obj.pk:
        release(hackathon, actor)
    hackathon.idea_round = round_obj
    hackathon.save(update_fields=['idea_round', 'updated_at'])
    for status, team_ids in plan.items():
        if team_ids:
            _decide(round_obj, actor, team_ids, status, confirm_published=confirm_published, confirm_over_limit=confirm_over_limit)
    log_audit_event(
        actor=actor, action='Linked Idea Review Round', target_model='Hackathon', target_id=hackathon.slug,
        details={'round': round_obj.name, 'shortlisted': result['shortlisted'], 'rejected': result['rejected']},
    )
    return result


def reveal(round_obj: Round, actor=None) -> None:
    """The linked round was published: tell the teams whose outcome was held, as the review would have."""
    hackathon = round_obj.hackathon
    if hackathon.idea_round_id != round_obj.pk:
        return
    release(hackathon, actor)


def release(hackathon: Hackathon, actor=None) -> int:
    """Show every held outcome to its team now, with the review's notification and, for an approval, its celebration."""
    from . import celebrate
    from .services import _notify_users

    held = list(
        IdeaSubmission.objects.select_for_update().filter(team__hackathon=hackathon, result_held=True).select_related('team__hackathon'),
    )
    if not held:
        return 0
    IdeaSubmission.objects.filter(pk__in=[idea.pk for idea in held]).update(result_held=False)
    for idea in held:
        idea.result_held = False
        if idea.status not in FINAL:
            continue
        if idea.status == IdeaStatus.APPROVED:
            celebrate.idea_approved(idea)
        members = [m.user for m in idea.team.memberships.select_related('user')]
        message = f"Your idea '{idea.title}' {REVIEW_WORDING[idea.status]}" + (f' Note: {idea.feedback}' if idea.feedback else '')
        transaction.on_commit(lambda members=members, team=idea.team, message=message: _notify_users(
            members, f'{team.name}: idea reviewed', message, hackathon, actor,
        ))
    return len(held)


def _decide(round_obj: Round, actor, team_ids: list[int], status: str, **confirm) -> None:
    from .services import RoundService

    missing = set(team_ids) - set(round_obj.entries.filter(team_id__in=team_ids).values_list('team_id', flat=True))
    if missing:
        from .round_judging import RoundJudgingService
        RoundEntry.objects.bulk_create([RoundEntry(round=round_obj, team_id=team_id) for team_id in missing], ignore_conflicts=True)
        RoundJudgingService.on_entries_added(round_obj)
    RoundService.decide(round_obj, actor, team_ids, status, **confirm)
