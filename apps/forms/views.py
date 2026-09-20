from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response as DRFResponse
from rest_framework.pagination import PageNumberPagination
from django.utils import timezone
from django.db import transaction
from django.db.models import Count, Max, Prefetch, Q
from django.db.models.functions import Cast
from django.db.models import TextField

from .models import Form, FormField, FormStatus, Response, Answer, BulkIngestSession, MemberNote, sync_all_scheduled_form_statuses
from .serializers import (
    FormSerializer,
    ResponseSerializer,
    ResponseDetailSerializer,
    BulkIngestSessionSerializer,
    check_field_conditional_dependencies,
    evaluate_visible_fields,
)
from .validation import validate_submission, validate_form_definition, PARTIAL
from apps.audit.utils import log_audit_event
from apps.core.permissions import IsAdminOrClubLead, IsAdminOrClubLeadOrReadOnly, IsOwnerOrAdminOrClubLead
from apps.accounts.services.user_account_service import ClubIdImmutableError, ClubIdConflictError, RollNumberConflictError
from .services import FormAutomationService
from apps.core.models import EmailDelivery
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
    permission_classes = [IsAdminOrClubLeadOrReadOnly]
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
        Publish the form, making it live for public responses. Blocks on a
        structurally-invalid definition; returns soft warnings alongside success.
        """
        form = self.get_object()

        report = validate_form_definition(form)
        if not report.publishable:
            return DRFResponse({
                "detail": "This form cannot be published — its definition has blocking problems.",
                "code": "FORM_DEFINITION_INVALID",
                "errors": [e.as_dict() for e in report.errors],
                "warnings": [w.as_dict() for w in report.warnings],
            }, status=status.HTTP_400_BAD_REQUEST)

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
        payload = dict(serializer.data)
        payload["warnings"] = [w.as_dict() for w in report.warnings]
        return DRFResponse(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='validate')
    def validate_definition(self, request, slug=None):
        """
        GET /api/forms/{slug}/validate/
        Returns the form-definition report (blocking errors + soft warnings +
        publishable flag) so the builder can show pre-publish diagnostics.
        """
        report = validate_form_definition(self.get_object())
        return DRFResponse(report.as_dict(), status=status.HTTP_200_OK)

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

        Runs the validation engine in PARTIAL mode: structural / type / option
        errors block, but required + constraint failures are returned as
        ``warnings`` and the entry is still stored (admins routinely capture
        incomplete legacy data). Pass ``?force=true`` to also store rows that
        have blocking errors (recorded in the audit log).
        """
        form = self.get_object()
        answers_data = request.data.get('answers', [])
        force = request.query_params.get('force') == 'true'

        report = validate_submission(form, answers_data, mode=PARTIAL)
        if report.errors and not force:
            return DRFResponse({
                "detail": "This manual entry has blocking problems.",
                "code": "VALIDATION_FAILED",
                "errors": [e.as_dict() for e in report.errors],
                "warnings": [w.as_dict() for w in report.warnings],
            }, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            response_obj = Response.objects.create(
                form=form,
                user=None,
                is_manual_entry=True,
                created_by_admin=request.user if (request.user and request.user.is_authenticated) else None,
                form_version=form.version,
            )
            for field_id, value in report.cleaned_answers.items():
                Answer.objects.create(response=response_obj, field_id=field_id, value=value)

        log_audit_event(
            actor=request.user,
            action="Submitted Offline Manual Entry",
            target_model="Form",
            target_id=form.slug,
            details={
                "title": form.title, "response_id": response_obj.id, "is_manual_entry": True,
                "forced": bool(report.errors and force),
                "warning_count": len(report.warnings),
            }
        )

        payload = dict(ResponseDetailSerializer(response_obj).data)
        payload["warnings"] = [w.as_dict() for w in report.warnings]
        if report.errors:
            payload["errors"] = [e.as_dict() for e in report.errors]
        return DRFResponse(payload, status=status.HTTP_201_CREATED)

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

                    # Convert the {str(field_id): value} row into the engine's
                    # answer wire shape, then validate in PARTIAL mode.
                    raw_answers = [
                        {"field": fid, "value": row.get(fid)}
                        for fid in fields_by_id
                        if row.get(fid) not in (None, "")
                    ]
                    report = validate_submission(form, raw_answers, mode=PARTIAL)

                    if report.errors:
                        row_errors = [{
                            "row": row_number,
                            "field": e.label or (str(e.field_id) if e.field_id else "__row__"),
                            "value": "",
                            "error": e.message,
                        } for e in report.errors]
                        if not skip_errors:
                            raise Exception(f"Validation error at row {row_number}: {row_errors}")
                        error_log.extend(row_errors)
                        skipped_count += 1
                        continue

                    # Partial-mode warnings (missing required, constraint misses) are
                    # recorded but do not block the row.
                    for w in report.warnings:
                        error_log.append({
                            "row": row_number,
                            "field": w.label or "__row__",
                            "value": "",
                            "error": f"[warning] {w.message}",
                        })

                    # Duplicate check by the form's email field.
                    email_field = next((f for f in active_fields if f.type == 'EMAIL'), None)
                    email_value = None
                    if email_field:
                        cleaned_email = report.cleaned_answers.get(email_field.id)
                        email_value = str(cleaned_email).strip() if cleaned_email else None

                    if not form.allow_multiple_responses and email_value:
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
                            for field_id, value in report.cleaned_answers.items():
                                Answer.objects.create(
                                    response=response_obj, field_id=field_id, value=value,
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

    @action(detail=True, methods=['post'], url_path='responses/bulk-delete',
            permission_classes=[IsAdminOrClubLead])
    def bulk_delete_responses(self, request, slug=None):
        """
        POST /api/forms/{slug}/responses/bulk-delete/
        Body: {"response_ids": [1, 2, 3]}

        Deletes the given responses in one transaction. Every id must belong
        to this form — if any don't (or don't exist at all), nothing is
        deleted and a 400 lists the offending ids.
        """
        form = self.get_object()
        response_ids = request.data.get('response_ids')
        if not isinstance(response_ids, list) or not response_ids:
            return DRFResponse(
                {"error": "response_ids must be a non-empty list of response IDs."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            requested_ids = {int(rid) for rid in response_ids}
        except (TypeError, ValueError):
            return DRFResponse(
                {"error": "response_ids must be a list of integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = Response.objects.filter(id__in=requested_ids, form=form)
        valid_ids = set(qs.values_list('id', flat=True))
        invalid_ids = requested_ids - valid_ids
        if invalid_ids:
            return DRFResponse(
                {"error": f"Response(s) not found on this form: {sorted(invalid_ids)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        deleted_count = qs.count()
        with transaction.atomic():
            qs.delete()

        log_audit_event(
            actor=request.user,
            action=f"Bulk-deleted {deleted_count} Response(s)",
            target_model="Form",
            target_id=form.slug,
            details={"response_ids": sorted(requested_ids)},
        )

        return DRFResponse({"deleted_count": deleted_count}, status=status.HTTP_200_OK)


class ResponseViewSet(viewsets.ModelViewSet):
    """
    Response records may contain sensitive PII from any form (phone numbers,
    addresses, etc). create() stays open to anonymous callers because public
    form submission (including by logged-out visitors) is a supported flow,
    but listing/reading/editing another user's response is admin-only or
    restricted to the response's own owner.
    """
    queryset = Response.objects.select_related('form', 'user', 'created_by_admin').prefetch_related(
        'answers__field',
        Prefetch('confirmation_email_deliveries', queryset=EmailDelivery.objects.order_by('-created_at')),
    ).all().order_by('-submitted_at')
    serializer_class = ResponseSerializer
    pagination_class = StandardResultsSetPagination

    def get_permissions(self):
        if self.action == 'create':
            return [permissions.AllowAny()]
        if self.action in ['list', 'retrieve', 'resend_confirmation_email']:
            return [IsAdminOrClubLead()]
        return [permissions.IsAuthenticated(), IsOwnerOrAdminOrClubLead()]

    def get_throttles(self):
        # Public form submission is anonymous-writable; scope-limit it beyond the
        # global anon rate so a form can't be flooded.
        if self.action == 'create':
            from rest_framework.throttling import ScopedRateThrottle
            self.throttle_scope = 'form_submit'
            return [ScopedRateThrottle()]
        return super().get_throttles()

    @staticmethod
    def _effective_max_responses(form_obj):
        """None == unlimited. A single-submission form is handled elsewhere."""
        if not form_obj.allow_multiple_responses:
            return 1
        m = form_obj.max_responses_per_user or 0
        return m if m > 1 else None

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

        resp_status = status.HTTP_201_CREATED
        resolved_user = None
        response_obj = None

        # Note: a plain `return` from inside `transaction.atomic()` COMMITS (Django
        # only rolls back on a propagating exception) — so the Club ID conflict
        # exceptions below are deliberately left to propagate out of this block
        # instead of being caught-and-returned from inside it, and are only turned
        # into an HTTP response in the `except` after the block (and its automatic
        # rollback) has finished.
        try:
            with transaction.atomic():
                # Check existing user response & edit window vs deduplication
                if user_to_assign:
                    user_responses = Response.objects.filter(
                        form=form_obj, user=user_to_assign, is_test_submission=False
                    )
                    existing = user_responses.first()
                    if existing:
                        now = timezone.now()
                        # Only meaningful in the allow_multiple_responses=False branch
                        # below — "edit my one response in place" is a single-response
                        # concept. When multiple responses ARE allowed, a prior response
                        # must never block or redirect a new one (the frontend used to
                        # get this wrong too: it forced edit mode — PATCHing the first
                        # response — the moment any response existed, regardless of this
                        # flag, so a second independent submission was never reachable
                        # through the UI even though this endpoint already supported it
                        # via the max_responses_per_user cap below).
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
                            response_obj = serializer.instance
                            resp_status = status.HTTP_200_OK
                        else:
                            # Multiple submissions allowed — enforce max_responses_per_user.
                            cap = self._effective_max_responses(form_obj)
                            if cap is not None and user_responses.count() >= cap:
                                return DRFResponse(
                                    {"error": f"You have reached the maximum of {cap} submissions for this form.",
                                     "code": "MAX_RESPONSES_REACHED"},
                                    status=status.HTTP_400_BAD_REQUEST,
                                )

                if response_obj is None:
                    serializer = ResponseSerializer(
                        data=request.data, context={'form': form_obj, 'request': request}
                    )
                    serializer.is_valid(raise_exception=True)
                    serializer.save(user=user_to_assign)
                    response_obj = serializer.instance
                    resp_status = status.HTTP_201_CREATED

                submission_warnings = getattr(serializer, '_validation_warnings', [])

                # Club Member ID automation: find-or-create the club member by the
                # form's mapped email field and allocate a permanent Club ID if they
                # don't already have one. Runs inside this same atomic block so a
                # conflict (e.g. ClubIdImmutableError) rolls back the response with it
                # — "all or nothing," matching the requirement that a Club ID is only
                # ever persisted once the response itself is fully completed.
                if form_obj.club_id_enabled:
                    answers_by_field_id = {
                        a.field_id: a.value for a in response_obj.answers.all()
                    }
                    resolved_user = FormAutomationService.resolve_club_member(form_obj, answers_by_field_id)
                    if resolved_user and response_obj.user_id != resolved_user.id:
                        response_obj.user = resolved_user
                        response_obj.save(update_fields=['user'])

                # QR-code attendance automation: issue this response's permanent
                # attendance badge as soon as it completes. get_or_create'd, so
                # this is a no-op on the "edit an existing single-submission
                # response" branch above (the badge already exists).
                if form_obj.attendance_enabled:
                    from apps.attendance.services import issue_badge
                    issue_badge(response_obj)

                # Auto-close once the form-wide response cap is reached. Only a
                # genuine new response (201) counts toward the total — editing an
                # existing one in place (200, the `allow_multiple_responses=False`
                # auto-update branch above) doesn't change the total response count.
                if resp_status == status.HTTP_201_CREATED and form_obj.max_total_responses is not None:
                    total_responses = Response.objects.filter(form=form_obj, is_test_submission=False).count()
                    if total_responses >= form_obj.max_total_responses and form_obj.status != FormStatus.CLOSED:
                        form_obj.status = FormStatus.CLOSED
                        form_obj.save(update_fields=['status', 'updated_at'])

                resp_data = ResponseSerializer(response_obj, context={'form': form_obj, 'request': request}).data
                if submission_warnings:
                    resp_data = dict(resp_data)
                    resp_data['warnings'] = submission_warnings
        except (ClubIdImmutableError, ClubIdConflictError, RollNumberConflictError) as ex:
            return DRFResponse({"error": str(ex)}, status=status.HTTP_400_BAD_REQUEST)

        # Confirmation email dispatch happens AFTER the transaction commits — never
        # from inside an open transaction, so a slow/failed send can't hold a DB lock
        # or roll back an otherwise-successful submission.
        if form_obj.confirmation_email_enabled:
            answers_by_field_id = {a.field_id: a.value for a in response_obj.answers.all()}
            FormAutomationService.dispatch_confirmation_email(form_obj, resolved_user, answers_by_field_id, response=response_obj)

        store_idempotent_response(key, request, resp_status, resp_data)
        return DRFResponse(resp_data, status=resp_status)

    def update(self, request, *args, **kwargs):
        """
        PUT/PATCH /api/forms/submissions/{id}/ — direct response edit.

        Previously this inherited path skipped validation entirely (the serializer
        had no ``form`` in context). It now injects the form and fully
        re-validates against the current definition, and refuses edits once the
        form is CLOSED or past its edit window.
        """
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        form_obj = instance.form
        now = timezone.now()

        if form_obj.status == 'CLOSED':
            return DRFResponse({"error": "This form is closed; responses can no longer be edited.",
                                "code": "FORM_CLOSED"}, status=status.HTTP_400_BAD_REQUEST)
        if not form_obj.allow_response_editing:
            return DRFResponse({"error": "Response editing is disabled for this form.",
                                "code": "EDITING_DISABLED"}, status=status.HTTP_400_BAD_REQUEST)
        if form_obj.allow_edits_until and now > form_obj.allow_edits_until:
            return DRFResponse({"error": "The edit window for this form has closed.",
                                "code": "EDIT_WINDOW_CLOSED"}, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(
            instance, data=request.data, partial=partial,
            context={'form': form_obj, 'request': request, 'mode': 'strict'},
        )
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            serializer.save()

        log_audit_event(
            actor=request.user if request.user.is_authenticated else None,
            action="Edited Form Response",
            target_model="Form",
            target_id=str(form_obj.id),
            details={"response_id": instance.id, "form_title": form_obj.title,
                     "form_version": instance.form_version},
        )

        data = dict(ResponseDetailSerializer(instance).data)
        warnings = getattr(serializer, '_validation_warnings', [])
        if warnings:
            data['warnings'] = warnings
        return DRFResponse(data, status=status.HTTP_200_OK)

    def partial_update(self, request, *args, **kwargs):
        kwargs['partial'] = True
        return self.update(request, *args, **kwargs)

    @action(detail=True, methods=['post'], url_path='resend-confirmation-email')
    def resend_confirmation_email(self, request, pk=None):
        """
        POST /api/forms/submissions/{id}/resend-confirmation-email/
        Manually re-triggers the form's confirmation email for this response.
        Admin/Club-Lead only (see get_permissions). Unlike the auto-fire path,
        failures are reported back rather than swallowed, since an admin
        explicitly asking for a resend needs to know if it didn't work.
        """
        response_obj = self.get_object()
        form_obj = response_obj.form
        answers_by_field_id = {a.field_id: a.value for a in response_obj.answers.all()}

        try:
            job = FormAutomationService.send_confirmation_email(
                form_obj, response_obj.user, answers_by_field_id, response=response_obj,
            )
        except Exception as ex:
            return DRFResponse({"error": str(ex)}, status=status.HTTP_400_BAD_REQUEST)

        log_audit_event(
            actor=request.user,
            action="Confirmation Email Resent",
            target_model="Response",
            target_id=str(response_obj.id),
            details={"form": form_obj.title, "job_id": str(job.id)},
        )

        delivery = job.deliveries.first()
        return DRFResponse({
            "success": True,
            "job_id": str(job.id),
            "status": delivery.status if delivery else job.status,
            "recipient_email": delivery.recipient_email if delivery else None,
        })


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
