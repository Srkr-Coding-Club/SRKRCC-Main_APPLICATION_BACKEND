import base64
import re
import uuid
import logging

from rest_framework import serializers
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from .models import Form, FormField, FieldType, Response, Answer, BulkIngestSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conditional logic evaluation
# ---------------------------------------------------------------------------

def _evaluate_single_rule(rule: dict, submitted_map: dict) -> bool:
    """Evaluate one rule dict against the submitted answer map."""
    trigger_field_id = rule.get("if")
    if not trigger_field_id:
        return True

    trigger_val = str(submitted_map.get(str(trigger_field_id), ""))
    # Support legacy 'equals' key as well as explicit 'operator'/'value' keys
    if "equals" in rule and "operator" not in rule:
        return trigger_val == str(rule["equals"])

    op = rule.get("operator", "equals")
    expected = str(rule.get("value", ""))

    if op == "equals":
        return trigger_val == expected
    elif op == "not_equals":
        return trigger_val != expected
    elif op == "greater_than":
        try:
            return float(trigger_val) > float(expected)
        except (ValueError, TypeError):
            return False
    elif op == "less_than":
        try:
            return float(trigger_val) < float(expected)
        except (ValueError, TypeError):
            return False
    elif op == "contains":
        return expected.lower() in trigger_val.lower()
    return True


def evaluate_condition(conditional_logic: dict, submitted_map: dict) -> bool:
    """
    Evaluate conditional_logic against submitted_map.

    Supports two schemas:
    - Legacy: {"if": field_id, "equals": value}
    - Multi-rule: {"logic": "AND"|"OR", "rules": [...]}
    """
    if not conditional_logic or not isinstance(conditional_logic, dict):
        return True

    # Multi-rule schema
    if "rules" in conditional_logic:
        rules = conditional_logic.get("rules", [])
        if not rules:
            return True
        logic = conditional_logic.get("logic", "AND").upper()
        results = [_evaluate_single_rule(r, submitted_map) for r in rules]
        if logic == "OR":
            return any(results)
        return all(results)  # AND is default

    # Legacy single-rule schema
    return _evaluate_single_rule(conditional_logic, submitted_map)


def evaluate_visible_fields(form_fields, submitted_map: dict) -> set:
    """
    Return set of field IDs that should be visible given submitted_map.

    Includes a cycle guard: iterates at most len(fields)+1 passes so that
    circular conditional dependencies (A shows if B, B shows if A) cannot
    cause an infinite loop.
    """
    visible: set = set()
    MAX_PASSES = len(form_fields) + 1
    changed = True
    passes = 0

    while changed and passes < MAX_PASSES:
        changed = False
        passes += 1
        for field in form_fields:
            cl = field.conditional_logic
            if not cl:
                # No condition — always visible
                if field.id not in visible:
                    visible.add(field.id)
                    changed = True
            else:
                should_show = evaluate_condition(cl, submitted_map)
                if should_show and field.id not in visible:
                    visible.add(field.id)
                    changed = True
                elif not should_show and field.id in visible:
                    visible.discard(field.id)
                    changed = True

    return visible


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


# ---------------------------------------------------------------------------
# Field-level validation helper (used by bulk_ingest view)
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


def enforce_field_validation(field: FormField, value) -> list:
    """
    Run type-based and required validation for a single FormField value.

    Returns a list of error strings (empty list = valid).
    FILE and SIGNATURE fields cannot be ingested via CSV — always returns an error.
    """
    errors = []

    # Unsupported CSV field types
    if field.type in (FieldType.FILE, FieldType.MULTI_FILE, FieldType.SIGNATURE):
        errors.append(
            f"Field '{field.label}' is of type {field.type} which cannot be imported via CSV."
        )
        return errors

    is_empty = value is None or value == '' or value == [] or value == {}

    if field.is_required and is_empty:
        errors.append(f"Field '{field.label}' is required.")
        return errors

    if is_empty:
        return errors  # Optional field, nothing to validate

    str_value = str(value).strip()

    if field.type == FieldType.EMAIL:
        if not EMAIL_RE.match(str_value):
            errors.append(f"Field '{field.label}': '{str_value}' is not a valid email address.")

    elif field.type in (FieldType.NUMBER, FieldType.RATING, FieldType.LINEAR_SCALE):
        try:
            float(str_value)
        except (ValueError, TypeError):
            errors.append(f"Field '{field.label}': '{str_value}' is not a valid number.")

    return errors


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


class FormSerializer(serializers.ModelSerializer):
    fields = FormFieldSerializer(many=True, required=False)
    # Populated via annotation in get_queryset; safe to omit in write operations
    response_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Form
        fields = [
            'id', 'title', 'slug', 'description', 'image_url', 'category', 'status',
            'version', 'allow_multiple_responses', 'allow_edits_until', 'open_at', 'close_at',
            'fields', 'created_at', 'response_count',
        ]

    def create(self, validated_data):
        fields_data = validated_data.pop('fields', [])
        form = Form.objects.create(**validated_data)
        for order, field_data in enumerate(fields_data, start=1):
            field_data.pop('id', None)
            FormField.objects.create(form=form, order=field_data.get('order', order), **field_data)
        return form

    def update(self, instance, validated_data):
        fields_data = validated_data.pop('fields', None)
        instance.version += 1
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if fields_data is not None:
            incoming_field_ids = {f.get('id') for f in fields_data if f.get('id')}
            # Soft delete fields that are no longer present in the incoming payload
            instance.fields.filter(is_deleted=False).exclude(id__in=incoming_field_ids).update(is_deleted=True)

            for order, field_data in enumerate(fields_data, start=1):
                f_id = field_data.get('id')
                if f_id and FormField.all_objects.filter(id=f_id, form=instance).exists():
                    field_obj = FormField.all_objects.get(id=f_id, form=instance)
                    field_obj.is_deleted = False
                    field_obj.order = field_data.get('order', order)
                    for key, val in field_data.items():
                        if key != 'id':
                            setattr(field_obj, key, val)
                    field_obj.save()
                else:
                    field_data.pop('id', None)
                    FormField.objects.create(form=instance, order=field_data.get('order', order), **field_data)
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

    class Meta:
        model = Response
        fields = [
            'id', 'submitted_at', 'is_manual_entry', 'is_test_submission',
            'form_version', 'user', 'answers',
        ]

    def get_user(self, obj):
        if obj.user:
            return {
                'id': obj.user.id,
                'name': f'{obj.user.first_name} {obj.user.last_name}'.strip() or obj.user.username,
                'email': obj.user.email,
            }
        if obj.is_manual_entry and obj.created_by_admin:
            return {
                'id': obj.created_by_admin.id,
                'name': f'Admin: {obj.created_by_admin.email}',
                'email': obj.created_by_admin.email,
            }
        return None


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
    answers = AnswerSerializer(many=True)

    class Meta:
        model = Response
        fields = [
            'id', 'form', 'user', 'form_version', 'is_test_submission',
            'submitted_at', 'is_manual_entry', 'created_by_admin', 'answers',
        ]
        read_only_fields = ['form_version']

    def validate(self, data):
        form = self.context.get('form') or data.get('form')
        answers = data.get('answers', [])
        submitted_map = {str(a['field'].id): a.get('value') for a in answers if 'field' in a}

        if form:
            active_fields = list(form.fields.filter(is_deleted=False))
            visible_ids = evaluate_visible_fields(active_fields, submitted_map)

            for field in active_fields:
                if field.id not in visible_ids:
                    continue
                if field.is_required:
                    val = submitted_map.get(str(field.id))
                    if val is None or val == "" or val == [] or val == {}:
                        raise serializers.ValidationError({
                            'answers': f"Field '{field.label}' is required."
                        })

                # Validate matrix answer shape
                if field.type in (FieldType.MATRIX_RADIO, FieldType.MATRIX_CHECKBOX):
                    val = submitted_map.get(str(field.id))
                    if val is not None and not isinstance(val, dict):
                        raise serializers.ValidationError({
                            'answers': (
                                f"Field '{field.label}' expects a matrix answer "
                                f"in the shape {{\"Row\": \"Column\"}}."
                            )
                        })

        return data

    def create(self, validated_data):
        form = validated_data.get('form')
        if form:
            validated_data['form_version'] = form.version

        answers_data = validated_data.pop('answers', [])
        response = Response.objects.create(**validated_data)

        for answer_data in answers_data:
            field = answer_data.get('field')
            value = answer_data.get('value')

            # Handle SIGNATURE: decode base64 and persist via storage backend
            if field and field.type == FieldType.SIGNATURE and isinstance(value, str) and value:
                value = save_signature_to_storage(value)
                answer_data['value'] = value

            Answer.objects.create(response=response, **answer_data)

        return response


# ---------------------------------------------------------------------------
# Field soft-delete dependency checker (used by views)
# ---------------------------------------------------------------------------

def check_field_conditional_dependencies(field_id: int, form) -> list:
    """
    Return a list of FormField instances whose conditional_logic references
    the given field_id, indicating a dependency that would break on deletion.
    """
    dependent = []
    field_id_str = str(field_id)
    for f in form.fields.filter(is_deleted=False).exclude(id=field_id):
        cl = f.conditional_logic
        if not cl:
            continue
        # Multi-rule schema
        for rule in cl.get("rules", []):
            if str(rule.get("if", "")) == field_id_str:
                dependent.append(f)
                break
        # Legacy schema
        if not cl.get("rules") and str(cl.get("if", "")) == field_id_str:
            dependent.append(f)
    return dependent
