from rest_framework import serializers
from .models import Form, FormField, Response, Answer

def evaluate_condition(rule, submitted_map):
    if not rule or not isinstance(rule, dict):
        return True
    trigger_field_id = rule.get("if")
    if not trigger_field_id:
        return True
    
    trigger_val = str(submitted_map.get(str(trigger_field_id), ""))
    expected = rule.get("equals") if "equals" in rule else rule.get("value", "")
    op = rule.get("operator", "equals")

    if op == "equals":
        return str(trigger_val) == str(expected)
    elif op == "not_equals":
        return str(trigger_val) != str(expected)
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
    return True

class FormFieldSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)

    class Meta:
        model = FormField
        fields = ['id', 'label', 'type', 'placeholder', 'is_required', 'options', 'conditional_logic', 'validation_rules', 'order']

class FormSerializer(serializers.ModelSerializer):
    fields = FormFieldSerializer(many=True, required=False)

    class Meta:
        model = Form
        fields = [
            'id', 'title', 'slug', 'description', 'image_url', 'category', 'status',
            'version', 'allow_multiple_responses', 'allow_edits_until', 'open_at', 'close_at',
            'fields', 'created_at'
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
            # Soft delete missing fields
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

class ResponseSerializer(serializers.ModelSerializer):
    answers = AnswerSerializer(many=True)

    class Meta:
        model = Response
        fields = ['id', 'form', 'user', 'form_version', 'is_test_submission', 'submitted_at', 'is_manual_entry', 'created_by_admin', 'answers']
        read_only_fields = ['form_version']

    def validate(self, data):
        form = self.context.get('form') or data.get('form')
        answers = data.get('answers', [])
        submitted_map = {str(a['field'].id): a.get('value') for a in answers if 'field' in a}

        if form:
            for field in form.fields.all():
                if field.is_deleted:
                    continue
                is_visible = evaluate_condition(field.conditional_logic, submitted_map)
                if is_visible and field.is_required:
                    val = submitted_map.get(str(field.id))
                    if val is None or val == "":
                        raise serializers.ValidationError({
                            'answers': f"Field '{field.label}' is required."
                        })
        return data

    def create(self, validated_data):
        form = validated_data.get('form')
        if form:
            validated_data['form_version'] = form.version

        answers_data = validated_data.pop('answers', [])
        response = Response.objects.create(**validated_data)
        for answer_data in answers_data:
            Answer.objects.create(response=response, **answer_data)
        return response
