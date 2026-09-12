from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()


class UserRoleUpdateTests(TestCase):
    """PATCH /auth/users/{id}/ — the admin Users tab's role dropdown."""

    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username='admin1', email='admin1@srkr.ac.in', password='pw12345!', role='ADMIN',
        )
        self.club_lead = User.objects.create_user(
            username='lead1', email='lead1@srkr.ac.in', password='pw12345!', role='CLUB_LEAD',
        )
        self.member = User.objects.create_user(
            username='member1', email='member1@srkr.ac.in', password='pw12345!', role='MEMBER',
        )

    def _url(self, user):
        return f'/api/auth/users/{user.id}/'

    def test_admin_can_promote_member_to_volunteer(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.member), {'role': 'VOLUNTEER'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, 'VOLUNTEER')

    def test_admin_can_promote_member_to_admin(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.member), {'role': 'ADMIN'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, 'ADMIN')

    def test_club_lead_cannot_promote_to_admin(self):
        self.client.force_authenticate(self.club_lead)
        resp = self.client.patch(self._url(self.member), {'role': 'ADMIN'}, format='json')
        self.assertEqual(resp.status_code, 403)
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, 'MEMBER')

    def test_club_lead_cannot_promote_to_club_lead(self):
        self.client.force_authenticate(self.club_lead)
        resp = self.client.patch(self._url(self.member), {'role': 'CLUB_LEAD'}, format='json')
        self.assertEqual(resp.status_code, 403)

    def test_club_lead_can_change_low_privilege_roles(self):
        self.client.force_authenticate(self.club_lead)
        resp = self.client.patch(self._url(self.member), {'role': 'VOLUNTEER'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, 'VOLUNTEER')

    def test_cannot_change_own_role(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.admin), {'role': 'MEMBER'}, format='json')
        self.assertEqual(resp.status_code, 403)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, 'ADMIN')

    def test_member_forbidden_from_endpoint(self):
        self.client.force_authenticate(self.member)
        resp = self.client.patch(self._url(self.club_lead), {'role': 'MEMBER'}, format='json')
        self.assertEqual(resp.status_code, 403)

    def test_unauthenticated_forbidden(self):
        resp = self.client.patch(self._url(self.member), {'role': 'VOLUNTEER'}, format='json')
        self.assertIn(resp.status_code, (401, 403))

    def test_club_lead_can_change_membership_status(self):
        # Unlike `role`, `membership_status` isn't a privilege field — a
        # CLUB_LEAD may set it even though they can't grant elevated roles.
        self.client.force_authenticate(self.club_lead)
        resp = self.client.patch(self._url(self.member), {'membership_status': 'SUSPENDED'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.membership_status, 'SUSPENDED')

    def test_club_lead_can_change_membership_status_of_elevated_user(self):
        # Regression guard: the role-escalation check must key off the role
        # actually being submitted, not the target's current role — otherwise
        # a membership_status-only PATCH targeting an ADMIN/CLUB_LEAD user
        # would be wrongly rejected as a role escalation attempt.
        self.client.force_authenticate(self.club_lead)
        resp = self.client.patch(self._url(self.admin), {'membership_status': 'INACTIVE'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.membership_status, 'INACTIVE')
        self.assertEqual(self.admin.role, 'ADMIN')

    def test_invalid_membership_status_rejected(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.member), {'membership_status': 'NOT_A_REAL_STATUS'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.member.refresh_from_db()
        self.assertEqual(self.member.membership_status, 'ACTIVE')
