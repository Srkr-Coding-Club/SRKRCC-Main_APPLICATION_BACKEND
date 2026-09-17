from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.forms.models import FieldType, FormStatus, Response
from .factories import make_form, add_field

User = get_user_model()


class BulkDeleteResponsesTests(APITestCase):
    """
    POST /api/forms/{slug}/responses/bulk-delete/

    Admins/club leads may delete a batch of a form's responses in one call.
    Every id in the request must belong to the target form — if any don't,
    nothing is deleted and the endpoint reports a 400.
    """

    def setUp(self):
        self.form = make_form(status=FormStatus.PUBLISHED)
        self.name_field = add_field(self.form, FieldType.TEXT, label="Name", required=True, order=1)

        self.other_form = make_form(status=FormStatus.PUBLISHED)

        self.admin = User.objects.create_user(
            username="bulkadmin", email="bulkadmin@srkr.ac.in", password="pw12345!", role="ADMIN",
        )
        self.club_lead = User.objects.create_user(
            username="bulklead", email="bulklead@srkr.ac.in", password="pw12345!", role="CLUB_LEAD",
        )
        self.member = User.objects.create_user(
            username="bulkmember", email="bulkmember@srkr.ac.in", password="pw12345!", role="MEMBER",
        )

        self.responses = [
            Response.objects.create(form=self.form, form_version=self.form.version)
            for _ in range(3)
        ]
        self.other_response = Response.objects.create(form=self.other_form, form_version=self.other_form.version)

    def _url(self, slug=None):
        return f"/api/forms/{slug or self.form.slug}/responses/bulk-delete/"

    def test_unauthenticated_cannot_bulk_delete(self):
        resp = self.client.post(self._url(), {"response_ids": [self.responses[0].id]}, format="json")
        self.assertIn(resp.status_code, (401, 403))
        self.assertTrue(Response.objects.filter(id=self.responses[0].id).exists())

    def test_non_admin_cannot_bulk_delete(self):
        self.client.force_authenticate(self.member)
        resp = self.client.post(self._url(), {"response_ids": [self.responses[0].id]}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Response.objects.filter(id=self.responses[0].id).exists())

    def test_admin_can_bulk_delete(self):
        self.client.force_authenticate(self.admin)
        ids = [r.id for r in self.responses[:2]]
        resp = self.client.post(self._url(), {"response_ids": ids}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["deleted_count"], 2)
        self.assertEqual(Response.objects.filter(id__in=ids).count(), 0)
        # The response not included in the request survives.
        self.assertTrue(Response.objects.filter(id=self.responses[2].id).exists())

    def test_club_lead_can_bulk_delete(self):
        self.client.force_authenticate(self.club_lead)
        ids = [self.responses[0].id]
        resp = self.client.post(self._url(), {"response_ids": ids}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["deleted_count"], 1)

    def test_rejects_responses_not_belonging_to_form(self):
        self.client.force_authenticate(self.admin)
        ids = [self.responses[0].id, self.other_response.id]
        resp = self.client.post(self._url(), {"response_ids": ids}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.data)
        # Nothing was deleted since the request was rejected as a whole.
        self.assertTrue(Response.objects.filter(id=self.responses[0].id).exists())
        self.assertTrue(Response.objects.filter(id=self.other_response.id).exists())

    def test_rejects_nonexistent_response_ids(self):
        self.client.force_authenticate(self.admin)
        bogus_id = self.other_response.id + 10_000
        resp = self.client.post(self._url(), {"response_ids": [bogus_id]}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.data)

    def test_rejects_empty_list(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post(self._url(), {"response_ids": []}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_rejects_non_list_payload(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post(self._url(), {"response_ids": "not-a-list"}, format="json")
        self.assertEqual(resp.status_code, 400)
