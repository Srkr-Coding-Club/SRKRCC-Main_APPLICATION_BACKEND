from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()


class UserRoleUpdateTests(TestCase):
    """PATCH /auth/users/{id}/ - the admin Users tab's role dropdown."""

    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username='admin1', email='admin1@srkr.ac.in', password='pw12345!', role='ADMIN',
        )
        self.club_lead = User.objects.create_user(
            username='lead1', email='lead1@srkr.ac.in', password='pw12345!', role='CLUB_LEAD',
        )
        self.member = User.objects.create_user(
            username='member1', email='member1@srkr.ac.in', password='pw12345!', role='NON_AFFILIATE',
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
        self.assertEqual(self.member.role, 'NON_AFFILIATE')

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

    def test_admin_cannot_promote_to_affiliate_without_a_club_id(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.member), {'role': 'AFFILIATE'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('club_id', resp.data)
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, 'NON_AFFILIATE')

    def test_admin_can_promote_to_affiliate_when_club_id_already_set(self):
        self.member.club_id = '25SCC420'
        self.member.save(update_fields=['club_id'])
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.member), {'role': 'AFFILIATE'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, 'AFFILIATE')

    def test_admin_can_promote_to_affiliate_and_assign_club_id_in_same_request(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(
            self._url(self.member),
            {'role': 'AFFILIATE', 'club_id': '25SCC777'},
            format='json'
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, 'AFFILIATE')
        self.assertEqual(self.member.club_id, '25SCC777')

    def test_admin_promote_to_affiliate_with_malformed_club_id_rejected(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(
            self._url(self.member),
            {'role': 'AFFILIATE', 'club_id': 'not-a-club-id'},
            format='json'
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('club_id', resp.data)
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, 'NON_AFFILIATE')
        self.assertIsNone(self.member.club_id)

    def test_admin_promote_to_affiliate_with_duplicate_club_id_rejected(self):
        User.objects.create_user(
            username='existing_affiliate',
            email='affiliate@srkr.ac.in',
            password='pw12345!',
            role='AFFILIATE',
            club_id='25SCC999'
        )
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(
            self._url(self.member),
            {'role': 'AFFILIATE', 'club_id': '25SCC999'},
            format='json'
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('club_id', resp.data)
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, 'NON_AFFILIATE')

    def test_admin_cannot_clear_club_id_for_existing_affiliate(self):
        self.member.role = 'AFFILIATE'
        self.member.club_id = '25SCC420'
        self.member.save(update_fields=['role', 'club_id'])
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.member), {'club_id': ''}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('club_id', resp.data)
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, 'AFFILIATE')
        self.assertEqual(self.member.club_id, '25SCC420')

    def test_cannot_change_own_role(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.admin), {'role': 'NON_AFFILIATE'}, format='json')
        self.assertEqual(resp.status_code, 403)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, 'ADMIN')

    def test_member_forbidden_from_endpoint(self):
        self.client.force_authenticate(self.member)
        resp = self.client.patch(self._url(self.club_lead), {'role': 'NON_AFFILIATE'}, format='json')
        self.assertEqual(resp.status_code, 403)

    def test_unauthenticated_forbidden(self):
        resp = self.client.patch(self._url(self.member), {'role': 'VOLUNTEER'}, format='json')
        self.assertIn(resp.status_code, (401, 403))

    def test_club_lead_can_change_membership_status(self):
        # Unlike `role`, `membership_status` isn't a privilege field - a
        # CLUB_LEAD may set it even though they can't grant elevated roles.
        self.client.force_authenticate(self.club_lead)
        resp = self.client.patch(self._url(self.member), {'membership_status': 'SUSPENDED'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.membership_status, 'SUSPENDED')

    def test_club_lead_can_change_membership_status_of_elevated_user(self):
        # Regression guard: the role-escalation check must key off the role
        # actually being submitted, not the target's current role - otherwise
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

    # --- Roll number: admin can set/correct/clear it any time --------------
    # Unlike the member's own self-service PATCH /api/auth/me/ (which locks
    # roll_number after the member's first self-set - see
    # test_profile_update_validation.py), this endpoint has no such lock:
    # an admin can add, correct, or clear it whenever needed.

    def test_admin_can_set_roll_number_for_a_member_with_none(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.member), {'roll_number': '21B91A0501'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.member.refresh_from_db()
        self.assertEqual(self.member.roll_number, '21B91A0501')

    def test_admin_can_correct_a_members_already_set_roll_number(self):
        self.member.roll_number = '21B91A0501'
        self.member.save(update_fields=['roll_number'])
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.member), {'roll_number': '21B91A0502'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.member.refresh_from_db()
        self.assertEqual(self.member.roll_number, '21B91A0502')

    def test_admin_can_clear_a_members_roll_number(self):
        self.member.roll_number = '21B91A0501'
        self.member.save(update_fields=['roll_number'])
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.member), {'roll_number': ''}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.member.refresh_from_db()
        self.assertIsNone(self.member.roll_number)

    def test_club_lead_can_also_edit_roll_number(self):
        # roll_number carries no privilege risk (unlike `role`), so it follows
        # membership_status's rule: any requester who can reach this endpoint
        # (IsAdminOrClubLead) may set it.
        self.client.force_authenticate(self.club_lead)
        resp = self.client.patch(self._url(self.member), {'roll_number': '21B91A0501'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.member.refresh_from_db()
        self.assertEqual(self.member.roll_number, '21B91A0501')

    def test_admin_roll_number_edit_still_rejects_malformed_values(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.member), {'roll_number': 'bad!'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('roll_number', resp.data)

    def test_admin_roll_number_edit_still_rejects_duplicates(self):
        self.club_lead.roll_number = '21B91A0501'
        self.club_lead.save(update_fields=['roll_number'])
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.member), {'roll_number': '21B91A0501'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('roll_number', resp.data)

    def test_admin_can_edit_user_profile_details(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.member), {
            'first_name': 'Updated',
            'last_name': 'Member',
            'branch': 'IT',
            'year': 3,
            'phone_number': '9876543210',
            'github_profile': 'https://github.com/updatedmember',
            'linkedin_profile': 'https://linkedin.com/in/updatedmember',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.member.refresh_from_db()
        self.assertEqual(self.member.first_name, 'Updated')
        self.assertEqual(self.member.last_name, 'Member')
        self.assertEqual(self.member.branch, 'IT')
        self.assertEqual(self.member.year, 3)
        self.assertEqual(self.member.phone_number, '9876543210')
        self.assertEqual(self.member.github_profile, 'https://github.com/updatedmember')
        self.assertEqual(self.member.linkedin_profile, 'https://linkedin.com/in/updatedmember')

    def test_user_list_filters_by_role_and_affiliates_only(self):
        self.member.role = 'AFFILIATE'
        self.member.club_id = '25SCC100'
        self.member.save(update_fields=['role', 'club_id'])
        self.client.force_authenticate(self.admin)

        # Role filter
        resp = self.client.get('/api/auth/users/?role=AFFILIATE')
        self.assertEqual(resp.status_code, 200)
        items = resp.data if isinstance(resp.data, list) else resp.data.get('results', [])
        user_ids = [u['id'] for u in items]
        self.assertIn(self.member.id, user_ids)
        self.assertNotIn(self.club_lead.id, user_ids)

        # Affiliates only filter
        resp_aff = self.client.get('/api/auth/users/?affiliates_only=true')
        self.assertEqual(resp_aff.status_code, 200)
        items_aff = resp_aff.data if isinstance(resp_aff.data, list) else resp_aff.data.get('results', [])
        aff_ids = [u['id'] for u in items_aff]
        self.assertIn(self.member.id, aff_ids)
        self.assertNotIn(self.club_lead.id, aff_ids)

    def test_admin_can_update_own_profile_details_without_changing_role(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.admin), {
            'first_name': 'Super',
            'phone_number': '9988776655',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.first_name, 'Super')
        self.assertEqual(self.admin.phone_number, '9988776655')
        self.assertEqual(self.admin.role, 'ADMIN')

    def test_admin_cannot_change_own_role(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.admin), {'role': 'VOLUNTEER'}, format='json')
        self.assertEqual(resp.status_code, 403)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, 'ADMIN')

    def test_admin_social_links_without_scheme_auto_prefixed(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.member), {
            'github_profile': 'github.com/mygit',
            'linkedin_profile': 'linkedin.com/in/mylinkedin',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.member.refresh_from_db()
        self.assertEqual(self.member.github_profile, 'https://github.com/mygit')
        self.assertEqual(self.member.linkedin_profile, 'https://linkedin.com/in/mylinkedin')

    def test_club_lead_cannot_demote_admin_account(self):
        self.client.force_authenticate(self.club_lead)
        resp = self.client.patch(self._url(self.admin), {
            'role': 'VOLUNTEER',
        }, format='json')
        self.assertEqual(resp.status_code, 403)

