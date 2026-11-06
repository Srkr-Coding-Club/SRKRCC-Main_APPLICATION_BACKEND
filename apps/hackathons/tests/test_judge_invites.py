"""Phase 1 of round judging: the data it adds, and inviting judges without anyone handling a password."""
import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.accounts.models import PasswordSetupToken, PasswordStatus
from apps.accounts.services.password_setup_service import InvalidSetupTokenError, PasswordSetupService
from apps.audit.models import AuditLog
from apps.hackathons import judge_invites
from apps.hackathons.errors import HackathonError
from apps.hackathons.judge_invites import JudgeInviteService
from apps.hackathons.judging import CriteriaService
from apps.hackathons.models import (
    AssignmentMode, CriterionTarget, EvaluationMode, EvaluationStatus, HackathonStaff, JudgeInvite, JudgeInviteState, JudgingCriterion,
    Round, RoundEntry, ScoringTarget,
)
from .helpers import make_hackathon, make_team, make_user

User = get_user_model()
GOOD_PASSWORD = 'Judge#Pass2026'


class Base(TestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, features={'judging': True})
        self.admin = make_user(role='ADMIN')
        patcher = mock.patch.object(judge_invites.EmailNotificationService, 'send_email', return_value=True)
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def mail(self):
        """(subject, body) of the last invitation email."""
        call = self.send.call_args
        return call.kwargs['subject'], call.kwargs['plain_content']

    def token_in(self, body):
        return re.search(r'token=([\w-]+)', body).group(1)


class InvitingANewPerson(Base):
    def test_creates_an_account_with_no_password_and_a_judge_only_staff_row(self):
        result = JudgeInviteService.invite(self.hack, self.admin, 'Dr  Asha   Rao', 'Asha.Rao@Example.org')
        user = User.objects.get(email='asha.rao@example.org')
        self.assertEqual((user.first_name, user.last_name), ('Dr', 'Asha Rao'))
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.password_status, PasswordStatus.NEEDS_SETUP)
        self.assertEqual((user.role, user.created_from), ('NON_AFFILIATE', 'ADMIN'))
        staff = HackathonStaff.objects.get(hackathon=self.hack, user=user)
        self.assertEqual((staff.role, staff.permissions), ('JUDGE', ['judge']))
        self.assertEqual((result['state'], result['account_created'], result['email_sent']), ('INVITED', True, True))

    def test_the_email_carries_a_one_time_link_and_never_a_password(self):
        JudgeInviteService.invite(self.hack, self.admin, 'Asha Rao', 'asha@example.org')
        subject, body = self.mail()
        self.assertIn(self.hack.title, subject)
        self.assertIn('/account/setup-password?token=', body)
        self.assertIn(f'/hackathons/{self.hack.slug}/judging', body)
        self.assertNotIn('password:', body.lower())
        token = self.token_in(body)
        self.assertNotIn(token, ''.join(PasswordSetupToken.objects.values_list('token_hash', flat=True)))  # only the hash is stored

    def test_the_link_lasts_seven_days_and_works_once(self):
        result = JudgeInviteService.invite(self.hack, self.admin, 'Asha Rao', 'asha@example.org')
        expires = PasswordSetupToken.objects.get(user__email='asha@example.org').expires_at
        self.assertAlmostEqual((expires - timezone.now()).total_seconds(), 7 * 86400, delta=60)
        self.assertEqual(result['link_expires_at'], expires)
        token = self.token_in(self.mail()[1])
        user = PasswordSetupService.confirm_password_setup(token, GOOD_PASSWORD)
        self.assertTrue(user.check_password(GOOD_PASSWORD))
        with self.assertRaises(InvalidSetupTokenError):
            PasswordSetupService.confirm_password_setup(token, GOOD_PASSWORD)

    def test_choosing_a_password_activates_the_invite_and_verifies_the_address(self):
        JudgeInviteService.invite(self.hack, self.admin, 'Asha Rao', 'asha@example.org')
        PasswordSetupService.confirm_password_setup(self.token_in(self.mail()[1]), GOOD_PASSWORD)
        invite = JudgeInvite.objects.get(email='asha@example.org')
        self.assertEqual(invite.state, JudgeInviteState.ACTIVE)
        self.assertIsNotNone(invite.user.email_verified_at)

    def test_an_expired_link_shows_as_expired(self):
        JudgeInviteService.invite(self.hack, self.admin, 'Asha Rao', 'asha@example.org')
        JudgeInvite.objects.update(link_expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(JudgeInvite.objects.get().state, JudgeInviteState.EXPIRED)

    def test_the_name_and_email_are_checked(self):
        for name, email, field in (('', 'a@example.org', 'name'), ('x' * 101, 'a@example.org', 'name'), ('Asha', 'not-an-email', 'email'), ('Asha', '', 'email')):
            with self.assertRaises(HackathonError) as ctx:
                JudgeInviteService.invite(self.hack, self.admin, name, email)
            self.assertEqual(ctx.exception.detail['field'], field)
        self.assertFalse(User.objects.filter(first_name='Asha').exists())

    def test_two_people_with_the_same_email_name_get_distinct_usernames(self):
        JudgeInviteService.invite(self.hack, self.admin, 'A One', 'sam@one.org')
        JudgeInviteService.invite(self.hack, self.admin, 'A Two', 'sam@two.org')
        self.assertEqual(User.objects.filter(email__in=['sam@one.org', 'sam@two.org']).values('username').distinct().count(), 2)

    def test_it_is_audited_without_the_token(self):
        JudgeInviteService.invite(self.hack, self.admin, 'Asha Rao', 'asha@example.org')
        entry = AuditLog.objects.get(action='Invited Hackathon Judge')
        self.assertEqual(entry.details['email'], 'asha@example.org')
        self.assertNotIn(self.token_in(self.mail()[1]), str(entry.details))


class InvitingSomeoneWhoHasAnAccount(Base):
    def test_an_existing_account_is_added_without_a_setup_link(self):
        person = make_user(email='prof@example.org')
        result = JudgeInviteService.invite(self.hack, self.admin, 'Prof', 'prof@example.org')
        self.assertTrue(HackathonStaff.objects.filter(hackathon=self.hack, user=person, role='JUDGE').exists())
        self.assertEqual((result['state'], result['account_created'], result['link_expires_at']), ('ACTIVE', False, None))
        self.assertNotIn('setup-password', self.mail()[1])
        self.assertEqual(PasswordSetupToken.objects.count(), 0)
        person.refresh_from_db()
        self.assertTrue(person.has_usable_password())  # never touched

    def test_an_imported_member_still_needing_setup_gets_a_link(self):
        person = make_user(email='old@example.org')
        person.set_unusable_password()
        person.password_status = PasswordStatus.NEEDS_SETUP
        person.save()
        result = JudgeInviteService.invite(self.hack, self.admin, 'Old Member', 'old@example.org')
        self.assertEqual((result['state'], result['account_created']), ('INVITED', False))
        self.assertIn('setup-password', self.mail()[1])

    def test_a_participant_cannot_be_invited_and_nothing_is_left_behind(self):
        leader = make_user(email='lead@example.org')
        make_team(self.hack, leader)
        with self.assertRaises(HackathonError) as ctx:
            JudgeInviteService.invite(self.hack, self.admin, 'Lead', 'lead@example.org')
        self.assertEqual(ctx.exception.detail['code'], 'CONFLICT')
        self.assertFalse(JudgeInvite.objects.exists())
        self.assertFalse(HackathonStaff.objects.filter(hackathon=self.hack, role='JUDGE').exists())

    def test_a_participant_refusal_does_not_create_an_account_for_a_new_address(self):
        before = User.objects.count()
        with mock.patch('apps.hackathons.judging.StaffService.add', side_effect=HackathonError('No.', 'CONFLICT')):
            with self.assertRaises(HackathonError):
                JudgeInviteService.invite(self.hack, self.admin, 'New Person', 'new@example.org')
        self.assertEqual(User.objects.count(), before)

    def test_inviting_the_same_person_twice_is_refused_until_revoked(self):
        JudgeInviteService.invite(self.hack, self.admin, 'Asha', 'asha@example.org')
        with self.assertRaises(HackathonError) as ctx:
            JudgeInviteService.invite(self.hack, self.admin, 'Asha', 'ASHA@example.org')
        self.assertEqual(ctx.exception.detail['code'], 'ALREADY_INVITED')
        invite = JudgeInvite.objects.get()
        JudgeInviteService.revoke(self.hack, self.admin, invite.id)
        again = JudgeInviteService.invite(self.hack, self.admin, 'Asha', 'asha@example.org')
        self.assertEqual((again['id'], again['state']), (invite.id, 'INVITED'))
        self.assertEqual(JudgeInvite.objects.count(), 1)

    def test_someone_without_the_staff_permission_cannot_hand_out_what_they_lack(self):
        delegate = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=delegate, role='ORGANIZER', permissions=['staff'])
        with self.assertRaises(PermissionDenied):
            JudgeInviteService.invite(self.hack, delegate, 'Asha', 'asha@example.org')
        self.assertFalse(User.objects.filter(email='asha@example.org').exists())


class ManyAtOnce(Base):
    def test_each_row_stands_alone(self):
        make_team(self.hack, make_user(email='lead@example.org'))
        results = JudgeInviteService.invite_many(self.hack, self.admin, [
            {'name': 'Asha Rao', 'email': 'asha@example.org'}, {'name': 'Bad', 'email': 'nope'},
            {'name': 'Lead', 'email': 'lead@example.org'}, {'name': 'Ben Roy', 'email': 'ben@example.org'}, 'junk',
        ])
        self.assertEqual([r['ok'] for r in results], [True, False, False, True, False])
        self.assertEqual(results[1]['code'], 'INVALID')
        self.assertEqual(results[2]['code'], 'CONFLICT')
        self.assertEqual(JudgeInvite.objects.count(), 2)

    def test_the_batch_has_a_limit_and_must_not_be_empty(self):
        for rows in ([], None, [{'name': 'A', 'email': f'a{i}@example.org'} for i in range(51)]):
            with self.assertRaises(HackathonError):
                JudgeInviteService.invite_many(self.hack, self.admin, rows)


class ResendAndRevoke(Base):
    def setUp(self):
        super().setUp()
        JudgeInviteService.invite(self.hack, self.admin, 'Asha Rao', 'asha@example.org')
        self.invite = JudgeInvite.objects.get()
        self.first_token = self.token_in(self.mail()[1])

    def age(self, seconds=61):
        JudgeInvite.objects.update(last_sent_at=timezone.now() - timedelta(seconds=seconds))

    def test_resending_gives_a_new_link_and_kills_the_old_one(self):
        self.age()
        result = JudgeInviteService.resend(self.hack, self.admin, self.invite.id)
        self.assertEqual((result['resend_count'], result['email_sent']), (1, True))
        new_token = self.token_in(self.mail()[1])
        self.assertNotEqual(new_token, self.first_token)
        with self.assertRaises(InvalidSetupTokenError):
            PasswordSetupService.confirm_password_setup(self.first_token, GOOD_PASSWORD)
        PasswordSetupService.confirm_password_setup(new_token, GOOD_PASSWORD)

    def test_resending_waits_a_minute_and_is_capped(self):
        with self.assertRaises(HackathonError) as ctx:
            JudgeInviteService.resend(self.hack, self.admin, self.invite.id)
        self.assertEqual(ctx.exception.detail['code'], 'TOO_SOON')
        for _ in range(judge_invites.MAX_RESENDS):
            self.age()
            JudgeInviteService.resend(self.hack, self.admin, self.invite.id)
        self.age()
        with self.assertRaises(HackathonError) as ctx:
            JudgeInviteService.resend(self.hack, self.admin, self.invite.id)
        self.assertEqual(ctx.exception.detail['code'], 'LIMIT_REACHED')

    def test_a_judge_who_already_set_up_cannot_be_resent_a_link(self):
        PasswordSetupService.confirm_password_setup(self.first_token, GOOD_PASSWORD)
        self.age()
        with self.assertRaises(HackathonError) as ctx:
            JudgeInviteService.resend(self.hack, self.admin, self.invite.id)
        self.assertEqual(ctx.exception.detail['code'], 'ALREADY_ACTIVE')

    def test_revoking_removes_the_judge_and_kills_the_link(self):
        result = JudgeInviteService.revoke(self.hack, self.admin, self.invite.id)
        self.assertEqual(result['state'], 'REVOKED')
        self.assertFalse(HackathonStaff.objects.filter(hackathon=self.hack, role='JUDGE').exists())
        with self.assertRaises(InvalidSetupTokenError):
            PasswordSetupService.confirm_password_setup(self.first_token, GOOD_PASSWORD)
        self.assertTrue(User.objects.filter(email='asha@example.org').exists())  # the account is kept
        with self.assertRaises(HackathonError) as ctx:
            JudgeInviteService.resend(self.hack, self.admin, self.invite.id)
        self.assertEqual(ctx.exception.detail['code'], 'REVOKED')

    def test_an_unknown_invite_is_a_404(self):
        with self.assertRaises(HackathonError) as ctx:
            JudgeInviteService.revoke(self.hack, self.admin, 99999)
        self.assertEqual(ctx.exception.detail['code'], 'NOT_FOUND')

    def test_a_failed_email_does_not_undo_the_invite(self):
        self.age()
        self.send.return_value = False
        result = JudgeInviteService.resend(self.hack, self.admin, self.invite.id)
        self.assertFalse(result['email_sent'])
        self.assertEqual(JudgeInvite.objects.get().resend_count, 1)

    def test_the_list_shows_every_state(self):
        rows = JudgeInviteService.list(self.hack)
        self.assertEqual([(r['email'], r['state']) for r in rows], [('asha@example.org', 'INVITED')])


class TheDataRoundJudgingAdds(Base):
    def test_a_round_is_a_plain_shortlisting_round_unless_asked(self):
        round_obj = Round.objects.create(hackathon=self.hack, order=1, name='Round 1')
        self.assertEqual(
            (round_obj.evaluation_mode, round_obj.evaluation_status, round_obj.assignment_mode, round_obj.scoring_target, round_obj.judges_per_team, round_obj.min_reviews_per_team, round_obj.blind),
            (EvaluationMode.NONE, EvaluationStatus.NOT_STARTED, AssignmentMode.BALANCED, ScoringTarget.PROJECT, 3, 1, False),
        )
        entry = RoundEntry.objects.create(round=round_obj, team=make_team(self.hack, make_user()))
        self.assertEqual((entry.score, entry.review_count, entry.final_rank), (0.0, 0, None))

    def test_a_rounds_own_criteria_never_leak_into_the_hackathon_rubric(self):
        round_obj = Round.objects.create(hackathon=self.hack, order=1, name='Final')
        JudgingCriterion.objects.create(hackathon=self.hack, round=round_obj, target=CriterionTarget.PROJECT, name='Q and A', max_score=10, weight=2)
        self.assertEqual(CriteriaService.seed_defaults(self.hack, self.admin)[0].round_id, None)  # not blocked by the round's criterion
        replaced = CriteriaService.replace(self.hack, self.admin, [{'name': 'Only one', 'max_score': 10, 'weight': 1}])
        self.assertEqual([c.name for c in replaced], ['Only one'])
        self.assertEqual(list(round_obj.criteria.values_list('name', flat=True)), ['Q and A'])  # untouched

    def test_cloning_copies_the_hackathon_rubric_only(self):
        from apps.hackathons.cloning import CloneService
        round_obj = Round.objects.create(hackathon=self.hack, order=1, name='Final')
        JudgingCriterion.objects.create(hackathon=self.hack, round=round_obj, name='Round only', max_score=10, weight=1)
        JudgingCriterion.objects.create(hackathon=self.hack, name='Shared', max_score=10, weight=1)
        copy = CloneService.clone(self.hack, self.admin, title='Copy', slug='copy-of-it', start_date=self.hack.start_date + timedelta(days=365))
        self.assertEqual(list(copy.criteria.values_list('name', flat=True)), ['Shared'])
