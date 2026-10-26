import re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse


def normalize_image_url(url: str | None) -> str | None:
    """
    Normalizes common cloud storage sharing URLs (Google Drive, Dropbox) into
    direct raw image URLs that browsers can render inside an <img> tag.
    """
    if not url:
        return None
    trimmed = url.strip()
    if not trimmed:
        return None

    # Google Drive:
    # https://drive.google.com/file/d/{id}/view...
    m = re.search(r'(?:drive|docs)\.google\.com/file/d/([a-zA-Z0-9_-]+)', trimmed, re.IGNORECASE)
    if m:
        return f'https://lh3.googleusercontent.com/d/{m.group(1)}'

    # https://drive.google.com/open?id={id} or /uc?id={id} or /thumbnail?id={id}
    m = re.search(r'(?:drive|docs)\.google\.com/(?:open|uc|thumbnail)\?(?:[^#]*&)?id=([a-zA-Z0-9_-]+)', trimmed, re.IGNORECASE)
    if m:
        return f'https://lh3.googleusercontent.com/d/{m.group(1)}'

    # Dropbox: change ?dl=0 to ?raw=1 so it delivers direct media bytes
    if 'dropbox.com' in trimmed.lower():
        try:
            parsed = urlparse(trimmed)
            qs = parse_qs(parsed.query)
            qs['raw'] = ['1']
            qs.pop('dl', None)
            new_query = urlencode(qs, doseq=True)
            return urlunparse(parsed._replace(query=new_query))
        except Exception:
            pass

    return trimmed
