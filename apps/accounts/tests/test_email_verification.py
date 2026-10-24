"""Email verification: the state, the gate, the emailed links, "This wasn't me", deadlines and the maintenance commands."""

import re
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import EmailVerificationSuppression, EmailVerificationToken, PasswordSetupToken, PasswordStatus
from apps.accounts.services import email_verification as ev
from apps.accounts.services.password_setup_service import PasswordSetupService
from apps.core.services.email_service import EmailNotificationService

User = get_user_model()
PASSWORD = 'Str0ng!Passw0rd'


def make_user(email='dee@example.org', *, verified=False, role='NON_AFFILIATE', **extra):
    user = User.objects.create_user(username=email.split('@')[0] + str(User.objects.count()), email=email, password=PASSWORD, role=role, **extra)
    if verified:
        User.objects.filter(pk=user.pk).update(email_verified_at=timezone.now())
        user.refresh_from_db()
    return user


def signed_in(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def links_in(message):
    body = message.body
    return (re.search(r'verify-email\?token=([\w-]+)', body).group(1), re.search(r'not-me\?token=([\w-]+)', body).group(1))


class _Case(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        mail.outbox = []


class State(_Case):
    def test_a_new_account_gets_a_seven_day_deadline(self):
        user = make_user()
        remaining = user.email_verify_deadline - timezone.now()
        self.assertAlmostEqual(remaining.total_seconds(), 7 * 86400, delta=60)
        self.assertEqual(ev.days_left(user), 7)

    def test_the_state_is_in_auth_me_in_every_mode(self):
        user = make_user()
        for mode in ('off', 'banner', 'features', 'block'):
            with override_settings(EMAIL_VERIFICATION_MODE=mode):
                data = signed_in(user).get('/api/auth/me/').data['email_verification']
            self.assertEqual((data['mode'], data['verified'], data['blocked']), (mode, False, False), mode)
            self.assertEqual(data['days_left'], 7)
            self.assertEqual(data['email_masked'], 'd**@example.org')

    def test_verified_accounts_have_no_deadline_in_the_state(self):
        data = signed_in(make_user(verified=True)).get('/api/auth/me/').data['email_verification']
        self.assertEqual((data['verified'], data['days_left'], data['deadline'], data['blocked']), (True, None, None, False))

    def test_the_deadline_boundary(self):
        user = make_user()
        User.objects.filter(pk=user.pk).update(email_verify_deadline=timezone.now() + timedelta(seconds=30))
        user.refresh_from_db()
        self.assertFalse(ev.is_past_deadline(user))
        User.objects.filter(pk=user.pk).update(email_verify_deadline=timezone.now() - timedelta(seconds=1))
        user.refresh_from_db()
        self.assertTrue(ev.is_past_deadline(user))
        self.assertEqual(ev.days_left(user), 0)

    def test_an_account_with_no_stored_deadline_falls_back_to_the_signup_date(self):
        user = make_user()
        User.objects.filter(pk=user.pk).update(email_verify_deadline=None)
        user.refresh_from_db()
        self.assertAlmostEqual((ev.deadline(user) - timezone.now()).total_seconds(), 7 * 86400, delta=120)

    def test_an_unknown_mode_means_off(self):
        with override_settings(EMAIL_VERIFICATION_MODE='nonsense'):
            self.assertEqual(ev.mode(), 'off')


class TheGate(_Case):
    def setUp(self):
        super().setUp()
        self.member = make_user('member@example.org')

    def past_deadline(self, user):
        User.objects.filter(pk=user.pk).update(email_verify_deadline=timezone.now() - timedelta(days=1))
        user.refresh_from_db()

    @override_settings(EMAIL_VERIFICATION_MODE='off')
    def test_off_changes_nothing_even_past_the_deadline(self):
        self.past_deadline(self.member)
        self.assertEqual(signed_in(self.member).get('/api/forms/999/attendance/my-badge/').status_code, 404)

    @override_settings(EMAIL_VERIFICATION_MODE='banner')
    def test_banner_restricts_nothing(self):
        self.past_deadline(self.member)
        self.assertEqual(signed_in(self.member).get('/api/forms/999/attendance/my-badge/').status_code, 404)

    @override_settings(EMAIL_VERIFICATION_MODE='features')
    def test_features_an_unverified_member_can_read_and_edit_their_profile_but_not_use_private_features(self):
        client = signed_in(self.member)
        self.assertEqual(client.get('/api/auth/me/').status_code, 200)
        self.assertEqual(client.patch('/api/auth/me/', {'github_profile': 'https://github.com/dee'}, format='json').status_code, 200)
        reply = client.get('/api/forms/999/attendance/my-badge/')
        self.assertEqual((reply.status_code, reply.data['code'], reply.data['reason']), (403, 'EMAIL_VERIFICATION_REQUIRED', 'feature'))
        self.assertFalse(reply.data['blocked'])

    @override_settings(EMAIL_VERIFICATION_MODE='features')
    def test_features_a_verified_member_passes(self):
        self.assertEqual(signed_in(make_user('ok@example.org', verified=True)).get('/api/forms/999/attendance/my-badge/').status_code, 404)

    @override_settings(EMAIL_VERIFICATION_MODE='features')
    def test_features_writes_to_private_endpoints_are_refused_for_an_unverified_member(self):
        reply = signed_in(self.member).post('/api/hackathons/teams/1/invite-by-email/', {'email': 'a@example.org'}, format='json')
        self.assertEqual((reply.status_code, reply.data['code']), (403, 'EMAIL_VERIFICATION_REQUIRED'))

    @override_settings(EMAIL_VERIFICATION_MODE='features')
    def test_features_public_pages_stay_open_to_everyone(self):
        for path in ('/api/forms/', '/api/events/', '/api/hackathons/'):
            self.assertEqual(signed_in(self.member).get(path).status_code, 200, path)
            self.assertEqual(APIClient().get(path).status_code, 200, path)

    @override_settings(EMAIL_VERIFICATION_MODE='features')
    def test_features_admins_and_club_leads_are_not_exempt(self):
        for role in ('ADMIN', 'CLUB_LEAD'):
            person = make_user(f'{role.lower()}@example.org', role=role)
            reply = signed_in(person).get('/api/auth/users/')
            self.assertEqual((reply.status_code, reply.data['reason']), (403, 'staff'), role)
            self.assertEqual(signed_in(person).get('/api/auth/me/').status_code, 200, role)  # they can still see the verify screen
        verified = make_user('verified-admin@example.org', role='ADMIN', verified=True)
        self.assertEqual(signed_in(verified).get('/api/auth/users/').status_code, 200)

    @override_settings(EMAIL_VERIFICATION_MODE='features')
    def test_features_django_superusers_are_not_exempt_either(self):
        boss = make_user('boss@example.org', is_superuser=True, is_staff=True)
        self.assertEqual(signed_in(boss).get('/api/auth/users/').status_code, 403)

    @override_settings(EMAIL_VERIFICATION_MODE='block')
    def test_block_before_the_deadline_behaves_like_features(self):
        client = signed_in(self.member)
        self.assertEqual(client.get('/api/forms/some-form/draft/').status_code, 404)  # reading is still fine
        self.assertEqual(client.get('/api/forms/999/attendance/my-badge/').status_code, 403)

    @override_settings(EMAIL_VERIFICATION_MODE='block')
    def test_block_after_the_deadline_locks_the_private_app_but_not_the_way_out(self):
        self.past_deadline(self.member)
        client = signed_in(self.member)
        reply = client.get('/api/forms/some-form/draft/')
        self.assertEqual((reply.status_code, reply.data['code'], reply.data['reason'], reply.data['blocked']), (403, 'EMAIL_VERIFICATION_REQUIRED', 'deadline', True))
        self.assertEqual(client.patch('/api/auth/me/', {'github_profile': 'https://github.com/dee'}, format='json').status_code, 403)
        self.assertEqual(client.get('/api/notifications/').status_code, 200)
        # still reachable: the profile read, the verification endpoints, sign out, public pages
        self.assertTrue(client.get('/api/auth/me/').data['email_verification']['blocked'])
        self.assertEqual(client.post('/api/auth/email/verification/request/').status_code, 200)
        self.assertEqual(client.get('/api/forms/').status_code, 200)
        self.assertEqual(APIClient().get('/api/events/').status_code, 200)

    @override_settings(EMAIL_VERIFICATION_MODE='block')
    def test_verifying_unblocks_at_once_without_signing_in_again(self):
        self.past_deadline(self.member)
        client = signed_in(self.member)
        self.assertEqual(client.get('/api/forms/some-form/draft/').status_code, 403)
        ev.mark_verified(self.member)
        self.assertEqual(client.get('/api/forms/some-form/draft/').status_code, 404)

    @override_settings(EMAIL_VERIFICATION_MODE='block')
    def test_a_blocked_account_can_still_reset_its_password_and_that_verifies_it(self):
        self.past_deadline(self.member)
        User.objects.filter(pk=self.member.pk).update(password_status=PasswordStatus.ACTIVE)
        PasswordSetupService.request_setup_link(self.member.email, '203.0.113.5')
        raw = re.search(r'token=([\w-]+)', mail.outbox[-1].body).group(1)
        PasswordSetupService.confirm_password_setup(raw, 'An0ther!Passw0rd')
        self.member.refresh_from_db()
        self.assertTrue(ev.is_verified(self.member))
        self.assertEqual(signed_in(self.member).get('/api/forms/some-form/draft/').status_code, 404)

    @override_settings(EMAIL_VERIFICATION_MODE='features')
    def test_submitting_a_form_needs_a_signed_in_verified_account(self):
        from apps.forms.models import FieldType, FormStatus
        from apps.forms.tests.factories import add_field, make_form
        form = make_form(status=FormStatus.PUBLISHED)
        field = add_field(form, FieldType.TEXT, label='Name')
        body = {'form': form.id, 'answers': [{'field': field.id, 'value': 'x'}]}
        anonymous = APIClient().post('/api/forms/submissions/', body, format='json')
        self.assertEqual((anonymous.status_code, anonymous.data['code']), (401, 'SIGN_IN_REQUIRED'))
        unverified = signed_in(self.member).post('/api/forms/submissions/', body, format='json')
        self.assertEqual((unverified.status_code, unverified.data['code']), (403, 'EMAIL_VERIFICATION_REQUIRED'))
        verified = signed_in(make_user('v@example.org', verified=True)).post('/api/forms/submissions/', body, format='json')
        self.assertEqual(verified.status_code, 201, verified.data)

    @override_settings(EMAIL_VERIFICATION_MODE='features')
    def test_saving_a_form_draft_needs_a_verified_account_but_reading_it_does_not(self):
        from apps.forms.models import FormStatus
        from apps.forms.tests.factories import make_form
        form = make_form(status=FormStatus.PUBLISHED)
        client = signed_in(self.member)
        self.assertEqual(client.put(f'/api/forms/{form.slug}/draft/', {'answers': {}, 'current_page': 0}, format='json').status_code, 403)
        self.assertEqual(client.get(f'/api/forms/{form.slug}/draft/').status_code, 200)
        self.assertEqual(signed_in(make_user('v2@example.org', verified=True)).put(f'/api/forms/{form.slug}/draft/', {'answers': {}, 'current_page': 0}, format='json').status_code, 200)

    @override_settings(EMAIL_VERIFICATION_MODE='features')
    def test_people_are_matched_by_email_only_when_verified(self):
        from apps.forms.models import FieldType, FormStatus
        from apps.forms.services import FormAutomationService
        from apps.forms.tests.factories import add_field, make_form
        form = make_form(status=FormStatus.PUBLISHED, club_id_enabled=True)
        email_field = add_field(form, FieldType.EMAIL, label='Email')
        form.club_id_field_mapping = {'email': email_field.id}
        form.save()
        squatter = make_user('victim@example.org')
        self.assertIsNone(FormAutomationService.resolve_club_member(form, {email_field.id: 'victim@example.org'}))
        User.objects.filter(pk=squatter.pk).update(email_verified_at=timezone.now())
        self.assertEqual(FormAutomationService.resolve_club_member(form, {email_field.id: 'victim@example.org'}).pk, squatter.pk)


class RequestingTheLink(_Case):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.client_ = signed_in(self.user)

    def request(self):
        return self.client_.post('/api/auth/email/verification/request/')

    def test_it_sends_one_email_with_both_links(self):
        reply = self.request()
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.data['email_masked'], 'd**@example.org')
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ['dee@example.org'])
        verify, dispute = links_in(message)
        self.assertNotEqual(verify, dispute)
        self.assertIn("This wasn't me", message.body)
        row = EmailVerificationToken.objects.get()
        self.assertNotIn(verify, row.token_hash)          # only hashes are stored
        self.assertEqual((row.status, row.send_status, row.purpose), ('PENDING', 'SENT', 'VERIFY'))

    def test_nothing_is_sent_at_signup(self):
        make_user('someone@example.org')
        self.assertEqual(len(mail.outbox), 0)

    def test_a_second_request_inside_a_minute_is_refused_with_a_wait(self):
        self.request()
        again = self.request()
        self.assertEqual((again.status_code, again.data['code']), (429, 'COOLDOWN'))
        self.assertGreaterEqual(again.data['retry_after'], 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_five_a_hour_per_account(self):
        for _ in range(5):
            cache.delete(f'email_verify:cooldown:{self.user.pk}')
            self.assertEqual(self.request().status_code, 200)
        cache.delete(f'email_verify:cooldown:{self.user.pk}')
        sixth = self.request()
        self.assertEqual((sixth.status_code, sixth.data['code']), (429, 'TOO_MANY_REQUESTS'))

    def test_six_a_day_per_address_across_accounts(self):
        for i in range(6):
            person = make_user(f'asker{i}@example.org')
            reply = signed_in(person).post('/api/auth/email/change/', {'new_email': 'shared@example.org', 'password': PASSWORD}, format='json')
            self.assertEqual(reply.status_code, 200, (i, reply.data))
        seventh = make_user('asker6@example.org')
        reply = signed_in(seventh).post('/api/auth/email/change/', {'new_email': 'shared@example.org', 'password': PASSWORD}, format='json')
        self.assertEqual((reply.status_code, reply.data['code']), (429, 'ADDRESS_LIMIT'))

    def test_a_verified_account_is_told_so_and_sent_nothing(self):
        ev.mark_verified(self.user)
        reply = self.request()
        self.assertEqual((reply.status_code, reply.data['already_verified']), (200, True))
        self.assertEqual(len(mail.outbox), 0)

    def test_a_failed_send_is_reported_recorded_and_retryable_soon(self):
        with mock.patch.object(EmailNotificationService, 'send_email', return_value=False):
            reply = self.request()
        self.assertEqual((reply.status_code, reply.data['code']), (503, 'SEND_FAILED'))
        self.assertEqual(EmailVerificationToken.objects.get().send_status, 'FAILED')
        self.assertEqual(self.user.pk and cache.get(f'email_verify:hour:{self.user.pk}', 0), 0)  # a failure does not use up the hourly budget

    def test_a_provider_that_raises_does_not_break_the_request(self):
        with mock.patch.object(EmailNotificationService, 'send_email', side_effect=RuntimeError('smtp down')):
            reply = self.request()
        self.assertEqual(reply.status_code, 503)

    def test_a_newer_link_replaces_the_older_one(self):
        self.request()
        old_verify, _ = links_in(mail.outbox[0])
        cache.delete(f'email_verify:cooldown:{self.user.pk}')
        self.request()
        self.assertEqual(APIClient().post('/api/auth/email/verification/confirm/', {'token': old_verify}, format='json').data['code'], 'LINK_EXPIRED')
        new_verify, _ = links_in(mail.outbox[1])
        self.assertTrue(APIClient().post('/api/auth/email/verification/confirm/', {'token': new_verify}, format='json').data['verified'])

    def test_anonymous_callers_cannot_request(self):
        self.assertEqual(APIClient().post('/api/auth/email/verification/request/').status_code, 401)

    def test_the_link_page_address_points_at_the_frontend(self):
        self.request()
        self.assertRegex(mail.outbox[0].body, r'https?://[^/\s]+/account/verify-email\?token=')


class TwoTapsAtOnce(_Case):
    def test_the_cooldown_is_claimed_atomically_so_a_double_tap_sends_one_email(self):
        user = make_user()
        with mock.patch.object(cache, 'get', side_effect=lambda key, default=None: default):  # as if both requests had read "no cooldown yet"
            ev.request_link(user)
            with self.assertRaises(ev.VerificationError) as caught:
                ev.request_link(user)
        self.assertEqual(caught.exception.code, 'COOLDOWN')
        self.assertEqual(len(mail.outbox), 1)


class TheAdminCreateUserScreen(_Case):
    """The admin "Create New User" modal posts to the public registration endpoint with the admin's token."""
    body = {'email': 'newbie@example.org', 'password': PASSWORD, 'first_name': 'New', 'branch': 'CSE', 'year': 2}

    @override_settings(EMAIL_VERIFICATION_MODE='features')
    def test_an_unverified_admin_cannot_create_users_but_a_verified_one_can(self):
        unverified_admin = make_user('a1@example.org', role='ADMIN')
        reply = signed_in(unverified_admin).post('/api/auth/register/', self.body, format='json')
        self.assertEqual((reply.status_code, reply.data['code']), (403, 'EMAIL_VERIFICATION_REQUIRED'))
        self.assertFalse(User.objects.filter(email='newbie@example.org').exists())
        verified_admin = make_user('a2@example.org', role='ADMIN', verified=True)
        self.assertEqual(signed_in(verified_admin).post('/api/auth/register/', self.body, format='json').status_code, 201)

    @override_settings(EMAIL_VERIFICATION_MODE='block')
    def test_self_signup_by_a_visitor_is_never_gated(self):
        reply = APIClient().post('/api/auth/register/', self.body, format='json')
        self.assertEqual(reply.status_code, 201, reply.data)

    @override_settings(EMAIL_VERIFICATION_MODE='off')
    def test_nothing_changes_when_verification_is_off(self):
        admin = make_user('a3@example.org', role='ADMIN')
        self.assertEqual(signed_in(admin).post('/api/auth/register/', self.body, format='json').status_code, 201)


class ConfirmingTheLink(_Case):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        signed_in(self.user).post('/api/auth/email/verification/request/')
        self.verify, self.dispute = links_in(mail.outbox[0])
        self.public = APIClient()

    def confirm(self, token=None):
        return self.public.post('/api/auth/email/verification/confirm/', {'token': token or self.verify}, format='json')

    def test_looking_at_the_link_never_uses_it(self):
        for _ in range(3):
            seen = self.public.get('/api/auth/email/verification/status/', {'token': self.verify})
            self.assertEqual((seen.status_code, seen.data['valid'], seen.data['email_masked']), (200, True, 'd**@example.org'))
        self.user.refresh_from_db()
        self.assertFalse(ev.is_verified(self.user))
        self.assertTrue(self.confirm().data['verified'])

    def test_confirming_verifies_the_account_without_being_signed_in(self):
        reply = self.confirm()
        self.assertEqual((reply.status_code, reply.data['verified'], reply.data['already']), (200, True, False))
        self.user.refresh_from_db()
        self.assertTrue(ev.is_verified(self.user))
        self.assertEqual(EmailVerificationToken.objects.get().status, 'USED')

    def test_pressing_twice_or_on_a_second_device_is_harmless(self):
        self.confirm()
        again = self.confirm()
        self.assertEqual((again.status_code, again.data['verified'], again.data['already']), (200, True, True))
        seen = self.public.get('/api/auth/email/verification/status/', {'token': self.verify})
        self.assertEqual((seen.data['valid'], seen.data['code'], seen.data['already_verified']), (False, 'ALREADY_USED', True))

    def test_an_expired_link_says_so(self):
        EmailVerificationToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        for reply in (self.confirm(), self.public.get('/api/auth/email/verification/status/', {'token': self.verify})):
            self.assertEqual((reply.status_code, reply.data['code']), (400, 'LINK_EXPIRED'))

    def test_junk_tokens_are_refused_without_errors(self):
        for junk in ('', 'nope', 'a' * 500, None, 123, ['x'], {'a': 1}, '../../etc/passwd', "' OR 1=1 --"):
            reply = self.public.post('/api/auth/email/verification/confirm/', {'token': junk}, format='json')
            self.assertEqual((reply.status_code, reply.data['code']), (400, 'INVALID_LINK'), repr(junk))
        self.assertEqual(self.public.post('/api/auth/email/verification/confirm/', {}, format='json').status_code, 400)
        self.assertEqual(self.public.get('/api/auth/email/verification/status/').status_code, 400)

    def test_the_dispute_token_cannot_be_used_to_verify(self):
        self.assertEqual(self.confirm(self.dispute).data['code'], 'INVALID_LINK')

    def test_if_the_address_changed_after_the_link_was_sent_it_no_longer_works(self):
        User.objects.filter(pk=self.user.pk).update(email='moved@example.org')
        self.assertEqual(self.confirm().data['code'], 'EMAIL_CHANGED')

    def test_a_deactivated_account_gets_a_generic_refusal(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.confirm().data['code'], 'INVALID_LINK')

    def test_confirming_while_signed_in_as_someone_else_verifies_the_link_owner_only(self):
        other = make_user('other@example.org')
        client = signed_in(other)
        self.assertEqual(client.post('/api/auth/email/verification/confirm/', {'token': self.verify}, format='json').status_code, 200)
        other.refresh_from_db(); self.user.refresh_from_db()
        self.assertFalse(ev.is_verified(other))
        self.assertTrue(ev.is_verified(self.user))

    def test_it_does_not_reset_the_deadline_or_touch_other_fields(self):
        before = User.objects.get(pk=self.user.pk).email_verify_deadline
        self.confirm()
        self.assertEqual(User.objects.get(pk=self.user.pk).email_verify_deadline, before)


class ChangingTheAddress(_Case):
    def setUp(self):
        super().setUp()
        self.user = make_user('old@example.org')
        self.client_ = signed_in(self.user)

    def change(self, new='new@example.org', password=PASSWORD):
        return self.client_.post('/api/auth/email/change/', {'new_email': new, 'password': password}, format='json')

    def test_the_link_goes_to_the_new_address_and_nothing_changes_until_confirmed(self):
        reply = self.change()
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(mail.outbox[0].to, ['new@example.org'])
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'old@example.org')
        self.assertEqual(signed_in(self.user).get('/api/auth/me/').data['email_verification']['pending_email_masked'], 'n**@example.org')

    def test_confirming_swaps_the_address_and_verifies_it(self):
        self.change()
        token, _ = links_in(mail.outbox[0])
        reply = APIClient().post('/api/auth/email/verification/confirm/', {'token': token}, format='json')
        self.assertEqual((reply.status_code, reply.data['purpose']), (200, 'CHANGE'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'new@example.org')
        self.assertTrue(ev.is_verified(self.user))
        self.assertIsNone(signed_in(self.user).get('/api/auth/me/').data['email_verification']['pending_email_masked'])

    def test_a_verified_member_stays_verified_while_a_change_is_pending(self):
        ev.mark_verified(self.user)
        self.change()
        self.user.refresh_from_db()
        self.assertTrue(ev.is_verified(self.user))

    def test_changing_the_address_does_not_restart_the_seven_days(self):
        before = User.objects.get(pk=self.user.pk).email_verify_deadline
        self.change()
        token, _ = links_in(mail.outbox[0])
        APIClient().post('/api/auth/email/verification/confirm/', {'token': token}, format='json')
        self.assertEqual(User.objects.get(pk=self.user.pk).email_verify_deadline, before)

    def test_the_refusals(self):
        self.assertEqual(self.change(password='wrong').data['code'], 'WRONG_PASSWORD')
        self.assertEqual(self.change(new='not-an-email').data['code'], 'INVALID_EMAIL')
        self.assertEqual(self.change(new='OLD@example.org').data['code'], 'SAME_EMAIL')
        make_user('taken@example.org')
        self.assertEqual(self.change(new='Taken@Example.org').data['code'], 'EMAIL_TAKEN')
        for junk in (None, '', '   ', 'a@', '@b.com', 'a b@c.com', 'x' * 300 + '@example.org'):
            self.assertEqual(self.client_.post('/api/auth/email/change/', {'new_email': junk, 'password': PASSWORD}, format='json').status_code, 400, repr(junk))
        self.assertEqual(len(mail.outbox), 0)

    def test_if_someone_takes_the_address_before_it_is_confirmed_the_change_fails_cleanly(self):
        self.change()
        token, _ = links_in(mail.outbox[0])
        make_user('new@example.org')
        reply = APIClient().post('/api/auth/email/verification/confirm/', {'token': token}, format='json')
        self.assertEqual(reply.data['code'], 'EMAIL_TAKEN')
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'old@example.org')

    def test_a_new_request_cancels_the_pending_one(self):
        self.change(new='first@example.org')
        first, _ = links_in(mail.outbox[0])
        cache.delete(f'email_verify:cooldown:{self.user.pk}')
        self.change(new='second@example.org')
        self.assertEqual(APIClient().post('/api/auth/email/verification/confirm/', {'token': first}, format='json').data['code'], 'LINK_EXPIRED')

    def test_the_profile_endpoint_still_cannot_change_the_address(self):
        self.client_.patch('/api/auth/me/', {'email': 'sneaky@example.org'}, format='json')
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'old@example.org')


class ThisWasntMe(_Case):
    def setUp(self):
        super().setUp()
        self.squatter = make_user('victim@example.org')
        signed_in(self.squatter).post('/api/auth/email/verification/request/')
        self.verify, self.dispute = links_in(mail.outbox[0])
        self.public = APIClient()

    def press(self, token='__real__'):
        return self.public.post('/api/auth/email/verification/dispute/', {'token': self.dispute if token == '__real__' else token}, format='json')

    def test_an_unused_account_is_removed_and_the_address_is_freed_but_protected_from_floods(self):
        reply = self.press()
        self.assertEqual((reply.status_code, reply.data['done'], reply.data['removed']), (200, True, True))
        self.assertFalse(User.objects.filter(email='victim@example.org').exists())
        self.assertTrue(EmailVerificationSuppression.objects.filter(email='victim@example.org').exists())
        # the real owner can now register, and nobody can use the address to mail-bomb them
        stranger = make_user('stranger@example.org')
        reply = signed_in(stranger).post('/api/auth/email/change/', {'new_email': 'victim@example.org', 'password': PASSWORD}, format='json')
        self.assertEqual((reply.status_code, reply.data['code']), (400, 'ADDRESS_SUPPRESSED'))
        self.assertEqual(len(mail.outbox), 1)

    def test_an_account_with_history_is_switched_off_not_deleted(self):
        User.objects.filter(pk=self.squatter.pk).update(last_login=timezone.now())
        reply = self.press()
        self.assertEqual(reply.data['removed'], False)
        user = User.objects.get(pk=self.squatter.pk)
        self.assertFalse(user.is_active)
        self.assertIsNotNone(user.email_disputed_at)

    def test_an_account_with_a_form_response_counts_as_used(self):
        from apps.forms.models import FormStatus, Response
        from apps.forms.tests.factories import make_form
        Response.objects.create(form=make_form(status=FormStatus.PUBLISHED), user=self.squatter)
        self.assertEqual(self.press().data['removed'], False)
        self.assertTrue(User.objects.filter(pk=self.squatter.pk).exists())

    def test_a_disputed_account_cannot_sign_in_or_verify_any_more(self):
        User.objects.filter(pk=self.squatter.pk).update(last_login=timezone.now())
        self.press()
        self.assertEqual(self.public.post('/api/auth/email/verification/confirm/', {'token': self.verify}, format='json').data['code'], 'INVALID_LINK')
        login = self.public.post('/api/auth/login/', {'email': 'victim@example.org', 'password': PASSWORD}, format='json')
        self.assertIn(login.status_code, (400, 401))

    def test_pressing_twice_is_harmless(self):
        User.objects.filter(pk=self.squatter.pk).update(last_login=timezone.now())
        self.press()
        self.assertEqual(self.press().data['already'], True)

    def test_an_expired_dispute_link_says_so(self):
        EmailVerificationToken.objects.update(dispute_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.press().data['code'], 'LINK_EXPIRED')
        self.assertTrue(User.objects.filter(pk=self.squatter.pk).exists())

    def test_junk_and_the_verify_token_do_nothing(self):
        for junk in ('', 'nope', 'z' * 400, None, 5):
            self.assertEqual(self.press(junk).status_code, 400, repr(junk))
        self.assertEqual(self.press(self.verify).status_code, 400)
        self.assertTrue(User.objects.filter(pk=self.squatter.pk).exists())

    def test_disputing_an_email_change_only_cancels_the_change(self):
        asker = make_user('asker@example.org')
        signed_in(asker).post('/api/auth/email/change/', {'new_email': 'victim2@example.org', 'password': PASSWORD}, format='json')
        verify, dispute = links_in(mail.outbox[-1])
        reply = self.press(dispute)
        self.assertEqual((reply.data['done'], reply.data['removed']), (True, False))
        self.assertTrue(User.objects.get(pk=asker.pk).is_active)
        self.assertEqual(APIClient().post('/api/auth/email/verification/confirm/', {'token': verify}, format='json').data['code'], 'LINK_EXPIRED')

    def test_sessions_of_a_disabled_account_are_revoked(self):
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
        from rest_framework_simplejwt.tokens import RefreshToken
        User.objects.filter(pk=self.squatter.pk).update(last_login=timezone.now())
        RefreshToken.for_user(self.squatter)
        self.press()
        self.assertTrue(BlacklistedToken.objects.exists())


class Deadlines(_Case):
    def test_existing_accounts_get_daily_waves_most_recently_active_first(self):
        users = [make_user(f'u{i}@example.org') for i in range(5)]
        for i, user in enumerate(users):
            User.objects.filter(pk=user.pk).update(last_login=timezone.now() - timedelta(days=i), email_verify_deadline=None)
        ev.schedule_deadlines(start_days=3, batch=2)
        days = [round((User.objects.get(pk=u.pk).email_verify_deadline - timezone.now()).total_seconds() / 86400) for u in users]
        self.assertEqual(days, [3, 3, 4, 4, 5])

    def test_verified_and_inactive_accounts_are_left_out(self):
        verified, inactive, member = make_user('a@example.org', verified=True), make_user('b@example.org'), make_user('c@example.org')
        User.objects.filter(pk=inactive.pk).update(is_active=False)
        before = User.objects.get(pk=verified.pk).email_verify_deadline
        self.assertEqual(ev.schedule_deadlines(start_days=1, batch=10), 1)
        self.assertEqual(User.objects.get(pk=verified.pk).email_verify_deadline, before)

    def test_only_unscheduled_leaves_existing_deadlines_alone(self):
        keep, fresh = make_user('k@example.org'), make_user('f@example.org')
        User.objects.filter(pk=fresh.pk).update(email_verify_deadline=None)
        kept_value = User.objects.get(pk=keep.pk).email_verify_deadline
        self.assertEqual(ev.schedule_deadlines(start_days=9, batch=10, only_unscheduled=True), 1)
        self.assertEqual(User.objects.get(pk=keep.pk).email_verify_deadline, kept_value)


class AdminAndCommands(_Case):
    def test_an_admin_can_vouch_for_a_member_and_a_member_cannot(self):
        member = make_user('member@example.org')
        admin = make_user('boss@example.org', role='ADMIN', verified=True)
        self.assertEqual(signed_in(make_user('x@example.org', verified=True)).post(f'/api/auth/users/{member.pk}/verify-email/').status_code, 403)
        self.assertEqual(signed_in(admin).post(f'/api/auth/users/{member.pk}/verify-email/').data, {'verified': True, 'changed': True})
        member.refresh_from_db()
        self.assertTrue(ev.is_verified(member))
        self.assertEqual(signed_in(admin).post('/api/auth/users/999999/verify-email/').status_code, 404)

    @override_settings(EMAIL_VERIFICATION_MODE='features')
    def test_an_unverified_admin_cannot_vouch_for_others(self):
        member = make_user('member@example.org')
        admin = make_user('boss@example.org', role='ADMIN')
        self.assertEqual(signed_in(admin).post(f'/api/auth/users/{member.pk}/verify-email/').status_code, 403)

    def run_command(self, *args):
        out = StringIO()
        call_command(*args, stdout=out)
        return out.getvalue()

    def test_verify_email_manually(self):
        user = make_user('lost@example.org')
        self.assertIn('marked verified', self.run_command('verify_email_manually', 'LOST@example.org'))
        user.refresh_from_db()
        self.assertTrue(ev.is_verified(user))
        self.assertIn('already verified', self.run_command('verify_email_manually', 'lost@example.org'))
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('verify_email_manually', 'nobody@example.org')

    def test_report_unverifiable_emails_finds_fake_addresses_and_flags_staff(self):
        make_user('fine@gmail.com')
        make_user('admin@example.com', role='ADMIN')
        make_user('lead@college.local', role='CLUB_LEAD')
        make_user('weird@nodot')
        report = self.run_command('report_unverifiable_emails')
        self.assertIn('[STAFF] ', report)
        self.assertIn('admin@example.com', report)
        self.assertIn('lead@college.local', report)
        self.assertIn('weird@nodot', report)
        self.assertNotIn('fine@gmail.com', report)
        self.assertNotIn('weird@nodot', self.run_command('report_unverifiable_emails', '--staff-only'))

    def test_extend_verification_deadlines(self):
        overdue, fresh, done = make_user('o@example.org'), make_user('n@example.org'), make_user('d@example.org', verified=True)
        User.objects.filter(pk=overdue.pk).update(email_verify_deadline=timezone.now() - timedelta(days=5))
        self.run_command('extend_verification_deadlines', '--days', '10', '--only-blocked')
        self.assertAlmostEqual((User.objects.get(pk=overdue.pk).email_verify_deadline - timezone.now()).total_seconds(), 10 * 86400, delta=300)
        untouched = User.objects.get(pk=fresh.pk).email_verify_deadline
        self.assertAlmostEqual((untouched - timezone.now()).total_seconds(), 7 * 86400, delta=300)
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('extend_verification_deadlines', '--days', '0')

    def test_purge_unverified_accounts(self):
        stale_unused = make_user('stale@example.org')
        stale_used = make_user('used@example.org', last_login=timezone.now())
        stale_staff = make_user('staff@example.org', is_staff=True)
        recent = make_user('recent@example.org')
        for user in (stale_unused, stale_used, stale_staff):
            User.objects.filter(pk=user.pk).update(email_verify_deadline=timezone.now() - timedelta(days=60))
        self.assertIn('Would remove 1', self.run_command('purge_unverified_accounts', '--dry-run'))
        self.assertTrue(User.objects.filter(pk=stale_unused.pk).exists())
        self.assertIn('Removed 1', self.run_command('purge_unverified_accounts'))
        self.assertFalse(User.objects.filter(pk=stale_unused.pk).exists())
        for survivor in (stale_used, stale_staff, recent):
            self.assertTrue(User.objects.filter(pk=survivor.pk).exists())

    def test_django_admin_actions(self):
        from django.contrib import admin
        from django.test import RequestFactory
        from apps.accounts.admin import UserAdmin
        superuser = make_user('root@example.org', is_superuser=True, is_staff=True, verified=True)
        target = make_user('target@example.org')
        model_admin = UserAdmin(User, admin.site)
        request = RequestFactory().post('/')
        request.user = superuser
        request.session, request._messages = {}, mock.MagicMock()
        model_admin.mark_email_verified(request, User.objects.filter(pk=target.pk))
        target.refresh_from_db()
        self.assertTrue(ev.is_verified(target))
        other = make_user('other@example.org')
        before = other.email_verify_deadline
        model_admin.extend_verification_7_days(request, User.objects.filter(pk=other.pk))
        other.refresh_from_db()
        self.assertEqual(other.email_verify_deadline, before + timedelta(days=7))


class WhatCannotLeak(_Case):
    def test_the_state_never_contains_the_full_address_or_a_token(self):
        data = signed_in(make_user('somebody@example.org')).get('/api/auth/me/').data['email_verification']
        self.assertNotIn('somebody', str(data))

    def test_masking(self):
        self.assertEqual(ev.mask_email('a@b.com'), 'a**@b.com')
        self.assertEqual(ev.mask_email('longname@b.com'), 'l******@b.com')
        self.assertEqual(ev.mask_email('nonsense'), '***')
        self.assertEqual(ev.mask_email(''), '***')

    def test_raw_tokens_are_never_stored(self):
        user = make_user()
        signed_in(user).post('/api/auth/email/verification/request/')
        raw, raw_dispute = links_in(mail.outbox[0])
        row = EmailVerificationToken.objects.get()
        for value in (raw, raw_dispute):
            self.assertNotIn(value, (row.token_hash, row.dispute_hash))
            self.assertEqual(len(value) >= 40, True)
