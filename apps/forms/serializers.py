import base64
import re
import uuid
import logging

from rest_framework import serializers
from rest_framework.exceptions import APIException
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone

from .models import Form, FormField, FieldType, FormStatus, Response, Answer, BulkIngestSession
from .validation import (
    validate_submission,
    validate_form_definition,
    normalize_validation_rules,
    normalize_conditional_logic,
    FieldError,
    STRICT,
    PARTIAL,
)
# Backward-compat: the conditional helpers now live in apps.forms.validation.
# Re-exported here because apps.forms.views still imports them by name.
from .validation.conditional import evaluate_condition, evaluate_visible_fields  # noqa: F401

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signature file handling
# ---------------------------------------------------------------------------

def save_signature_to_storage(base64_string: str) -> str:
    """
    Decode a base64 PNG string and save it via Django's default_storage.
    Returns the URL of the saved file.
    Supports both raw base64 and data-URI format ("data:image/png;base64,...").
    """
    try:
        if "," in base64_string:
            base64_string = base64_string.split(",", 1)[1]
        file_data = base64.b64decode(base64_string)
        filename = f"signatures/{uuid.uuid4().hex}.png"
        saved_path = default_storage.save(filename, ContentFile(file_data))
        return default_storage.url(saved_path)
    except Exception as exc:
        logger.warning("Signature file save failed, storing raw base64 fallback: %s", exc)
        # Graceful fallback: return the original string so no data is lost
        return base64_string


EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


class FormValidationError(APIException):
    """
    Carries the engine's structured report ({detail, code, errors[], warnings[]})
    to the client verbatim.

    A plain ``APIException`` (not ``serializers.ValidationError``) so it is NOT
    caught-and-rewrapped by ``serializer.is_valid()`` — it propagates straight to
    DRF's exception handler, which returns a dict ``detail`` as-is. Result: the
    client gets exactly ``report.as_dict()`` with a 400.
    """
    status_code = 400
    default_code = "VALIDATION_FAILED"

    def __init__(self, payload: dict):
        self.detail = payload


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

class FormFieldSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)

    class Meta:
        model = FormField
        fields = [
            'id', 'label', 'type', 'placeholder', 'is_required',
            'options', 'rows', 'min_value', 'max_value',
            'conditional_logic', 'validation_rules', 'order',
        ]

    def validate(self, attrs):
        """Canonicalize the two JSON config blobs on write so submissions and
        the publish gate always see one shape."""
        ftype = attrs.get('type') or getattr(self.instance, 'type', FieldType.TEXT)
        if 'validation_rules' in attrs:
            attrs['validation_rules'] = normalize_validation_rules(ftype, attrs.get('validation_rules'))
        if 'conditional_logic' in attrs:
            attrs['conditional_logic'] = normalize_conditional_logic(attrs.get('conditional_logic'))
        return attrs


class FormSerializer(serializers.ModelSerializer):
    fields = FormFieldSerializer(many=True, required=False)
    image_url = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    # Populated via annotation in get_queryset; safe to omit in write operations
    response_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Form
        fields = [
            'id', 'title', 'slug', 'description', 'image_url', 'category', 'status',
            'version', 'allow_multiple_responses', 'allow_response_editing', 'enable_prefill', 'max_responses_per_user',
            'max_total_responses', 'prevent_duplicate_email_answers',
            'allow_edits_until', 'open_at', 'close_at',
            'club_id_enabled', 'club_id_prefix', 'club_id_field_mapping',
            'confirmation_email_enabled', 'confirmation_email_template',
            'attendance_enabled', 'attendance_start_date', 'attendance_days',
            'attendance_sessions_per_day', 'attendance_window_minutes',
            'fields', 'created_at', 'response_count',
        ]

    def validate_image_url(self, value):
        if not value or not str(value).strip():
            return None
        return str(value).strip()

    def validate_club_id_prefix(self, value):
        clean = (value or '').strip().upper()
        if not re.match(r'^[A-Z]{2,6}$', clean):
            raise serializers.ValidationError("Club ID prefix must be 2-6 letters (e.g. 'SCC').")
        return clean

    def validate_attendance_days(self, value):
        if not (1 <= value <= 30):
            raise serializers.ValidationError("attendance_days must be between 1 and 30.")
        return value

    def validate_attendance_sessions_per_day(self, value):
        if value not in (1, 2, 3):
            raise serializers.ValidationError(
                "attendance_sessions_per_day must be 1 (Morning), 2 (Morning + Afternoon), or 3 (Morning + Afternoon + Evening)."
            )
        return value

    def validate(self, data):
        # DRAFT saves stay lenient (a form is built incrementally — the builder
        # itself tells the admin to enable Club ID / confirmation email, save
        # once to get real field IDs, *then* come back to finish the field
        # mapping / template pick). Only PUBLISHED / SCHEDULED must have this
        # automation fully configured, matching the same leniency the
        # form-definition check below already applies.
        target_status = data.get('status', getattr(self.instance, 'status', FormStatus.DRAFT))
        going_live = target_status in (FormStatus.PUBLISHED, FormStatus.SCHEDULED)

        club_id_enabled = data.get('club_id_enabled', getattr(self.instance, 'club_id_enabled', False))
        if club_id_enabled and going_live:
            mapping = data.get('club_id_field_mapping', getattr(self.instance, 'club_id_field_mapping', None) or {})
            if not mapping.get('email'):
                raise serializers.ValidationError({
                    'club_id_field_mapping': "Club ID generation requires an 'email' field mapping — pick which form field supplies the member's email.",
                })

        confirmation_email_enabled = data.get('confirmation_email_enabled', getattr(self.instance, 'confirmation_email_enabled', False))
        if confirmation_email_enabled and going_live:
            template = data.get('confirmation_email_template', getattr(self.instance, 'confirmation_email_template', None))
            if not template:
                raise serializers.ValidationError({
                    'confirmation_email_template': "Select or create a template before enabling the confirmation email.",
                })

        attendance_enabled = data.get('attendance_enabled', getattr(self.instance, 'attendance_enabled', False))
        if attendance_enabled and going_live:
            start_date = data.get('attendance_start_date', getattr(self.instance, 'attendance_start_date', None))
            if not start_date:
                raise serializers.ValidationError({
                    'attendance_start_date': "Attendance tracking requires a start date.",
                })

        # Form-definition validation. DRAFT saves stay lenient (a form is built
        # incrementally); PUBLISHED / SCHEDULED must have a fully valid definition
        # so every submission against it can be validated.
        if going_live and 'fields' in data:
            report = validate_form_definition({'fields': data.get('fields') or []})
            if not report.publishable:
                raise FormValidationError({
                    'detail': 'This form cannot be published — its definition has blocking problems.',
                    'code': 'FORM_DEFINITION_INVALID',
                    'errors': [e.as_dict() for e in report.errors],
                    'warnings': [w.as_dict() for w in report.warnings],
                })
            self._definition_warnings = [w.as_dict() for w in report.warnings]

        return data

    def create(self, validated_data):
        fields_data = validated_data.pop('fields', [])
        form = Form.objects.create(**validated_data)
        for order, field_data in enumerate(fields_data, start=1):
            field_data.pop('id', None)
            field_order = field_data.pop('order', order)
            FormField.objects.create(form=form, order=field_order, **field_data)

        # Attendance sessions are generated/kept in sync with the form's own
        # attendance_* config, the same way club_id / confirmation-email
        # automations react to their own toggle+config fields.
        from apps.attendance.services import generate_sessions
        generate_sessions(form)
        return form

    def update(self, instance, validated_data):
        fields_data = validated_data.pop('fields', None)
        instance.version += 1
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if fields_data is not None:
            incoming_field_ids = {
                f['id'] for f in fields_data
                if f.get('id') and isinstance(f['id'], int) and 0 < f['id'] < 2147483647
            }
            # Soft delete fields that are no longer present in the incoming payload
            if incoming_field_ids:
                instance.fields.filter(is_deleted=False).exclude(id__in=incoming_field_ids).update(is_deleted=True)
            else:
                instance.fields.filter(is_deleted=False).update(is_deleted=True)

            for order, field_data in enumerate(fields_data, start=1):
                f_id = field_data.get('id')
                if (
                    f_id
                    and isinstance(f_id, int)
                    and 0 < f_id < 2147483647
                    and FormField.all_objects.filter(id=f_id, form=instance).exists()
                ):
                    field_obj = FormField.all_objects.get(id=f_id, form=instance)
                    field_obj.is_deleted = False
                    field_order = field_data.pop('order', order)
                    field_obj.order = field_order
                    for key, val in field_data.items():
                        if key != 'id':
                            setattr(field_obj, key, val)
                    field_obj.save()
                else:
                    field_data.pop('id', None)
                    field_order = field_data.pop('order', order)
                    FormField.objects.create(form=instance, order=field_order, **field_data)

        # Re-sync attendance sessions whenever the form (and possibly its
        # attendance_* config) is saved. Idempotent and non-destructive of
        # already-scanned sessions — see apps.attendance.services.generate_sessions.
        from apps.attendance.services import generate_sessions
        self._attendance_undeletable_sessions = generate_sessions(instance)
        return instance


class AnswerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Answer
        fields = ['id', 'field', 'value']


class AnswerDetailSerializer(serializers.ModelSerializer):
    """Enriched answer serializer with field metadata for the responses viewer."""
    field_id = serializers.IntegerField(source='field.id', read_only=True)
    field_label = serializers.CharField(source='field.label', read_only=True)
    field_type = serializers.CharField(source='field.type', read_only=True)

    class Meta:
        model = Answer
        fields = ['field_id', 'field_label', 'field_type', 'value']


class ResponseDetailSerializer(serializers.ModelSerializer):
    """Full response serializer with enriched answers and user details, used by the responses viewer."""
    answers = AnswerDetailSerializer(many=True, read_only=True)
    user = serializers.SerializerMethodField()
    form_id = serializers.IntegerField(source='form.id', read_only=True)
    form_title = serializers.CharField(source='form.title', read_only=True)
    form_slug = serializers.CharField(source='form.slug', read_only=True)
    user_name = serializers.SerializerMethodField()
    user_email = serializers.SerializerMethodField()
    confirmation_email_enabled = serializers.BooleanField(source='form.confirmation_email_enabled', read_only=True)
    confirmation_email = serializers.SerializerMethodField()

    class Meta:
        model = Response
        fields = [
            'id', 'form_id', 'form_title', 'form_slug', 'submitted_at',
            'is_manual_entry', 'is_test_submission', 'form_version',
            'user', 'user_name', 'user_email', 'answers',
            'confirmation_email_enabled', 'confirmation_email',
        ]

    def get_user(self, obj):
        if obj.user:
            name = f'{obj.user.first_name} {obj.user.last_name}'.strip() or obj.user.username or obj.user.email
            return {
                'id': obj.user.id,
                'name': name,
                'email': obj.user.email,
            }
        if obj.is_manual_entry and obj.created_by_admin:
            return {
                'id': obj.created_by_admin.id,
                'name': f'Admin: {obj.created_by_admin.email}',
                'email': obj.created_by_admin.email,
            }
        return None

    def get_user_name(self, obj):
        u = self.get_user(obj)
        if u and u.get('name') and u['name'] != 'Anonymous':
            return u['name']
        for ans in obj.answers.all():
            if ans.field and any(k in ans.field.label.lower() for k in ['name', 'student', 'candidate', 'applicant']):
                if ans.value and str(ans.value).strip():
                    return str(ans.value).strip()
        return 'Anonymous Student'

    def get_user_email(self, obj):
        u = self.get_user(obj)
        if u and u.get('email'):
            return u['email']
        for ans in obj.answers.all():
            if ans.field and (ans.field.type == 'EMAIL' or 'email' in ans.field.label.lower()):
                if ans.value and str(ans.value).strip():
                    return str(ans.value).strip()
        return 'offline@srkr.ac.in'

    def get_confirmation_email(self, obj):
        # `.all()` here reads from the prefetch cache (Response queryset uses
        # Prefetch(..., queryset=EmailDelivery.objects.order_by('-created_at')))
        # rather than issuing a new query per row — do not call .order_by()/
        # .first() on the manager directly, that would bypass the cache.
        deliveries = list(obj.confirmation_email_deliveries.all())
        if not deliveries:
            return None
        latest = deliveries[0]
        return {
            'status': latest.status,
            'sent_at': latest.sent_at,
            'error_message': latest.error_message,
            'recipient_email': latest.recipient_email,
        }


class BulkIngestSessionSerializer(serializers.ModelSerializer):
    """Read-only serializer returning the summary of a bulk ingest session."""
    class Meta:
        model = BulkIngestSession
        fields = [
            'id', 'idempotency_key', 'status',
            'imported_count', 'skipped_count', 'duplicate_count', 'error_log',
            'created_at',
        ]

class ResponseSerializer(serializers.ModelSerializer):
    """
    Submission serializer. ``answers`` arrives as a raw list of ``{field, value}``
    dicts; the full validation engine (apps.forms.validation) runs in
    ``validate()`` and its normalized output is what actually gets persisted.

    Context keys the caller may pass:
      ``form``   — the target Form (falls back to ``instance.form`` on edit)
      ``mode``   — "strict" (default) or "partial" (admin manual entry / import)
      ``request``
    """
    answers = serializers.SerializerMethodField()

    class Meta:
        model = Response
        fields = [
            'id', 'form', 'user', 'form_version', 'is_test_submission',
            'submitted_at', 'is_manual_entry', 'created_by_admin', 'answers',
        ]
        read_only_fields = ['form_version']

    # -- read representation -------------------------------------------------

    def get_answers(self, obj):
        if not obj.pk:
            return []
        return [
            {'field': a.field_id, 'value': a.value}
            for a in obj.answers.all()
        ]

    # -- validation -------------------------------------------------------

    def _resolve_form(self, data):
        return self.context.get('form') or data.get('form') or getattr(self.instance, 'form', None)

    def _check_duplicate_emails(self, form, report):
        """
        Opt-in (Form.prevent_duplicate_email_answers) cross-response uniqueness
        check for EMAIL-type fields — rejects a submission whose email value has
        already been used to answer this same form. Off by default: some forms
        legitimately expect one email to submit more than once (e.g. a parent
        registering several children), so this is only enforced when an admin
        explicitly turns it on for a given form.
        """
        for f in form.fields.filter(is_deleted=False, type=FieldType.EMAIL):
            value = report.cleaned_answers.get(f.id)
            if not value:
                continue
            dup_qs = Answer.objects.filter(
                field=f, value=value,
                response__form=form, response__is_test_submission=False,
            )
            if self.instance is not None:
                dup_qs = dup_qs.exclude(response=self.instance)
            if dup_qs.exists():
                report.add_error(FieldError(
                    code='DUPLICATE_EMAIL',
                    message=f"'{value}' has already been used to respond to this form.",
                    field_id=f.id,
                    label=f.label,
                    rule='uniqueEmail',
                ))

    def validate(self, data):
        form = self._resolve_form(data)
        if form is None:
            raise FormValidationError({'detail': 'A target form is required.', 'code': 'FORM_REQUIRED',
                                       'errors': [], 'warnings': []})

        raw_answers = self.initial_data.get('answers', []) if hasattr(self, 'initial_data') else []
        mode = self.context.get('mode', STRICT)
        is_edit = self.instance is not None
        existing = None
        if is_edit:
            existing = {a.field_id: a.value for a in self.instance.answers.all()}

        report = validate_submission(
            form, raw_answers, mode=mode, is_edit=is_edit, existing_answers=existing,
        )

        if form.prevent_duplicate_email_answers and not report.errors:
            self._check_duplicate_emails(form, report)

        if report.errors:
            raise FormValidationError(report.as_dict())

        self._report = report
        self._validation_warnings = [w.as_dict() for w in report.warnings]
        data['form'] = form
        return data

    # -- persistence ----------------------------------------------------

    def _write_answers(self, response):
        cleaned = getattr(self, '_report', None)
        cleaned = cleaned.cleaned_answers if cleaned else {}
        keep_ids = set(cleaned.keys())

        # Remove answers no longer present (field hidden / cleared on edit).
        response.answers.exclude(field_id__in=keep_ids).delete()
        for field_id, value in cleaned.items():
            Answer.objects.update_or_create(
                response=response, field_id=field_id, defaults={'value': value},
            )

    def create(self, validated_data):
        validated_data.pop('answers', None)
        form = validated_data.get('form')
        if form:
            validated_data['form_version'] = form.version

        # Deliberately does NOT fall back to a client-supplied `user` ID from
        # request.data for anonymous callers — that was an IDOR letting anyone
        # attribute a response to an arbitrary user by ID (hijacking their
        # single-submission response / consuming their response quota).
        # ResponseViewSet.create() already passes the correct `user` via
        # `serializer.save(user=user_to_assign)` (the authenticated caller, or
        # None); an authenticated caller's own identity is the only implicit
        # fallback trusted here.
        request = self.context.get('request')
        if not validated_data.get('user') and request and request.user and request.user.is_authenticated:
            validated_data['user'] = request.user

        response = Response.objects.create(**validated_data)
        self._write_answers(response)
        return response

    def update(self, instance, validated_data):
        validated_data.pop('answers', None)
        form = validated_data.get('form', instance.form)
        if form:
            instance.form_version = form.version
        instance.submitted_at = timezone.now()
        instance.save()
        self._write_answers(instance)
        return instance


# ---------------------------------------------------------------------------
# Field soft-delete dependency checker (used by views)
# ---------------------------------------------------------------------------

def check_field_conditional_dependencies(field_id: int, form) -> list:
    """
    Return a list of FormField instances whose conditional_logic references
    the given field_id, indicating a dependency that would break on deletion.
    Understands the canonical shape ({"rules": [{"field": id}]}), the legacy
    multi-rule shape ({"rules": [{"if": id}]}) and the legacy single-rule shape
    ({"if": id}).
    """
    from .validation.schema import normalize_conditional_logic, iter_condition_field_refs

    dependent = []
    target = str(field_id)
    for f in form.fields.filter(is_deleted=False).exclude(id=field_id):
        norm = normalize_conditional_logic(f.conditional_logic)
        if not norm:
            continue
        if any(str(ref) == target for ref in iter_condition_field_refs(norm)):
            dependent.append(f)
    return dependent
