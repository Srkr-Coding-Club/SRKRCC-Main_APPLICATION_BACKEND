"""Registration payment, step 1: the stage setting, the two payment team statuses and the TeamPayment record."""
from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.hackathons import registration
from apps.hackathons.models import PaymentStage, PaymentStatus, Team, TeamPayment, TeamStatus, payment_key
from apps.hackathons.services import ACTIVE_TEAM_STATUSES, TeamService
from .helpers import make_hackathon, make_team, make_user

HOUR = timedelta(hours=1)


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


def _pay(team, transaction_id='UPI 123 456', **kw):
    return TeamPayment.objects.create(team=team, hackathon=team.hackathon, amount=200, transaction_id=transaction_id, **kw)


class PaymentModelTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1)
        self.team = make_team(self.hack, make_user(), name='Alpha')

    def test_new_hackathons_have_no_payment_stage(self):
        self.assertEqual((self.hack.payment_stage, self.hack.idea_fee_amount), (PaymentStage.NONE, 0))

    def test_the_payment_statuses_hold_no_seat(self):
        self.assertNotIn(TeamStatus.PAYMENT_PENDING, ACTIVE_TEAM_STATUSES)
        self.assertNotIn(TeamStatus.PAYMENT_REJECTED, ACTIVE_TEAM_STATUSES)
        self.hack.max_teams = 1
        self.hack.save()
        for status in (TeamStatus.PAYMENT_PENDING, TeamStatus.PAYMENT_REJECTED):
            Team.objects.filter(pk=self.team.pk).update(status=status)
            self.assertEqual(registration.placed_teams(self.hack).count(), 0, status)
            self.assertFalse(registration.teams_full(self.hack), status)
            self.assertEqual(registration.placed_participants(self.hack), 0, status)

    def test_the_status_values_fit_the_column(self):
        longest = max(len(value) for value in TeamStatus.values)
        self.assertLessEqual(longest, Team._meta.get_field('status').max_length)

    def test_the_reference_is_compared_without_spaces_or_case(self):
        self.assertEqual(payment_key(' UPI  Ref\t123 '), 'upiref123')
        self.assertEqual(payment_key(None), '')
        payment = _pay(self.team, 'AbC 12')
        self.assertEqual(payment.transaction_key, 'abc12')
        payment.transaction_id = 'ZZ 9'
        payment.save(update_fields=['transaction_id'])
        payment.refresh_from_db()
        self.assertEqual(payment.transaction_key, 'zz9')

    def test_a_reference_can_be_used_once_per_hackathon(self):
        _pay(self.team, 'UPI 123')
        other = make_team(self.hack, make_user(), name='Beta')
        with self.assertRaises(IntegrityError), transaction.atomic():
            _pay(other, 'upi123')
        elsewhere = make_hackathon(min_team_size=1)
        _pay(make_team(elsewhere, make_user(), name='Gamma'), 'upi123')  # another hackathon is fine

    def test_a_team_has_one_payment_and_new_ones_start_pending(self):
        payment = _pay(self.team)
        self.assertEqual(payment.status, PaymentStatus.PENDING)
        self.assertEqual(self.team.payment, payment)
        with self.assertRaises(IntegrityError), transaction.atomic():
            _pay(self.team, 'another-ref')

    def test_deleting_the_team_keeps_the_payment_record_for_the_money_to_be_settled(self):
        payment = _pay(self.team, team_name='Alpha', leader_email='asha@x.io')
        self.team.delete()
        payment.refresh_from_db()
        self.assertEqual((payment.team_id, payment.team_name, payment.leader_email), (None, 'Alpha', 'asha@x.io'))

    def test_a_cancelled_or_removed_payment_frees_its_reference(self):
        first = _pay(self.team, 'UPI 9')
        first.status = PaymentStatus.CANCELLED
        first.save()
        again = _pay(make_team(self.hack, make_user(), name='Beta'), 'upi9')
        self.assertEqual(again.status, PaymentStatus.PENDING)
        with self.assertRaises(IntegrityError), transaction.atomic():
            _pay(make_team(self.hack, make_user(), name='Gamma'), 'UPI9')

    def test_an_organizer_cannot_move_a_team_into_or_out_of_the_payment_statuses(self):
        admin = make_user(role='ADMIN')
        for start, target in ((TeamStatus.REGISTERED, TeamStatus.PAYMENT_PENDING), (TeamStatus.PAYMENT_PENDING, TeamStatus.REGISTERED)):
            Team.objects.filter(pk=self.team.pk).update(status=start)
            self.team.refresh_from_db()
            from apps.hackathons.errors import HackathonError
            with self.assertRaises(HackathonError) as caught:
                TeamService.admin_set_status(self.team, admin, target)
            self.assertEqual(caught.exception.detail['code'], 'USE_PAYMENTS')


class PaymentStageSettingTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1)
        self.admin = make_user(role='ADMIN')
        self.url = f'/api/hackathons/{self.hack.slug}/'

    def patch(self, body):
        return _api(self.admin).patch(self.url, body, format='json')

    @override_settings(FORM_UPLOADS_BACKEND='s3', FORM_UPLOAD_BUCKET='test-bucket')
    def test_the_stage_and_fee_are_saved_and_returned(self):
        reply = self.patch({'payment_stage': 'REGISTRATION', 'idea_fee_amount': 250})
        self.assertEqual((reply.status_code, reply.data['payment_stage'], reply.data['idea_fee_amount']), (200, 'REGISTRATION', 250))
        self.assertEqual(_api(self.admin).get(self.url).data['payment_stage'], 'REGISTRATION')

    def test_an_unknown_stage_is_refused(self):
        self.assertIn('payment_stage', self.patch({'payment_stage': 'LATER'}).data)

    @override_settings(FORM_UPLOADS_BACKEND='s3', FORM_UPLOAD_BUCKET='test-bucket')
    def test_a_fee_set_without_a_stage_still_means_with_the_idea(self):
        reply = self.patch({'idea_fee_amount': 150})
        self.assertEqual((reply.status_code, reply.data['payment_stage']), (200, 'IDEA'))

    @override_settings(FORM_UPLOADS_BACKEND='s3', FORM_UPLOAD_BUCKET='test-bucket')
    def test_a_chosen_stage_is_not_overridden_by_the_fee(self):
        reply = self.patch({'payment_stage': 'REGISTRATION', 'idea_fee_amount': 150})
        self.assertEqual(reply.data['payment_stage'], 'REGISTRATION')

    @override_settings(FORM_UPLOADS_BACKEND='inline')
    def test_a_fee_needs_file_uploads_to_be_available_at_any_stage(self):
        self.assertIn('idea_fee_amount', self.patch({'payment_stage': 'REGISTRATION', 'idea_fee_amount': 300}).data)
        self.assertEqual(self.patch({'payment_stage': 'REGISTRATION', 'idea_fee_amount': 0}).status_code, 200)

    @override_settings(FORM_UPLOADS_BACKEND='s3', FORM_UPLOAD_BUCKET='test-bucket')
    def test_the_stage_cannot_leave_registration_while_payments_wait(self):
        self.patch({'payment_stage': 'REGISTRATION', 'idea_fee_amount': 250})
        team = make_team(self.hack, make_user(), name='Waiting')
        Team.objects.filter(pk=team.pk).update(status=TeamStatus.PAYMENT_PENDING)
        blocked = self.patch({'payment_stage': 'IDEA'})
        self.assertEqual(blocked.status_code, 400)
        self.assertIn('1 team is waiting', blocked.data['payment_stage'][0])
        self.assertEqual(self.patch({'payment_stage': 'REGISTRATION', 'idea_fee_amount': 300}).status_code, 200)
        Team.objects.filter(pk=team.pk).update(status=TeamStatus.REGISTERED)
        self.assertEqual(self.patch({'payment_stage': 'IDEA'}).status_code, 200)

    @override_settings(FORM_UPLOADS_BACKEND='s3', FORM_UPLOAD_BUCKET='test-bucket')
    def test_rejected_teams_do_not_block_a_stage_change(self):
        self.patch({'payment_stage': 'REGISTRATION', 'idea_fee_amount': 250})
        team = make_team(self.hack, make_user(), name='Turned down')
        Team.objects.filter(pk=team.pk).update(status=TeamStatus.PAYMENT_REJECTED)
        self.assertEqual(self.patch({'payment_stage': 'NONE'}).status_code, 200)

    @override_settings(FORM_UPLOADS_BACKEND='s3', FORM_UPLOAD_BUCKET='test-bucket')
    def test_the_stage_is_copied_when_a_hackathon_is_cloned(self):
        self.patch({'payment_stage': 'REGISTRATION', 'idea_fee_amount': 250})
        reply = _api(self.admin).post(f'{self.url}clone/', {'title': 'Next year', 'start_date': (timezone.now() + 30 * HOUR).isoformat()}, format='json')
        self.assertEqual(reply.status_code, 201, reply.data)
        copy = type(self.hack).objects.get(slug=reply.data['slug'])
        self.assertEqual((copy.payment_stage, copy.idea_fee_amount), (PaymentStage.REGISTRATION, 250))

    def test_the_idea_form_shows_a_fee_only_when_it_is_paid_with_the_idea(self):
        from apps.hackathons.views_ops import _idea_config

        type(self.hack).objects.filter(pk=self.hack.pk).update(idea_fee_amount=200, payment_stage='REGISTRATION')
        self.hack.refresh_from_db()
        self.assertEqual(_idea_config(self.hack)['fee_amount'], 0)
        type(self.hack).objects.filter(pk=self.hack.pk).update(payment_stage='IDEA')
        self.hack.refresh_from_db()
        self.assertEqual(_idea_config(self.hack)['fee_amount'], 200)


class PaymentStageMigrationTests(TransactionTestCase):
    """Hackathons that already charge a fee with the idea keep doing so after the migration."""

    before = [('hackathons', '0033_team_payments')]
    after = [('hackathons', '0034_payment_stage_for_existing_fees')]

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_only_hackathons_with_a_fee_get_the_idea_stage(self):
        executor = MigrationExecutor(connection)
        accounts = executor.loader.graph.leaf_nodes('accounts')
        executor.migrate(self.before + accounts)
        old = executor.loader.project_state(self.before + accounts).apps.get_model('hackathons', 'Hackathon')
        start = timezone.now() + 5 * HOUR

        def hackathon(slug, **kw):
            return old.objects.create(title=slug, slug=slug, theme='t', description='d', start_date=start, end_date=start + HOUR, **kw)

        hackathon('paid', idea_fee_amount=200)
        hackathon('free')

        executor = MigrationExecutor(connection)
        executor.migrate(self.after + accounts)
        new = executor.loader.project_state(self.after + accounts).apps.get_model('hackathons', 'Hackathon')
        self.assertEqual(dict(new.objects.values_list('slug', 'payment_stage')), {'paid': 'IDEA', 'free': 'NONE'})
