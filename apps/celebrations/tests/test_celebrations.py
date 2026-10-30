"""Celebrations: awarding once per moment, what is pending and in what order, seen, expiry, preference and who may see what."""
from datetime import timedelta
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.accounts.models import User
from apps.celebrations import kinds, services
from apps.celebrations.models import Celebration, CelebrationSetting, CelebrationSize

_n = {'i': 0}


def make_user(**kw):
    _n['i'] += 1
    defaults = dict(username=f'cel{_n["i"]}', email=f'cel{_n["i"]}@srkr.ac.in', password='pw12345!', role='NON_AFFILIATE')
    defaults.update(kw)
    return User.objects.create_user(**defaults)


def api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


class KindsTests(TestCase):
    def test_every_kind_has_a_title_a_valid_size_and_renders_with_no_context(self):
        for name, kind in kinds.KINDS.items():
            self.assertTrue(kind.title, name)
            self.assertIn(kind.size, CelebrationSize.values, name)
            self.assertNotIn('{', kinds.render(kind.title, {}), name)
            self.assertNotIn('{', kinds.render(kind.message, {}), name)

    def test_placeholders_are_filled_and_a_missing_one_is_empty_not_an_error(self):
        self.assertEqual(kinds.render('{team} is in {hackathon}', {'team': 'Alpha', 'hackathon': 'HackOverflow'}), 'Alpha is in HackOverflow')
        self.assertEqual(kinds.render('Hi {name}!', {}), 'Hi !')
        self.assertEqual(kinds.render('Day {days}', {'days': 7}), 'Day 7')
        self.assertEqual(kinds.render('{x}', {'x': None}), '')


class AwardTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_awarding_creates_a_celebration_from_the_kind(self):
        celebration = services.award(self.user, 'hackathon.team_registered', 'team-registered:1', link_url='/hackathons/demo/dashboard', team='Alpha', hackathon='HackOverflow')
        self.assertEqual((celebration.title, celebration.size, celebration.kind), ("You're in!", 'BIG', 'hackathon.team_registered'))
        self.assertIn('Alpha is registered for HackOverflow', celebration.message)
        self.assertEqual((celebration.link_url, celebration.payload), ('/hackathons/demo/dashboard', {'team': 'Alpha', 'hackathon': 'HackOverflow'}))
        self.assertIsNone(celebration.seen_at)
        self.assertAlmostEqual((celebration.expires_at - timezone.now()).total_seconds(), services.LIFETIME.total_seconds(), delta=60)

    def test_the_same_moment_is_celebrated_once_per_person(self):
        self.assertIsNotNone(services.award(self.user, 'account.welcome', 'welcome'))
        self.assertIsNone(services.award(self.user, 'account.welcome', 'welcome'))
        self.assertEqual(Celebration.objects.count(), 1)
        other = make_user()
        self.assertIsNotNone(services.award(other, 'account.welcome', 'welcome'))     # another person has their own

    def test_the_database_itself_refuses_a_duplicate(self):
        Celebration.objects.create(user=self.user, kind='account.welcome', key='k', title='t', expires_at=timezone.now())
        with self.assertRaises(IntegrityError), transaction.atomic():
            Celebration.objects.create(user=self.user, kind='account.welcome', key='k', title='t', expires_at=timezone.now())

    def test_an_unknown_kind_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            services.award(self.user, 'nonsense', 'k')

    def test_nothing_is_awarded_to_someone_with_celebrations_off_or_who_cannot_sign_in(self):
        services.set_enabled(self.user, False)
        self.assertIsNone(services.award(self.user, 'account.welcome', 'welcome'))
        gone = make_user(is_active=False)
        self.assertIsNone(services.award(gone, 'account.welcome', 'welcome'))
        self.assertIsNone(services.award(None, 'account.welcome', 'welcome'))
        self.assertEqual(Celebration.objects.count(), 0)

    def test_text_and_payload_are_bounded_and_only_plain_values_are_kept(self):
        celebration = services.award(self.user, 'custom', 'c1', title='T' * 500, message='M' * 900, link_url='/x' * 400, secret=object(), count=3)
        self.assertEqual((len(celebration.title), len(celebration.message), len(celebration.link_url)), (120, 300, 300))
        self.assertEqual(celebration.payload.get('count'), 3)
        self.assertNotIn('secret', celebration.payload)

    def test_a_long_key_is_cut_to_fit(self):
        celebration = services.award(self.user, 'account.welcome', 'k' * 400)
        self.assertEqual(len(celebration.key), 120)

    def test_the_size_can_be_overridden(self):
        self.assertEqual(services.award(self.user, 'account.welcome', 'big', size='BIG').size, 'BIG')


class AwardAfterCommitTests(TestCase):
    def test_it_waits_for_the_commit_and_celebrates_everyone(self):
        people = [make_user(), make_user()]
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            services.award_after_commit(people, 'hackathon.team_registered', 'team-registered:9', team='Alpha', hackathon='H')
        self.assertEqual(Celebration.objects.count(), 0)                         # not before the commit
        for callback in callbacks:
            callback()
        self.assertEqual(Celebration.objects.filter(key='team-registered:9').count(), 2)

    def test_a_rolled_back_action_celebrates_nothing(self):
        person = make_user()
        try:
            with transaction.atomic():
                services.award_after_commit([person], 'account.welcome', 'welcome')
                raise RuntimeError('the action failed')
        except RuntimeError:
            pass
        self.assertEqual(Celebration.objects.count(), 0)

    def test_a_failure_for_one_person_is_logged_and_does_not_stop_the_others(self):
        first, second = make_user(), make_user()
        real = services.award
        calls = []

        def flaky(user, *args, **kwargs):
            calls.append(user.pk)
            if user.pk == first.pk:
                raise RuntimeError('boom')
            return real(user, *args, **kwargs)

        with mock.patch.object(services, 'award', flaky), self.assertLogs('apps.celebrations.services', level='WARNING'):
            with self.captureOnCommitCallbacks(execute=True):
                services.award_after_commit([first, second], 'account.welcome', 'welcome')
        self.assertEqual(calls, [first.pk, second.pk])
        self.assertEqual(list(Celebration.objects.values_list('user_id', flat=True)), [second.pk])

    def test_nobody_to_celebrate_is_fine(self):
        services.award_after_commit([], 'account.welcome', 'welcome')
        services.award_after_commit([None], 'account.welcome', 'welcome')


class PendingTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def award(self, kind, key, **kw):
        return services.award(self.user, kind, key, **kw)

    def test_the_biggest_come_first_then_the_oldest(self):
        small = self.award('account.email_verified', 's')
        big_late = self.award('hackathon.team_registered', 'b2', team='B', hackathon='H')
        medium = self.award('account.welcome', 'm')
        big_early = self.award('hackathon.winner', 'b1', team='A', award='1st', hackathon='H')
        Celebration.objects.filter(pk=big_early.pk).update(created_at=timezone.now() - timedelta(hours=2))
        order = [c.pk for c in services.pending(self.user)]
        self.assertEqual(order, [big_early.pk, big_late.pk, medium.pk, small.pk])

    def test_seen_and_expired_ones_are_not_pending(self):
        seen = self.award('account.welcome', 'seen')
        expired = self.award('account.email_verified', 'old')
        alive = self.award('account.profile_completed', 'alive')
        services.mark_seen(self.user, [seen.pk])
        Celebration.objects.filter(pk=expired.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual([c.pk for c in services.pending(self.user)], [alive.pk])

    def test_only_the_persons_own_and_nothing_when_they_are_off(self):
        self.award('account.welcome', 'mine')
        services.award(make_user(), 'account.welcome', 'theirs')
        self.assertEqual(len(services.pending(self.user)), 1)
        services.set_enabled(self.user, False)
        self.assertEqual(services.pending(self.user), [])

    def test_the_list_is_capped(self):
        for number in range(services.PENDING_LIMIT + 4):
            self.award('account.email_verified', f'k{number}')
        self.assertEqual(len(services.pending(self.user)), services.PENDING_LIMIT)


class SeenRetractSweepTests(TestCase):
    def setUp(self):
        self.user, self.other = make_user(), make_user()

    def test_marking_seen_touches_only_the_callers_own_unseen_ones(self):
        mine = services.award(self.user, 'account.welcome', 'w')
        theirs = services.award(self.other, 'account.welcome', 'w')
        self.assertEqual(services.mark_seen(self.user, [mine.pk, theirs.pk, 'x', 999999]), 1)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.seen_at)
        self.assertEqual(services.mark_seen(self.user, [mine.pk]), 0)           # already seen: not counted again

    def test_a_seen_celebration_is_never_awarded_again(self):
        first = services.award(self.user, 'account.welcome', 'w')
        services.mark_seen(self.user, [first.pk])
        self.assertIsNone(services.award(self.user, 'account.welcome', 'w'))
        self.assertEqual(services.pending(self.user), [])

    def test_retracting_removes_only_unseen_ones_for_that_moment(self):
        for person in (self.user, self.other):
            services.award(person, 'hackathon.team_registered', 'team-registered:5', team='A', hackathon='H')
        services.award(self.user, 'hackathon.team_registered', 'team-registered:6', team='B', hackathon='H')
        seen = Celebration.objects.get(user=self.other, key='team-registered:5')
        services.mark_seen(self.other, [seen.pk])
        self.assertEqual(services.retract('team-registered:5'), 1)
        self.assertEqual(Celebration.objects.filter(key='team-registered:5').count(), 1)    # the seen one stays
        self.assertEqual(Celebration.objects.filter(key='team-registered:6').count(), 1)
        services.award(self.other, 'hackathon.member_joined', 'joined:1', member='M', team='T')
        self.assertEqual(services.retract('joined:1', users=[self.user]), 0)                # a different person: untouched

    def test_the_sweep_drops_expired_unseen_and_old_seen_ones_only(self):
        expired = services.award(self.user, 'account.welcome', 'expired')
        old = services.award(self.user, 'account.email_verified', 'old')
        recent = services.award(self.user, 'account.profile_completed', 'recent')
        live = services.award(self.user, 'account.club_member', 'live', club_id='C1')
        Celebration.objects.filter(pk=expired.pk).update(expires_at=timezone.now() - timedelta(days=1))
        services.mark_seen(self.user, [old.pk, recent.pk])
        Celebration.objects.filter(pk=old.pk).update(seen_at=timezone.now() - services.KEEP_SEEN - timedelta(days=1))
        self.assertEqual(services.sweep(), {'expired': 1, 'old_seen': 1})
        self.assertEqual(sorted(Celebration.objects.values_list('pk', flat=True)), sorted([recent.pk, live.pk]))

    def test_the_sweep_command_reports(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command('sweep_celebrations', stdout=out)
        self.assertIn('Removed 0 expired and 0 old seen', out.getvalue())

    def test_deleting_the_user_deletes_their_celebrations_and_setting(self):
        services.award(self.user, 'account.welcome', 'w')
        services.set_enabled(self.user, False)
        self.user.delete()
        self.assertEqual((Celebration.objects.count(), CelebrationSetting.objects.count()), (0, 0))


class ApiTests(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.other = make_user()

    def test_the_endpoints_need_a_signed_in_person(self):
        for method, path in (('get', 'pending'), ('post', 'seen'), ('get', 'settings'), ('patch', 'settings')):
            self.assertEqual(getattr(api(), method)(f'/api/celebrations/{path}/').status_code, 401, (method, path))

    def test_pending_lists_the_callers_celebrations_with_what_the_card_needs(self):
        services.award(self.user, 'hackathon.team_registered', 'team-registered:1', link_url='/hackathons/demo/dashboard', team='Alpha', hackathon='H')
        services.award(self.other, 'account.welcome', 'w')
        reply = api(self.user).get('/api/celebrations/pending/')
        self.assertEqual(reply.status_code, 200)
        self.assertTrue(reply.data['enabled'])
        self.assertEqual(len(reply.data['results']), 1)
        card = reply.data['results'][0]
        self.assertEqual({'id', 'kind', 'size', 'title', 'message', 'link_url', 'payload', 'created_at'}, set(card))
        self.assertEqual((card['kind'], card['size'], card['link_url']), ('hackathon.team_registered', 'BIG', '/hackathons/demo/dashboard'))
        self.assertNotIn('user', card)
        self.assertNotIn('key', card)

    def test_seen_marks_them_and_only_the_callers_own(self):
        mine = services.award(self.user, 'account.welcome', 'w')
        theirs = services.award(self.other, 'account.welcome', 'w')
        reply = api(self.user).post('/api/celebrations/seen/', {'ids': [mine.pk, theirs.pk]}, format='json')
        self.assertEqual((reply.status_code, reply.data['marked']), (200, 1))
        self.assertEqual(api(self.user).get('/api/celebrations/pending/').data['results'], [])
        self.assertEqual(len(api(self.other).get('/api/celebrations/pending/').data['results']), 1)

    def test_seen_rejects_a_bad_body(self):
        for body in ({}, {'ids': 'x'}, {'ids': list(range(51))}):
            self.assertEqual(api(self.user).post('/api/celebrations/seen/', body, format='json').status_code, 400, body)

    def test_the_setting_can_be_read_and_changed_and_turns_celebrations_off(self):
        self.assertEqual(api(self.user).get('/api/celebrations/settings/').data, {'enabled': True})
        services.award(self.user, 'account.welcome', 'w')
        self.assertEqual(api(self.user).patch('/api/celebrations/settings/', {'enabled': False}, format='json').data, {'enabled': False})
        reply = api(self.user).get('/api/celebrations/pending/')
        self.assertEqual((reply.data['enabled'], reply.data['results']), (False, []))
        self.assertIsNone(services.award(self.user, 'account.email_verified', 'v'))     # nothing new while off
        self.assertEqual(api(self.user).patch('/api/celebrations/settings/', {'enabled': True}, format='json').data, {'enabled': True})
        self.assertEqual(len(api(self.user).get('/api/celebrations/pending/').data['results']), 1)   # the earlier one is still waiting

    def test_the_setting_needs_a_real_boolean(self):
        for body in ({}, {'enabled': 'yes'}, {'enabled': 1}, {'enabled': None}):
            self.assertEqual(api(self.user).patch('/api/celebrations/settings/', body, format='json').status_code, 400, body)

    def test_someone_blocked_for_not_verifying_their_email_can_still_be_celebrated(self):
        from django.test import override_settings

        newcomer = make_user(email_verify_deadline=timezone.now() - timedelta(days=1))
        services.award(newcomer, 'account.welcome', 'welcome')
        with override_settings(EMAIL_VERIFICATION_MODE='block'):
            gated = api(newcomer).get('/api/hackathons/my-teams/')               # a feature the block really closes
            reply = api(newcomer).get('/api/celebrations/pending/')
            marked = api(newcomer).post('/api/celebrations/seen/', {'ids': [reply.data['results'][0]['id']]}, format='json')
        self.assertEqual(gated.status_code, 403)
        self.assertEqual((reply.status_code, len(reply.data['results'])), (200, 1))
        self.assertEqual((marked.status_code, marked.data['marked']), (200, 1))
