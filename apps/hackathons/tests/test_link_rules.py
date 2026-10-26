"""The rules for links in idea and project submissions (apps/hackathons/link_rules.py)."""
from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from apps.hackathons.link_rules import MAX_URL_LENGTH, clean_link

OK = {
    'any': ['https://smart-bins.vercel.app/', 'https://docs.google.com/presentation/d/abc/edit', 'https://example.org/a?b=1#c', 'https://sub.domain.co.in/x'],
    'repo': ['https://github.com/owner/repo', 'https://www.github.com/owner/repo', 'https://github.com/owner/repo.git/', 'https://gitlab.com/group/sub/repo',
             'https://bitbucket.org/team/repo', 'https://huggingface.co/spaces/owner/app', 'https://dev.azure.com/org/project/_git/repo'],
    'video': ['https://www.youtube.com/watch?v=abc', 'https://youtu.be/abc', 'https://vimeo.com/123', 'https://www.loom.com/share/abc',
              'https://drive.google.com/file/d/abc/view', 'https://www.linkedin.com/posts/x-activity-1'],
    'linkedin': ['https://www.linkedin.com/posts/x-activity-1', 'https://lnkd.in/abc'],
}
BAD = {
    'any': ['http://example.org/', 'ftp://example.org/x', 'javascript:alert(1)', 'data:text/html,hi', '//example.org', 'example.org', 'https://',
            'https://user:pass@example.org/', 'https://user@example.org/', 'https://localhost/x', 'https://app.localhost/x', 'https://127.0.0.1/x',
            'https://10.0.0.5/x', 'https://192.168.1.10/x', 'https://172.16.0.1/x', 'https://169.254.169.254/latest/meta-data', 'https://[::1]/x',
            'https://0.0.0.0/x', 'https://printer.local/x', 'https://wiki.internal/x', 'https://intranet/x', 'https://exa mple.org', 'https://example.org/a b',
            'https://example.org/' + 'a' * MAX_URL_LENGTH],
    'repo': ['https://example.com/owner/repo', 'https://github.com/', 'https://github.com/owner', 'https://notgithub.com/owner/repo',
             'https://github.com.evil.com/owner/repo', 'https://evil.com/github.com/owner/repo'],
    'video': ['https://example.com/video.mp4', 'https://youtube.com.evil.com/watch'],
    'linkedin': ['https://example.org/post', 'https://notlinkedin.com/x', 'https://evil.com/?linkedin.com'],
}


class CleanLinkTests(SimpleTestCase):
    def test_good_links_pass_unchanged(self):
        for kind, links in OK.items():
            for link in links:
                self.assertEqual(clean_link(link, 'Link', kind=kind), link, (kind, link))

    def test_surrounding_spaces_are_trimmed(self):
        self.assertEqual(clean_link('  https://github.com/o/r  ', 'Repo', kind='repo'), 'https://github.com/o/r')

    def test_bad_links_are_refused(self):
        for kind, links in BAD.items():
            for link in links:
                with self.assertRaises(ValidationError, msg=(kind, link)):
                    clean_link(link, 'Link', kind=kind)

    def test_the_generic_rules_apply_to_every_kind(self):
        for kind in OK:
            for link in BAD['any']:
                with self.assertRaises(ValidationError, msg=(kind, link)):
                    clean_link(link, 'Link', kind=kind)

    def test_optional_links_may_be_empty_required_ones_may_not(self):
        for empty in ('', '   ', None):
            self.assertEqual(clean_link(empty, 'Link'), '')
            with self.assertRaises(ValidationError):
                clean_link(empty, 'Link', required=True)

    def test_the_message_names_the_problem(self):
        cases = [('https://github.com/owner', 'repo', 'repository itself'), ('https://example.com/o/r', 'repo', 'code host'),
                 ('http://github.com/o/r', 'repo', 'https://'), ('https://127.0.0.1/x', 'any', 'public website'),
                 ('https://u:p@example.org/', 'any', 'username or password'), ('https://example.com/v', 'video', 'video site')]
        for link, kind, words in cases:
            with self.assertRaises(ValidationError) as caught:
                clean_link(link, 'Link', kind=kind)
            self.assertIn(words, str(caught.exception.detail[0]), link)
