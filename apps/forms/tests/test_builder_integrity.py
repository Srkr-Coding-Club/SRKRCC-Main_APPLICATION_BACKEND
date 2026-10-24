"""Form-builder integrity: help text is kept, saves are all-or-nothing, answered questions keep their
type, and deleting a question leaves no dangling rules or settings behind."""

from unittest import mock

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.forms.models import Answer, FieldType, Form, FormField, FormStatus, Response
from apps.forms.validation import codes
from .factories import add_field, make_form

User = get_user_model()


def _rule(field, op="equals", value="x"):
    return {"field": field.id, "operator": op, "value": value}


class _AdminCase(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="builder", email="builder@srkr.ac.in", password="x", role="ADMIN", is_staff=True)
        self.client.force_authenticate(self.admin)

    def _put(self, form, fields, **extra):
        body = {"title": form.title, "slug": form.slug, "status": form.status, "fields": fields, **extra}
        return self.client.put(f"/api/forms/{form.slug}/", body, format="json")

    @staticmethod
    def _payload(field, **overrides):
        data = {
            "id": field.id, "label": field.label, "type": field.type, "options": field.options,
            "order": field.order, "is_required": field.is_required, "validation_rules": field.validation_rules,
            "conditional_logic": field.conditional_logic,
        }
        data.update(overrides)
        return data


class HelpTextTests(_AdminCase):
    def test_question_help_text_is_saved_and_returned(self):
        resp = self.client.post("/api/forms/", {
            "title": "Help", "slug": "help-form", "fields": [
                {"label": "Roll no", "type": "TEXT", "description": "Printed on your ID card", "order": 1},
            ],
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["fields"][0]["description"], "Printed on your ID card")
        self.assertEqual(FormField.objects.get(form__slug="help-form").description, "Printed on your ID card")

    def test_help_text_can_be_edited_and_cleared(self):
        form = make_form(status=FormStatus.DRAFT)
        field = add_field(form, FieldType.TEXT, label="Q", order=1)
        self.assertEqual(self._put(form, [self._payload(field, description="Use caps")]).status_code, 200)
        field.refresh_from_db()
        self.assertEqual(field.description, "Use caps")
        self.assertEqual(self._put(form, [self._payload(field, description="")]).status_code, 200)
        field.refresh_from_db()
        self.assertEqual(field.description, "")


class AtomicSaveTests(_AdminCase):
    def test_a_failed_field_write_rolls_back_the_whole_save(self):
        form = make_form(status=FormStatus.DRAFT)
        keep = add_field(form, FieldType.TEXT, label="Keep", order=1)
        gone = add_field(form, FieldType.TEXT, label="Gone", order=2)
        version = form.version

        def boom(*args, **kwargs):
            raise RuntimeError("database went away")

        with mock.patch.object(FormField.objects, "create", side_effect=boom):
            self.client.raise_request_exception = False
            resp = self._put(form, [
                self._payload(keep, label="Renamed"),
                {"label": "Brand new", "type": "TEXT", "order": 3},
            ], title="New title")
        self.assertEqual(resp.status_code, 500)

        form.refresh_from_db()
        keep.refresh_from_db()
        gone.refresh_from_db()
        self.assertEqual(form.version, version)
        self.assertNotEqual(form.title, "New title")
        self.assertEqual(keep.label, "Keep")
        self.assertFalse(gone.is_deleted)


class TypeLockTests(_AdminCase):
    def setUp(self):
        super().setUp()
        self.form = make_form(status=FormStatus.PUBLISHED)
        self.q = add_field(self.form, FieldType.TEXT, label="Age", order=1)

    def _answer(self):
        response = Response.objects.create(form=self.form)
        Answer.objects.create(response=response, field=self.q, value="19")

    def test_type_change_is_refused_once_the_question_has_answers(self):
        self._answer()
        resp = self._put(self.form, [self._payload(self.q, type="NUMBER")])
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["code"], codes.FIELD_TYPE_LOCKED)
        self.assertEqual(resp.data["errors"][0]["field_id"], self.q.id)
        self.q.refresh_from_db()
        self.assertEqual(self.q.type, FieldType.TEXT)

    def test_other_edits_to_an_answered_question_are_still_allowed(self):
        self._answer()
        resp = self._put(self.form, [self._payload(self.q, label="Your age", description="In years")])
        self.assertEqual(resp.status_code, 200, resp.data)
        self.q.refresh_from_db()
        self.assertEqual((self.q.label, self.q.type), ("Your age", FieldType.TEXT))

    def test_type_change_is_fine_before_any_response(self):
        resp = self._put(self.form, [self._payload(self.q, type="NUMBER")])
        self.assertEqual(resp.status_code, 200, resp.data)
        self.q.refresh_from_db()
        self.assertEqual(self.q.type, FieldType.NUMBER)


class DeleteFieldReferenceTests(_AdminCase):
    def setUp(self):
        super().setUp()
        self.form = make_form(status=FormStatus.DRAFT)
        self.trigger = add_field(self.form, FieldType.RADIO, label="Trigger", options=["a", "b"], order=1)
        self.other = add_field(self.form, FieldType.RADIO, label="Other", options=["a", "b"], order=2)

    def _delete(self, confirm=False):
        suffix = "?confirm=true" if confirm else ""
        return self.client.delete(f"/api/forms/{self.form.slug}/fields/{self.trigger.id}/{suffix}")

    def test_canonical_condition_rules_are_cleared_not_left_dangling(self):
        dependent = add_field(self.form, FieldType.TEXT, label="Dep", order=3, conditional_logic={
            "logic": "OR", "action": "show", "rules": [_rule(self.trigger, value="a"), _rule(self.other, value="b")],
        })
        warned = self._delete()
        self.assertEqual(warned.status_code, 200)
        self.assertIn("warning", warned.data)
        self.assertEqual([d["id"] for d in warned.data["dependent_fields"]], [dependent.id])

        self.assertEqual(self._delete(confirm=True).data["status"], "deleted")
        dependent.refresh_from_db()
        self.assertEqual([r["field"] for r in dependent.conditional_logic["rules"]], [self.other.id])
        self.assertEqual(dependent.conditional_logic["action"], "show")
        self.assertEqual(dependent.conditional_logic["logic"], "OR")

    def test_a_condition_that_only_used_the_deleted_question_is_removed_entirely(self):
        dependent = add_field(self.form, FieldType.TEXT, label="Dep", order=3, conditional_logic={
            "logic": "AND", "rules": [_rule(self.trigger)], "action": "hide",
        })
        self._delete(confirm=True)
        dependent.refresh_from_db()
        self.assertEqual(dependent.conditional_logic, {})

    def test_nested_groups_and_legacy_if_rules_are_cleared(self):
        nested = add_field(self.form, FieldType.TEXT, label="Nested", order=3, conditional_logic={
            "logic": "AND", "action": "show", "rules": [
                {"logic": "OR", "rules": [_rule(self.trigger), _rule(self.other, value="b")]},
            ],
        })
        legacy = add_field(self.form, FieldType.TEXT, label="Legacy", order=4,
                           conditional_logic={"if": self.trigger.id, "equals": "a"})
        self._delete(confirm=True)
        nested.refresh_from_db()
        legacy.refresh_from_db()
        self.assertEqual(nested.conditional_logic["rules"][0]["rules"], [_rule(self.other, value="b")])
        self.assertEqual(legacy.conditional_logic, {})

    def test_cross_field_rules_pointing_at_the_question_are_cleared(self):
        dependent = add_field(self.form, FieldType.TEXT, label="Dep", order=3, validation_rules={
            "minLength": 2, "crossField": [
                {"field": self.trigger.id, "op": "ne"}, {"field": self.other.id, "op": "ne"},
            ],
        })
        warned = self._delete()
        self.assertEqual([d["id"] for d in warned.data["dependent_fields"]], [dependent.id])
        self._delete(confirm=True)
        dependent.refresh_from_db()
        self.assertEqual(dependent.validation_rules["crossField"], [{"field": self.other.id, "op": "ne"}])
        self.assertEqual(dependent.validation_rules["minLength"], 2)

    def test_club_id_mapping_pointing_at_the_question_is_reported_and_cleared(self):
        self.form.club_id_field_mapping = {"email": self.trigger.id, "full_name": self.other.id}
        self.form.save()
        warned = self._delete()
        self.assertEqual(warned.data["form_settings"], ["email"])
        self.assertFalse(FormField.objects.get(pk=self.trigger.pk).is_deleted)

        self._delete(confirm=True)
        self.form.refresh_from_db()
        self.assertEqual(self.form.club_id_field_mapping, {"full_name": self.other.id})

    def test_a_question_nothing_depends_on_deletes_without_a_warning(self):
        resp = self._delete()
        self.assertEqual(resp.data, {"status": "deleted", "id": self.trigger.id})
        self.assertTrue(FormField.all_objects.get(pk=self.trigger.pk).is_deleted)
