"""Version history, diffs, restore, review-before-save, duplicate, templates and import / export."""

import json

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.forms import versions as form_versions
from apps.forms.models import Answer, FieldType, Form, FormField, FormStatus, FormVersion, Response
from .factories import add_field, make_form

User = get_user_model()


def _rule(field, op="equals", value="x", **extra):
    return {"field": field.id, "operator": op, "value": value, **extra}


class _AdminCase(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="hist", email="hist@srkr.ac.in", password="x", role="ADMIN", is_staff=True)
        self.client.force_authenticate(self.admin)

    def _put(self, form, fields, **extra):
        body = {"title": extra.pop("title", form.title), "slug": form.slug, "status": form.status, "fields": fields, **extra}
        return self.client.put(f"/api/forms/{form.slug}/", body, format="json")

    @staticmethod
    def _payload(field, **over):
        data = {"id": field.id, "label": field.label, "type": field.type, "options": field.options, "rows": field.rows,
                "order": field.order, "is_required": field.is_required, "validation_rules": field.validation_rules,
                "conditional_logic": field.conditional_logic, "description": field.description}
        data.update(over)
        return data


class VersionRecordingTests(_AdminCase):
    def test_every_save_leaves_a_snapshot_of_the_definition_only(self):
        created = self.client.post("/api/forms/", {
            "title": "History", "slug": "history", "fields": [{"label": "Name", "type": "TEXT", "order": 1}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        form = Form.objects.get(slug="history")
        first = form.versions.get(version=1)
        self.assertEqual(first.note, "Created")
        self.assertEqual(first.created_by, self.admin)
        self.assertEqual([f["label"] for f in first.snapshot["fields"]], ["Name"])
        self.assertNotIn("slug", first.snapshot["form"])
        self.assertNotIn("status", first.snapshot["form"])

        field = form.fields.get()
        self.assertEqual(self._put(form, [self._payload(field, label="Full name")]).status_code, 200)
        form.refresh_from_db()
        self.assertEqual(form.version, 2)
        self.assertEqual(form.versions.get(version=2).snapshot["fields"][0]["label"], "Full name")
        self.assertEqual(form.versions.get(version=1).snapshot["fields"][0]["label"], "Name")

    def test_publishing_is_recorded(self):
        form = make_form(status=FormStatus.DRAFT)
        add_field(form, FieldType.TEXT, label="Q", required=True, order=1)
        self.assertEqual(self.client.post(f"/api/forms/{form.slug}/publish/").status_code, 200)
        form.refresh_from_db()
        self.assertEqual(form.versions.get(version=form.version).note, "Published")

    def test_only_the_newest_fifty_versions_are_kept(self):
        form = make_form()
        for n in range(1, 56):
            form.version = n
            form.save()
            form_versions.record_version(form, self.admin, f"v{n}")
        kept = list(form.versions.values_list("version", flat=True))
        self.assertEqual((len(kept), kept[0], kept[-1]), (50, 55, 6))

    def test_history_is_for_admins_only(self):
        form = make_form()
        form_versions.record_version(form, self.admin)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(f"/api/forms/{form.slug}/versions/").status_code, (401, 403))
        self.client.force_authenticate(User.objects.create_user(username="plain", email="p@srkr.ac.in", password="x", role="NON_AFFILIATE"))
        self.assertEqual(self.client.get(f"/api/forms/{form.slug}/versions/").status_code, 403)


class DiffTests(_AdminCase):
    def _snap(self, form):
        return form_versions.snapshot_form(form)

    def test_describes_every_kind_of_change(self):
        form = make_form(status=FormStatus.DRAFT, description="old", max_responses_per_user=1)
        name = add_field(form, FieldType.TEXT, label="Name", order=1)
        track = add_field(form, FieldType.RADIO, label="Track", options=["AI", "Web"], order=2)
        gone = add_field(form, FieldType.TEXT, label="Gone", order=3)
        before = self._snap(form)

        form.description = "new"
        form.save()
        name.label, name.is_required = "Full name", True
        name.validation_rules = {"minLength": 2}
        name.save()
        track.options = ["AI", "IoT"]
        track.conditional_logic = {"logic": "AND", "action": "show", "rules": [_rule(name)]}
        track.save()
        gone.is_deleted = True
        gone.save()
        add_field(form, FieldType.NUMBER, label="Age", order=4)
        diff = form_versions.diff_snapshots(before, self._snap(form))

        self.assertEqual([s["key"] for s in diff["form"]], ["description"])
        self.assertEqual([f["label"] for f in diff["fields"]["added"]], ["Age"])
        self.assertEqual([f["label"] for f in diff["fields"]["removed"]], ["Gone"])
        by_label = {c["label"]: {ch["key"]: ch for ch in c["changes"]} for c in diff["fields"]["changed"]}
        self.assertEqual(set(by_label["Full name"]), {"label", "is_required", "validation_rules"})
        self.assertEqual(by_label["Full name"]["validation_rules"]["changed"], [{"rule": "minLength", "before": None, "after": 2}])
        self.assertEqual((by_label["Track"]["options"]["added"], by_label["Track"]["options"]["removed"]), (["IoT"], ["Web"]))
        self.assertIn("conditional_logic", by_label["Track"])
        self.assertEqual(diff["summary"], {"settings": 1, "added": 1, "removed": 1, "changed": 2, "reordered": False})
        self.assertFalse(diff["is_empty"])

    def test_an_unchanged_form_has_an_empty_diff(self):
        form = make_form()
        add_field(form, FieldType.TEXT, label="Q", order=1)
        snap = self._snap(form)
        self.assertTrue(form_versions.diff_snapshots(snap, snap)["is_empty"])

    def test_reordering_is_noticed_without_inventing_other_changes(self):
        form = make_form()
        a = add_field(form, FieldType.TEXT, label="A", order=1)
        b = add_field(form, FieldType.TEXT, label="B", order=2)
        before = self._snap(form)
        a.order, b.order = 2, 1
        a.save()
        b.save()
        diff = form_versions.diff_snapshots(before, self._snap(form))
        self.assertTrue(diff["fields"]["reordered"])
        self.assertEqual(diff["fields"]["changed"], [])
        self.assertEqual((diff["fields"]["added"], diff["fields"]["removed"]), ([], []))


class VersionApiTests(_AdminCase):
    def setUp(self):
        super().setUp()
        self.form = make_form(status=FormStatus.DRAFT)
        self.q = add_field(self.form, FieldType.TEXT, label="Original", order=1)
        form_versions.record_version(self.form, self.admin, "Created")
        self._put(self.form, [self._payload(self.q, label="Edited")])
        self.form.refresh_from_db()

    def test_the_list_says_what_changed_in_each_version(self):
        rows = self.client.get(f"/api/forms/{self.form.slug}/versions/").data
        self.assertEqual([r["version"] for r in rows], [2, 1])
        self.assertTrue(rows[0]["is_current"])
        self.assertEqual(rows[0]["summary"]["changed"], 1)
        self.assertIsNone(rows[1]["summary"])
        self.assertEqual(rows[0]["created_by"], self.admin.email)

    def test_a_version_shows_its_questions_and_the_diff_against_the_one_before(self):
        data = self.client.get(f"/api/forms/{self.form.slug}/versions/2/").data
        self.assertEqual(data["snapshot"]["fields"][0]["label"], "Edited")
        label_change = data["diff"]["fields"]["changed"][0]["changes"][0]
        self.assertEqual((label_change["before"], label_change["after"]), ("Original", "Edited"))

    def test_an_old_version_can_be_compared_with_the_current_form(self):
        data = self.client.get(f"/api/forms/{self.form.slug}/versions/1/?against=current").data
        change = data["diff"]["fields"]["changed"][0]["changes"][0]
        self.assertEqual((change["before"], change["after"]), ("Edited", "Original"))  # what going back to v1 would do

    def test_a_missing_version_is_404(self):
        self.assertEqual(self.client.get(f"/api/forms/{self.form.slug}/versions/99/").status_code, 404)


class RestoreTests(_AdminCase):
    def setUp(self):
        super().setUp()
        self.form = make_form(status=FormStatus.DRAFT)
        self.keep = add_field(self.form, FieldType.TEXT, label="Keep", order=1)
        self.drop = add_field(self.form, FieldType.TEXT, label="Dropped later", order=2)
        form_versions.record_version(self.form, self.admin, "Created")
        # v2: remove one question, add another, rename the first, change a setting
        self._put(self.form, [self._payload(self.keep, label="Keep (renamed)"), {"label": "New", "type": "TEXT", "order": 2}],
                  description="changed")
        self.form.refresh_from_db()

    def test_restoring_brings_back_questions_and_settings_as_a_new_version(self):
        resp = self.client.post(f"/api/forms/{self.form.slug}/versions/1/restore/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.form.refresh_from_db()
        self.assertEqual(self.form.version, 3)
        self.assertEqual(self.form.description, "")
        labels = list(self.form.fields.order_by("order").values_list("label", flat=True))
        self.assertEqual(labels, ["Keep", "Dropped later"])
        self.assertEqual(FormField.all_objects.get(pk=self.drop.pk).is_deleted, False)  # same question, same id
        self.assertEqual(self.form.versions.get(version=3).note, "Restored from version 1")
        self.assertEqual(self.form.versions.get(version=2).snapshot["fields"][0]["label"], "Keep (renamed)")  # history intact

    def test_restoring_never_changes_the_address_or_unpublishes(self):
        self.form.status = FormStatus.PUBLISHED
        self.form.save()
        slug = self.form.slug
        self.assertEqual(self.client.post(f"/api/forms/{slug}/versions/1/restore/").status_code, 200)
        self.form.refresh_from_db()
        self.assertEqual((self.form.slug, self.form.status), (slug, FormStatus.PUBLISHED))

    def test_a_restore_cannot_change_the_type_of_an_answered_question(self):
        self.keep.type = FieldType.NUMBER
        self.keep.save()
        response = Response.objects.create(form=self.form)
        Answer.objects.create(response=response, field=self.keep, value=5)
        resp = self.client.post(f"/api/forms/{self.form.slug}/versions/1/restore/")  # v1 had Keep as TEXT
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["code"], "FIELD_TYPE_LOCKED")
        self.keep.refresh_from_db()
        self.assertEqual(self.keep.type, FieldType.NUMBER)

    def test_restoring_a_missing_version_is_404_and_needs_an_admin(self):
        self.assertEqual(self.client.post(f"/api/forms/{self.form.slug}/versions/42/restore/").status_code, 404)
        self.client.force_authenticate(User.objects.create_user(username="np", email="np@srkr.ac.in", password="x", role="NON_AFFILIATE"))
        self.assertEqual(self.client.post(f"/api/forms/{self.form.slug}/versions/1/restore/").status_code, 403)


class ReviewBeforeSavingTests(_AdminCase):
    def setUp(self):
        super().setUp()
        self.form = make_form(status=FormStatus.PUBLISHED, allow_multiple_responses=True, max_responses_per_user=9)
        self.track = add_field(self.form, FieldType.RADIO, label="Track", options=["AI", "Web", "IoT"], order=1)
        self.note = add_field(self.form, FieldType.TEXT, label="Note", order=2)
        for value in ("AI", "AI", "Web", "IoT"):
            r = Response.objects.create(form=self.form)
            Answer.objects.create(response=r, field=self.track, value=value)
            Answer.objects.create(response=r, field=self.note, value="n")
        form_versions.record_version(self.form, self.admin)

    def _preview(self, fields, **extra):
        body = {"title": self.form.title, "fields": fields, **extra}
        return self.client.post(f"/api/forms/{self.form.slug}/preview-changes/", body, format="json")

    def test_nothing_is_saved(self):
        resp = self._preview([self._payload(self.track, label="Renamed"), self._payload(self.note)])
        self.assertEqual(resp.status_code, 200, resp.data)
        self.track.refresh_from_db()
        self.form.refresh_from_db()
        self.assertEqual((self.track.label, self.form.version), ("Track", 1))
        self.assertEqual(self.form.versions.count(), 1)

    def test_an_unchanged_form_has_nothing_to_confirm(self):
        resp = self._preview([self._payload(self.track), self._payload(self.note)])
        self.assertTrue(resp.data["diff"]["is_empty"], resp.data["diff"])

    def test_it_says_what_the_change_means_for_collected_responses(self):
        resp = self._preview([
            self._payload(self.track, options=["AI", "Robotics"], is_required=True),   # drops Web + IoT, now required
            {"label": "Phone", "type": "PHONE", "order": 2, "is_required": True},     # new required question; "Note" removed
        ])
        self.assertEqual(resp.status_code, 200, resp.data)
        impact = resp.data["impact"]
        self.assertEqual(impact["response_count"], 4)
        self.assertEqual(sorted((o["option"], o["answers"]) for o in impact["options_removed_with_answers"]), [("IoT", 1), ("Web", 1)])
        self.assertEqual([r["label"] for r in impact["removed_with_answers"]], ["Note"])
        self.assertEqual(impact["removed_with_answers"][0]["answers"], 4)
        self.assertEqual([r["label"] for r in impact["new_required"]], ["Phone"])
        self.assertEqual([r["label"] for r in impact["now_required"]], ["Track"])
        self.assertEqual(resp.data["diff"]["summary"]["added"], 1)

    def test_a_question_that_becomes_required_is_flagged(self):
        resp = self._preview([self._payload(self.track), self._payload(self.note, is_required=True)])
        self.assertEqual([r["label"] for r in resp.data["impact"]["now_required"]], ["Note"])

    def test_a_blocked_type_change_is_reported_by_the_same_validation_as_a_real_save(self):
        resp = self._preview([self._payload(self.track, type="TEXT"), self._payload(self.note)])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["code"], "FIELD_TYPE_LOCKED")

    def test_setting_changes_show_up_in_the_diff(self):
        resp = self._preview([self._payload(self.track), self._payload(self.note)], max_responses_per_user=2)
        self.assertEqual([s["key"] for s in resp.data["diff"]["form"]], ["max_responses_per_user"])

    def test_admins_only(self):
        self.client.force_authenticate(User.objects.create_user(username="rv", email="rv@srkr.ac.in", password="x", role="NON_AFFILIATE"))
        self.assertEqual(self._preview([]).status_code, 403)


class DuplicateTests(_AdminCase):
    def setUp(self):
        super().setUp()
        self.form = make_form(status=FormStatus.PUBLISHED, title="Workshop", open_at=timezone.now(), close_at=timezone.now(),
                              max_total_responses=50, club_id_enabled=False)
        self.email = add_field(self.form, FieldType.EMAIL, label="Email", order=1)
        self.role = add_field(self.form, FieldType.RADIO, label="Role", options=["student", "staff"], order=2)
        self.start = add_field(self.form, FieldType.DATE, label="Start", order=3)
        self.end = add_field(self.form, FieldType.DATE, label="End", order=4, validation_rules={
            "crossField": [{"op": "gte", "field": self.start.id, "message": "End after start"}]})
        self.dept = add_field(self.form, FieldType.TEXT, label="Dept for {{%d}}" % self.role.id, order=5, conditional_logic={
            "logic": "AND", "action": "show", "rules": [_rule(self.role, value="student"), _rule(self.end, "after", None, value_field=self.start.id)]})
        self.price = add_field(self.form, FieldType.NUMBER, label="Price", order=6)
        self.total = add_field(self.form, FieldType.CALCULATED, label="Total", order=7,
                               validation_rules={"formula": "{%d} * 2" % self.price.id, "decimals": 2})
        self.brk1 = add_field(self.form, FieldType.PAGE_BREAK, label="More", order=8, conditional_logic={
            "logic": "AND", "action": "skip_to_section", "rules": [_rule(self.role, value="staff")]})
        add_field(self.form, FieldType.TEXT, label="Page two", order=9)
        self.brk2 = add_field(self.form, FieldType.PAGE_BREAK, label="Last", order=10)
        add_field(self.form, FieldType.TEXT, label="Page three", order=11)
        self.brk1.conditional_logic["action_value"] = self.brk2.id
        self.brk1.save()
        self.form.club_id_field_mapping = {"email": self.email.id}
        self.form.save()
        r = Response.objects.create(form=self.form)
        Answer.objects.create(response=r, field=self.email, value="a@b.co")

    def _copy(self):
        resp = self.client.post(f"/api/forms/{self.form.slug}/duplicate/", {}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        return Form.objects.get(slug=resp.data["slug"])

    def test_the_copy_is_a_new_draft_without_responses_or_dates(self):
        copy = self._copy()
        self.assertNotEqual(copy.pk, self.form.pk)
        self.assertEqual((copy.title, copy.status, copy.version, copy.is_template), ("Copy of Workshop", FormStatus.DRAFT, 1, False))
        self.assertEqual(copy.created_by, self.admin)
        self.assertIsNone(copy.open_at)
        self.assertIsNone(copy.close_at)
        self.assertEqual(copy.max_total_responses, 50)
        self.assertEqual(copy.responses.count(), 0)
        self.assertEqual(copy.fields.count(), self.form.fields.count())
        self.assertEqual(self.form.fields.count(), 11)  # the original is untouched
        self.assertEqual(copy.versions.get().note, "Copy of 'Workshop'")

    def test_every_reference_points_at_the_copys_own_questions(self):
        copy = self._copy()
        by_label = {f.label.split(" for ")[0]: f for f in copy.fields.all()}
        new_ids = {f.id for f in copy.fields.all()}
        old_ids = {f.id for f in self.form.fields.all()}
        self.assertFalse(new_ids & old_ids)

        dept = by_label["Dept"]
        self.assertEqual(dept.label, "Dept for {{%d}}" % by_label["Role"].id)
        rules = dept.conditional_logic["rules"]
        self.assertEqual(rules[0]["field"], by_label["Role"].id)
        self.assertEqual((rules[1]["field"], rules[1]["value_field"]), (by_label["End"].id, by_label["Start"].id))
        cross = by_label["End"].validation_rules["crossField"]
        self.assertEqual(cross, [{"op": "gte", "field": by_label["Start"].id, "message": "End after start"}])
        self.assertEqual(by_label["Total"].validation_rules["formula"], "{%d} * 2" % by_label["Price"].id)
        route = by_label["More"].conditional_logic
        self.assertEqual((route["rules"][0]["field"], route["action_value"]), (by_label["Role"].id, by_label["Last"].id))
        self.assertEqual(copy.club_id_field_mapping, {"email": by_label["Email"].id})

    def test_the_copy_can_be_edited_and_published_on_its_own(self):
        copy = self._copy()
        self.assertEqual(self.client.post(f"/api/forms/{copy.slug}/publish/").status_code, 200)
        self.form.refresh_from_db()
        self.assertEqual(self.form.version, 1)

    def test_a_custom_title_and_unique_slugs(self):
        first = self.client.post(f"/api/forms/{self.form.slug}/duplicate/", {"title": "Spring workshop"}, format="json").data
        second = self.client.post(f"/api/forms/{self.form.slug}/duplicate/", {"title": "Spring workshop"}, format="json").data
        self.assertEqual(first["title"], "Spring workshop")
        self.assertNotEqual(first["slug"], second["slug"])

    def test_admins_only(self):
        self.client.force_authenticate(User.objects.create_user(username="dp", email="dp@srkr.ac.in", password="x", role="NON_AFFILIATE"))
        self.assertEqual(self.client.post(f"/api/forms/{self.form.slug}/duplicate/").status_code, 403)


class TemplateTests(_AdminCase):
    def setUp(self):
        super().setUp()
        self.form = make_form(status=FormStatus.PUBLISHED, title="Event signup")
        add_field(self.form, FieldType.TEXT, label="Name", order=1)

    def _ids(self, query=""):
        resp = self.client.get(f"/api/forms/{query}")
        rows = resp.data["results"] if isinstance(resp.data, dict) else resp.data
        return {row["slug"] for row in rows}

    def test_a_template_is_kept_out_of_the_normal_lists(self):
        resp = self.client.post(f"/api/forms/{self.form.slug}/save-as-template/", {}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        template = Form.objects.get(slug=resp.data["slug"])
        self.assertTrue(template.is_template)
        self.assertEqual(template.status, FormStatus.DRAFT)
        self.assertTrue(resp.data["is_template"])
        self.assertNotIn(template.slug, self._ids())
        self.assertIn(self.form.slug, self._ids())
        self.assertEqual(self._ids("?templates=only"), {template.slug})

    def test_only_admins_can_list_templates(self):
        template = Form.objects.get(slug=self.client.post(f"/api/forms/{self.form.slug}/save-as-template/").data["slug"])
        self.client.force_authenticate(User.objects.create_user(username="tp", email="tp@srkr.ac.in", password="x", role="NON_AFFILIATE"))
        self.assertNotIn(template.slug, self._ids("?templates=only"))
        self.assertEqual(self.client.get(f"/api/forms/{template.slug}/").status_code, 404)  # a draft is invisible to everyone else

    def test_a_form_made_from_a_template_is_an_ordinary_form(self):
        template = Form.objects.get(slug=self.client.post(f"/api/forms/{self.form.slug}/save-as-template/").data["slug"])
        made = self.client.post(f"/api/forms/{template.slug}/duplicate/", {"title": "Autumn signup"}, format="json").data
        self.assertFalse(made["is_template"])
        self.assertIn(made["slug"], self._ids())


class ImportExportTests(_AdminCase):
    def setUp(self):
        super().setUp()
        self.form = make_form(status=FormStatus.PUBLISHED, title="Portable", description="d", open_at=timezone.now())
        self.a = add_field(self.form, FieldType.RADIO, label="Choice", options=["x", "y"], order=1)
        self.b = add_field(self.form, FieldType.TEXT, label="Why {{%d}}" % self.a.id, order=2, conditional_logic={
            "logic": "AND", "action": "show", "rules": [_rule(self.a, value="x")]})

    def _export(self):
        resp = self.client.get(f"/api/forms/{self.form.slug}/export-definition/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp["Content-Disposition"])
        return resp.data

    def _import(self, doc):
        return self.client.post("/api/forms/import-definition/", doc, format="json")

    def test_a_form_survives_a_round_trip(self):
        doc = self._export()
        self.assertEqual((doc["format"], doc["version"]), ("srkrcc-form", 1))
        json.dumps(doc, default=str)  # a plain file
        resp = self._import(json.loads(json.dumps(doc, default=str)))
        self.assertEqual(resp.status_code, 201, resp.data)
        made = Form.objects.get(slug=resp.data["slug"])
        self.assertEqual((made.title, made.status, made.description), ("Portable", FormStatus.DRAFT, "d"))
        self.assertIsNone(made.open_at)
        new_a, new_b = list(made.fields.order_by("order"))
        self.assertNotEqual(new_a.id, self.a.id)
        self.assertEqual(new_b.label, "Why {{%d}}" % new_a.id)
        self.assertEqual(new_b.conditional_logic["rules"][0]["field"], new_a.id)
        self.assertEqual(made.versions.get().note, "Imported from a form file")

    def test_references_to_questions_missing_from_the_file_are_dropped_not_misdirected(self):
        doc = self._export()
        doc["fields"].pop(0)  # the question the second one depends on is gone
        made = Form.objects.get(slug=self._import(doc).data["slug"])
        only = made.fields.get()
        self.assertEqual(only.conditional_logic, {})
        self.assertEqual(only.label, "Why {{%d}}" % self.a.id)  # an unmapped pipe is left for the publish check to flag

    def test_the_email_template_and_dates_do_not_travel(self):
        doc = self._export()
        doc["form"]["confirmation_email_template"] = 424242
        doc["form"]["confirmation_email_enabled"] = True
        doc["form"]["close_at"] = "2030-01-01T00:00:00Z"
        made = Form.objects.get(slug=self._import(doc).data["slug"])
        self.assertIsNone(made.confirmation_email_template)
        self.assertFalse(made.confirmation_email_enabled)
        self.assertIsNone(made.close_at)

    def test_unusable_files_are_refused_with_a_reason(self):
        good = self._export()
        cases = {
            "not a form file": {"hello": "world"},
            "wrong version": {**good, "version": 99},
            "no fields": {**good, "fields": "nope"},
            "no title": {**good, "form": {**good["form"], "title": "  "}},
            "too many questions": {**good, "fields": [{"label": f"Q{i}", "type": "TEXT"} for i in range(201)]},
        }
        for name, doc in cases.items():
            resp = self._import(doc)
            self.assertEqual(resp.status_code, 400, f"{name}: {resp.data}")
            self.assertEqual(resp.data["code"], "INVALID_FORM_FILE", name)
        self.assertEqual(Form.objects.filter(title__in=("Portable",)).count(), 1)

    def test_a_file_with_an_invalid_question_creates_nothing(self):
        doc = self._export()
        doc["fields"][0]["type"] = "NOT_A_TYPE"
        before = Form.objects.count()
        self.assertEqual(self._import(doc).status_code, 400)
        self.assertEqual(Form.objects.count(), before)

    def test_admins_only(self):
        doc = self._export()
        self.client.force_authenticate(User.objects.create_user(username="ie", email="ie@srkr.ac.in", password="x", role="NON_AFFILIATE"))
        self.assertEqual(self._import(doc).status_code, 403)
        self.assertEqual(self.client.get(f"/api/forms/{self.form.slug}/export-definition/").status_code, 403)
