"""
Verifies the mapping rule the 0006_affiliate_non_affiliate_roles data
migration applies to historical MEMBER rows: a MEMBER with a club_id becomes
AFFILIATE, a MEMBER without one becomes NON_AFFILIATE. This tests the rule
directly against the current model (not the migration file itself — Django
migrations aren't easily unit-testable without extra tooling this repo
doesn't have) so it stays meaningful as living documentation even after the
one-time migration has run in every environment.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

User = get_user_model()


class MemberRoleSplitMappingTests(TestCase):
    def test_member_with_club_id_maps_to_affiliate(self):
        user = User.objects.create_user(
            username='hasid', email='hasid@srkr.ac.in', password='pw12345!',
            role='MEMBER', club_id='25SCC901',
        )
        User.objects.filter(role='MEMBER', club_id__isnull=False).update(role='AFFILIATE')
        User.objects.filter(role='MEMBER', club_id__isnull=True).update(role='NON_AFFILIATE')
        user.refresh_from_db()
        self.assertEqual(user.role, 'AFFILIATE')

    def test_member_without_club_id_maps_to_non_affiliate(self):
        user = User.objects.create_user(
            username='noid', email='noid@srkr.ac.in', password='pw12345!',
            role='MEMBER',
        )
        User.objects.filter(role='MEMBER', club_id__isnull=False).update(role='AFFILIATE')
        User.objects.filter(role='MEMBER', club_id__isnull=True).update(role='NON_AFFILIATE')
        user.refresh_from_db()
        self.assertEqual(user.role, 'NON_AFFILIATE')

    def test_non_member_roles_are_unaffected(self):
        volunteer = User.objects.create_user(
            username='vol1', email='vol1@srkr.ac.in', password='pw12345!', role='VOLUNTEER',
        )
        User.objects.filter(role='MEMBER', club_id__isnull=False).update(role='AFFILIATE')
        User.objects.filter(role='MEMBER', club_id__isnull=True).update(role='NON_AFFILIATE')
        volunteer.refresh_from_db()
        self.assertEqual(volunteer.role, 'VOLUNTEER')
