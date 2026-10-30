"""Club membership requests: who may ask, the organizers' queue, approval (the next Club ID) and rejection (with a reason and a wait)."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import ClubIdEvent, ClubMembershipRequest
from apps.accounts.services import club_id_registry as registry
from apps.celebrations.models import Celebration
from apps.notifications.models import Notification

User = get_user_model()
BASE = '/api/auth/club-membership'


def person(email, *, verified=True, complete=True, **extra):
    fields = dict(first_name='Sita', last_name='Rao', roll_number=f'R{User.objects.count()}', branch='CSE', year=2, phone_number='9000000000', other_college_name='SRKR') if complete else {}
    fields.update(extra)
    user = User.objects.create_user(username=email.split('@')[0] + str(User.objects.count()), email=email, password='Str0ng!Passw0rd', **fields)
    if verified:
        User.objects.filter(pk=user.pk).update(email_verified_at=timezone.now())
        user.refresh_from_db()
    return user


def api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


@override_settings(EMAIL_VERIFICATION_MODE='banner')
class Case(TestCase):
    def setUp(self):
        cache.clear()
        patcher = mock.patch('apps.core.tasks.run_in_background', side_effect=lambda fn: fn())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.admin = person('boss@example.org', role='ADMIN')
        self.lead = person('lead@example.org', role='CLUB_LEAD')
        self.sita = person('sita@example.org')

    def ask(self, user=None, **body):
        return api(user or self.sita).post(f'{BASE}/request/', body, format='json')

    def mine(self, user=None):
        return api(user or self.sita).get(f'{BASE}/me/').data

    def act(self, entry_id, action, user=None, **body):
        return api(user or self.admin).post(f'{BASE}/requests/{entry_id}/{action}/', body, format='json')

    def pending(self, user=None):
        reply = self.ask(user)
        self.assertEqual(reply.status_code, 201, reply.data)
        return reply.data['request']['id']


class Asking(Case):
    def test_a_verified_person_with_a_complete_profile_can_ask_and_the_organizers_are_told(self):
        self.assertTrue(self.mine()['can_request'])
        reply = self.ask(message='I joined the workshop')
        self.assertEqual((reply.status_code, reply.data['request']['status'], reply.data['auto_approved']), (201, 'PENDING', False))
        self.assertEqual(sorted(Notification.objects.filter(title__contains='wants to join').values_list('recipient__email', flat=True)), ['boss@example.org', 'lead@example.org'])
        state = self.mine()
        self.assertEqual((state['can_request'], [b['code'] for b in state['blockers']], state['request']['message']), (False, ['PENDING'], 'I joined the workshop'))

    def test_a_second_request_while_one_waits_is_refused(self):
        self.ask()
        again = self.ask()
        self.assertEqual((again.status_code, again.data['code']), (409, 'PENDING'))
        self.assertEqual(ClubMembershipRequest.objects.count(), 1)

    def test_an_unverified_email_blocks_unless_verification_is_off(self):
        unverified = person('new@example.org', verified=False)
        self.assertEqual(self.ask(unverified).data['code'], 'EMAIL_NOT_VERIFIED')
        self.assertEqual(self.mine(unverified)['blockers'][0]['fix'], '/profile')
        with override_settings(EMAIL_VERIFICATION_MODE='off'):
            self.assertEqual(self.ask(unverified).status_code, 201)

    def test_an_incomplete_profile_says_exactly_what_is_missing(self):
        bare = person('bare@example.org', complete=False, first_name='Bare')
        blocker = next(b for b in self.mine(bare)['blockers'] if b['code'] == 'PROFILE_INCOMPLETE')
        self.assertEqual(blocker['missing'], ['Last name', 'Roll number', 'Branch', 'Year', 'Phone number', 'College'])
        self.assertEqual(self.ask(bare).data['code'], 'PROFILE_INCOMPLETE')

    def test_members_and_suspended_people_cannot_ask(self):
        member = person('member@example.org', club_id='25SCC001', role='AFFILIATE')
        self.assertEqual(self.ask(member).data['code'], 'ALREADY_MEMBER')
        self.assertIn('25SCC001', self.mine(member)['blockers'][0]['message'])
        suspended = person('susp@example.org', membership_status='SUSPENDED')
        self.assertEqual(self.ask(suspended).data['code'], 'SUSPENDED')

    def test_visitors_cannot_ask_and_a_message_is_trimmed(self):
        self.assertEqual(api().post(f'{BASE}/request/', {}, format='json').status_code, 401)
        self.assertEqual(api().get(f'{BASE}/me/').status_code, 401)
        reply = self.ask(message='  ' + 'long ' * 100)
        self.assertLessEqual(len(reply.data['request']['message']), 300)

    def test_withdrawing_is_allowed_and_asking_again_has_no_wait(self):
        self.ask()
        self.assertEqual(api(self.sita).post(f'{BASE}/cancel/').data['request']['status'], 'CANCELLED')
        self.assertEqual(self.ask().status_code, 201)
        api(self.sita).post(f'{BASE}/cancel/')
        self.assertEqual(api(self.sita).post(f'{BASE}/cancel/').data['code'], 'NOT_PENDING')

    def test_an_id_reserved_for_the_email_is_attached_at_once_with_no_queue(self):
        registry.reserve(self.admin, email='sita@example.org', club_id='24SCC111', reason='Past member')
        reply = self.ask()
        self.assertEqual((reply.status_code, reply.data['auto_approved'], reply.data['request']['status']), (201, True, 'APPROVED'))
        self.sita.refresh_from_db()
        self.assertEqual((self.sita.club_id, self.sita.role), ('24SCC111', 'AFFILIATE'))
        self.assertFalse(Notification.objects.filter(title__contains='wants to join').exists())


class Approving(Case):
    def test_approval_gives_the_next_club_id_the_affiliate_role_and_tells_the_person(self):
        entry = self.pending()
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.act(entry, 'approve')
        self.assertEqual(reply.status_code, 200, reply.data)
        self.sita.refresh_from_db()
        year = f'{timezone.now().year % 100:02d}'
        self.assertEqual((self.sita.club_id, self.sita.role, self.sita.membership_status), (f'{year}SCC001', 'AFFILIATE', 'ACTIVE'))
        self.assertIsNotNone(self.sita.registered_at)
        self.assertEqual((reply.data['status'], reply.data['club_id'], reply.data['decided_by']), ('APPROVED', f'{year}SCC001', 'boss@example.org'))
        event = ClubIdEvent.objects.get(club_id=f'{year}SCC001')
        self.assertEqual((event.kind, event.actor_email, event.reason), ('ISSUED', 'boss@example.org', 'Club membership approved'))
        self.assertTrue(Notification.objects.filter(recipient=self.sita, title='You are a club member').exists())
        self.assertTrue(any('sita@example.org' in m.to and f'{year}SCC001' in m.body for m in mail.outbox))
        self.assertTrue(Celebration.objects.filter(user=self.sita, kind='account.club_member').exists())

    def test_numbers_are_given_in_order_and_skip_reserved_ones(self):
        year = timezone.now().year
        registry.reserve(self.admin, email='x@example.org', club_id=f'{year % 100:02d}SCC002', reason='held')
        first, second = self.pending(), self.pending(person('omar@example.org'))
        self.act(first, 'approve')
        self.act(second, 'approve')
        self.assertEqual(sorted(User.objects.exclude(club_id=None).values_list('club_id', flat=True)), [f'{year % 100:02d}SCC001', f'{year % 100:02d}SCC003'])

    def test_a_volunteer_keeps_their_role(self):
        volunteer = person('vol@example.org', role='VOLUNTEER')
        self.act(self.pending(volunteer), 'approve')
        volunteer.refresh_from_db()
        self.assertEqual((volunteer.role, bool(volunteer.club_id)), ('VOLUNTEER', True))

    def test_a_request_is_decided_once(self):
        entry = self.pending()
        self.act(entry, 'approve')
        again = self.act(entry, 'approve')
        self.assertEqual((again.status_code, again.data['code'], again.data['status_now']), (409, 'NOT_PENDING', 'APPROVED'))
        self.assertEqual(self.act(entry, 'reject', reason='x').data['code'], 'NOT_PENDING')

    def test_someone_who_got_an_id_in_the_meantime_is_not_given_a_second(self):
        entry = self.pending()
        registry.assign(self.admin, self.sita.pk, '25SCC050', reason='by hand')
        reply = self.act(entry, 'approve')
        self.assertEqual((reply.status_code, reply.data['code']), (409, 'ALREADY_MEMBER'))
        self.assertEqual(ClubMembershipRequest.objects.get(pk=entry).status, 'PENDING')

    def test_a_missing_request_and_a_club_lead_who_may_only_look(self):
        self.assertEqual(self.act(99999, 'approve').status_code, 404)
        entry = self.pending()
        self.assertEqual(self.act(entry, 'approve', user=self.lead).status_code, 403)
        self.assertEqual(self.act(entry, 'reject', user=self.lead, reason='x').status_code, 403)
        self.assertEqual(self.act(entry, 'approve', user=self.sita).status_code, 403)


class Rejecting(Case):
    def test_rejecting_needs_a_reason_the_person_is_shown_and_starts_a_wait(self):
        entry = self.pending()
        self.assertEqual(self.act(entry, 'reject', reason='  ').data['code'], 'REASON_REQUIRED')
        reply = self.act(entry, 'reject', reason='Please attend one workshop first')
        self.assertEqual((reply.status_code, reply.data['status'], reply.data['note']), (200, 'REJECTED', 'Please attend one workshop first'))
        note = Notification.objects.get(recipient=self.sita, title__contains='not approved')
        self.assertIn('Please attend one workshop first', note.message)
        state = self.mine()
        self.assertEqual((state['can_request'], state['blockers'][0]['code'], state['request']['note']), (False, 'COOLDOWN', 'Please attend one workshop first'))
        self.assertEqual(self.ask().data['code'], 'COOLDOWN')

    def test_after_the_wait_the_person_may_ask_again(self):
        entry = self.pending()
        self.act(entry, 'reject', reason='Not yet')
        ClubMembershipRequest.objects.filter(pk=entry).update(decided_at=timezone.now() - timedelta(days=8))
        self.assertTrue(self.mine()['can_request'])
        self.assertEqual(self.ask().status_code, 201)


class Queue(Case):
    def setUp(self):
        super().setUp()
        self.omar, self.meena = person('omar@example.org', first_name='Omar', roll_number='ROLL-OMAR'), person('meena@example.org', first_name='Meena')
        self.first, self.second, self.third = self.pending(self.sita), self.pending(self.omar), self.pending(self.meena)

    def rows(self, user=None, **params):
        return api(user or self.lead).get(f'{BASE}/requests/', params).data

    def test_lists_the_queue_with_the_person_and_counts_by_status(self):
        self.act(self.third, 'reject', reason='no')
        data = self.rows()
        self.assertEqual(data['counts'], {'PENDING': 2, 'APPROVED': 0, 'REJECTED': 1, 'CANCELLED': 0})
        row = next(item for item in data['results'] if item['id'] == self.second)
        self.assertEqual((row['person']['email'], row['person']['email_verified'], row['person']['branch'], row['person']['college']), ('omar@example.org', True, 'CSE', 'SRKR'))

    def test_filters_by_status_and_search_and_shows_the_longest_waiting_first(self):
        self.act(self.third, 'reject', reason='no')
        waiting = self.rows(status='PENDING')['results']
        self.assertEqual([item['id'] for item in waiting], [self.first, self.second])
        self.assertEqual([item['id'] for item in self.rows(q='roll-omar')['results']], [self.second])
        self.assertEqual([item['id'] for item in self.rows(q='meena', status='REJECTED')['results']], [self.third])
        self.assertEqual(self.rows(q='nobody')['count'], 0)

    def test_a_club_lead_may_read_and_a_participant_may_not(self):
        self.assertEqual(api(self.lead).get(f'{BASE}/requests/').status_code, 200)
        self.assertEqual(api(self.sita).get(f'{BASE}/requests/').status_code, 403)
        self.assertEqual(api().get(f'{BASE}/requests/').status_code, 401)

    def test_bulk_approval_reports_what_could_not_be_approved_and_does_the_rest(self):
        self.act(self.second, 'reject', reason='no')
        reply = api(self.admin).post(f'{BASE}/requests/bulk-approve/', {'ids': [self.first, self.second, self.third]}, format='json')
        self.assertEqual((reply.status_code, reply.data['done']), (200, 2))
        self.assertEqual([(item['id'], item['code']) for item in reply.data['failed']], [(self.second, 'NOT_PENDING')])
        self.assertEqual(User.objects.exclude(club_id=None).filter(email__in=['sita@example.org', 'meena@example.org']).count(), 2)

    def test_bulk_approval_validates_its_input_and_is_admin_only(self):
        post = lambda user, body: api(user).post(f'{BASE}/requests/bulk-approve/', body, format='json')  # noqa: E731
        self.assertEqual(post(self.admin, {'ids': []}).data['code'], 'INVALID')
        self.assertEqual(post(self.admin, {'ids': 'all'}).data['code'], 'INVALID')
        self.assertEqual(post(self.admin, {'ids': ['x']}).data['code'], 'INVALID')
        self.assertEqual(post(self.admin, {'ids': list(range(1, 102))}).data['code'], 'INVALID')
        self.assertEqual(post(self.lead, {'ids': [self.first]}).status_code, 403)

    def test_every_decision_is_audited(self):
        from apps.audit.models import AuditLog
        self.act(self.first, 'approve')
        self.act(self.second, 'reject', reason='no')
        for action in ('Club Membership Requested', 'Club Membership Approved', 'Club Membership Rejected'):
            self.assertTrue(AuditLog.objects.filter(action=action).exists(), action)
