"""
The S3 side of form file uploads. Everything that talks to the bucket lives here so the rest of the app (and the
tests) deal with a small interface and never with boto3.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from django.conf import settings

KEY_PREFIX = 'form-uploads'


@dataclass(frozen=True)
class StoredObject:
    size: int
    content_type: str


class UploadStorage:
    def __init__(self, client, bucket: str):
        self._client = client
        self._bucket = bucket

    def presign_post(self, *, key: str, content_type: str, min_bytes: int, max_bytes: int, expires: int) -> dict:
        """A signed form the browser posts the file to. S3 itself refuses a wrong key, type or size."""
        return self._client.generate_presigned_post(
            Bucket=self._bucket,
            Key=key,
            Fields={'Content-Type': content_type},
            Conditions=[
                {'Content-Type': content_type},
                ['content-length-range', min_bytes, max_bytes],
            ],
            ExpiresIn=expires,
        )

    def head(self, key: str) -> Optional[StoredObject]:
        from botocore.exceptions import ClientError
        try:
            found = self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as error:
            if error.response.get('Error', {}).get('Code') in ('404', 'NoSuchKey', 'NotFound'):
                return None
            raise
        return StoredObject(size=int(found['ContentLength']), content_type=str(found.get('ContentType') or ''))

    def read_start(self, key: str, length: int = 32) -> bytes:
        """The first bytes of the object, for checking that it is what its extension says."""
        reply = self._client.get_object(Bucket=self._bucket, Key=key, Range=f'bytes=0-{length - 1}')
        return reply['Body'].read()

    def put(self, key: str, body: bytes, content_type: str) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=body, ContentType=content_type)

    def scan_result(self, key: str) -> Optional[str]:
        """The verdict GuardDuty Malware Protection wrote on the object, or None while it has not been scanned."""
        tags = self._client.get_object_tagging(Bucket=self._bucket, Key=key)['TagSet']
        return next((t['Value'] for t in tags if t['Key'] == 'GuardDutyMalwareScanStatus'), None)

    def copy(self, source: str, target: str) -> None:
        self._client.copy_object(
            Bucket=self._bucket, Key=target, CopySource={'Bucket': self._bucket, 'Key': source},
            MetadataDirective='COPY',
        )

    def presign_get(self, *, key: str, filename: str, content_type: str, inline: bool, expires: int) -> str:
        """A short-lived link to read the object. Anything that is not safe to show in the page is forced to download."""
        from urllib.parse import quote
        disposition = f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(filename)}"
        params = {'Bucket': self._bucket, 'Key': key, 'ResponseContentDisposition': disposition}
        params['ResponseContentType'] = content_type if inline else 'application/octet-stream'
        return self._client.generate_presigned_url('get_object', Params=params, ExpiresIn=expires)

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)


def object_key(form_id: int, upload_id) -> str:
    return f'{KEY_PREFIX}/tmp/{form_id}/{upload_id}'


def final_key(form_id: int, upload_id) -> str:
    """Where a file lives once it belongs to a response. Only the tmp/ prefix expires."""
    return f'{KEY_PREFIX}/files/{form_id}/{upload_id}'


def get_upload_storage() -> UploadStorage:
    import boto3
    from botocore.config import Config
    client = boto3.client(
        's3', region_name=settings.FORM_UPLOAD_REGION, endpoint_url=settings.FORM_UPLOAD_ENDPOINT_URL or None,
        config=Config(signature_version='s3v4', retries={'max_attempts': 3, 'mode': 'standard'}),
    )
    return UploadStorage(client, settings.FORM_UPLOAD_BUCKET)
