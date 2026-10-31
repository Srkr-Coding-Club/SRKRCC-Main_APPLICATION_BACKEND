"""The admin user directory: filters, paging, the verification state of each person, the numbers and the admin actions."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import MembershipStatus
from apps.audit.models import AuditLog

User = get_user_model()
BASE = '/api/auth/users/directory/'
EMAIL_MODE = override_settings(EMAIL_VERIFICATION_MODE='banner', EMAIL_VERIFY_GRACE_DAYS=7)


def person(email, *, verified=False, role='NON_AFFILIATE', days_old=0, **extra):
    user = User.objects.create_user(username=email.split('@')[0] + str(User.objects.count()), email=email, password='Str0ng!Passw0rd', role=role, **extra)
    fields = {'created_at': timezone.now() - timedelta(days=days_old), 'email_verify_deadline': None}   # no stored deadline: signup date plus the grace period
    if verified:
        fields['email_verified_at'] = timezone.now() - timedelta(days=1)
    User.objects.filter(pk=user.pk).update(**fields)
    user.refresh_from_db()
    return user


def api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


@EMAIL_MODE
class DirectoryCase(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = person('boss@example.org', role='ADMIN', verified=True)
        self.lead = person('lead@example.org', role='CLUB_LEAD', verified=True)
        self.sita = person('sita@example.org', first_name='Sita', last_name='Rao', verified=True, role='AFFILIATE', club_id='25SCC001', branch='CSE', year=3, roll_number='R1', phone_number='9000000001')
        self.meena = person('meena@example.org', first_name='Meena', last_name='K', days_old=2, branch='IT', year=2)
        self.omar = person('omar@example.org', first_name='Omar', last_name='Z', days_old=30, branch='CSE', year=4)
        self.ravi = person('ravi@example.org', first_name='Ravi', days_old=3, role='AFFILIATE', club_id='25SCC002', membership_status=MembershipStatus.ALUMNI)

    def ask(self, user=None, **params):
        return api(user or self.admin).get(BASE, params)

    def emails(self, **params):
        return sorted(row['email'] for row in self.ask(**params).data['results'])


class Listing(DirectoryCase):
    def test_returns_a_page_with_the_envelope_and_the_fields_the_page_needs(self):
        reply = self.ask(q='sita')
        self.assertEqual((reply.status_code, reply.data['count']), (200, 1))
        row = reply.data['results'][0]
        self.assertEqual((row['full_name'], row['club_id'], row['email_state'], row['role']), ('Sita Rao', '25SCC001', 'VERIFIED', 'AFFILIATE'))
        self.assertIn('email_verified_at', row)
        self.assertNotIn('password', row)

    def test_pages_of_25_by_default_and_never_more_than_100(self):
        for number in range(30):
            person(f'bulk{number}@example.org', verified=True)
        first = self.ask()
        self.assertEqual((len(first.data['results']), first.data['count']), (25, 36))
        self.assertIsNotNone(first.data['next'])
        self.assertEqual(len(self.ask(page=2).data['results']), 11)
        self.assertEqual(len(self.ask(page_size=500).data['results']), 36)
        self.assertLessEqual(len(self.ask(page_size=500).data['results']), 100)

    def test_only_admins_and_club_leads_may_look(self):
        self.assertEqual(self.ask(user=self.lead).status_code, 200)
        self.assertEqual(self.ask(user=self.sita).status_code, 403)
        self.assertEqual(api().get(BASE).status_code, 401)


class Searching(DirectoryCase):
    def test_a_full_name_finds_the_person_not_just_one_word(self):
        self.assertEqual(self.emails(q='sita rao'), ['sita@example.org'])
        self.assertEqual(self.emails(q='rao sita'), ['sita@example.org'])

    def test_finds_by_email_club_id_roll_number_and_phone(self):
        self.assertEqual(self.emails(q='25scc001'), ['sita@example.org'])
        self.assertEqual(self.emails(q='r1'), ['sita@example.org'])
        self.assertEqual(self.emails(q='9000000001'), ['sita@example.org'])
        self.assertEqual(self.emails(q='omar@'), ['omar@example.org'])

    def test_nothing_matches_gives_an_empty_page_not_an_error(self):
        reply = self.ask(q='nobody-like-this')
        self.assertEqual((reply.status_code, reply.data['count'], reply.data['results']), (200, 0, []))

    def test_more_words_than_allowed_and_odd_characters_are_safe(self):
        self.assertEqual(self.ask(q='a b c d e f g h ' + 'x' * 500).status_code, 200)
        self.assertEqual(self.ask(q="'; DROP TABLE users; --").data['count'], 0)
        self.assertEqual(User.objects.count(), 6)


class Filtering(DirectoryCase):
    def test_by_role_and_by_several_roles(self):
        self.assertEqual(self.emails(role='AFFILIATE'), ['ravi@example.org', 'sita@example.org'])
        self.assertEqual(self.emails(role='ADMIN,CLUB_LEAD'), ['boss@example.org', 'lead@example.org'])

    def test_by_membership_status(self):
        self.assertEqual(self.emails(membership_status='ALUMNI'), ['ravi@example.org'])

    def test_by_club_membership(self):
        self.assertEqual(self.emails(member='yes'), ['ravi@example.org', 'sita@example.org'])
        self.assertEqual(len(self.emails(member='no')), 4)

    def test_by_email_state(self):
        self.assertEqual(self.emails(email='verified'), ['boss@example.org', 'lead@example.org', 'sita@example.org'])
        self.assertEqual(self.emails(email='unverified'), ['meena@example.org', 'omar@example.org', 'ravi@example.org'])
        self.assertEqual(self.emails(email='overdue'), ['omar@example.org'])

    def test_overdue_uses_the_stored_deadline_when_there_is_one(self):
        User.objects.filter(pk=self.meena.pk).update(email_verify_deadline=timezone.now() - timedelta(hours=1))
        User.objects.filter(pk=self.omar.pk).update(email_verify_deadline=timezone.now() + timedelta(days=5))
        self.assertEqual(self.emails(email='overdue'), ['meena@example.org'])

    def test_disputed(self):
        User.objects.filter(pk=self.meena.pk).update(email_disputed_at=timezone.now())
        self.assertEqual(self.emails(email='disputed'), ['meena@example.org'])
        self.assertEqual(self.ask(q='meena').data['results'][0]['email_state'], 'DISPUTED')

    def test_by_branch_year_and_joined_dates(self):
        self.assertEqual(self.emails(branch='cse'), ['omar@example.org', 'sita@example.org'])
        self.assertEqual(self.emails(year='4'), ['omar@example.org'])
        since = (timezone.now() - timedelta(days=10)).date().isoformat()
        self.assertNotIn('omar@example.org', self.emails(joined_from=since))
        self.assertEqual(self.emails(joined_to=since), ['omar@example.org'])

    def test_filters_combine_and_a_bad_value_matches_nothing(self):
        self.assertEqual(self.emails(role='AFFILIATE', email='verified'), ['sita@example.org'])
        self.assertEqual(self.emails(role='WIZARD'), [])
        self.assertEqual(self.ask(year='abc', joined_from='not-a-date').status_code, 200)

    def test_ordering(self):
        newest = [row['email'] for row in self.ask(ordering='newest').data['results']]
        oldest = [row['email'] for row in self.ask(ordering='oldest').data['results']]
        self.assertEqual(newest, oldest[::-1])
        self.assertEqual(self.ask(ordering='email').data['results'][0]['email'], 'boss@example.org')
        self.assertEqual(self.ask(ordering='nonsense').status_code, 200)


class MembersScope(DirectoryCase):
    def test_the_members_scope_lists_only_club_id_holders_and_ignores_blank_ids(self):
        User.objects.filter(pk=self.meena.pk).update(club_id='')
        self.assertEqual(self.emails(scope='members'), ['ravi@example.org', 'sita@example.org'])

    def test_it_combines_with_status_and_search(self):
        self.assertEqual(self.emails(scope='members', membership_status='ALUMNI'), ['ravi@example.org'])
        self.assertEqual(self.emails(scope='members', q='omar'), [])


class Summary(DirectoryCase):
    def numbers(self, **params):
        return api(self.admin).get(BASE + 'summary/', params).data

    def test_counts_the_whole_set(self):
        data = self.numbers()
        self.assertEqual((data['total'], data['verified'], data['unverified'], data['overdue'], data['members']), (6, 3, 3, 1, 2))
        self.assertEqual(data['by_role']['AFFILIATE'], 2)
        self.assertEqual(data['by_membership_status']['ALUMNI'], 1)

    def test_members_scope_counts_only_members(self):
        data = self.numbers(scope='members')
        self.assertEqual((data['total'], data['verified'], data['unverified']), (2, 1, 1))

    def test_club_leads_may_see_it_and_others_may_not(self):
        self.assertEqual(api(self.lead).get(BASE + 'summary/').status_code, 200)
        self.assertEqual(api(self.sita).get(BASE + 'summary/').status_code, 403)


class Recipients(DirectoryCase):
    def test_lists_id_and_email_of_everyone_the_filters_match(self):
        reply = api(self.admin).get(BASE + 'recipients/', {'scope': 'members', 'membership_status': 'ALUMNI'})
        self.assertEqual(reply.status_code, 200)
        self.assertEqual([row['email'] for row in reply.data['recipients']], ['ravi@example.org'])
        self.assertFalse(reply.data['truncated'])

    def test_is_capped_and_says_so(self):
        with mock.patch('apps.accounts.views_directory.MAX_RECIPIENTS', 2):
            reply = api(self.admin).get(BASE + 'recipients/')
        self.assertEqual((len(reply.data['recipients']), reply.data['truncated']), (2, True))

    def test_participants_cannot_use_it(self):
        self.assertEqual(api(self.sita).get(BASE + 'recipients/').status_code, 403)


class Actions(DirectoryCase):
    def post(self, path, user=None, **body):
        return api(user or self.admin).post(f'/api/auth/users/{self.meena.pk}/verification/{path}/', body, format='json')

    def test_an_admin_can_extend_the_deadline_from_the_later_of_now_and_the_current_one(self):
        before = timezone.now()
        reply = self.post('extend', days=10)
        self.assertEqual(reply.status_code, 200, reply.data)
        self.meena.refresh_from_db()
        self.assertGreaterEqual(self.meena.email_verify_deadline, before + timedelta(days=10))
        self.assertTrue(AuditLog.objects.filter(action='Email Verification Deadline Extended').exists())

    def test_extending_never_shortens_a_longer_deadline(self):
        far = timezone.now() + timedelta(days=40)
        User.objects.filter(pk=self.meena.pk).update(email_verify_deadline=far)
        self.post('extend', days=5)
        self.meena.refresh_from_db()
        self.assertEqual(self.meena.email_verify_deadline, far + timedelta(days=5))

    def test_days_must_be_between_one_and_sixty_and_a_verified_person_needs_no_extension(self):
        for days in (0, 61, -3, 'x', None):
            self.assertEqual(self.post('extend', days=days).data['code'], 'INVALID', days)
        verified = api(self.admin).post(f'/api/auth/users/{self.sita.pk}/verification/extend/', {'days': 5}, format='json')
        self.assertEqual((verified.status_code, verified.data['code']), (409, 'ALREADY_VERIFIED'))

    def test_resending_sends_one_email_and_is_audited(self):
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.post('resend')
        self.assertEqual(reply.status_code, 200, reply.data)
        self.assertTrue(reply.data['sent'])
        self.assertTrue(any('meena@example.org' in message.to for message in mail.outbox))
        self.assertTrue(AuditLog.objects.filter(action='Verification Email Resent By Admin').exists())

    def test_resending_to_a_verified_person_is_refused(self):
        reply = api(self.admin).post(f'/api/auth/users/{self.sita.pk}/verification/resend/')
        self.assertEqual((reply.status_code, reply.data['code']), (409, 'ALREADY_VERIFIED'))

    def test_only_admins_may_act_and_unknown_people_are_not_found(self):
        self.assertEqual(self.post('extend', user=self.lead, days=3).status_code, 403)
        self.assertEqual(self.post('resend', user=self.lead).status_code, 403)
        self.assertEqual(api(self.admin).post('/api/auth/users/999999/verification/extend/', {'days': 3}, format='json').status_code, 404)
        self.assertEqual(api(self.admin).post('/api/auth/users/999999/verification/resend/').status_code, 404)

    def test_the_admin_who_marks_verified_changes_the_state_shown(self):
        api(self.admin).post(f'/api/auth/users/{self.meena.pk}/verify-email/')
        self.assertEqual(self.ask(q='meena').data['results'][0]['email_state'], 'VERIFIED')
