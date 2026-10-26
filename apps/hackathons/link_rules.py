"""
What counts as an acceptable link in an idea or a project submission.

Every link must be a public https address: no password in it, no localhost / private network / ``.local`` host, a real
domain, and short enough for the database column (200 characters). On top of that:

* a repository link must be on a known code host and point at a repository (``host/owner/repo``);
* a video link must be on a known video host;
* a LinkedIn link must be on linkedin.com / lnkd.in.

Demo and slides links may be any public site (teams host those on their own domains). Whether the page really exists
is deliberately not checked: fetching a team-supplied address from the server would be a server-side request forgery
door, and a link that works today can break tomorrow. The organizers open the link and judge what they find.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from rest_framework import serializers

MAX_URL_LENGTH = 200   # the URLField columns

CODE_HOSTS = ('github.com', 'gitlab.com', 'bitbucket.org', 'codeberg.org', 'dev.azure.com', 'sourceforge.net', 'git.sr.ht', 'huggingface.co')
VIDEO_HOSTS = (
    'youtube.com', 'youtu.be', 'vimeo.com', 'loom.com', 'streamable.com', 'drive.google.com', 'dropbox.com', '1drv.ms',
    'onedrive.live.com', 'sharepoint.com', 'linkedin.com', 'lnkd.in', 'instagram.com', 'facebook.com', 'fb.watch', 'x.com', 'twitter.com',
)
LINKEDIN_HOSTS = ('linkedin.com', 'lnkd.in')

_https_validator = URLValidator(schemes=['https'])
_PRIVATE_SUFFIXES = ('.local', '.localhost', '.internal', '.lan', '.home', '.corp', '.intranet')


def _host_matches(host: str, names) -> bool:
    return any(host == name or host.endswith('.' + name) for name in names)


def _is_private_host(host: str) -> bool:
    if host == 'localhost' or host.endswith(_PRIVATE_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host.strip('[]'))
    except ValueError:
        return False
    return not address.is_global or address.is_multicast


def clean_link(value, label: str, *, kind: str = 'any', required: bool = False) -> str:
    """The cleaned link, '' when optional and empty. ``kind`` is 'any', 'repo', 'video' or 'linkedin'."""
    text = str(value or '').strip()
    if not text:
        if required:
            raise serializers.ValidationError(f'{label} is required.')
        return ''
    if len(text) > MAX_URL_LENGTH:
        raise serializers.ValidationError(f'{label} is too long (at most {MAX_URL_LENGTH} characters). Use a shorter link.')
    if re.search(r'\s', text):
        raise serializers.ValidationError(f'{label} must not contain spaces.')
    if not text.lower().startswith('https://'):
        raise serializers.ValidationError(f'{label} must be a full https:// link.')
    try:
        _https_validator(text)
    except DjangoValidationError:
        raise serializers.ValidationError(f'{label} must be a full https:// link.')
    parts = urlsplit(text)
    host = (parts.hostname or '').lower().rstrip('.')
    if parts.username or parts.password:
        raise serializers.ValidationError(f'{label} must not contain a username or password.')
    if not host or _is_private_host(host) or ('.' not in host and not host.startswith('[')):
        raise serializers.ValidationError(f'{label} must be a public website, not a local or private address.')

    if kind == 'repo':
        if not _host_matches(host, CODE_HOSTS):
            raise serializers.ValidationError(f'{label} must be on a code host such as GitHub, GitLab or Bitbucket.')
        segments = [segment for segment in parts.path.split('/') if segment]
        if len(segments) < 2:
            raise serializers.ValidationError(f'{label} must point at the repository itself (for example https://github.com/owner/repo).')
    elif kind == 'video' and not _host_matches(host, VIDEO_HOSTS):
        raise serializers.ValidationError(f'{label} must be on a video site such as YouTube, Vimeo, Loom or Google Drive.')
    elif kind == 'linkedin' and not _host_matches(host, LINKEDIN_HOSTS):
        raise serializers.ValidationError(f'{label} must be a LinkedIn link (linkedin.com).')
    return text
