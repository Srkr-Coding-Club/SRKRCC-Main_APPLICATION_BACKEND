from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response as DRFResponse
from rest_framework.pagination import PageNumberPagination
from django.utils import timezone
from django.db import transaction
from django.db.models import Count, Max, Q
from django.db.models.functions import Cast
from django.db.models import TextField

from .models import Form, FormField, Response, Answer, BulkIngestSession, MemberNote, sync_all_scheduled_form_statuses
from .serializers import (
    FormSerializer,
    ResponseSerializer,
    ResponseDetailSerializer,
    BulkIngestSessionSerializer,
    check_field_conditional_dependencies,
    evaluate_visible_fields,
    enforce_field_validation,
)
from apps.audit.utils import log_audit_event
from apps.core.permissions import IsAdminOrClubLead
from apps.core.idempotency import (
    get_idempotency_key,
    check_idempotent_response,
    store_idempotent_response,
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
        # Auto-sync and transition schedule states (SCHEDULED -> PUBLISHED on open_at, PUBLISHED/SCHEDULED -> CLOSED on close_at)
        try:
            sync_all_scheduled_form_statuses()
        except Exception:
            pass

        return Form.objects.annotate(
            response_count=Count(
                'responses',
                filter=Q(responses__is_test_submission=False),
            )
        ).order_by('-created_at')

    def create(self, request, *args, **kwargs):
        key = get_idempotency_key(request)
        cached = check_idempotent_response(key, request)
        if cached:
            return cached

        resp = super().create(request, *args, **kwargs)
        if resp.status_code in [200, 201]:
            store_idempotent_response(key, request, resp.status_code, resp.data)
        return resp

    def perform_create(self, serializer):
        form = serializer.save()
        log_audit_event(
            actor=self.request.user,
            action=f"Created Dynamic Form ({form.status})",
            target_model="Form",
            target_id=form.slug,
            details={"title": form.title, "status": form.status, "fields_count": form.fields.count()}
        )

    def perform_update(self, serializer):
        form = serializer.save()
        log_audit_event(
            actor=self.request.user,
            action=f"Updated Form Schema (v{form.version})",
            target_model="Form",
            target_id=form.slug,
            details={"title": form.title, "status": form.status, "version": form.version}
        )

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
    # Form Lifecycle Management Actions
    # -----------------------------------------------------------------------

    @action(detail=True, methods=['post'], url_path='publish')
    def publish(self, request, slug=None):
        """
        POST /api/forms/{slug}/publish/
        Publish the form, making it live for public responses.
        """
        form = self.get_object()
        form.status = 'PUBLISHED'
        form.version += 1
        form.save(update_fields=['status', 'version', 'updated_at'])
        log_audit_event(
            actor=request.user,
            action="Published Dynamic Form",
            target_model="Form",
            target_id=form.slug,
            details={"title": form.title, "status": "PUBLISHED", "version": form.version}
        )
        serializer = self.get_serializer(form)
        return DRFResponse(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='unpublish')
    def unpublish(self, request, slug=None):
        """
        POST /api/forms/{slug}/unpublish/
        Reverts a published or scheduled form back to DRAFT mode (Undo publish).
        """
        form = self.get_object()
        form.status = 'DRAFT'
        form.save(update_fields=['status', 'updated_at'])
        log_audit_event(
            actor=request.user,
            action="Unpublished Form to Draft",
            target_model="Form",
            target_id=form.slug,
            details={"title": form.title, "status": "DRAFT"}
        )
        serializer = self.get_serializer(form)
        return DRFResponse(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='my-response')
    def my_response(self, request, slug=None):
        """
        GET /api/forms/{slug}/my-response/
        Returns the logged in user's most recent response for this form (if any),
        along with whether response editing is permitted.
        """
        form = self.get_object()
        user = request.user if request.user and request.user.is_authenticated else None
        if not user and request.query_params.get('user_id'):
            try:
                from django.contrib.auth import get_user_model
                User = get_user_model()
                user = User.objects.get(id=request.query_params.get('user_id'))
            except Exception:
                user = None

        if not user:
            return DRFResponse({
                "has_submitted": False,
                "can_edit": True,
                "allow_multiple_responses": form.allow_multiple_responses,
                "allow_response_editing": form.allow_response_editing,
                "response": None,
            }, status=status.HTTP_200_OK)

        response_obj = Response.objects.filter(
            form=form, user=user, is_test_submission=False
        ).order_by('-submitted_at').first()

        if not response_obj:
            return DRFResponse({
                "has_submitted": False,
                "can_edit": True,
                "allow_multiple_responses": form.allow_multiple_responses,
                "allow_response_editing": form.allow_response_editing,
                "response": None,
            }, status=status.HTTP_200_OK)

        now = timezone.now()
        can_edit = form.allow_response_editing
        if form.allow_edits_until and now > form.allow_edits_until:
            can_edit = False
        if form.status == 'CLOSED':
            can_edit = False

        serializer = ResponseDetailSerializer(response_obj)
        return DRFResponse({
            "has_submitted": True,
            "can_edit": can_edit,
            "allow_multiple_responses": form.allow_multiple_responses,
            "allow_response_editing": form.allow_response_editing,
            "response": serializer.data,
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='close')
    def close(self, request, slug=None):
        """
        POST /api/forms/{slug}/close/
        Closes the form to public submissions.
        """
        form = self.get_object()
        form.status = 'CLOSED'
        form.save(update_fields=['status', 'updated_at'])
        log_audit_event(
            actor=request.user,
            action="Closed Form Submissions",
            target_model="Form",
            target_id=form.slug,
            details={"title": form.title, "status": "CLOSED"}
        )
        serializer = self.get_serializer(form)
        return DRFResponse(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='schedule')
    def schedule(self, request, slug=None):
        """
        POST /api/forms/{slug}/schedule/
        Sets form to SCHEDULED with open_at and close_at windows.
        """
        from django.utils.dateparse import parse_datetime
        from datetime import datetime

        form = self.get_object()
        open_at_raw = request.data.get('open_at')
        close_at_raw = request.data.get('close_at')

        def _parse_dt(val):
            if not val or not str(val).strip():
                return None
            val_str = str(val).strip()
            dt = parse_datetime(val_str)
            if not dt:
                try:
                    dt = datetime.fromisoformat(val_str)
                except Exception:
                    pass
            if dt and timezone.is_naive(dt):
                dt = timezone.make_aware(dt)
            return dt

        try:
            parsed_open = _parse_dt(open_at_raw) if open_at_raw is not None else form.open_at
            parsed_close = _parse_dt(close_at_raw) if close_at_raw is not None else form.close_at

            if parsed_open and parsed_close and parsed_open > parsed_close:
                return DRFResponse(
                    {"error": "Opening date/time cannot be later than closing date/time."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            form.status = 'SCHEDULED'
            form.open_at = parsed_open
            form.close_at = parsed_close
            form.save(update_fields=['status', 'open_at', 'close_at', 'updated_at'])

            log_audit_event(
                actor=request.user,
                action="Scheduled Form Window",
                target_model="Form",
                target_id=form.slug,
                details={
                    "title": form.title,
                    "open_at": form.open_at.isoformat() if form.open_at else None,
                    "close_at": form.close_at.isoformat() if form.close_at else None,
                },
            )
            serializer = self.get_serializer(form)
            return DRFResponse(serializer.data, status=status.HTTP_200_OK)
        except Exception as e:
            return DRFResponse(
                {"error": f"Failed to schedule form: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=True, methods=['post'], url_path='reopen')
    def reopen(self, request, slug=None):
        """
        POST /api/forms/{slug}/reopen/
        Re-opens a closed form as DRAFT or PUBLISHED.
        """
        form = self.get_object()
        target_status = request.data.get('status', 'DRAFT')
        if target_status not in ['DRAFT', 'PUBLISHED', 'SCHEDULED']:
            target_status = 'DRAFT'
        form.status = target_status
        form.save(update_fields=['status', 'updated_at'])
        log_audit_event(
            actor=request.user,
            action=f"Re-opened Form as {target_status}",
            target_model="Form",
            target_id=form.slug,
            details={"title": form.title, "status": target_status}
        )
        serializer = self.get_serializer(form)
        return DRFResponse(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='manual-entry')
    def manual_entry(self, request, slug=None):
        """
        POST /api/forms/{slug}/manual-entry/
        Records an administrative manual submission for ANY form (Live, Draft, or Closed).
        """
        form = self.get_object()
        answers_data = request.data.get('answers', [])
        
        response_obj = Response.objects.create(
            form=form,
            user=None,
            is_manual_entry=True,
            created_by_admin=request.user if (request.user and request.user.is_authenticated) else None,
            form_version=form.version,
        )

        for ans in answers_data:
            field_id = ans.get('field')
            value = ans.get('value')
            try:
                field_obj = FormField.all_objects.get(id=field_id, form=form)
                Answer.objects.create(
                    response=response_obj,
                    field=field_obj,
                    value=value,
                )
            except FormField.DoesNotExist:
                continue

        log_audit_event(
            actor=request.user,
            action="Submitted Offline Manual Entry",
            target_model="Form",
            target_id=form.slug,
            details={"title": form.title, "response_id": response_obj.id, "is_manual_entry": True}
        )

        serializer = ResponseDetailSerializer(response_obj)
        return DRFResponse(serializer.data, status=status.HTTP_201_CREATED)

    # -----------------------------------------------------------------------
    # New: bulk CSV ingest
    # -----------------------------------------------------------------------

    @action(detail=True, methods=['post'], url_path='bulk-ingest',
            permission_classes=[IsAdminOrClubLead])
    def bulk_ingest(self, request, slug=None):
        """
        POST /api/forms/{slug}/bulk-ingest/

        Bulk-import pre-validated rows into this form.
        Supports idempotency via idempotency_key.
        Uses per-row savepoints so skip_errors=true works correctly.
        """
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
            permission_classes=[IsAdminOrClubLead])
    def check_duplicates(self, request, slug=None):
        """
        POST /api/forms/{slug}/check-duplicates/
        Body: { "emails": ["email1@x.com", ...] }

        Returns existing responses matched by email (JSONField-safe cast).
        """
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
            permission_classes=[IsAdminOrClubLead])
    def data_health(self, request):
        """
        GET /api/forms/data-health/

        Returns aggregated stats, active warnings, and recent activity feed.
        """
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
            permission_classes=[IsAdminOrClubLead])
    def responses(self, request, slug=None):
        """
        GET /api/forms/{slug}/responses/
        Params: page, page_size, search, date_from, date_to, manual_only, test_only
        """
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
    queryset = Response.objects.select_related('form', 'user', 'created_by_admin').prefetch_related('answers__field').all().order_by('-submitted_at')
    serializer_class = ResponseSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    pagination_class = StandardResultsSetPagination

    def get_serializer_class(self):
        if self.action in ['list', 'retrieve']:
            return ResponseDetailSerializer
        return ResponseSerializer

    def create(self, request, *args, **kwargs):
        key = get_idempotency_key(request)
        cached = check_idempotent_response(key, request)
        if cached:
            return cached

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

        now = timezone.now()
        form_obj.evaluate_schedule_status(now)

        if form_obj.status == 'CLOSED':
            return DRFResponse({"error": "This form has closed to public submissions."}, status=status.HTTP_400_BAD_REQUEST)
        if form_obj.status == 'DRAFT':
            return DRFResponse({"error": "This form is in draft mode and not yet open for responses."}, status=status.HTTP_400_BAD_REQUEST)
        if form_obj.status == 'SCHEDULED':
            if form_obj.open_at and now < form_obj.open_at:
                return DRFResponse({"error": f"Submissions for this form will open on {form_obj.open_at.isoformat()}."}, status=status.HTTP_400_BAD_REQUEST)
        if form_obj.close_at and now > form_obj.close_at:
            return DRFResponse({"error": f"Submissions for this form closed on {form_obj.close_at.isoformat()}."}, status=status.HTTP_400_BAD_REQUEST)

        user_to_assign = request.user if request.user and request.user.is_authenticated else None
        if not user_to_assign and request.data.get('user'):
            try:
                from django.contrib.auth import get_user_model
                User = get_user_model()
                user_to_assign = User.objects.get(id=request.data.get('user'))
            except Exception:
                user_to_assign = None

        with transaction.atomic():
            # Check existing user response & edit window vs deduplication
            if user_to_assign:
                existing = Response.objects.filter(
                    form=form_obj, user=user_to_assign, is_test_submission=False
                ).first()
                if existing:
                    now = timezone.now()
                    is_edit_locked = (
                        not form_obj.allow_response_editing
                        or (form_obj.allow_edits_until and now > form_obj.allow_edits_until)
                        or form_obj.status == 'CLOSED'
                    )

                    if not form_obj.allow_multiple_responses:
                        if is_edit_locked:
                            return DRFResponse(
                                {"error": "You have already submitted a response for this form and edits are not permitted."},
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                        # Auto-update existing response when single submission is configured and edit is allowed
                        serializer = ResponseSerializer(
                            existing, data=request.data, partial=True,
                            context={'form': form_obj, 'request': request},
                        )
                        serializer.is_valid(raise_exception=True)
                        serializer.save(user=user_to_assign)
                        resp_data = serializer.data
                        store_idempotent_response(key, request, 200, resp_data)
                        return DRFResponse(resp_data, status=status.HTTP_200_OK)

            serializer = ResponseSerializer(
                data=request.data, context={'form': form_obj, 'request': request}
            )
            serializer.is_valid(raise_exception=True)
            serializer.save(user=user_to_assign)
            resp_data = serializer.data
            store_idempotent_response(key, request, 201, resp_data)
            return DRFResponse(resp_data, status=status.HTTP_201_CREATED)


class MemberViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET /api/members/
    Aggregates responses by user (excludes anonymous/null-user responses).
    Supports ?search=, ?form_id=, ?page=, ?page_size=
    """
    permission_classes = [IsAdminOrClubLead]
    pagination_class = StandardResultsSetPagination

    def list(self, request, *args, **kwargs):

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
