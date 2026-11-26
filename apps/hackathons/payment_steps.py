"""
Payment steps: fees the teams shortlisted in a round pay once that round's results are published (e.g. a phase 2 fee).

Each step charges per member, with a lower fee for club affiliates (a Club ID and an active membership). Only the team
leader pays: outside the platform, then a screenshot and the transaction id here. An organizer verifies or rejects it;
after a rejection the leader sees the reason and can pay again. While a payment is waiting or verified the team's
members are locked (organizers can still change them), so the amount always matches the team. A step can be required
before project submission.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.audit.utils import log_audit_event
from apps.forms.models import UploadState

from . import access, cohorts, idea_files
from .errors import HackathonError
from .models import (
    EntryStatus, Hackathon, IdeaFile, IdeaFileKind, PaymentStatus, PaymentStep, StepPayment, Team, TeamMember,
    TeamPayment, payment_key,
)

MAX_NAME = 120
MAX_NOTE = 2000
MAX_TRANSACTION_ID = 60
MAX_REASON = 300
MAX_FEE = 1_000_000
#: A payment in one of these holds the team's members and blocks paying again.
OPEN_STATUSES = (PaymentStatus.PENDING, PaymentStatus.VERIFIED)
#: Registration payments that no longer hold their transaction id.
_CLOSED_TEAM_PAYMENT = (PaymentStatus.CANCELLED, PaymentStatus.REMOVED)


# --- who pays what ----------------------------------------------------------------------------------------------

def is_affiliate(user) -> bool:
    """A club affiliate pays the lower fee: has a Club ID and an active membership, whatever their role."""
    from apps.accounts.models import MembershipStatus
    return bool(user and (user.club_id or '').strip() and user.membership_status == MembershipStatus.ACTIVE)


def _name(user) -> str:
    return (f'{user.first_name} {user.last_name}'.strip() or user.email) if user else ''


def quote(step: PaymentStep, team: Team) -> dict:
    """What the team owes for this step right now: each member with their fee, and the total."""
    members = []
    memberships = sorted(team.memberships.all(), key=lambda m: (m.joined_at, m.pk))
    for membership in memberships:
        affiliate = is_affiliate(membership.user)
        members.append({
            'user_id': membership.user_id, 'name': _name(membership.user), 'affiliate': affiliate,
            'fee': step.affiliate_fee_per_person if affiliate else step.fee_per_person,
        })
    return {'members': members, 'total': sum(m['fee'] for m in members)}


def eligible_teams(step: PaymentStep):
    """The teams this step asks to pay: shortlisted in its round, once the round's results are published."""
    if not step.is_active or step.round_id is None or not step.round.results_published:
        return Team.objects.none()
    return cohorts.competing(step.hackathon).filter(
        round_entries__round=step.round, round_entries__status=EntryStatus.SHORTLISTED,
    ).distinct()


def is_due(step: PaymentStep, team: Team) -> bool:
    """The team has to pay this step: it is eligible and owes more than nothing (a ₹0 total asks nothing)."""
    return eligible_teams(step).filter(pk=team.pk).exists() and quote(step, team)['total'] > 0


def open_payment(step: PaymentStep, team: Team) -> StepPayment | None:
    return step.payments.filter(team=team, status__in=OPEN_STATUSES).first()


def roster_locked(team: Team) -> bool:
    """A payment is waiting or verified, so members may not be added or removed except by an organizer."""
    return StepPayment.objects.filter(team=team, status__in=OPEN_STATUSES).exists()


def project_block(team: Team | None) -> str:
    """Why this team may not submit a project yet because of a payment step, or '' when nothing is owed."""
    if team is None:
        return ''
    for step in team.hackathon.payment_steps.filter(is_active=True, required_for_projects=True).select_related('round'):
        if not is_due(step, team):
            continue
        payment = open_payment(step, team)
        if payment is None:
            return f'Pay the {step.name} first: your team leader pays it from the team dashboard.'
        if payment.status == PaymentStatus.PENDING:
            return f'Your {step.name} payment is waiting for the organizers to check it.'
    return ''


# --- the team's view and the leader paying ----------------------------------------------------------------------

def _payment_for_team(payment: StepPayment | None) -> dict | None:
    if payment is None:
        return None
    return {
        'id': payment.id, 'status': payment.status, 'amount': payment.amount, 'breakdown': payment.breakdown,
        'transaction_id': payment.transaction_id, 'reason': payment.reason if payment.status == PaymentStatus.REJECTED else '',
        'submitted_at': payment.created_at, 'reviewed_at': payment.reviewed_at,
    }


def my_steps(hackathon: Hackathon, user) -> list[dict]:
    """The steps the person's team has to pay, with the amount, how to pay and where its payment stands."""
    membership = TeamMember.objects.filter(hackathon=hackathon, user=user).select_related('team').first()
    if membership is None:
        return []
    team = membership.team
    result = []
    for step in hackathon.payment_steps.filter(is_active=True).select_related('round', 'qr'):
        if not is_due(step, team):
            continue
        latest = step.payments.filter(team=team).first()
        open_now = latest if latest is not None and latest.status in OPEN_STATUSES else None
        result.append({
            'id': step.id, 'name': step.name, 'round_name': step.round.name, 'deadline': step.deadline,
            'payment_note': step.payment_note, 'has_qr': bool(step.qr_id and step.qr.state == UploadState.ATTACHED),
            'required_for_projects': step.required_for_projects,
            'fee_per_person': step.fee_per_person, 'affiliate_fee_per_person': step.affiliate_fee_per_person,
            'quote': quote(step, team), 'payment': _payment_for_team(latest),
            'is_leader': team.leader_id == user.id, 'can_pay': team.leader_id == user.id and open_now is None,
        })
    return result


def _step(hackathon: Hackathon, step_id) -> PaymentStep:
    step = hackathon.payment_steps.select_related('round', 'qr').filter(pk=step_id).first()
    if step is None:
        raise HackathonError('That payment step was not found.', 'NOT_FOUND', status=404)
    return step


def _leader_team(hackathon: Hackathon, step: PaymentStep, user) -> Team:
    membership = TeamMember.objects.filter(hackathon=hackathon, user=user).select_related('team').first()
    team = membership.team if membership else None
    if team is None or not is_due(step, team):
        raise HackathonError('Your team does not have to pay this.', 'NOT_DUE', status=404)
    if team.leader_id != user.id:
        raise PermissionDenied('Only the team leader can pay.')
    return team


def _require_can_pay(step: PaymentStep, team: Team) -> None:
    if open_payment(step, team) is not None:
        raise HackathonError('A payment for this is already waiting or verified.', 'ALREADY_PAID', status=409)


def request_upload(hackathon: Hackathon, step_id, user, name, size) -> dict:
    step = _step(hackathon, step_id)
    team = _leader_team(hackathon, step, user)
    _require_can_pay(step, team)
    return idea_files._issue(hackathon, team, user, IdeaFileKind.STEP_PAYMENT, name, size)


def complete_upload(hackathon: Hackathon, step_id, user, file_id) -> IdeaFile:
    step = _step(hackathon, step_id)
    team = _leader_team(hackathon, step, user)
    file = idea_files._verify(hackathon, team, file_id)
    if file.kind != IdeaFileKind.STEP_PAYMENT or file.uploader_id != user.id:
        raise HackathonError('That upload was not found.', 'NOT_FOUND', status=404)
    return file


def _reference_taken(hackathon: Hackathon, key: str) -> bool:
    return (
        StepPayment.objects.filter(hackathon=hackathon, transaction_key=key, status__in=OPEN_STATUSES).exists()
        or TeamPayment.objects.filter(hackathon=hackathon, transaction_key=key).exclude(status__in=_CLOSED_TEAM_PAYMENT).exists()
    )


@transaction.atomic
def pay(hackathon: Hackathon, step_id, user, data: dict) -> StepPayment:
    """The leader records the payment: the amount is worked out again here and kept with it."""
    from .services import _lock_team, _notify_after_commit

    step = _step(hackathon, step_id)
    team = _lock_team(_leader_team(hackathon, step, user))
    _require_can_pay(step, team)
    data = data or {}
    reference = ' '.join(str(data.get('transaction_id') or '').split())
    if not reference:
        raise HackathonError('Enter the transaction ID of your payment.', 'INVALID', field='transaction_id')
    if len(reference) > MAX_TRANSACTION_ID:
        raise HackathonError(f'The transaction ID is limited to {MAX_TRANSACTION_ID} characters.', 'INVALID', field='transaction_id')
    if _reference_taken(hackathon, payment_key(reference)):
        raise HackathonError('This transaction ID has already been used. Check it, or contact the club representatives.', 'TRANSACTION_REUSED', field='transaction_id')
    try:
        file = IdeaFile.objects.select_for_update().get(
            pk=data.get('payment_file_id'), hackathon=hackathon, team=team, kind=IdeaFileKind.STEP_PAYMENT, uploader=user,
            state=UploadState.UPLOADED,
        )
    except Exception:  # noqa: BLE001 - missing, unknown, malformed or not finished uploading
        raise HackathonError('Upload the screenshot of your payment.', 'INVALID', field='payment_file_id')
    owed = quote(step, team)
    try:
        with transaction.atomic():
            payment = StepPayment.objects.create(
                step=step, hackathon=hackathon, team=team, submitted_by=user, amount=owed['total'], breakdown=owed['members'],
                transaction_id=reference, screenshot=file,
            )
    except IntegrityError:
        raise HackathonError('A payment for this is already waiting, or this transaction ID was already used.', 'ALREADY_PAID', status=409)
    idea_files.attach(file, None)
    log_audit_event(
        actor=user, action='Submitted Step Payment', target_model='StepPayment', target_id=payment.pk,
        details={'hackathon': hackathon.slug, 'step': step.name, 'team': team.name, 'amount': payment.amount, 'reference': reference},
    )
    _notify_after_commit(
        [m.user for m in team.memberships.select_related('user')], f'{team.name}: {step.name} submitted',
        f'Your team leader paid ₹{payment.amount} for {step.name}. The organizers will check it soon. Until then the team members cannot change.',
        hackathon, user,
    )
    return payment


# --- organizers: the steps --------------------------------------------------------------------------------------

def _require(hackathon: Hackathon, user, permission: str) -> None:
    if not access.has_permission(hackathon, user, permission):
        raise PermissionDenied('You are not allowed to do this for this hackathon.')


def _clean(hackathon: Hackathon, data: dict, *, partial: bool) -> dict:
    cleaned: dict = {}

    def given(key):
        return key in data or not partial

    if given('name'):
        name = ' '.join(str(data.get('name') or '').split())
        if not name or len(name) > MAX_NAME:
            raise HackathonError(f'Give the step a name of up to {MAX_NAME} characters.', 'INVALID', field='name')
        cleaned['name'] = name
    if given('round'):
        round_id = data.get('round')
        found = hackathon.rounds.filter(pk=round_id).first() if round_id else None
        if round_id and found is None:
            raise HackathonError('Pick a round of this hackathon.', 'ROUND_INVALID', field='round')
        cleaned['round'] = found
    for key in ('fee_per_person', 'affiliate_fee_per_person'):
        if given(key):
            try:
                value = int(data.get(key))
            except (TypeError, ValueError):
                raise HackathonError('Enter an amount in rupees.', 'INVALID', field=key)
            if value < 0 or value > MAX_FEE:
                raise HackathonError(f'Enter an amount between 0 and {MAX_FEE}.', 'INVALID', field=key)
            cleaned[key] = value
    if given('deadline'):
        raw = data.get('deadline')
        if raw:
            from django.utils.dateparse import parse_datetime
            when = parse_datetime(str(raw))
            if when is None:
                raise HackathonError('That is not a valid date and time.', 'INVALID', field='deadline')
            cleaned['deadline'] = when
        else:
            cleaned['deadline'] = None
    if given('payment_note'):
        note = str(data.get('payment_note') or '').strip()
        if len(note) > MAX_NOTE:
            raise HackathonError(f'The payment note is limited to {MAX_NOTE} characters.', 'INVALID', field='payment_note')
        cleaned['payment_note'] = note
    for key in ('required_for_projects', 'is_active'):
        if key in data:
            cleaned[key] = bool(data.get(key))
    return cleaned


def _require_nothing_waiting(step: PaymentStep) -> None:
    waiting = step.payments.filter(status=PaymentStatus.PENDING).count()
    if waiting:
        raise HackathonError(
            f'{waiting} payment{"s are" if waiting != 1 else " is"} still waiting for a check. Verify or reject them first.',
            'PAYMENTS_WAITING', status=409,
        )


def _check_fees(fee: int, affiliate_fee: int) -> None:
    if affiliate_fee > fee:
        raise HackathonError('The affiliate fee should not be higher than the normal fee.', 'INVALID', field='affiliate_fee_per_person')


def _queue_teams(step: PaymentStep) -> tuple[list[Team], dict[int, StepPayment]]:
    """The teams the queue lists, each with its latest payment: every team that owes the step, plus any team whose
    payment is still waiting although it no longer owes it (unshortlisted, withdrawn, round unpublished or deleted),
    so an organizer can still settle it."""
    due_ids = set(eligible_teams(step).values_list('pk', flat=True))
    waiting_ids = set(step.payments.filter(status=PaymentStatus.PENDING).values_list('team_id', flat=True))
    teams = list(
        Team.objects.filter(pk__in=due_ids | waiting_ids).select_related('leader').prefetch_related('memberships__user'),
    )
    latest: dict[int, StepPayment] = {}
    for payment in step.payments.filter(team__in=teams).select_related('screenshot', 'reviewed_by').order_by('-created_at', '-id'):
        latest.setdefault(payment.team_id, payment)
    owed = [t for t in teams if t.pk in waiting_ids or (t.pk in due_ids and (latest.get(t.pk) or quote(step, t)['total'] > 0))]
    return owed, latest


def _counts(teams: list[Team], latest: dict[int, StepPayment]) -> dict:
    statuses = [latest[t.pk].status if t.pk in latest else None for t in teams]
    return {
        'due': len(teams), 'unpaid': sum(1 for s in statuses if s in (None, PaymentStatus.REJECTED)),
        'pending': statuses.count(PaymentStatus.PENDING), 'verified': statuses.count(PaymentStatus.VERIFIED),
        'rejected': statuses.count(PaymentStatus.REJECTED),
    }


def serialize_step(step: PaymentStep, counts: dict | None = None) -> dict:
    if counts is None:
        counts = _counts(*_queue_teams(step))
    return {
        'id': step.id, 'name': step.name, 'round': step.round_id, 'round_name': step.round.name if step.round_id else None,
        'round_published': bool(step.round_id and step.round.results_published),
        'fee_per_person': step.fee_per_person, 'affiliate_fee_per_person': step.affiliate_fee_per_person,
        'deadline': step.deadline, 'payment_note': step.payment_note, 'required_for_projects': step.required_for_projects,
        'is_active': step.is_active, 'order': step.order, 'qr': idea_files._serialize_hackathon_file(step.qr),
        'counts': counts,
    }


def list_steps(hackathon: Hackathon, user) -> list[dict]:
    _require(hackathon, user, 'teams')
    return [serialize_step(step) for step in hackathon.payment_steps.select_related('round', 'qr')]


@transaction.atomic
def create_step(hackathon: Hackathon, user, data: dict) -> PaymentStep:
    _require(hackathon, user, 'settings')
    cleaned = _clean(hackathon, data or {}, partial=False)
    _check_fees(cleaned['fee_per_person'], cleaned['affiliate_fee_per_person'])
    last = hackathon.payment_steps.order_by('-order').values_list('order', flat=True).first() or 0
    step = PaymentStep.objects.create(hackathon=hackathon, order=last + 1, **cleaned)
    log_audit_event(actor=user, action='Created Payment Step', target_model='PaymentStep', target_id=step.pk,
                    details={'hackathon': hackathon.slug, 'name': step.name, 'round': step.round.name if step.round else None})
    return step


@transaction.atomic
def update_step(hackathon: Hackathon, user, step_id, data: dict) -> PaymentStep:
    _require(hackathon, user, 'settings')
    step = PaymentStep.objects.select_for_update().filter(pk=step_id, hackathon=hackathon).first()
    if step is None:
        raise HackathonError('That payment step was not found.', 'NOT_FOUND', status=404)
    cleaned = _clean(hackathon, data or {}, partial=True)
    _check_fees(cleaned.get('fee_per_person', step.fee_per_person), cleaned.get('affiliate_fee_per_person', step.affiliate_fee_per_person))
    if cleaned.get('is_active') is False and step.is_active:
        _require_nothing_waiting(step)
    for key, value in cleaned.items():
        setattr(step, key, value)
    step.save()
    log_audit_event(actor=user, action='Updated Payment Step', target_model='PaymentStep', target_id=step.pk,
                    details={'hackathon': hackathon.slug, 'name': step.name, 'changed': sorted(cleaned)})
    return step


@transaction.atomic
def delete_step(hackathon: Hackathon, user, step_id) -> str:
    """Delete a step nobody paid yet; one with payments is switched off instead, so the money stays on record."""
    _require(hackathon, user, 'settings')
    step = PaymentStep.objects.select_for_update().filter(pk=step_id, hackathon=hackathon).first()
    if step is None:
        raise HackathonError('That payment step was not found.', 'NOT_FOUND', status=404)
    if step.payments.exists():
        _require_nothing_waiting(step)
        step.is_active = False
        step.save(update_fields=['is_active', 'updated_at'])
        outcome = 'deactivated'
    else:
        if step.qr_id:
            idea_files.attach(None, step.qr)
        step.delete()
        outcome = 'deleted'
    log_audit_event(actor=user, action='Removed Payment Step', target_model='PaymentStep', target_id=step_id,
                    details={'hackathon': hackathon.slug, 'name': step.name, 'outcome': outcome})
    return outcome


# --- organizers: the step's QR code -----------------------------------------------------------------------------

def request_qr_upload(hackathon: Hackathon, user, step_id, name, size) -> dict:
    _require(hackathon, user, 'settings')
    _step(hackathon, step_id)
    return idea_files._issue(hackathon, None, user, IdeaFileKind.STEP_QR, name, size)


@transaction.atomic
def complete_qr_upload(hackathon: Hackathon, user, step_id, file_id) -> PaymentStep:
    _require(hackathon, user, 'settings')
    step = PaymentStep.objects.select_for_update().filter(pk=step_id, hackathon=hackathon).first()
    if step is None:
        raise HackathonError('That payment step was not found.', 'NOT_FOUND', status=404)
    if step.qr_id and str(step.qr_id) == str(file_id):
        return step
    file = idea_files._verify(hackathon, None, file_id)
    if file.kind != IdeaFileKind.STEP_QR:
        raise HackathonError('That upload was not found.', 'NOT_FOUND', status=404)
    idea_files.attach(file, step.qr)
    step.qr = file
    step.save(update_fields=['qr', 'updated_at'])
    return step


@transaction.atomic
def remove_qr(hackathon: Hackathon, user, step_id) -> PaymentStep:
    _require(hackathon, user, 'settings')
    step = PaymentStep.objects.select_for_update().filter(pk=step_id, hackathon=hackathon).first()
    if step is None:
        raise HackathonError('That payment step was not found.', 'NOT_FOUND', status=404)
    if step.qr_id:
        previous = step.qr
        step.qr = None
        step.save(update_fields=['qr', 'updated_at'])
        idea_files.attach(None, previous)
    return step


def qr_link(hackathon: Hackathon, user, step_id) -> dict:
    """The step's QR code: organizers, and members of a team that has to pay it."""
    step = _step(hackathon, step_id)
    file = step.qr
    allowed = access.has_any_permission(hackathon, user, ('settings', 'teams'))
    if not allowed:
        membership = TeamMember.objects.filter(hackathon=hackathon, user=user).select_related('team').first()
        allowed = membership is not None and is_due(step, membership.team)
    if file is None or file.state != UploadState.ATTACHED or not allowed:
        raise HackathonError('That file was not found.', 'NOT_FOUND', status=404)
    return idea_files._signed_link(file, inline=True)


# --- organizers: checking payments ------------------------------------------------------------------------------

def _row(team: Team, step: PaymentStep, payment: StepPayment | None, *, due: bool, duplicates: set[str]) -> dict:
    leader = team.leader
    return {
        'team_id': team.id, 'team_name': team.name, 'leader_name': _name(leader), 'leader_email': leader.email if leader else '',
        'leader_phone': (getattr(leader, 'phone_number', '') or '') if leader else '',
        'quote': quote(step, team), 'due': due,
        'payment': None if payment is None else {
            **_payment_for_team(payment), 'reason': payment.reason, 'screenshot': idea_files.serialize(payment.screenshot),
            'reviewed_by': _name(payment.reviewed_by) if payment.reviewed_by_id else None,
            'duplicate_reference': payment.transaction_key in duplicates,
        },
    }


def _duplicate_keys(hackathon: Hackathon, payments: list[StepPayment]) -> set[str]:
    """Transaction ids of these payments that another team also used (on a step payment of any status, or on a live
    registration payment). A team paying again with its own rejected reference is not a duplicate."""
    keys = {p.transaction_key for p in payments}
    if not keys:
        return set()
    teams_by_key: dict[str, set[int]] = {}
    for key, team_id in StepPayment.objects.filter(hackathon=hackathon, transaction_key__in=keys).values_list('transaction_key', 'team_id'):
        teams_by_key.setdefault(key, set()).add(team_id)
    duplicates = {key for key, teams in teams_by_key.items() if len(teams) > 1}
    registration = TeamPayment.objects.filter(hackathon=hackathon, transaction_key__in=keys).exclude(status__in=_CLOSED_TEAM_PAYMENT)
    return duplicates | set(registration.values_list('transaction_key', flat=True))


def queue(hackathon: Hackathon, user, step_id, *, check: bool = True) -> dict:
    """Every team that owes this step with its latest payment: waiting first, then unpaid, rejected and verified.
    ``check=False`` is for callers that already checked the permission (the export)."""
    if check:
        _require(hackathon, user, 'teams')
    step = _step(hackathon, step_id)
    teams, latest = _queue_teams(step)
    due_ids = set(eligible_teams(step).values_list('pk', flat=True))
    duplicates = _duplicate_keys(hackathon, list(latest.values()))
    order = {PaymentStatus.PENDING: 0, None: 1, PaymentStatus.REJECTED: 2, PaymentStatus.VERIFIED: 3}
    rows = [_row(team, step, latest.get(team.id), due=team.id in due_ids, duplicates=duplicates) for team in teams]
    rows.sort(key=lambda row: (order[row['payment']['status'] if row['payment'] else None], row['team_name'].lower()))
    return {'step': serialize_step(step, _counts(teams, latest)), 'rows': rows}


def screenshot_link(hackathon: Hackathon, user, payment_id, *, inline: bool) -> dict:
    """A 60-second link to the screenshot: organizers and the team's own leader."""
    payment = StepPayment.objects.filter(pk=payment_id, hackathon=hackathon).select_related('screenshot', 'team').first()
    file = payment.screenshot if payment else None
    if file is None or file.state != UploadState.ATTACHED:
        raise HackathonError('That file was not found.', 'NOT_FOUND', status=404)
    if not (access.has_permission(hackathon, user, 'teams') or payment.team.leader_id == user.id):
        raise HackathonError('That file was not found.', 'NOT_FOUND', status=404)
    log_audit_event(actor=user, action='Viewed Step Payment Proof', target_model='StepPayment', target_id=payment.pk,
                    details={'hackathon': hackathon.slug, 'team': payment.team.name})
    return idea_files._signed_link(file, inline=inline)


def _locked_payment(hackathon: Hackathon, user, payment_id) -> StepPayment:
    _require(hackathon, user, 'teams')
    payment = StepPayment.objects.select_for_update().filter(pk=payment_id, hackathon=hackathon).select_related('step', 'team').first()
    if payment is None:
        raise HackathonError('That payment was not found.', 'NOT_FOUND', status=404)
    if payment.status != PaymentStatus.PENDING:
        word = {PaymentStatus.VERIFIED: 'verified', PaymentStatus.REJECTED: 'rejected'}.get(payment.status, 'handled')
        raise HackathonError(f'This payment was already {word}.', 'PAYMENT_ALREADY_HANDLED', status=409, payment_status=payment.status)
    return payment


def _members(team: Team) -> list:
    return [m.user for m in team.memberships.select_related('user')]


@transaction.atomic
def verify(hackathon: Hackathon, user, payment_id) -> StepPayment:
    from .services import _notify_after_commit

    payment = _locked_payment(hackathon, user, payment_id)
    payment.status, payment.reviewed_by, payment.reviewed_at, payment.reason = PaymentStatus.VERIFIED, user, timezone.now(), ''
    payment.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'reason', 'updated_at'])
    log_audit_event(actor=user, action='Verified Step Payment', target_model='StepPayment', target_id=payment.pk,
                    details={'hackathon': hackathon.slug, 'step': payment.step.name, 'team': payment.team.name, 'amount': payment.amount})
    unlocked = ' You can now submit your project.' if payment.step.required_for_projects and not project_block(payment.team) else ''
    _notify_after_commit(_members(payment.team), f'{payment.team.name}: {payment.step.name} verified',
                         f'Your payment of ₹{payment.amount} for {payment.step.name} was verified.{unlocked}', hackathon, user)
    return payment


@transaction.atomic
def reject(hackathon: Hackathon, user, payment_id, reason: str) -> StepPayment:
    """Turn the payment down with a reason the leader sees; the leader can then pay again and the team is unlocked."""
    from .services import _notify_after_commit

    payment = _locked_payment(hackathon, user, payment_id)
    reason = ' '.join(str(reason or '').split())
    if not reason:
        raise HackathonError('Tell the leader why, so they can pay again correctly.', 'REASON_REQUIRED', field='reason')
    if len(reason) > MAX_REASON:
        raise HackathonError(f'Keep the reason under {MAX_REASON} characters.', 'INVALID', field='reason')
    payment.status, payment.reviewed_by, payment.reviewed_at, payment.reason = PaymentStatus.REJECTED, user, timezone.now(), reason
    payment.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'reason', 'updated_at'])
    log_audit_event(actor=user, action='Rejected Step Payment', target_model='StepPayment', target_id=payment.pk,
                    details={'hackathon': hackathon.slug, 'step': payment.step.name, 'team': payment.team.name, 'reason': reason})
    _notify_after_commit(_members(payment.team), f'{payment.team.name}: {payment.step.name} not accepted',
                         f'Your payment for {payment.step.name} was not accepted: {reason} Your team leader can pay again from the team dashboard.',
                         hackathon, user)
    return payment


def remind_unpaid(hackathon: Hackathon, user, step_id) -> int:
    """Notify and email the members of every team that has to pay and has nothing waiting or verified."""
    from .services import _notify_users

    _require(hackathon, user, 'teams')
    step = _step(hackathon, step_id)
    paid = set(step.payments.filter(status__in=OPEN_STATUSES).values_list('team_id', flat=True))
    teams = [
        team for team in eligible_teams(step).prefetch_related('memberships__user')
        if team.pk not in paid and quote(step, team)['total'] > 0
    ]
    when = f" Deadline: {timezone.localtime(step.deadline).strftime('%d %b %Y, %I:%M %p')}." if step.deadline else ''
    for team in teams:
        owed = quote(step, team)['total']
        _notify_users(_members(team), f'{team.name}: {step.name} is due',
                      f'Your team owes ₹{owed} for {step.name}.{when} The team leader pays from the team dashboard.', hackathon, user)
    _email_reminders(hackathon, step, teams, user)
    log_audit_event(actor=user, action='Reminded Teams To Pay', target_model='PaymentStep', target_id=step.pk,
                    details={'hackathon': hackathon.slug, 'step': step.name, 'teams': len(teams)})
    return len(teams)


def _email_reminders(hackathon: Hackathon, step: PaymentStep, teams: list[Team], user) -> None:
    """One announcement-style email to the reminded teams, through the announcement email engine."""
    if not teams:
        return
    from .models import AnnouncementAudience
    from .services import AnnouncementService

    AnnouncementService.create(
        # Email only (is_active=False): a note left on the dashboard would still say "not paid" after the team pays.
        hackathon, user, title=f'{step.name} is due', type='WARNING', audience=AnnouncementAudience.TEAMS, send_email=True, is_active=False,
        message=(f'Your team has not paid {step.name} yet. Open your team dashboard to see the amount and how to pay; '
                 'only the team leader can submit the payment.'),
        target_team_ids=[team.pk for team in teams],
    )
