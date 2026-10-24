import base64
import json
import re
import uuid
import logging

from rest_framework import serializers
from rest_framework.exceptions import APIException
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import TextField
from django.db.models.functions import Cast, Lower
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
from .validation import codes
from . import capacity, spam, upload_attach
from .validation import limits
from .validation.schema import condition_depth
from . import versions as form_versions
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
User = get_user_model()

# Value getters for FormField.profile_field - keys match apps.forms.models.ProfileField.
PROFILE_FIELD_GETTERS = {
    'full_name': lambda u: f'{u.first_name} {u.last_name}'.strip(),
    'email': lambda u: u.email,
    'phone_number': lambda u: u.phone_number,
    'branch': lambda u: u.branch,
    'roll_number': lambda u: u.roll_number,
    'college': lambda u: u.college.name if u.college_id else (u.other_college_name or None),
    'year': lambda u: u.year,
    'club_id': lambda u: u.club_id,
}


class _SimpleReport:
    """The 400 body for failures found after validation (a file that vanished, storage trouble)."""

    def __init__(self, errors):
        self.errors = errors

    def as_dict(self):
        return {'detail': self.errors[0].message, 'code': self.errors[0].code,
                'errors': [e.as_dict() for e in self.errors], 'warnings': []}


class FormValidationError(APIException):
    """
    Carries the engine's structured report ({detail, code, errors[], warnings[]})
    to the client verbatim.

    A plain ``APIException`` (not ``serializers.ValidationError``) so it is NOT
    caught-and-rewrapped by ``serializer.is_valid()`` - it propagates straight to
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

def clean_redirect_url(value) -> str:
    """An https:// address or a path on this site; anything else (javascript:, data:, //host, plain http) is refused."""
    from urllib.parse import urlparse
    value = (value or '').strip()
    if not value:
        return ''
    if len(value) > 500 or re.search(r'[\s\\]', value):
        raise serializers.ValidationError("The redirect address must be a single address of at most 500 characters.")
    if value.startswith('/') and not value.startswith('//'):
        return value
    parsed = urlparse(value)
    if parsed.scheme == 'https' and parsed.netloc and not parsed.username:
        return value
    raise serializers.ValidationError("The redirect must be an https:// address or a path on this site such as /events.")


class FormFieldSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)

    class Meta:
        model = FormField
        fields = [
            'id', 'label', 'type', 'placeholder', 'description', 'is_required',
            'options', 'rows', 'min_value', 'max_value',
            'conditional_logic', 'validation_rules', 'order', 'profile_field',
        ]

    def validate(self, attrs):
        """Canonicalize the two JSON config blobs on write so submissions and
        the publish gate always see one shape."""
        ftype = attrs.get('type') or getattr(self.instance, 'type', FieldType.TEXT)
        if 'validation_rules' in attrs:
            attrs['validation_rules'] = normalize_validation_rules(ftype, attrs.get('validation_rules'))
        if 'conditional_logic' in attrs:
            if condition_depth(attrs.get('conditional_logic')) > limits.MAX_CONDITION_DEPTH:
                raise serializers.ValidationError({'conditional_logic': f"A condition can nest groups at most {limits.MAX_CONDITION_DEPTH} deep."})
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
            'id', 'title', 'slug', 'description', 'image_url', 'category', 'status', 'is_template',
            'version', 'allow_multiple_responses', 'allow_response_editing', 'enable_prefill', 'max_responses_per_user',
            'max_total_responses', 'prevent_duplicate_email_answers',
            'allow_edits_until', 'open_at', 'close_at',
            'club_id_enabled', 'club_id_prefix', 'club_id_field_mapping', 'club_id_verification_enabled',
            'confirmation_email_enabled', 'confirmation_email_template',
            'confirmation_notification_enabled', 'notify_admin_on_submission',
            'notify_members_on_publish', 'notification_title', 'notification_message',
            'confirmation_message', 'redirect_url', 'redirect_delay_seconds', 'captcha_enabled', 'language',
            'attendance_enabled', 'attendance_start_date', 'attendance_days',
            'attendance_sessions_per_day', 'attendance_window_minutes',
            'fields', 'created_at', 'response_count',
        ]
        read_only_fields = ['is_template']

    def validate_title(self, value):
        # DRF's CharField only rejects a literal "" ("may not be blank") - a
        # whitespace-only title like "   " passes that check as a non-empty
        # string, so a form can end up with a title that renders as a blank
        # row wherever it's listed (e.g. the admin's "Select a form" dropdown
        # in the responses viewer). Strip and require real content.
        title = (value or '').strip()
        if not title:
            raise serializers.ValidationError("Title is required.")
        return title

    def validate_image_url(self, value):
        if not value or not str(value).strip():
            return None
        return str(value).strip()

    def validate_club_id_prefix(self, value):
        clean = (value or '').strip().upper()
        if not re.match(r'^[A-Z]{2,6}$', clean):
            raise serializers.ValidationError("Club ID prefix must be 2-6 letters (e.g. 'SCC').")
        return clean

    def validate_confirmation_message(self, value):
        value = (value or '').strip()
        if len(value) > 2000:
            raise serializers.ValidationError("The thank-you message can be at most 2000 characters.")
        return value

    def validate_language(self, value):
        value = (value or 'en').strip().lower()
        if value not in ('en', 'te', 'hi'):
            raise serializers.ValidationError("language must be 'en' (English), 'te' (Telugu) or 'hi' (Hindi).")
        return value

    def validate_redirect_url(self, value):
        return clean_redirect_url(value)

    def validate_redirect_delay_seconds(self, value):
        if not (0 <= value <= 30):
            raise serializers.ValidationError("redirect_delay_seconds must be between 0 and 30.")
        return value

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
        # DRAFT saves stay lenient (a form is built incrementally - the builder
        # itself tells the admin to enable Club ID / confirmation email, save
        # once to get real field IDs, *then* come back to finish the field
        # mapping / template pick). Only PUBLISHED / SCHEDULED must have this
        # automation fully configured, matching the same leniency the
        # form-definition check below already applies.
        target_status = data.get('status', getattr(self.instance, 'status', FormStatus.DRAFT))
        going_live = target_status in (FormStatus.PUBLISHED, FormStatus.SCHEDULED)

        club_id_enabled = data.get('club_id_enabled', getattr(self.instance, 'club_id_enabled', False))
        club_id_verification_enabled = data.get(
            'club_id_verification_enabled', getattr(self.instance, 'club_id_verification_enabled', False)
        )
        if club_id_enabled and club_id_verification_enabled:
            raise serializers.ValidationError({
                'club_id_verification_enabled': "A form either generates a new Club ID or verifies an existing one, not both. Disable 'Generate Club Member ID' first.",
            })

        if club_id_enabled and going_live:
            mapping = data.get('club_id_field_mapping', getattr(self.instance, 'club_id_field_mapping', None) or {})
            if not mapping.get('email'):
                raise serializers.ValidationError({
                    'club_id_field_mapping': "Club ID generation requires an 'email' field mapping. Pick which form field supplies the member's email.",
                })

        if club_id_verification_enabled and going_live:
            mapping = data.get('club_id_field_mapping', getattr(self.instance, 'club_id_field_mapping', None) or {})
            if not mapping.get('club_id'):
                raise serializers.ValidationError({
                    'club_id_field_mapping': "Club ID verification requires a 'club_id' field mapping. Pick which form field collects the member's Club ID.",
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

        captcha_on = data.get('captcha_enabled', getattr(self.instance, 'captcha_enabled', False))
        if going_live and captcha_on and not spam.captcha_configured():
            raise serializers.ValidationError({'captcha_enabled': spam.CAPTCHA_NOT_CONFIGURED_MESSAGE})

        self._check_no_type_changes_on_answered_fields(data)

        # Form-definition validation. DRAFT saves stay lenient (a form is built
        # incrementally); PUBLISHED / SCHEDULED must have a fully valid definition
        # so every submission against it can be validated.
        if going_live and 'fields' in data:
            report = validate_form_definition({'fields': data.get('fields') or []})
            if not report.publishable:
                raise FormValidationError({
                    'detail': 'This form cannot be published because its definition has blocking problems.',
                    'code': 'FORM_DEFINITION_INVALID',
                    'errors': [e.as_dict() for e in report.errors],
                    'warnings': [w.as_dict() for w in report.warnings],
                })
            self._definition_warnings = [w.as_dict() for w in report.warnings]

        return data

    def _check_no_type_changes_on_answered_fields(self, data):
        """Changing a question's type once people have answered it would reinterpret their stored answers."""
        if self.instance is None or not data.get('fields'):
            return
        incoming = {
            f['id']: f for f in data['fields']
            if isinstance(f.get('id'), int) and 0 < f['id'] < 2147483647
        }
        if not incoming:
            return
        locked = []
        for existing in FormField.all_objects.filter(form=self.instance, id__in=incoming.keys()):
            new_type = incoming[existing.id].get('type')
            if new_type and new_type != existing.type and Answer.objects.filter(field=existing).exists():
                locked.append(FieldError(
                    code=codes.FIELD_TYPE_LOCKED, field_id=existing.id, label=existing.label,
                    message=(
                        f"'{existing.label}' already has responses, so its type cannot change from "
                        f"{existing.type} to {new_type}. Add a new question instead."
                    ),
                ))
        if locked:
            raise FormValidationError({
                'detail': 'A question that already has responses cannot change type.',
                'code': codes.FIELD_TYPE_LOCKED,
                'errors': [e.as_dict() for e in locked],
                'warnings': [],
            })

    @transaction.atomic
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
        self._record_version(form, self.context.get('version_note') or 'Created')
        return form

    def _record_version(self, form, note):
        request = self.context.get('request')
        form_versions.record_version(form, getattr(request, 'user', None), note)

    @transaction.atomic
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
        # already-scanned sessions - see apps.attendance.services.generate_sessions.
        from apps.attendance.services import generate_sessions
        self._attendance_undeletable_sessions = generate_sessions(instance)
        self._record_version(instance, self.context.get('version_note') or 'Saved')
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
    confirmation_notification_enabled = serializers.BooleanField(source='form.confirmation_notification_enabled', read_only=True)
    confirmation_notification = serializers.SerializerMethodField()

    class Meta:
        model = Response
        fields = [
            'id', 'form_id', 'form_title', 'form_slug', 'submitted_at',
            'is_manual_entry', 'is_test_submission', 'form_version',
            'user', 'user_name', 'user_email', 'answers',
            'confirmation_email_enabled', 'confirmation_email',
            'confirmation_notification_enabled', 'confirmation_notification',
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
        # rather than issuing a new query per row - do not call .order_by()/
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

    def get_confirmation_notification(self, obj):
        # Reads from prefetch cache if available
        notifs = list(obj.notifications.all())
        if obj.user_id:
            submitter_notifs = [n for n in notifs if n.recipient_id == obj.user_id]
            if submitter_notifs:
                notifs = submitter_notifs
        if not notifs:
            return None
        latest = notifs[0]
        return {
            'id': latest.id,
            'title': latest.title,
            'message': latest.message,
            'type': latest.type,
            'category': latest.category,
            'is_read': latest.is_read,
            'created_at': latest.created_at,
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
      ``form``   - the target Form (falls back to ``instance.form`` on edit)
      ``mode``   - "strict" (default) or "partial" (admin manual entry / import)
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

    def _report_problem(self, report, error):
        """A rule an admin's own import may break stays a warning in partial mode; otherwise it blocks the submission."""
        (report.add_warning if self.context.get('mode', STRICT) == PARTIAL else report.add_error)(error)

    def _check_unique_fields(self, form, report):
        """Questions marked ``unique``: nobody else may have given the same answer (case-insensitively)."""
        for f in form.fields.filter(is_deleted=False):
            rules = f.validation_rules if isinstance(f.validation_rules, dict) else {}
            value = report.cleaned_answers.get(f.id)
            if not rules.get('unique') or value in (None, ''):
                continue
            wanted = json.dumps(value, ensure_ascii=False).lower()
            others = Answer.objects.annotate(_stored=Lower(Cast('value', TextField()))).filter(
                field=f, _stored=wanted, response__form=form, response__is_test_submission=False,
            )
            if self.instance is not None:
                others = others.exclude(response=self.instance)
            if others.exists():
                custom = (rules.get('messages') or {}).get('unique')
                self._report_problem(report, FieldError(
                    code=codes.DUPLICATE_VALUE, field_id=f.id, label=f.label, rule='unique',
                    message=custom or f"'{value}' has already been used to answer '{f.label}'.",
                ))

    def _check_option_capacity(self, form, report):
        """Options with a limit (``optionLimits``) stop being available once enough responses have taken them."""
        for f in capacity.limited_fields(form):
            value = report.cleaned_answers.get(f.id)
            if value in (None, ''):
                continue
            caps = capacity.caps_by_option(f)
            counts = capacity.option_counts(form, f, exclude_response=self.instance)
            for item in value if isinstance(value, list) else [value]:
                key = str(item).strip().lower()
                if key in caps and counts.get(key, 0) >= caps[key]:
                    custom = (f.validation_rules.get('messages') or {}).get('optionLimits')
                    self._report_problem(report, FieldError(
                        code=codes.OPTION_FULL, field_id=f.id, label=f.label, rule='optionLimits',
                        message=custom or f"'{item}' is full. Please pick another option.",
                    ))

    def _check_duplicate_emails(self, form, report):
        """
        Opt-in (Form.prevent_duplicate_email_answers) cross-response uniqueness
        check for EMAIL-type fields - rejects a submission whose email value has
        already been used to answer this same form. Off by default: some forms
        legitimately expect one email to submit more than once (e.g. a parent
        registering several children), so this is only enforced when an admin
        explicitly turns it on for a given form.
        """
        for f in form.fields.filter(is_deleted=False, type=FieldType.EMAIL):
            value = report.cleaned_answers.get(f.id)
            if not value:
                continue
            # Email addresses are case-insensitive in practice: "A@x.com" and "a@x.com" are one person.
            stored_form = json.dumps(str(value), ensure_ascii=False).lower()
            dup_qs = Answer.objects.annotate(
                _stored=Lower(Cast('value', TextField())),
            ).filter(
                field=f, _stored=stored_form,
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

    # Human-readable labels for the mismatch message - keyed the same as
    # club_id_field_mapping (minus 'club_id' itself, which is the lookup key,
    # not something compared against).
    _CLUB_ID_VERIFY_LABELS = {
        'full_name': 'name',
        'email': 'email',
        'phone_number': 'phone number',
        'branch': 'branch',
        'roll_number': 'roll number',
    }

    def _check_club_id_verification(self, form, report):
        """
        Opt-in (Form.club_id_verification_enabled) check: the submitter claims
        an existing Club ID (via the mapped 'club_id' field) - reject the
        submission if that Club ID isn't registered, or if any other mapped
        field (name/email/phone/branch/roll number) doesn't match that
        member's actual record. Catches both typos and someone submitting
        with a Club ID that isn't theirs.

        Mirrors _check_duplicate_emails's shape: adds FieldError entries to
        the same `report` rather than raising directly, so a mismatch behaves
        exactly like any other field-level validation error on the client.
        """
        mapping = form.club_id_field_mapping or {}

        def field_id_of(key):
            raw = mapping.get(key)
            try:
                return int(raw) if raw is not None else None
            except (TypeError, ValueError):
                return None

        club_id_field_id = field_id_of('club_id')
        if club_id_field_id is None:
            dedicated_field = form.fields.filter(
                is_deleted=False, type=FieldType.CLUB_ID,
            ).order_by('order', 'id').first()
            club_id_field_id = dedicated_field.id if dedicated_field else None
        if club_id_field_id is None:
            # Not configured - same leniency as resolve_club_member() when its
            # email mapping is missing; the publish gate is what actually
            # enforces this is set before the form can go live.
            return

        submitted_club_id = report.cleaned_answers.get(club_id_field_id)
        if not submitted_club_id or not str(submitted_club_id).strip():
            # Blank - required-field validation already flagged this if the
            # field is required; nothing more to check here.
            return

        field_labels = {f.id: f.label for f in form.fields.all()}
        normalized_club_id = str(submitted_club_id).strip()
        member = User.objects.filter(club_id__iexact=normalized_club_id).first()

        # One generic failure for "no such Club ID" and "details don't match", so this
        # endpoint can't be used to enumerate Club IDs or probe a member's details.
        # The specific cause is logged for admins instead.
        def verification_failed(reason):
            logger.info("Club ID verification failed on form %s: %s", form.pk, reason)
            report.add_error(FieldError(
                code=codes.CLUB_ID_VERIFICATION_FAILED,
                message="We couldn't verify these details against that Club ID. Check them and try again.",
                field_id=club_id_field_id,
                label=field_labels.get(club_id_field_id),
                rule='clubIdVerification',
            ))

        if member is None:
            verification_failed('unknown club id')
            return

        def norm_text(v):
            return re.sub(r'\s+', ' ', str(v or '')).strip().casefold()

        def norm_digits(v):
            return re.sub(r'\D', '', str(v or ''))

        member_values = {
            'full_name': (f'{member.first_name} {member.last_name}'.strip(), norm_text),
            'email': (member.email, norm_text),
            'phone_number': (member.phone_number, norm_digits),
            'branch': (member.branch, norm_text),
            'roll_number': (member.roll_number, norm_text),
        }

        for key, (member_value, normalize) in member_values.items():
            field_id = field_id_of(key)
            if field_id is None:
                continue
            submitted_value = report.cleaned_answers.get(field_id)
            if not submitted_value or not str(submitted_value).strip():
                continue
            if not member_value:
                # Nothing on file to compare against (e.g. member has no phone
                # number saved) - skip rather than reject over missing data
                # that isn't the submitter's fault.
                continue
            if normalize(submitted_value) != normalize(member_value):
                verification_failed(f'{self._CLUB_ID_VERIFY_LABELS[key]} mismatch')
                return

    def _resolve_profile_autofill(self, form, mode):
        """
        STRICT-mode only: for each FormField mapped to a profile attribute
        (``FormField.profile_field``), resolve its value from the authenticated
        submitter's own profile rather than the client, so it can never be
        retyped incorrectly or tampered with. Admin manual entry / CSV import
        (mode=PARTIAL) skip this entirely - the record being entered there
        often isn't the authenticated caller, so those fields behave like any
        ordinary answer supplied directly in the payload.

        Returns ``(managed_ids, overrides, errors)``:
          managed_ids - every FormField id mapped to a profile attribute on
                      this form, resolved or not. The caller strips ALL of
                      these from the client's payload unconditionally - a
                      profile-bound field never has a real input for the user
                      to answer, so nothing it "submitted" for one is trusted,
                      even where the profile itself has nothing to offer.
          overrides - {field_id: value} for the subset that resolved to a
                      real value, to inject in place of what was stripped.
          errors    - FieldError entries for a profile-mapped field that can't
                      be resolved (not logged in, or the profile itself is
                      missing that data and the field is required).
        """
        managed_ids = set()
        overrides = {}
        errors = []
        if mode != STRICT:
            return managed_ids, overrides, errors

        profile_fields = list(
            form.fields.filter(is_deleted=False).exclude(profile_field='').exclude(profile_field__isnull=True)
        )
        if not profile_fields:
            return managed_ids, overrides, errors
        managed_ids = {f.id for f in profile_fields}

        request = self.context.get('request')
        user = request.user if request and request.user and request.user.is_authenticated else None
        if user is None:
            for f in profile_fields:
                errors.append(FieldError(
                    code='PROFILE_FIELD_REQUIRES_LOGIN', field_id=f.id, label=f.label,
                    message=f"'{f.label}' is filled in from your profile. Please log in to continue.",
                    rule='profileAutofill',
                ))
            return managed_ids, overrides, errors

        for f in profile_fields:
            getter = PROFILE_FIELD_GETTERS.get(f.profile_field)
            value = getter(user) if getter else None
            if value in (None, ''):
                if f.is_required:
                    errors.append(FieldError(
                        code='PROFILE_FIELD_MISSING', field_id=f.id, label=f.label,
                        message=f"Your profile is missing '{f.label}'. Please update your profile before registering.",
                        rule='profileAutofill',
                    ))
                continue
            overrides[f.id] = value
        return managed_ids, overrides, errors

    @staticmethod
    def _merge_answer_overrides(raw_answers, managed_ids, overrides):
        """Strip any client-supplied answer for a profile-managed field id
        (whether or not it resolved to a value), then append the
        server-resolved overrides - same wire shape either input takes (a
        dict, or the normal ``[{"field": id, "value": v}, ...]`` list)."""
        if not managed_ids:
            return raw_answers
        managed_str_ids = {str(fid) for fid in managed_ids}
        if isinstance(raw_answers, dict):
            merged = {k: v for k, v in raw_answers.items() if str(k) not in managed_str_ids}
            merged.update({str(fid): val for fid, val in overrides.items()})
            return merged
        merged = [
            item for item in (raw_answers or [])
            if not (isinstance(item, dict) and str(item.get('field', item.get('field_id'))) in managed_str_ids)
        ]
        merged.extend({'field': fid, 'value': val} for fid, val in overrides.items())
        return merged

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

        managed_ids, overrides, profile_errors = self._resolve_profile_autofill(form, mode)
        raw_answers = self._merge_answer_overrides(raw_answers, managed_ids, overrides)
        raw_answers, upload_errors = upload_attach.resolve_references(form, self._caller(), raw_answers, response=self.instance)

        report = validate_submission(
            form, raw_answers, mode=mode, is_edit=is_edit, existing_answers=existing,
        )

        report.errors.extend(upload_errors)

        if profile_errors:
            # Replace the engine's generic REQUIRED/etc. error for these same
            # field ids with our more actionable "update your profile" one,
            # rather than surfacing both for the same field.
            managed_ids = {e.field_id for e in profile_errors}
            report.errors = [e for e in report.errors if e.field_id not in managed_ids]
            report.errors.extend(profile_errors)

        if form.prevent_duplicate_email_answers and not report.errors:
            self._check_duplicate_emails(form, report)

        if not report.errors:
            self._check_unique_fields(form, report)
        if not report.errors:
            self._check_option_capacity(form, report)

        has_dedicated_club_id_field = form.fields.filter(
            is_deleted=False, type=FieldType.CLUB_ID,
        ).exists()
        if (form.club_id_verification_enabled or has_dedicated_club_id_field) and not report.errors:
            self._check_club_id_verification(form, report)

        if report.errors:
            raise FormValidationError(report.as_dict())

        self._report = report
        self._validation_warnings = [w.as_dict() for w in report.warnings]
        data['form'] = form
        return data

    # -- persistence ----------------------------------------------------

    def _caller(self):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        return user if user is not None and user.is_authenticated else None

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
        problems = upload_attach.attach(response, self._caller(), cleaned)
        if problems:
            raise FormValidationError(_SimpleReport(problems).as_dict())

    def create(self, validated_data):
        validated_data.pop('answers', None)
        form = validated_data.get('form')
        if form:
            validated_data['form_version'] = form.version

        # Deliberately does NOT fall back to a client-supplied `user` ID from
        # request.data for anonymous callers - that was an IDOR letting anyone
        # attribute a response to an arbitrary user by ID (hijacking their
        # single-submission response / consuming their response quota).
        # ResponseViewSet.create() already passes the correct `user` via
        # `serializer.save(user=user_to_assign)` (the authenticated caller, or
        # None); an authenticated caller's own identity is the only implicit
        # fallback trusted here.
        request = self.context.get('request')
        if not validated_data.get('user') and request and request.user and request.user.is_authenticated:
            validated_data['user'] = request.user

        with transaction.atomic():
            response = Response.objects.create(**validated_data)
            self._write_answers(response)
        return response

    def update(self, instance, validated_data):
        validated_data.pop('answers', None)
        form = validated_data.get('form', instance.form)
        if form:
            instance.form_version = form.version
        instance.submitted_at = timezone.now()
        with transaction.atomic():
            instance.save()
            self._write_answers(instance)
        return instance


# ---------------------------------------------------------------------------
# Field soft-delete dependency checker (used by views)
# ---------------------------------------------------------------------------

def _condition_mentions(normalized, target: str) -> bool:
    from .validation.schema import iter_condition_field_refs
    return bool(normalized) and any(str(ref) == target for ref in iter_condition_field_refs(normalized))


def _cross_field_target(rule):
    return str(rule.get('field', rule.get('otherField'))) if isinstance(rule, dict) else None


def find_field_references(field_id: int, form):
    """
    Everything that would break if ``field_id`` were deleted:
      * other questions whose conditional rules or cross-field rules point at it
      * form settings (the Club ID field mapping) that point at it
    Understands the canonical condition shape and the legacy ``if`` shapes.
    Returns ``(dependent_fields, mapping_keys)``.
    """
    target = str(field_id)
    dependent = []
    for f in form.fields.filter(is_deleted=False).exclude(id=field_id):
        in_condition = _condition_mentions(normalize_conditional_logic(f.conditional_logic), target)
        cross = (f.validation_rules or {}).get('crossField')
        in_cross = isinstance(cross, list) and any(_cross_field_target(r) == target for r in cross)
        if in_condition or in_cross:
            dependent.append(f)
    mapping_keys = sorted(k for k, v in (form.club_id_field_mapping or {}).items() if str(v) == target)
    return dependent, mapping_keys


def check_field_conditional_dependencies(field_id: int, form) -> list:
    """Questions that reference ``field_id`` in a condition or cross-field rule."""
    return find_field_references(field_id, form)[0]


def _prune_condition(node: dict, target: str):
    """``node`` without any rule that points at ``target``; None when nothing is left."""
    if 'rules' in node:
        kept = [p for p in (_prune_condition(r, target) for r in node['rules']) if p]
        return {**node, 'rules': kept} if kept else None
    refs = {str(node.get('field_raw', node.get('field'))), str(node.get('value_field'))}
    return None if target in refs else node


def remove_field_references(field_id: int, form) -> None:
    """Strip every reference to ``field_id`` found by ``find_field_references`` so deleting it leaves no dead rules."""
    target = str(field_id)
    dependents, mapping_keys = find_field_references(field_id, form)
    for f in dependents:
        normalized = normalize_conditional_logic(f.conditional_logic)
        if _condition_mentions(normalized, target):
            f.conditional_logic = _prune_condition(normalized, target) or {}
        rules = dict(f.validation_rules or {})
        cross = rules.get('crossField')
        if isinstance(cross, list):
            left = [r for r in cross if _cross_field_target(r) != target]
            if left:
                rules['crossField'] = left
            else:
                rules.pop('crossField', None)
            f.validation_rules = rules
        f.save(update_fields=['conditional_logic', 'validation_rules'])
    if mapping_keys:
        form.club_id_field_mapping = {
            k: v for k, v in (form.club_id_field_mapping or {}).items() if str(v) != target
        }
        form.save(update_fields=['club_id_field_mapping', 'updated_at'])
