from django.test import TestCase
from apps.core.image_urls import normalize_image_url


class ImageUrlNormalizationTests(TestCase):
    def test_normalizes_google_drive_file_view_link(self):
        url = 'https://drive.google.com/file/d/13P3gwdVKZ29hqByYw2TestId/view?usp=sharing'
        self.assertEqual(
            normalize_image_url(url),
            'https://lh3.googleusercontent.com/d/13P3gwdVKZ29hqByYw2TestId'
        )

    def test_normalizes_google_drive_open_link(self):
        url = 'https://drive.google.com/open?id=13P3gwdVKZ29hqByYw2TestId'
        self.assertEqual(
            normalize_image_url(url),
            'https://lh3.googleusercontent.com/d/13P3gwdVKZ29hqByYw2TestId'
        )

    def test_normalizes_dropbox_link(self):
        url = 'https://www.dropbox.com/s/xyz123/banner.jpg?dl=0'
        normalized = normalize_image_url(url)
        self.assertIn('raw=1', normalized)
        self.assertNotIn('dl=0', normalized)

    def test_preserves_standard_image_url(self):
        url = 'https://images.unsplash.com/photo-1555066931-4365d14bab8c?auto=format'
        self.assertEqual(normalize_image_url(url), url)

    def test_empty_or_none(self):
        self.assertIsNone(normalize_image_url(''))
        self.assertIsNone(normalize_image_url(None))
