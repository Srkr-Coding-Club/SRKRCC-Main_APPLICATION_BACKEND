import io
from PIL import Image as PILImage
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()


class HackathonAssetUploadTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username='asset_admin', email='asset_admin@srkr.ac.in', password='pw12345!', role='ADMIN'
        )
        self.member = User.objects.create_user(
            username='member_user', email='member@srkr.ac.in', password='pw12345!', role='MEMBER'
        )

    def _create_test_image_file(self, width=800, height=400, fmt='PNG'):
        file_io = io.BytesIO()
        image = PILImage.new('RGB', (width, height), color=(255, 120, 0))
        image.save(file_io, format=fmt)
        file_io.seek(0)
        return SimpleUploadedFile(
            f'test_image.{fmt.lower()}', file_io.read(), content_type=f'image/{fmt.lower()}'
        )

    def test_admin_can_upload_valid_image(self):
        self.client.force_authenticate(self.admin)
        uploaded = self._create_test_image_file(width=1920, height=1080)
        resp = self.client.post('/api/hackathons/upload-asset/', {'file': uploaded}, format='multipart')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIn('url', resp.data)
        self.assertTrue(resp.data['url'].startswith('/media/hackathons/assets/'))
        self.assertEqual(resp.data['width'], 1920)
        self.assertEqual(resp.data['height'], 1080)
        self.assertEqual(resp.data['format'], 'PNG')

    def test_unauthenticated_or_member_rejected(self):
        uploaded = self._create_test_image_file()
        resp = self.client.post('/api/hackathons/upload-asset/', {'file': uploaded}, format='multipart')
        self.assertEqual(resp.status_code, 401)

        self.client.force_authenticate(self.member)
        resp = self.client.post('/api/hackathons/upload-asset/', {'file': uploaded}, format='multipart')
        self.assertEqual(resp.status_code, 403)

    def test_rejects_corrupted_or_non_image(self):
        self.client.force_authenticate(self.admin)
        fake_file = SimpleUploadedFile('fake.png', b'NOT_A_REAL_IMAGE_DATA', content_type='image/png')
        resp = self.client.post('/api/hackathons/upload-asset/', {'file': fake_file}, format='multipart')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Corrupt or invalid image data', resp.data['error'])

    def test_presigned_url_local_fallback_when_s3_unset(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post('/api/hackathons/asset-presigned-url/', {
            'filename': 'banner.png',
            'content_type': 'image/png',
            'size': 1024,
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['mode'], 'local')
        self.assertEqual(resp.data['upload_url'], '/api/hackathons/upload-asset/')

    def test_presigned_url_s3_mode(self):
        from unittest.mock import MagicMock, patch
        self.client.force_authenticate(self.admin)
        mock_s3 = MagicMock()
        mock_s3.generate_presigned_post.return_value = {
            'url': 'https://test-bucket.s3.ap-south-1.amazonaws.com/',
            'fields': {'key': 'hackathons/assets/common_123.png', 'Content-Type': 'image/png'},
        }

        with patch('boto3.client', return_value=mock_s3), \
             self.settings(FORM_UPLOAD_BUCKET='test-bucket', AWS_S3_REGION_NAME='ap-south-1', FORM_UPLOADS_BACKEND='s3'):
            resp = self.client.post('/api/hackathons/asset-presigned-url/', {
                'filename': 'banner.png',
                'content_type': 'image/png',
                'size': 2048,
            }, format='json')
            self.assertEqual(resp.status_code, 200, resp.data)
            self.assertEqual(resp.data['mode'], 's3')
            self.assertIn('post', resp.data)
            self.assertTrue(resp.data['public_url'].startswith('https://test-bucket.s3.ap-south-1.amazonaws.com/hackathons/assets/'))

