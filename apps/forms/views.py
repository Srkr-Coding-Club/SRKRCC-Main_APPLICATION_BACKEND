from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response as DRFResponse
from rest_framework.pagination import PageNumberPagination
from django.utils import timezone
from django.db import transaction
from django.db.models import Count, Max, Q
from django.db.models.functions import Cast
from django.db.models import TextField

from .models import Form, FormField, Response, Answer, BulkIngestSession, MemberNote
from .serializers import (
    FormSerializer,
    ResponseSerializer,
    ResponseDetailSerializer,
    BulkIngestSessionSerializer,
    check_field_conditional_dependencies,
    evaluate_visible_fields,
    enforce_field_validation,
)


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class FormViewSet(viewsets.ModelViewSet):
    serializer_class = FormSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    lookup_field = 'slug'

    def get_queryset(self):
        """Annotate forms with real response count (excluding test submissions)."""
        return Form.objects.annotate(
            response_count=Count(
                'responses',
                filter=Q(responses__is_test_submission=False),
            )
        ).order_by('-created_at')

    # -----------------------------------------------------------------------
    # Existing: soft-delete a field
    # -----------------------------------------------------------------------

    @action(detail=True, methods=['delete'], url_path='fields/(?P<field_id>[0-9]+)')
    def soft_delete_field(self, request, slug=None, field_id=None):
        """
        Soft-delete a single field on a form.

        Returns HTTP 200 with a warning payload if the field is referenced
        by other fields' conditional_logic rules, so the caller can decide
        whether to proceed. The caller must send ?confirm=true to finalize
        deletion when warnings are present.
        """
        form = self.get_object()
        try:
            field = FormField.all_objects.get(id=field_id, form=form, is_deleted=False)
        except FormField.DoesNotExist:
            return DRFResponse({"error": "Field not found."}, status=status.HTTP_404_NOT_FOUND)

        dependent_fields = check_field_conditional_dependencies(field.id, form)
        if dependent_fields and request.query_params.get('confirm') != 'true':
            return DRFResponse(
                {
                    "warning": (
                        f"Field '{field.label}' is referenced by {len(dependent_fields)} "
                        f"conditional rule(s). Pass ?confirm=true to force delete and "
                        f"cascade-clear those rules."
                    ),
                    "dependent_fields": [{"id": f.id, "label": f.label} for f in dependent_fields],
                },
                status=status.HTTP_200_OK,
            )

        # Cascade-clear rules that reference this field
        if dependent_fields:
            field_id_str = str(field.id)
            for dep in dependent_fields:
                cl = dep.conditional_logic
                if cl and "rules" in cl:
                    cl["rules"] = [r for r in cl["rules"] if str(r.get("if", "")) != field_id_str]
                    dep.conditional_logic = cl
                    dep.save(update_fields=["conditional_logic"])
                elif cl and str(cl.get("if", "")) == field_id_str:
                    dep.conditional_logic = {}
                    dep.save(update_fields=["conditional_logic"])

        field.is_deleted = True
        field.save(update_fields=["is_deleted"])
        return DRFResponse({"status": "deleted", "id": field.id}, status=status.HTTP_200_OK)

    # -----------------------------------------------------------------------
    # New: bulk CSV ingest
    # -----------------------------------------------------------------------

    @action(detail=True, methods=['post'], url_path='bulk-ingest',
            permission_classes=[permissions.IsAdminUser])
    def bulk_ingest(self, request, slug=None):
        """
        POST /api/forms/{slug}/bulk-ingest/

        Bulk-import pre-validated rows into this form.
        Supports idempotency via idempotency_key.
        Uses per-row savepoints so skip_errors=true works correctly.
        """
        if not request.user.is_staff:
            return DRFResponse({"error": "Staff access required."}, status=status.HTTP_403_FORBIDDEN)

        form = self.get_object()
        rows = request.data.get('rows', [])
        idempotency_key = request.data.get('idempotency_key', '')
        skip_errors = request.data.get('skip_errors', True)

        if not idempotency_key:
            return DRFResponse(
                {"error": "idempotency_key is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Idempotency: return cached result if this key was already processed
        try:
            existing_session = BulkIngestSession.objects.get(idempotency_key=idempotency_key)
            return DRFResponse(
                {
                    "imported": existing_session.imported_count,
                    "skipped": existing_session.skipped_count,
                    "duplicates": existing_session.duplicate_count,
                    "errors": existing_session.error_log,
                    "session_id": existing_session.id,
                    "cached": True,
                },
                status=status.HTTP_200_OK,
            )
        except BulkIngestSession.DoesNotExist:
            pass

        active_fields = list(form.fields.filter(is_deleted=False).order_by('order'))
        fields_by_id = {str(f.id): f for f in active_fields}

        imported_count = 0
        skipped_count = 0
        duplicate_count = 0
        error_log = []

        # Outer transaction wraps everything; per-row savepoints handle skip_errors logic
        try:
            with transaction.atomic():
                for row_index, row in enumerate(rows):
                    row_number = row_index + 2  # 1-indexed + header offset

                    # Build submitted_map for conditional logic evaluation
                    submitted_map = {
                        field_id: row.get(field_id, '')
                        for field_id in fields_by_id
                    }
                    visible_field_ids = evaluate_visible_fields(active_fields, submitted_map)

                    row_errors = []

                    # Validate each visible field
                    for field_id_str, field in fields_by_id.items():
                        if field.id not in visible_field_ids:
                            continue
                        value = row.get(field_id_str, None)
                        field_errors = enforce_field_validation(field, value)
                        for err in field_errors:
                            row_errors.append({
                                "row": row_number,
                                "field": field.label,
                                "value": str(value) if value is not None else '',
                                "error": err,
                            })

                    if row_errors:
                        if not skip_errors:
                            # Hard fail: roll back everything
                            raise Exception(f"Validation error at row {row_number}: {row_errors}")
                        error_log.extend(row_errors)
                        skipped_count += 1
                        continue

                    # Duplicate check: if form doesn't allow multiple responses,
                    # check for an existing response from the same user
                    email_field = next(
                        (f for f in active_fields if f.type == 'EMAIL' and f.id in visible_field_ids),
                        None
                    )
                    email_value = None
                    if email_field:
                        email_value = str(row.get(str(email_field.id), '') or '').strip()

                    if not form.allow_multiple_responses and email_value:
                        # Check using JSONField-compatible Cast lookup
                        quoted_email = f'"{email_value}"'
                        existing_answer = (
                            Answer.objects
                            .filter(response__form=form, field__type='EMAIL')
                            .annotate(email_str=Cast('value', output_field=TextField()))
                            .filter(email_str=quoted_email)
                            .first()
                        )
                        if existing_answer:
                            duplicate_count += 1
                            continue

                    # Per-row savepoint: if something fails here unexpectedly,
                    # only this row is rolled back (not the whole batch)
                    try:
                        with transaction.atomic():
                            response_obj = Response.objects.create(
                                form=form,
                                user=None,
                                is_manual_entry=True,
                                created_by_admin=request.user,
                                form_version=form.version,
                            )
                            for field_id_str, field in fields_by_id.items():
                                if field.id not in visible_field_ids:
                                    continue
                                value = row.get(field_id_str, None)
                                if value is not None:
                                    Answer.objects.create(
                                        response=response_obj,
                                        field=field,
                                        value=value,
                                    )
                            imported_count += 1
                    except Exception as row_exc:
                        if not skip_errors:
                            raise
                        error_log.append({
                            "row": row_number,
                            "field": "__row__",
                            "value": "",
                            "error": str(row_exc),
                        })
                        skipped_count += 1

                # Determine session status
                if error_log and imported_count == 0:
                    session_status = BulkIngestSession.STATUS_FAILED
                elif error_log:
                    session_status = BulkIngestSession.STATUS_PARTIAL
                else:
                    session_status = BulkIngestSession.STATUS_COMPLETED

                session = BulkIngestSession.objects.create(
                    form=form,
                    idempotency_key=idempotency_key,
                    created_by=request.user,
                    imported_count=imported_count,
                    skipped_count=skipped_count,
                    duplicate_count=duplicate_count,
                    error_log=error_log,
                    status=session_status,
                )

        except Exception as exc:
            # skip_errors=False path: entire batch failed
            return DRFResponse(
                {
                    "imported": 0,
                    "skipped": len(rows),
                    "duplicates": 0,
                    "errors": [{"row": 0, "field": "__batch__", "value": "", "error": str(exc)}],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return DRFResponse(
            {
                "imported": imported_count,
                "skipped": skipped_count,
                "duplicates": duplicate_count,
                "errors": error_log,
                "session_id": session.id,
            },
            status=status.HTTP_201_CREATED,
        )

    # -----------------------------------------------------------------------
    # New: lightweight duplicate check (used in CSV step 3)
    # -----------------------------------------------------------------------

    @action(detail=True, methods=['post'], url_path='check-duplicates',
            permission_classes=[permissions.IsAdminUser])
    def check_duplicates(self, request, slug=None):
        """
        POST /api/forms/{slug}/check-duplicates/
        Body: { "emails": ["email1@x.com", ...] }

        Returns existing responses matched by email (JSONField-safe cast).
        """
        if not request.user.is_staff:
            return DRFResponse({"error": "Staff access required."}, status=status.HTTP_403_FORBIDDEN)

        form = self.get_object()
        emails = request.data.get('emails', [])

        if not emails:
            return DRFResponse({"duplicates": []}, status=status.HTTP_200_OK)

        # JSONField stores strings as JSON-encoded values, e.g. "\"foo@bar.com\""
        # We cast to TEXT and compare against the JSON-encoded form of each email.
        quoted_emails = [f'"{e}"' for e in emails]

        matches = (
            Answer.objects
            .filter(response__form=form, field__type='EMAIL')
            .annotate(email_str=Cast('value', output_field=TextField()))
            .filter(email_str__in=quoted_emails)
            .select_related('response')
        )

        duplicates = []
        for match in matches:
            raw_email = str(match.value).strip('"')
            duplicates.append({
                "email": raw_email,
                "response_id": match.response.id,
                "submitted_at": match.response.submitted_at.isoformat(),
            })

        return DRFResponse({"duplicates": duplicates}, status=status.HTTP_200_OK)

    # -----------------------------------------------------------------------
    # New: data health dashboard
    # -----------------------------------------------------------------------

    @action(detail=False, methods=['get'], url_path='data-health',
            permission_classes=[permissions.IsAdminUser])
    def data_health(self, request):
        """
        GET /api/forms/data-health/

        Returns aggregated stats, active warnings, and recent activity feed.
        """
        if not request.user.is_staff:
            return DRFResponse({"error": "Staff access required."}, status=status.HTTP_403_FORBIDDEN)

        all_forms = Form.objects.all()
        total_forms = all_forms.count()
        published_forms = all_forms.filter(status='PUBLISHED').count()
        total_responses = Response.objects.filter(is_test_submission=False).count()

        # Completion rate: responses with no empty required answers / total responses
        responses_with_gaps = 0
        for form in all_forms:
            required_fields = list(form.fields.filter(is_required=True, is_deleted=False))
            if not required_fields:
                continue
            required_ids = [f.id for f in required_fields]
            form_responses = Response.objects.filter(form=form, is_test_submission=False)
            for resp in form_responses:
                answered_ids = set(
                    Answer.objects
                    .filter(response=resp, field_id__in=required_ids)
                    .exclude(value=None).exclude(value='').exclude(value=[])
                    .values_list('field_id', flat=True)
                )
                if set(required_ids) - answered_ids:
                    responses_with_gaps += 1

        completion_rate = (
            round(((total_responses - responses_with_gaps) / total_responses) * 100)
            if total_responses > 0 else 100
        )

        # Scan for warnings
        warnings = []

        # 1. Empty required fields in responses (manual entries)
        for form in all_forms:
            required_fields = list(form.fields.filter(is_required=True, is_deleted=False))
            if not required_fields:
                continue
            required_ids = [f.id for f in required_fields]
            gap_count = 0
            for resp in Response.objects.filter(form=form, is_manual_entry=True):
                answered_ids = set(
                    Answer.objects
                    .filter(response=resp, field_id__in=required_ids)
                    .exclude(value=None).exclude(value='').exclude(value=[])
                    .values_list('field_id', flat=True)
                )
                if set(required_ids) - answered_ids:
                    gap_count += 1
            if gap_count > 0:
                warnings.append({
                    "type": "empty_required_fields",
                    "form_id": form.id,
                    "form_title": form.title,
                    "message": f"{gap_count} response(s) have empty required fields (manual entries)",
                    "action_link": "responses",
                })

        # 2. Broken conditional rules (rule references a soft-deleted field)
        for field in FormField.objects.filter(is_deleted=False):
            rules = field.conditional_logic or {}
            for rule in rules.get('rules', []):
                ref_id = rule.get('if')
                if ref_id and not FormField.all_objects.filter(
                    id=ref_id, is_deleted=False
                ).exists():
                    warnings.append({
                        "type": "broken_conditional_rule",
                        "form_id": field.form_id,
                        "form_title": field.form.title,
                        "message": f"Field '{field.label}' has a conditional rule referencing a deleted field",
                        "action_link": "forms",
                    })
                    break  # One warning per field is enough
            # Legacy single-rule format
            if not rules.get('rules'):
                ref_id = rules.get('if')
                if ref_id and not FormField.all_objects.filter(
                    id=ref_id, is_deleted=False
                ).exists():
                    warnings.append({
                        "type": "broken_conditional_rule",
                        "form_id": field.form_id,
                        "form_title": field.form.title,
                        "message": f"Field '{field.label}' has a conditional rule referencing a deleted field",
                        "action_link": "forms",
                    })

        # 3. Published forms with no close date for 90+ days
        ninety_days_ago = timezone.now() - timezone.timedelta(days=90)
        for form in all_forms.filter(status='PUBLISHED', close_at__isnull=True):
            if form.created_at and form.created_at <= ninety_days_ago:
                warnings.append({
                    "type": "no_close_date",
                    "form_id": form.id,
                    "form_title": form.title,
                    "message": "Published for 90+ days with no close date set",
                    "action_link": "forms",
                })

        # Last export (most recent BulkIngestSession)
        last_session = BulkIngestSession.objects.order_by('-created_at').first()
        last_export = last_session.created_at.isoformat() if last_session else None

        # Recent activity (last 10 responses + ingest sessions)
        recent_responses = (
            Response.objects
            .filter(is_test_submission=False)
            .select_related('user', 'form')
            .order_by('-submitted_at')[:5]
        )
        recent_ingests = BulkIngestSession.objects.select_related('created_by', 'form').order_by('-created_at')[:5]

        activity = []
        for resp in recent_responses:
            actor = 'Anonymous'
            if resp.user:
                actor = f'{resp.user.first_name} {resp.user.last_name}'.strip() or resp.user.email
            elif resp.is_manual_entry and resp.created_by_admin:
                actor = resp.created_by_admin.email
            activity.append({
                "type": "submission",
                "actor": actor,
                "detail": f"submitted {resp.form.title}",
                "timestamp": resp.submitted_at.isoformat(),
            })
        for sess in recent_ingests:
            actor = sess.created_by.email if sess.created_by else 'Admin'
            activity.append({
                "type": "csv_import",
                "actor": actor,
                "detail": f"imported {sess.imported_count} rows into {sess.form.title}",
                "timestamp": sess.created_at.isoformat(),
            })

        activity.sort(key=lambda x: x['timestamp'], reverse=True)
        activity = activity[:10]

        return DRFResponse({
            "stats": {
                "total_forms": total_forms,
                "published_forms": published_forms,
                "total_responses": total_responses,
                "completion_rate": completion_rate,
                "warning_count": len(warnings),
                "last_export": last_export,
            },
            "warnings": warnings,
            "recent_activity": activity,
        }, status=status.HTTP_200_OK)

    # -----------------------------------------------------------------------
    # New: paginated responses viewer
    # -----------------------------------------------------------------------

    @action(detail=True, methods=['get'], url_path='responses',
            permission_classes=[permissions.IsAdminUser])
    def responses(self, request, slug=None):
        """
        GET /api/forms/{slug}/responses/
        Params: page, page_size, search, date_from, date_to, manual_only, test_only
        """
        if not request.user.is_staff:
            return DRFResponse({"error": "Staff access required."}, status=status.HTTP_403_FORBIDDEN)

        form = self.get_object()
        qs = Response.objects.filter(form=form).select_related(
            'user', 'created_by_admin'
        ).prefetch_related('answers__field').order_by('-submitted_at')

        # Filters
        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(user__email__icontains=search) |
                Q(user__first_name__icontains=search) |
                Q(user__last_name__icontains=search) |
                Q(answers__value__icontains=search)
            ).distinct()

        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        if date_from:
            qs = qs.filter(submitted_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(submitted_at__date__lte=date_to)

        if request.query_params.get('manual_only') == 'true':
            qs = qs.filter(is_manual_entry=True)
        if request.query_params.get('test_only') == 'true':
            qs = qs.filter(is_test_submission=True)
        else:
            # By default exclude test submissions unless explicitly requested
            if request.query_params.get('test_only') != 'true':
                pass  # Show all unless test_only is set

        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(qs, request)
        serializer = ResponseDetailSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class ResponseViewSet(viewsets.ModelViewSet):
    queryset = Response.objects.all().order_by('-submitted_at')
    serializer_class = ResponseSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    pagination_class = StandardResultsSetPagination

    def create(self, request, *args, **kwargs):
        is_test = request.query_params.get('test') == 'true'
        if is_test:
            if not request.user.is_staff:
                return DRFResponse(
                    {"error": "Test submission mode requires admin staff access."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            return DRFResponse(
                {
                    "status": "SUCCESS",
                    "message": "Test mode simulation complete — no DB record written.",
                    "payload": request.data,
                },
                status=status.HTTP_200_OK,
            )

        form_id = request.data.get('form')
        if not form_id:
            return DRFResponse({"error": "form ID is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            form_obj = Form.objects.get(id=form_id)
        except Form.DoesNotExist:
            return DRFResponse({"error": "Form not found."}, status=status.HTTP_404_NOT_FOUND)

        # Check existing user response & edit window vs deduplication
        if request.user and request.user.is_authenticated:
            existing = Response.objects.filter(
                form=form_obj, user=request.user, is_test_submission=False
            ).first()
            if existing:
                if form_obj.allow_edits_until and timezone.now() <= form_obj.allow_edits_until:
                    serializer = ResponseSerializer(
                        existing, data=request.data, partial=True,
                        context={'form': form_obj, 'request': request},
                    )
                    serializer.is_valid(raise_exception=True)
                    serializer.save()
                    return DRFResponse(serializer.data, status=status.HTTP_200_OK)
                elif not form_obj.allow_multiple_responses:
                    return DRFResponse(
                        {"error": "You have already submitted a response for this form."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        serializer = ResponseSerializer(
            data=request.data, context={'form': form_obj, 'request': request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return DRFResponse(serializer.data, status=status.HTTP_201_CREATED)


class MemberViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET /api/members/
    Aggregates responses by user (excludes anonymous/null-user responses).
    Supports ?search=, ?form_id=, ?page=, ?page_size=
    """
    permission_classes = [permissions.IsAdminUser]
    pagination_class = StandardResultsSetPagination

    def list(self, request, *args, **kwargs):
        if not request.user.is_staff:
            return DRFResponse({"error": "Staff access required."}, status=status.HTTP_403_FORBIDDEN)

        # Only users who have submitted at least one response
        # Exclude anonymous (null user) responses
        user_agg = (
            Response.objects
            .filter(user__isnull=False, is_test_submission=False)
            .values('user', 'user__email', 'user__first_name', 'user__last_name')
            .annotate(
                total_submissions=Count('id'),
                last_active=Max('submitted_at'),
            )
            .order_by('-last_active')
        )

        search = request.query_params.get('search', '').strip()
        if search:
            user_agg = user_agg.filter(
                Q(user__email__icontains=search) |
                Q(user__first_name__icontains=search) |
                Q(user__last_name__icontains=search)
            )

        form_id = request.query_params.get('form_id')
        if form_id:
            user_agg = user_agg.filter(form_id=form_id)

        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(list(user_agg), request)

        results = []
        for row in (page or user_agg):
            user_id = row['user']
            forms_submitted = (
                Response.objects
                .filter(user_id=user_id, is_test_submission=False)
                .select_related('form')
                .order_by('-submitted_at')
            )
            results.append({
                "user_id": user_id,
                "name": f"{row['user__first_name']} {row['user__last_name']}".strip(),
                "email": row['user__email'],
                "total_submissions": row['total_submissions'],
                "last_active": row['last_active'].isoformat() if row['last_active'] else None,
                "forms_submitted": [
                    {
                        "form_id": r.form.id,
                        "form_title": r.form.title,
                        "submitted_at": r.submitted_at.isoformat(),
                    }
                    for r in forms_submitted
                ],
            })

        if page is not None:
            return paginator.get_paginated_response(results)
        return DRFResponse(results)

    def retrieve(self, request, *args, **kwargs):
        return DRFResponse({"detail": "Not implemented."}, status=status.HTTP_404_NOT_FOUND)
