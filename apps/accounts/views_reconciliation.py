"""Past-member sheet reconciliation endpoints. Anyone who may manage members can read and export; only Admins upload and act."""
import json

from django.db.models import Q
from django.http import Http404, HttpResponse
from rest_framework import generics, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import ReconciliationOutcome as Outcome, ReconciliationResolution as Resolution, ReconciliationRow, ReconciliationRun, User
from apps.accounts.services import member_reconciliation as service
from apps.accounts.services.member_reconciliation import ReconciliationError
from apps.accounts.user_directory import DirectoryUserSerializer
from apps.audit.utils import log_audit_event
from apps.core.dmc.contracts import ColumnDefinition
from apps.core.dmc.export_service import export_filename, serialize_records
from apps.core.pagination import StandardPagination
from apps.core.permissions import IsAdmin, IsAdminOrClubLead

GROUPS = {
    'matched': (Outcome.MATCH, Outcome.NAME_DIFFERS),
    'link': (Outcome.ID_MISSING_IN_APP,),
    'mismatch': (Outcome.ID_MISMATCH, Outcome.ID_HELD_BY_OTHER, Outcome.EMAIL_MISMATCH, Outcome.ID_RESERVED, Outcome.ID_RETIRED),
    'no_account': (Outcome.NO_ACCOUNT, Outcome.NO_ACCOUNT_NO_ID),
    'sheet': (Outcome.INVALID, Outcome.DUPLICATE, Outcome.NO_ID_IN_SHEET),
}
EXPORT_FORMATS = ('csv', 'xlsx', 'json')


class RunPagination(StandardPagination):
    page_size = 25
    max_page_size = 100


def _error(exc: ReconciliationError) -> Response:
    return Response({'detail': exc.message, 'code': exc.code, **exc.extra}, status=exc.status)


def _run(pk) -> ReconciliationRun:
    run = ReconciliationRun.objects.select_related('created_by').filter(pk=pk).first()
    if run is None:
        raise Http404
    return run


def _person(user):
    return None if user is None else {'id': user.pk, 'name': f'{user.first_name} {user.last_name}'.strip() or user.email.split('@')[0], 'email': user.email, 'club_id': user.club_id or None, 'role': user.role}


def _actions(row: ReconciliationRow) -> list[str]:
    """What an Admin can do with this row right now."""
    if row.outcome == Outcome.ID_MISSING_IN_APP and row.resolution == Resolution.PENDING:
        return ['link', 'follow_up', 'dismiss']
    if row.outcome == Outcome.ID_RETIRED and row.resolution == Resolution.PENDING:
        return ['link_retired', 'follow_up', 'dismiss']
    if row.outcome == Outcome.ID_MISMATCH and row.resolution == Resolution.PENDING:
        return ['use_sheet', 'keep_app', 'follow_up', 'dismiss']
    if row.outcome == Outcome.NO_ACCOUNT and row.resolution == Resolution.PENDING and row.sheet_email and row.sheet_club_id and not row.invited_at:
        return ['invite', 'follow_up', 'dismiss']
    if row.outcome in service.NEEDS_DECISION and row.resolution == Resolution.PENDING:
        return ['keep_app', 'follow_up', 'dismiss']
    return []


def _row(row: ReconciliationRow) -> dict:
    return {
        'id': row.pk, 'row_number': row.row_number, 'name': row.sheet_name, 'email': row.sheet_email, 'club_id': row.sheet_club_id,
        'outcome': row.outcome, 'outcome_label': row.get_outcome_display(), 'detail': row.detail, 'resolution': row.resolution,
        'resolution_label': row.get_resolution_display(), 'note': row.note, 'matched_user': _person(row.matched_user), 'other_user': _person(row.other_user),
        'resolved_by': getattr(row.resolved_by, 'email', ''), 'resolved_at': row.resolved_at, 'invited_at': row.invited_at, 'actions': _actions(row),
    }


def _audit(request, action: str, run: ReconciliationRun, **details) -> None:
    log_audit_event(actor=request.user, action=action, target_model='ReconciliationRun', target_id=str(run.id), details={'file': run.source_filename, **details})


def _clean_rows(run, params):
    rows = run.rows.select_related('matched_user', 'other_user', 'resolved_by')
    if group := params.get('group'):
        rows = rows.filter(outcome__in=GROUPS.get(group, ()))
    if outcome := params.get('outcome'):
        rows = rows.filter(outcome__in=outcome.split(','))
    if resolution := params.get('resolution'):
        rows = rows.filter(resolution__in=resolution.split(','))
    if params.get('open') in ('1', 'true'):
        rows = rows.filter(resolution=Resolution.PENDING, outcome__in=service.NEEDS_DECISION)
    if text := (params.get('q') or '').strip():
        rows = rows.filter(Q(sheet_name__icontains=text) | Q(sheet_email__icontains=text) | Q(sheet_club_id__icontains=text) | Q(detail__icontains=text))
    return rows


class InspectView(APIView):
    """POST /api/auth/reconciliation/inspect/ (file, sheet?) - read the sheet and suggest which columns are which. Nothing is saved."""
    permission_classes = [IsAdmin]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        upload = request.FILES.get('file')
        if upload is None:
            return Response({'detail': 'Choose a file.', 'code': 'NO_FILE'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            data = service.read_sheet(upload, upload.name, request.data.get('sheet') or None)
        except ReconciliationError as exc:
            return _error(exc)
        return Response({'sheets': data['sheets'], 'sheet': data['sheet'], 'headers': data['headers'], 'suggested': data['suggested'], 'total_rows': len(data['rows']), 'sample': data['rows'][:5]})


class RunsView(APIView):
    """GET /api/auth/reconciliation/runs/ - earlier sheets. POST (Admin, file, sheet?, mapping) - compare a sheet and keep the result."""
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        return [(IsAdmin if self.request.method == 'POST' else IsAdminOrClubLead)()]

    def get(self, request):
        return Response({'results': [service.summary(run) for run in ReconciliationRun.objects.select_related('created_by')[:50]]})

    def post(self, request):
        upload = request.FILES.get('file')
        if upload is None:
            return Response({'detail': 'Choose a file.', 'code': 'NO_FILE'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            mapping = request.data.get('mapping') or '{}'
            mapping = json.loads(mapping) if isinstance(mapping, str) else mapping
            run = service.create_run(request.user, upload, upload.name, sheet=request.data.get('sheet') or None, mapping=mapping)
        except json.JSONDecodeError:
            return Response({'detail': 'The column choice could not be read.', 'code': 'BAD_MAPPING'}, status=status.HTTP_400_BAD_REQUEST)
        except ReconciliationError as exc:
            return _error(exc)
        _audit(request, 'Past-Member Sheet Compared', run, rows=run.total_rows)
        return Response(service.summary(run), status=status.HTTP_201_CREATED)


class RunDetailView(APIView):
    """GET (read) /api/auth/reconciliation/runs/{id}/ - the numbers. DELETE (Admin) - remove the run and what it stored."""

    def get_permissions(self):
        return [(IsAdmin if self.request.method == 'DELETE' else IsAdminOrClubLead)()]

    def get(self, request, pk):
        return Response(service.summary(_run(pk)))

    def delete(self, request, pk):
        run = _run(pk)
        _audit(request, 'Past-Member Sheet Deleted', run, rows=run.total_rows)
        run.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class RowsView(generics.ListAPIView):
    """GET /api/auth/reconciliation/runs/{id}/rows/ - filters: group, outcome, resolution, open, q."""
    permission_classes = [IsAdminOrClubLead]
    pagination_class = RunPagination

    def list(self, request, *args, **kwargs):
        rows = _clean_rows(_run(kwargs['pk']), request.query_params)
        page = self.paginate_queryset(rows)
        service.refresh_rows(page)   # what the accounts say now, not what they said when the sheet was read
        return self.get_paginated_response([_row(row) for row in page])


class RecheckView(APIView):
    """POST /api/auth/reconciliation/runs/{id}/recheck/ - look at every row again against the accounts as they are now."""
    permission_classes = [IsAdminOrClubLead]

    def post(self, request, pk):
        run = _run(pk)
        return Response({**service.recheck(run), 'summary': service.summary(run)})


class MissingView(generics.ListAPIView):
    """GET /api/auth/reconciliation/runs/{id}/missing/ - members in the app who are not in the sheet."""
    permission_classes = [IsAdminOrClubLead]
    pagination_class = RunPagination
    serializer_class = DirectoryUserSerializer

    def get_queryset(self):
        return service.missing_from_sheet(_run(self.kwargs['pk'])).select_related('college', 'referred_by_user').order_by('club_id')


class RowActionView(APIView):
    """POST (Admin) /api/auth/reconciliation/runs/{id}/rows/{row}/{action}/ - link, link-retired, use-sheet, decide, invite."""
    permission_classes = [IsAdmin]

    def post(self, request, pk, row_id, action):
        run = _run(pk)
        body = request.data
        try:
            if action == 'link':
                row = service.link(request.user, run, row_id, allow_retired=bool(body.get('allow_retired')))
            elif action == 'use-sheet':
                row = service.use_sheet_value(request.user, run, row_id, body.get('note', ''))
            elif action == 'decide':
                row = service.decide(request.user, run, row_id, body.get('choice', ''), body.get('note', ''))
            elif action == 'invite':
                row = service.invite(request.user, run, row_id)
            else:
                raise Http404
        except ReconciliationError as exc:
            return _error(exc)
        _audit(request, f'Past-Member Row {action.title()}', run, row=row.row_number)
        return Response(_row(ReconciliationRow.objects.select_related('matched_user', 'other_user', 'resolved_by').get(pk=row.pk)))


class BulkView(APIView):
    """POST (Admin) /api/auth/reconciliation/runs/{id}/bulk/ {action: link|invite, outcome, expected} - every open row of that outcome."""
    permission_classes = [IsAdmin]

    def post(self, request, pk):
        run = _run(pk)
        try:
            result = service.bulk(request.user, run, request.data.get('action', ''), request.data.get('outcome', ''), int(request.data.get('expected', -1)))
        except (TypeError, ValueError):
            return Response({'detail': 'Say how many rows you saw.', 'code': 'INVALID'}, status=status.HTTP_400_BAD_REQUEST)
        except ReconciliationError as exc:
            return _error(exc)
        _audit(request, f"Past-Member Bulk {request.data.get('action', '').title()}", run, done=result['done'], failed=len(result['failed']))
        return Response(result)


REPORT_COLUMNS = [
    ColumnDefinition(key=key, label=label, type=kind) for key, label, kind in (
        ('row', 'Sheet Row', 'number'), ('name', 'Name In Sheet', 'text'), ('email', 'Email In Sheet', 'text'), ('club_id', 'Club ID In Sheet', 'text'),
        ('outcome', 'Result', 'text'), ('detail', 'What It Means', 'multiline'), ('resolution', 'Decision', 'text'), ('note', 'Note', 'multiline'),
        ('app_email', 'App Account Email', 'text'), ('app_club_id', 'App Club ID', 'text'), ('resolved_by', 'Decided By', 'text'), ('resolved_at', 'Decided At', 'datetime'),
    )
]
MISSING_COLUMNS = [
    ColumnDefinition(key=key, label=label, type=kind) for key, label, kind in (
        ('club_id', 'Club ID', 'text'), ('name', 'Name', 'text'), ('email', 'Email', 'text'), ('phone', 'Phone', 'text'), ('status', 'Membership Status', 'text'),
        ('branch', 'Branch', 'text'), ('year', 'Year', 'text'),
    )
]


class ExportView(APIView):
    """GET /api/auth/reconciliation/runs/{id}/export/?kind=report|missing&export_format=csv|xlsx|json - the full report, or the members not in the sheet."""
    permission_classes = [IsAdminOrClubLead]

    def get(self, request, pk):
        run = _run(pk)
        fmt = (request.query_params.get('export_format') or 'csv').lower()
        if fmt not in EXPORT_FORMATS:
            return Response({'detail': f"Choose a format: {', '.join(EXPORT_FORMATS)}.", 'code': 'INVALID'}, status=status.HTTP_400_BAD_REQUEST)
        if request.query_params.get('kind') == 'missing':
            records = [
                {'club_id': u.club_id, 'name': f'{u.first_name} {u.last_name}'.strip(), 'email': u.email, 'phone': u.phone_number or '', 'status': u.membership_status, 'branch': u.branch or '', 'year': u.year or ''}
                for u in service.missing_from_sheet(run).order_by('club_id')
            ]
            data, content_type = serialize_records(MISSING_COLUMNS, iter(records), fmt)
            name = 'not-in-sheet'
        else:
            records = [
                {
                    'row': row.row_number, 'name': row.sheet_name, 'email': row.sheet_email, 'club_id': row.sheet_club_id, 'outcome': row.get_outcome_display(), 'detail': row.detail,
                    'resolution': row.get_resolution_display(), 'note': row.note, 'app_email': getattr(row.matched_user, 'email', '') or getattr(row.other_user, 'email', ''),
                    'app_club_id': (row.matched_user.club_id if row.matched_user_id and row.matched_user.club_id else getattr(row.other_user, 'club_id', '')) or '',
                    'resolved_by': getattr(row.resolved_by, 'email', ''), 'resolved_at': row.resolved_at or '',
                }
                for row in _clean_rows(run, request.query_params)
            ]
            data, content_type = serialize_records(REPORT_COLUMNS, iter(records), fmt)
            name = 'past-members-report'
        _audit(request, f'Past-Member Export ({fmt.upper()})', run, kind=name, rows=len(records))
        response = HttpResponse(data, content_type=content_type)
        response['Content-Disposition'] = f'attachment; filename="{export_filename("members", name, fmt)}"'
        return response
