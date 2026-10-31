"""The Club ID control panel: look up, reserve, assign, move, clear and count. Anyone who may manage members can read; only Admins change."""
from django.db.models import Q
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import ClubIdEvent, ClubIdReservation, ReservationStatus, User
from apps.accounts.services import club_id_registry as registry
from apps.accounts.services.club_id_registry import ClubIdRegistryError
from apps.accounts.services.club_id_service import ClubIDService, InvalidClubIdError
from apps.accounts.services.email_verification import is_verified
from apps.audit.utils import log_audit_event
from apps.core.pagination import StandardPagination
from apps.core.permissions import IsAdmin, IsAdminOrClubLead

PERSON_RESULTS = 10


def _error(exc: ClubIdRegistryError) -> Response:
    return Response({'detail': exc.message, 'code': exc.code}, status=exc.status)


def _person(user) -> dict | None:
    if user is None:
        return None
    return {
        'id': user.pk, 'name': f'{user.first_name} {user.last_name}'.strip() or user.email.split('@')[0], 'email': user.email, 'role': user.role,
        'membership_status': user.membership_status, 'club_id': user.club_id or None, 'email_verified': is_verified(user),
    }


def _reservation(row: ClubIdReservation | None) -> dict | None:
    if row is None:
        return None
    return {
        'id': row.pk, 'club_id': row.club_id, 'email': row.reserved_for_email, 'reason': row.reason, 'source': row.source, 'status': row.status,
        'created_at': row.created_at, 'created_by': getattr(row.created_by, 'email', ''), 'claimed_at': row.claimed_at, 'claimed_by': getattr(row.claimed_by, 'email', ''),
        'released_at': row.released_at,
    }


def _event(row: ClubIdEvent) -> dict:
    return {
        'id': row.pk, 'club_id': row.club_id, 'kind': row.kind, 'kind_label': row.get_kind_display(), 'user_email': row.user_email, 'other_email': row.other_email,
        'actor_email': row.actor_email, 'reason': row.reason, 'details': row.details, 'created_at': row.created_at,
    }


def _typed_again(typed, club_id: str) -> bool:
    return str(typed or '').strip().upper() == club_id.upper()


def _audit(request, action: str, target, **details) -> None:
    log_audit_event(actor=request.user, action=action, target_model='User', target_id=str(target), details=details)


class ClubIdOverviewView(APIView):
    """GET /api/auth/club-ids/overview/ - counters with what is in use and the gaps, and the totals."""
    permission_classes = [IsAdminOrClubLead]

    def get(self, request):
        return Response(registry.overview())


class ClubIdLookupView(APIView):
    """GET /api/auth/club-ids/lookup/?q= - an id (its holder, reservation, history) or a person (their ids)."""
    permission_classes = [IsAdminOrClubLead]

    def get(self, request):
        text = (request.query_params.get('q') or '').strip()
        if not text:
            return Response({'detail': 'Type a Club ID, a name or an email.', 'code': 'EMPTY'}, status=status.HTTP_400_BAD_REQUEST)
        if ClubIDService.validate_club_id_format(text):
            club_id = registry.canonical(text)
            found = registry.lookup_id(club_id)
            return Response({
                'kind': 'id', 'club_id': club_id, 'holder': _person(found['holder']), 'reservation': _reservation(found['reservation']),
                'retired': found['retired'], 'events': [_event(row) for row in found['events']],
            })
        people = User.objects.all()
        for word in text.split()[:5]:
            people = people.filter(Q(email__icontains=word) | Q(first_name__icontains=word) | Q(last_name__icontains=word))
        reservations = ClubIdReservation.objects.filter(reserved_for_email__icontains=text.lower(), status=ReservationStatus.RESERVED)[:PERSON_RESULTS]
        return Response({
            'kind': 'person', 'people': [_person(user) for user in people.order_by('first_name', 'email')[:PERSON_RESULTS]], 'reservations': [_reservation(row) for row in reservations],
        })


class ClubIdEventsView(generics.ListAPIView):
    """GET /api/auth/club-ids/events/ - the history, newest first. Filters: club_id, q (an email), kind."""
    permission_classes = [IsAdminOrClubLead]
    pagination_class = StandardPagination

    def get_queryset(self):
        rows = ClubIdEvent.objects.all()
        params = self.request.query_params
        if club_id := (params.get('club_id') or '').strip():
            rows = rows.filter(club_id__iexact=club_id)
        if text := (params.get('q') or '').strip():
            rows = rows.filter(Q(user_email__icontains=text) | Q(other_email__icontains=text) | Q(actor_email__icontains=text) | Q(club_id__icontains=text))
        if kind := params.get('kind'):
            rows = rows.filter(kind__in=kind.split(','))
        return rows

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response([_event(row) for row in page])


class ReservationsView(APIView):
    """GET (read) /api/auth/club-ids/reservations/?status= ; POST (Admin) {email, club_id?, reason, year?} - hold an id for someone."""

    def get_permissions(self):
        return [(IsAdmin if self.request.method == 'POST' else IsAdminOrClubLead)()]

    def get(self, request):
        rows = ClubIdReservation.objects.select_related('created_by', 'claimed_by')
        if wanted := request.query_params.get('status'):
            rows = rows.filter(status__in=wanted.split(','))
        return Response({'results': [_reservation(row) for row in rows[:500]]})

    def post(self, request):
        data = request.data
        try:
            year = int(data.get('year')) if data.get('year') else None
            reservation = registry.reserve(request.user, email=data.get('email', ''), club_id=data.get('club_id', ''), reason=data.get('reason', ''), year=year)
        except ValueError:
            return Response({'detail': 'Choose a real year.', 'code': 'INVALID'}, status=status.HTTP_400_BAD_REQUEST)
        except ClubIdRegistryError as exc:
            return _error(exc)
        _audit(request, 'Club ID Reserved', reservation.club_id, email=reservation.reserved_for_email)
        return Response(_reservation(reservation), status=status.HTTP_201_CREATED)


class ReservationReleaseView(APIView):
    """POST (Admin) /api/auth/club-ids/reservations/{id}/release/ {reason} - let an id go back to the pool."""
    permission_classes = [IsAdmin]

    def post(self, request, pk):
        try:
            reservation = registry.release(request.user, pk, reason=request.data.get('reason', ''))
        except ClubIdRegistryError as exc:
            return _error(exc)
        _audit(request, 'Club ID Reservation Released', reservation.club_id)
        return Response(_reservation(reservation))


class AllocateView(APIView):
    """POST (Admin) /api/auth/club-ids/allocate/ {user_id, reason, year?} - give a person the next number."""
    permission_classes = [IsAdmin]

    def post(self, request):
        try:
            year = int(request.data.get('year')) if request.data.get('year') else None
            user = registry.allocate_for(request.user, request.data.get('user_id'), reason=request.data.get('reason', ''), year=year)
        except ValueError:
            return Response({'detail': 'Choose a real year.', 'code': 'INVALID'}, status=status.HTTP_400_BAD_REQUEST)
        except ClubIdRegistryError as exc:
            return _error(exc)
        _audit(request, 'Club ID Allocated', user.pk, club_id=user.club_id)
        return Response({'person': _person(user)})


class AssignView(APIView):
    """POST (Admin) /api/auth/club-ids/assign/ {user_id, club_id, reason, allow_retired?} - give a person a particular id."""
    permission_classes = [IsAdmin]

    def post(self, request):
        try:
            user = registry.assign(
                request.user, request.data.get('user_id'), request.data.get('club_id'), reason=request.data.get('reason', ''),
                allow_retired=bool(request.data.get('allow_retired')),
            )
        except ClubIdRegistryError as exc:
            return _error(exc)
        _audit(request, 'Club ID Assigned', user.pk, club_id=user.club_id)
        return Response({'person': _person(user)})


class ReassignView(APIView):
    """POST (Admin) /api/auth/club-ids/reassign/ {club_id, to_user_id, reason, confirm} - move an id; `confirm` is the id typed again."""
    permission_classes = [IsAdmin]

    def post(self, request):
        try:
            club_id = registry.canonical(request.data.get('club_id'))
            if not _typed_again(request.data.get('confirm'), club_id):
                raise ClubIdRegistryError('Type the Club ID again to confirm.', 'CONFIRM_MISMATCH')
            moved = registry.reassign(request.user, club_id, request.data.get('to_user_id'), reason=request.data.get('reason', ''))
        except ClubIdRegistryError as exc:
            return _error(exc)
        _audit(request, 'Club ID Moved', moved['to'].pk, club_id=club_id, previous_holder=moved['from'].email)
        return Response({'from': _person(moved['from']), 'to': _person(moved['to']), 'holder_demoted': moved['holder_demoted']})


class ClearView(APIView):
    """POST (Admin) /api/auth/club-ids/clear/ {user_id, reason, confirm} - remove a person's id (retired); `confirm` is the id typed again."""
    permission_classes = [IsAdmin]

    def post(self, request):
        user = User.objects.filter(pk=request.data.get('user_id')).first() if str(request.data.get('user_id') or '').isdigit() else None
        try:
            if user is None:
                raise ClubIdRegistryError('That person was not found.', 'NOT_FOUND', 404)
            if not user.club_id:
                raise ClubIdRegistryError(f'{user.email} has no Club ID.', 'NO_CLUB_ID', 409)
            if not _typed_again(request.data.get('confirm'), user.club_id):
                raise ClubIdRegistryError('Type the Club ID again to confirm.', 'CONFIRM_MISMATCH')
            done = registry.clear(request.user, user.pk, reason=request.data.get('reason', ''))
        except ClubIdRegistryError as exc:
            return _error(exc)
        _audit(request, 'Club ID Cleared', user.pk, club_id=done['club_id'], demoted=done['demoted'])
        return Response({'person': _person(done['user']), 'club_id': done['club_id'], 'demoted': done['demoted']})


class CounterView(APIView):
    """POST (Admin) /api/auth/club-ids/counters/ {year, next_sequence, reason, prefix?} - move the SCC counter forward."""
    permission_classes = [IsAdmin]

    def post(self, request):
        try:
            counter = registry.set_counter(
                request.user, prefix=request.data.get('prefix') or 'SCC', year=int(request.data.get('year')), next_sequence=int(request.data.get('next_sequence')),
                reason=request.data.get('reason', ''),
            )
        except (TypeError, ValueError):
            return Response({'detail': 'Give a year and a number.', 'code': 'INVALID'}, status=status.HTTP_400_BAD_REQUEST)
        except ClubIdRegistryError as exc:
            return _error(exc)
        _audit(request, 'Club ID Counter Moved', f'{counter.prefix}{counter.year}', next_sequence=counter.next_sequence)
        return Response({'prefix': counter.prefix, 'year': counter.year, 'next_sequence': counter.next_sequence})
