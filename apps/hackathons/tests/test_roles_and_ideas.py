"""Per-hackathon organizer roles and permissions, and the two-phase (idea, then project) submission flow."""
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.hackathons.models import Hackathon, HackathonStaff, IdeaSubmission, Submission, TeamStatus
from .helpers import make_hackathon, make_ps, make_team, make_user

HOUR = timezone.timedelta(hours=1)


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


def _staff(hackathon, user, role, permissions=None):
    return HackathonStaff.objects.create(hackathon=hackathon, user=user, role=role, permissions=permissions or [])


class OrganizerRoleTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, status='DRAFT')
        self.other = make_hackathon(min_team_size=1)
        self.organizer = make_user()
        _staff(self.hack, self.organizer, 'ORGANIZER')
        self.base = f'/api/hackathons/{self.hack.slug}'

    def test_organizer_sees_and_edits_only_their_hackathon(self):
        client = _api(self.organizer)
        slugs = [h['slug'] for h in client.get('/api/hackathons/').data]
        self.assertIn(self.hack.slug, slugs)  # a draft, visible to its own staff
        self.assertEqual(client.patch(f'{self.base}/', {'theme': 'New theme'}, format='json').status_code, 200)
        self.assertEqual(client.patch(f'/api/hackathons/{self.other.slug}/', {'theme': 'x'}, format='json').status_code, 403)
        self.assertEqual(_api(make_user()).get(f'{self.base}/').status_code, 404)

    def test_organizer_cannot_create_delete_or_clone(self):
        client = _api(self.organizer)
        payload = {'title': 'New', 'slug': 'brand-new', 'theme': 't', 'description': 'd',
                   'start_date': (timezone.now() + 5 * HOUR).isoformat(), 'end_date': (timezone.now() + 9 * HOUR).isoformat()}
        self.assertEqual(client.post('/api/hackathons/', payload, format='json').status_code, 403)
        self.assertEqual(client.delete(f'{self.base}/?confirm_slug={self.hack.slug}').status_code, 403)
        self.assertEqual(client.post(f'{self.base}/clone/', {'title': 'c', 'start_date': timezone.now().isoformat()}, format='json').status_code, 403)

    def test_preset_covers_content_teams_and_insights_but_not_staff(self):
        client = _api(self.organizer)
        self.assertEqual(client.get(f'{self.base}/teams/').status_code, 200)
        self.assertEqual(client.get(f'{self.base}/stats/').status_code, 200)
        self.assertEqual(client.get(f'{self.base}/analytics/').status_code, 200)
        self.assertEqual(client.get(f'{self.base}/staff/').status_code, 403)
        self.assertEqual(client.get(f'/api/hackathons/{self.other.slug}/teams/').status_code, 403)

    def test_permissions_can_be_narrowed(self):
        HackathonStaff.objects.filter(user=self.organizer).update(permissions=['content'])
        client = _api(self.organizer)
        self.assertEqual(client.get(f'{self.base}/teams/').status_code, 403)
        self.assertEqual(client.get(f'{self.base}/stats/').status_code, 403)
        self.assertEqual(client.patch(f'{self.base}/', {'theme': 'x'}, format='json').status_code, 403)
        self.assertEqual(client.post(f'{self.base}/problem-statements/', {'title': 'P', 'description': 'd', 'domain': 'AI'}, format='json').status_code, 201)

    def test_organizer_with_teams_permission_manages_teams(self):
        team = make_team(self.hack, make_user(), name='Managed')
        resp = _api(self.organizer).post(f'/api/hackathons/teams/{team.id}/admin-set-status/', {'status': 'DISQUALIFIED', 'reason': 'Plagiarism'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        team.refresh_from_db()
        self.assertEqual((team.status, team.status_reason), (TeamStatus.DISQUALIFIED, 'Plagiarism'))
        foreign = make_team(self.other, make_user(), name='Foreign')
        self.assertEqual(_api(self.organizer).post(f'/api/hackathons/teams/{foreign.id}/admin-set-status/', {'status': 'WITHDRAWN'}, format='json').status_code, 403)

    def test_my_permissions_reports_roles_and_catalog(self):
        data = _api(self.organizer).get(f'{self.base}/my-permissions/').data
        self.assertEqual(data['roles'], ['ORGANIZER'])
        self.assertIn('settings', data['permissions'])
        self.assertNotIn('staff', data['permissions'])
        self.assertIn('judge', [item['key'] for item in data['catalog']])
        admin = _api(make_user(role='ADMIN')).get(f'{self.base}/my-permissions/').data
        self.assertTrue(admin['is_site_organizer'])
        self.assertIn('staff', admin['permissions'])


class StaffManagementTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1)
        self.admin = make_user(role='ADMIN')
        self.base = f'/api/hackathons/{self.hack.slug}/staff/'

    def test_admin_adds_an_organizer_with_the_preset_then_narrows_it(self):
        person = make_user()
        resp = _api(self.admin).post(self.base, {'email': person.email, 'role': 'ORGANIZER'}, format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIn('settings', resp.data['permissions'])
        patched = _api(self.admin).patch(f"{self.base}{resp.data['id']}/", {'permissions': ['content', 'insights']}, format='json')
        self.assertEqual(patched.data['permissions'], ['content', 'insights'])
        bad = _api(self.admin).patch(f"{self.base}{resp.data['id']}/", {'permissions': ['teleport']}, format='json')
        self.assertEqual(bad.status_code, 400)
        empty = _api(self.admin).patch(f"{self.base}{resp.data['id']}/", {'permissions': []}, format='json')
        self.assertEqual(empty.status_code, 400)

    def test_a_delegate_cannot_grant_more_than_they_hold_or_edit_themselves(self):
        delegate = make_user()
        _staff(self.hack, delegate, 'ORGANIZER', ['staff', 'content', 'judge'])
        target = make_user()
        client = _api(delegate)
        too_much = client.post(self.base, {'email': target.email, 'role': 'ORGANIZER', 'permissions': ['content', 'results']}, format='json')
        self.assertEqual(too_much.status_code, 403)
        ok = client.post(self.base, {'email': target.email, 'role': 'ORGANIZER', 'permissions': ['content']}, format='json')
        self.assertEqual(ok.status_code, 201, ok.data)
        own = HackathonStaff.objects.get(user=delegate)
        self.assertEqual(client.patch(f'{self.base}{own.id}/', {'permissions': ['staff', 'content']}, format='json').status_code, 403)
        # cannot change or remove someone who outranks them
        boss = make_user()
        boss_row = _staff(self.hack, boss, 'ORGANIZER')
        self.assertEqual(client.delete(f'{self.base}{boss_row.id}/').status_code, 403)
        self.assertEqual(client.patch(f'{self.base}{boss_row.id}/', {'permissions': ['content']}, format='json').status_code, 403)

    def test_participants_cannot_be_given_the_judge_permission(self):
        member = make_user()
        make_team(self.hack, member)
        resp = _api(self.admin).post(self.base, {'email': member.email, 'role': 'ORGANIZER', 'permissions': ['judge', 'content']}, format='json')
        self.assertEqual(resp.data['code'], 'CONFLICT')

    def test_judge_preset_scores_but_cannot_see_the_leaderboard(self):
        self.hack.features = {'judging': True}
        self.hack.save()
        judge = make_user()
        _staff(self.hack, judge, 'JUDGE')
        client = _api(judge)
        self.assertEqual(client.get(f'/api/hackathons/{self.hack.slug}/judging/').status_code, 200)
        self.assertEqual(client.get(f'/api/hackathons/{self.hack.slug}/leaderboard/').status_code, 403)
        self.assertEqual(client.put(f'/api/hackathons/{self.hack.slug}/criteria/', {'criteria': []}, format='json').status_code, 403)

    def test_legacy_rows_without_stored_permissions_fall_back_to_their_preset(self):
        self.hack.features = {'check_in': True}
        self.hack.save()
        volunteer = make_user()
        _staff(self.hack, volunteer, 'VOLUNTEER', [])
        self.assertEqual(_api(volunteer).get(f'/api/hackathons/{self.hack.slug}/check-in/').status_code, 200)

    def test_removing_staff_ends_access_immediately(self):
        person = make_user()
        row = _staff(self.hack, person, 'ORGANIZER')
        self.assertEqual(_api(person).get(f'/api/hackathons/{self.hack.slug}/stats/').status_code, 200)
        self.assertEqual(_api(self.admin).delete(f'{self.base}{row.id}/').status_code, 200)
        self.assertEqual(_api(person).get(f'/api/hackathons/{self.hack.slug}/stats/').status_code, 403)


class IdeaPhaseTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, features={'ideas': True, 'submissions': True})
        self.leader, self.member = make_user(), make_user()
        self.team = make_team(self.hack, self.leader, self.member)
        self.admin = make_user(role='ADMIN')
        self.idea_url = f'/api/hackathons/{self.hack.slug}/my-idea/'
        self.project_url = f'/api/hackathons/{self.hack.slug}/my-submission/'
        self.idea = {'title': 'Smart bins', 'problem': 'Campus waste is not sorted, so recyclables reach landfill.', 'solution': 'Sensors at each bin and an app that tells the cleaners what is full.', 'plan': 'Week 1 build.'}
        self.project = {'project_title': 'Smart bins', 'description': 'We built the bins, the sensor firmware and the cleaners app.', 'repo_url': 'https://github.com/x/bins'}

    def test_leader_submits_and_edits_until_it_is_decided(self):
        resp = _api(self.leader).put(self.idea_url, self.idea, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['idea']['status'], 'SUBMITTED')
        self.idea['title'] = 'Smarter bins'
        self.assertEqual(_api(self.leader).put(self.idea_url, self.idea, format='json').data['idea']['title'], 'Smarter bins')
        self.assertEqual(_api(self.member).put(self.idea_url, self.idea, format='json').status_code, 403)

    def test_missing_fields_and_bad_links_are_rejected_per_field(self):
        resp = _api(self.leader).put(self.idea_url, {**self.idea, 'problem': ''}, format='json')
        self.assertEqual((resp.status_code, resp.data['field']), (400, 'problem'))
        resp = _api(self.leader).put(self.idea_url, {**self.idea, 'deck_url': 'nope'}, format='json')
        self.assertEqual(resp.data['field'], 'deck_url')

    def test_window_and_feature_switch_are_enforced(self):
        self.hack.idea_deadline = timezone.now() - HOUR
        self.hack.save()
        self.assertEqual(_api(self.leader).put(self.idea_url, self.idea, format='json').data['code'], 'IDEAS_CLOSED')
        self.hack.idea_deadline, self.hack.idea_opens_at = None, timezone.now() + HOUR
        self.hack.save()
        self.assertEqual(_api(self.leader).put(self.idea_url, self.idea, format='json').data['code'], 'IDEAS_NOT_OPEN')
        self.hack.features = {}
        self.hack.save()
        self.assertEqual(_api(self.leader).put(self.idea_url, self.idea, format='json').data['code'], 'FEATURE_OFF')

    def test_review_flow_changes_requested_then_resubmit_then_approve_locks_it(self):
        _api(self.leader).put(self.idea_url, self.idea, format='json')
        idea = IdeaSubmission.objects.get(team=self.team)
        review = f'/api/hackathons/{self.hack.slug}/ideas/{idea.id}/review/'
        self.assertEqual(_api(self.leader).post(review, {'status': 'APPROVED'}, format='json').status_code, 403)
        self.assertEqual(_api(self.admin).post(review, {'status': 'CHANGES_REQUESTED'}, format='json').data['code'], 'FEEDBACK_REQUIRED')
        self.assertEqual(_api(self.admin).post(review, {'status': 'CHANGES_REQUESTED', 'feedback': 'Add a timeline'}, format='json').status_code, 200)
        state = _api(self.leader).get(self.idea_url).data
        self.assertEqual((state['idea']['status'], state['idea']['feedback'], state['can_edit']), ('CHANGES_REQUESTED', 'Add a timeline', True))
        self.assertEqual(_api(self.leader).put(self.idea_url, self.idea, format='json').data['idea']['status'], 'SUBMITTED')
        _api(self.admin).post(review, {'status': 'APPROVED'}, format='json')
        locked = _api(self.leader).put(self.idea_url, self.idea, format='json')
        self.assertEqual(locked.data['code'], 'IDEA_LOCKED')
        self.assertFalse(_api(self.leader).get(self.idea_url).data['can_edit'])

    def test_ideas_are_approved_automatically_when_review_is_off(self):
        self.hack.idea_review_required = False
        self.hack.save()
        self.assertEqual(_api(self.leader).put(self.idea_url, self.idea, format='json').data['idea']['status'], 'APPROVED')

    def test_project_phase_can_require_an_approved_idea(self):
        self.hack.project_requires_approved_idea = True
        self.hack.save()
        blocked = _api(self.leader).put(self.project_url, self.project, format='json')
        self.assertEqual(blocked.data['code'], 'IDEA_REQUIRED')
        self.assertFalse(_api(self.leader).get(self.project_url).data['gate']['satisfied'])
        _api(self.leader).put(self.idea_url, self.idea, format='json')
        pending = _api(self.leader).put(self.project_url, self.project, format='json')
        self.assertEqual(pending.data['code'], 'IDEA_REQUIRED')
        idea = IdeaSubmission.objects.get(team=self.team)
        _api(self.admin).post(f'/api/hackathons/{self.hack.slug}/ideas/{idea.id}/review/', {'status': 'APPROVED'}, format='json')
        self.assertEqual(_api(self.leader).put(self.project_url, self.project, format='json').status_code, 200)
        self.assertEqual(Submission.objects.filter(team=self.team).count(), 1)

    def test_a_rejected_idea_keeps_the_team_out_of_the_project_phase(self):
        self.hack.project_requires_approved_idea = True
        self.hack.save()
        _api(self.leader).put(self.idea_url, self.idea, format='json')
        idea = IdeaSubmission.objects.get(team=self.team)
        _api(self.admin).post(f'/api/hackathons/{self.hack.slug}/ideas/{idea.id}/review/', {'status': 'REJECTED', 'feedback': 'Out of scope'}, format='json')
        self.assertEqual(_api(self.leader).put(self.project_url, self.project, format='json').data['code'], 'IDEA_REQUIRED')

    def test_without_the_gate_projects_do_not_need_an_idea(self):
        self.assertEqual(_api(self.leader).put(self.project_url, self.project, format='json').status_code, 200)

    def test_admin_list_reminders_and_scoped_access(self):
        base = f'/api/hackathons/{self.hack.slug}/ideas/'
        data = _api(self.admin).get(base).data
        self.assertEqual([team['id'] for team in data['missing']], [self.team.id])
        self.assertEqual(_api(self.admin).post(f'{base}remind/').data['reminded'], 1)
        self.assertEqual(_api(self.leader).get(base).status_code, 403)
        reviewer = make_user()
        _staff(self.hack, reviewer, 'ORGANIZER', ['ideas'])
        self.assertEqual(_api(reviewer).get(base).status_code, 200)
        self.assertEqual(_api(reviewer).get(f'/api/hackathons/{self.hack.slug}/submissions/').status_code, 403)

    def test_deadline_ordering_is_validated(self):
        resp = _api(self.admin).patch(f'/api/hackathons/{self.hack.slug}/', {
            'idea_deadline': (timezone.now() + 5 * HOUR).isoformat(), 'submission_deadline': (timezone.now() + HOUR).isoformat()}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('idea_deadline', resp.data)
