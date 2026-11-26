"""Payment steps: a per-person fee (cheaper for club affiliates) the teams shortlisted in a round pay, checked by organizers."""
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient, APITestCase

from apps.forms.tests.test_upload_submission import CopyingStorage
from apps.hackathons.models import (
    EntryStatus, Hackathon, HackathonStaff, IdeaFile, PaymentStep, Round, RoundEntry, StepPayment, TeamPayment,
)
from .helpers import make_hackathon, make_team, make_user

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


@override_settings(FORM_UPLOADS_BACKEND='s3', FORM_UPLOAD_BUCKET='test-bucket')
class StepCase(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.storage = CopyingStorage()
        patcher = mock.patch('apps.forms.upload_storage.get_upload_storage', return_value=self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hack = make_hackathon(min_team_size=1, max_team_size=4, features={'ideas': True, 'submissions': True})
        self.admin = make_user(role='ADMIN')
        self.leader = make_user(club_id='SRKR001', membership_status='ACTIVE')
        self.member_aff = make_user(club_id='SRKR002', membership_status='ACTIVE', role='VOLUNTEER')
        self.member_plain = make_user()
        self.member_alumni = make_user(club_id='SRKR003', membership_status='ALUMNI')
        self.team = make_team(self.hack, self.leader, self.member_aff, self.member_plain, self.member_alumni, name='Alpha')
        self.other = make_team(self.hack, make_user(), name='Beta')       # not shortlisted
        self.round = Round.objects.create(hackathon=self.hack, order=1, name='Idea round', results_published=True)
        RoundEntry.objects.create(round=self.round, team=self.team, status=EntryStatus.SHORTLISTED)
        RoundEntry.objects.create(round=self.round, team=self.other, status=EntryStatus.REJECTED)
        self.base = f'/api/hackathons/{self.hack.slug}'
        reply = api(self.admin).post(f'{self.base}/payment-steps/', {
            'name': 'Phase 2 fee', 'round': self.round.id, 'fee_per_person': 200, 'affiliate_fee_per_person': 100,
            'payment_note': 'UPI: club@upi',
        }, format='json')
        self.assertEqual(reply.status_code, 201, reply.data)
        self.step = PaymentStep.objects.get(pk=reply.data['id'])

    def mine(self, user=None):
        return api(user or self.leader).get(f'{self.base}/my-payment-steps/').data

    def upload(self, user=None):
        client = api(user or self.leader)
        asked = client.post(f'{self.base}/payment-steps/{self.step.id}/files/', {'name': 'paid.png', 'size': len(PNG)}, format='json')
        self.assertEqual(asked.status_code, 201, asked.data)
        file_id = asked.data['file']['id']
        self.storage.put(IdeaFile.objects.get(pk=file_id).key, PNG)
        done = client.post(f'{self.base}/payment-steps/{self.step.id}/files/{file_id}/')
        self.assertEqual(done.status_code, 200, done.data)
        return file_id

    def pay(self, reference='UPI-555', user=None):
        file_id = self.upload(user)
        return api(user or self.leader).post(f'{self.base}/payment-steps/{self.step.id}/pay/', {
            'transaction_id': reference, 'payment_file_id': file_id,
        }, format='json')

    def act(self, payment_id, action, **body):
        return api(self.admin).post(f'{self.base}/step-payments/{payment_id}/{action}/', body, format='json')


class WhoPaysWhat(StepCase):
    def test_the_total_is_each_member_fee_with_affiliates_cheaper(self):
        step = self.mine()[0]
        fees = {m['name'].split()[0]: (m['affiliate'], m['fee']) for m in step['quote']['members']}
        self.assertEqual(sorted(v for v in fees.values()), [(False, 200), (False, 200), (True, 100), (True, 100)])
        self.assertEqual(step['quote']['total'], 600)  # two affiliates (any role) + a plain member + an alumnus
        self.assertEqual((step['payment_note'], step['is_leader'], step['can_pay']), ('UPI: club@upi', True, True))

    def test_only_shortlisted_teams_of_a_published_round_are_asked(self):
        self.assertEqual(self.mine(self.other.leader), [])
        Round.objects.filter(pk=self.round.pk).update(results_published=False)
        self.assertEqual(self.mine(), [])

    def test_members_see_it_but_only_the_leader_pays(self):
        step = self.mine(self.member_plain)[0]
        self.assertEqual((step['is_leader'], step['can_pay']), (False, False))
        reply = api(self.member_plain).post(f'{self.base}/payment-steps/{self.step.id}/files/', {'name': 'p.png', 'size': 10}, format='json')
        self.assertEqual(reply.status_code, 403)

    def test_the_affiliate_fee_cannot_be_above_the_normal_fee(self):
        reply = api(self.admin).patch(f'{self.base}/payment-steps/{self.step.id}/', {'affiliate_fee_per_person': 300}, format='json')
        self.assertEqual(reply.data['field'], 'affiliate_fee_per_person')


class Paying(StepCase):
    def test_the_leader_pays_and_the_amount_is_kept_with_the_payment(self):
        reply = self.pay()
        self.assertEqual(reply.status_code, 201, reply.data)
        payment = StepPayment.objects.get()
        self.assertEqual((payment.amount, payment.status, len(payment.breakdown)), (600, 'PENDING', 4))
        self.step.fee_per_person = 500
        self.step.save()
        self.assertEqual(StepPayment.objects.get().amount, 600)
        self.assertEqual(self.mine()[0]['can_pay'], False)

    def test_paying_twice_or_reusing_a_reference_is_refused(self):
        self.pay()
        reply = api(self.leader).post(f'{self.base}/payment-steps/{self.step.id}/files/', {'name': 'p.png', 'size': 10}, format='json')
        self.assertEqual(reply.data['code'], 'ALREADY_PAID')
        TeamPayment.objects.create(team=self.other, hackathon=self.hack, amount=100, transaction_id='REG-1')
        StepPayment.objects.all().update(status='REJECTED')
        self.assertEqual(self.pay('reg-1').data['code'], 'TRANSACTION_REUSED')

    def test_a_transaction_id_and_a_screenshot_are_needed(self):
        reply = api(self.leader).post(f'{self.base}/payment-steps/{self.step.id}/pay/', {'transaction_id': ''}, format='json')
        self.assertEqual(reply.data['field'], 'transaction_id')
        reply = api(self.leader).post(f'{self.base}/payment-steps/{self.step.id}/pay/', {'transaction_id': 'X1'}, format='json')
        self.assertEqual(reply.data['field'], 'payment_file_id')


class Review(StepCase):
    def setUp(self):
        super().setUp()
        self.pay()
        self.payment = StepPayment.objects.get()

    def test_the_queue_lists_the_team_with_its_breakdown(self):
        data = api(self.admin).get(f'{self.base}/payment-steps/{self.step.id}/queue/').data
        self.assertEqual([row['team_name'] for row in data['rows']], ['Alpha'])
        row = data['rows'][0]
        self.assertEqual((row['payment']['status'], row['payment']['amount'], row['quote']['total']), ('PENDING', 600, 600))
        self.assertEqual(data['step']['counts'], {'due': 1, 'unpaid': 0, 'pending': 1, 'verified': 0, 'rejected': 0})

    def test_rejecting_needs_a_reason_the_leader_sees_and_lets_them_pay_again(self):
        self.assertEqual(self.act(self.payment.id, 'reject').data['field'], 'reason')
        self.assertEqual(self.act(self.payment.id, 'reject', reason='Amount does not match.').status_code, 200)
        step = self.mine()[0]
        self.assertEqual((step['payment']['status'], step['payment']['reason'], step['can_pay']), ('REJECTED', 'Amount does not match.', True))
        self.assertEqual(self.pay('UPI-556').status_code, 201)
        self.assertEqual(StepPayment.objects.count(), 2)

    def test_a_payment_is_handled_once(self):
        self.act(self.payment.id, 'verify')
        self.assertEqual(self.act(self.payment.id, 'reject', reason='x').data['code'], 'PAYMENT_ALREADY_HANDLED')

    def test_only_organizers_with_the_teams_permission_check_payments(self):
        judge = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=judge, role='JUDGE', permissions=['judge'])
        reply = api(judge).post(f'{self.base}/step-payments/{self.payment.id}/verify/', {}, format='json')
        self.assertEqual(reply.status_code, 403)
        self.assertEqual(api(self.leader).get(f'{self.base}/payment-steps/{self.step.id}/queue/').status_code, 403)

    def test_the_screenshot_is_for_organizers_and_the_leader(self):
        url = f'{self.base}/step-payments/{self.payment.id}/screenshot/'
        self.assertEqual(api(self.admin).get(url).status_code, 200)
        self.assertEqual(api(self.leader).get(url).status_code, 200)
        self.assertEqual(api(self.member_plain).get(url).status_code, 404)


class Locks(StepCase):
    def test_members_are_locked_while_a_payment_waits_or_is_verified(self):
        Hackathon.objects.filter(pk=self.hack.pk).update(registration_closes_at=None)
        self.pay()
        reply = api(self.leader).post(f'/api/hackathons/teams/{self.team.id}/remove-member/', {'user_id': self.member_plain.id}, format='json')
        self.assertEqual(reply.data.get('code'), 'TEAM_PAID_LOCKED', reply.data)
        reply = api(self.member_plain).post(f'/api/hackathons/teams/{self.team.id}/leave/', {}, format='json')
        self.assertEqual(reply.data.get('code'), 'TEAM_PAID_LOCKED', reply.data)
        # organizers can still change members
        reply = api(self.admin).post(f'/api/hackathons/teams/{self.team.id}/remove-member/', {'user_id': self.member_plain.id}, format='json')
        self.assertEqual(reply.status_code, 200, reply.data)

    def test_a_rejected_payment_unlocks_the_team(self):
        self.pay()
        self.act(StepPayment.objects.get().id, 'reject', reason='Blurry screenshot.')
        from apps.hackathons import payment_steps
        self.assertFalse(payment_steps.roster_locked(self.team))

    def test_the_project_phase_waits_for_a_verified_payment(self):
        gate = lambda: api(self.leader).get(f'{self.base}/my-submission/').data['gate']  # noqa: E731
        self.assertIn('Pay the Phase 2 fee first', gate()['reason'])
        self.pay()
        self.assertIn('waiting for the organizers', gate()['reason'])
        self.act(StepPayment.objects.get().id, 'verify')
        self.assertTrue(gate()['satisfied'])

    def test_a_step_that_is_not_required_never_locks_projects(self):
        self.step.required_for_projects = False
        self.step.save()
        self.assertTrue(api(self.leader).get(f'{self.base}/my-submission/').data['gate']['satisfied'])


class Steps(StepCase):
    def test_a_step_with_payments_is_switched_off_not_deleted(self):
        self.pay()
        self.act(StepPayment.objects.get().id, 'verify')
        self.assertEqual(api(self.admin).delete(f'{self.base}/payment-steps/{self.step.id}/').data['outcome'], 'deactivated')
        self.assertEqual(self.mine(), [])
        fresh = PaymentStep.objects.create(hackathon=self.hack, name='Later', fee_per_person=1, affiliate_fee_per_person=1)
        self.assertEqual(api(self.admin).delete(f'{self.base}/payment-steps/{fresh.id}/').data['outcome'], 'deleted')

    def test_setting_up_steps_needs_the_settings_permission(self):
        teams_only = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=teams_only, role='ORGANIZER', permissions=['teams'])
        reply = api(teams_only).post(f'{self.base}/payment-steps/', {'name': 'x', 'fee_per_person': 1, 'affiliate_fee_per_person': 1}, format='json')
        self.assertEqual(reply.status_code, 403)
        self.assertEqual(api(teams_only).get(f'{self.base}/payment-steps/').status_code, 200)

    def test_reminders_reach_only_teams_that_have_not_paid(self):
        from apps.notifications.models import Notification
        with mock.patch('apps.hackathons.services.AnnouncementService.notify', return_value=0), self.captureOnCommitCallbacks(execute=True):
            reply = api(self.admin).post(f'{self.base}/payment-steps/{self.step.id}/remind/', {}, format='json')
        self.assertEqual(reply.data['reminded'], 1)
        self.assertTrue(Notification.objects.filter(recipient=self.leader, title__contains='is due').exists())
        self.assertFalse(Notification.objects.filter(recipient=self.other.leader, title__contains='is due').exists())
        self.pay()
        reply = api(self.admin).post(f'{self.base}/payment-steps/{self.step.id}/remind/', {}, format='json')
        self.assertEqual(reply.data['reminded'], 0)
        from apps.hackathons.models import HackathonAnnouncement
        note = HackathonAnnouncement.objects.get(title='Phase 2 fee is due')
        self.assertEqual((note.send_email, note.is_active), (True, False))  # emailed, but not left on the dashboard
        titles = [a['title'] for a in api(self.leader).get(f'{self.base}/announcements/').data]
        self.assertNotIn('Phase 2 fee is due', titles)


class ExportAndOverview(StepCase):
    def test_the_export_lists_each_team_that_has_to_pay(self):
        import csv
        import io
        self.pay()
        reply = api(self.admin).get(f'{self.base}/export/step-payments/', {'export_format': 'csv', 'step': self.step.id})
        rows = list(csv.reader(io.StringIO(reply.content.decode('utf-8-sig'))))
        header, row = rows[0], rows[1]
        record = dict(zip(header, row))
        self.assertEqual((record['Team'], record['Affiliates'], record['Amount Due'], record['Payment Status']), ('Alpha', '2', '600', 'PENDING'))
        self.assertEqual(api(self.admin).get(f'{self.base}/export/step-payments/', {'export_format': 'csv'}).status_code, 400)

    def test_the_overview_counts_payments_waiting_for_a_check(self):
        self.pay()
        data = api(self.admin).get(f'{self.base}/overview/').data['step_payments']
        self.assertEqual((data['pending'], data['steps'][0]['name']), (1, 'Phase 2 fee'))


class ReviewFixes(StepCase):
    def test_a_zero_total_asks_nothing(self):
        self.step.fee_per_person = self.step.affiliate_fee_per_person = 0
        self.step.save()
        self.assertEqual(self.mine(), [])
        self.assertTrue(api(self.leader).get(f'{self.base}/my-submission/').data['gate']['satisfied'])

    def test_a_waiting_payment_stays_in_the_queue_after_the_team_stops_owing(self):
        self.pay()
        RoundEntry.objects.filter(round=self.round, team=self.team).update(status=EntryStatus.REJECTED)
        data = api(self.admin).get(f'{self.base}/payment-steps/{self.step.id}/queue/').data
        row = data['rows'][0]
        self.assertEqual((row['team_name'], row['due'], row['payment']['status']), ('Alpha', False, 'PENDING'))
        self.assertEqual(data['step']['counts']['pending'], 1)

    def test_a_step_with_payments_waiting_cannot_be_switched_off(self):
        self.pay()
        self.assertEqual(api(self.admin).delete(f'{self.base}/payment-steps/{self.step.id}/').data['code'], 'PAYMENTS_WAITING')
        reply = api(self.admin).patch(f'{self.base}/payment-steps/{self.step.id}/', {'is_active': False}, format='json')
        self.assertEqual(reply.data['code'], 'PAYMENTS_WAITING')

    def test_paying_again_with_the_same_reference_is_not_flagged_as_a_duplicate(self):
        self.pay('UPI-777')
        self.act(StepPayment.objects.get().id, 'reject', reason='Blurry screenshot.')
        self.assertEqual(self.pay('UPI-777').status_code, 201)
        rows = api(self.admin).get(f'{self.base}/payment-steps/{self.step.id}/queue/').data['rows']
        self.assertFalse(rows[0]['payment']['duplicate_reference'])

    def test_the_queue_does_not_query_per_team(self):
        for index in range(5):
            team = make_team(self.hack, make_user(), make_user(), name=f'Extra {index}')
            RoundEntry.objects.create(round=self.round, team=team, status=EntryStatus.SHORTLISTED)
        url = f'{self.base}/payment-steps/{self.step.id}/queue/'
        api(self.admin).get(url)
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as few:
            api(self.admin).get(url)
        for index in range(5, 15):
            team = make_team(self.hack, make_user(), make_user(), name=f'Extra {index}')
            RoundEntry.objects.create(round=self.round, team=team, status=EntryStatus.SHORTLISTED)
        with CaptureQueriesContext(connection) as many:
            api(self.admin).get(url)
        self.assertLessEqual(len(many) - len(few), 2, 'queries grow with the number of teams')
