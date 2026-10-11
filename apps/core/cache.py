"""
apps/core/cache.py
------------------
HTTP Response Caching helpers and ViewSet mixins for safe, unauthenticated read paths.

Sets standards-compliant `Cache-Control` headers:
- Anonymous GET/HEAD on status 200 receives `public, max-age={cache_timeout}, stale-while-revalidate={cache_stale_while_revalidate}`
  so browsers, edge proxies, and CDNs can cache responses and serve instantly without hitting the database.
- Authenticated requests (users with JWT or session) receive `private, no-cache` so sensitive or role-dependent
  querysets (like drafts, admin counts, or hidden records) are NEVER cached in public/shared caches.
- Mutating methods (POST/PUT/PATCH/DELETE) or non-200 responses are left untouched or explicitly non-cached.
"""


class PublicReadCacheControlMixin:
    """
    Mixin for DRF ViewSets providing HTTP Cache-Control headers on read operations.
    """
    cache_timeout = 60
    cache_stale_while_revalidate = 120

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        if request.method in ('GET', 'HEAD') and response.status_code == 200:
            if not (request.user and request.user.is_authenticated):
                response['Cache-Control'] = (
                    f'public, max-age={self.cache_timeout}, '
                    f'stale-while-revalidate={self.cache_stale_while_revalidate}'
                )
            else:
                response['Cache-Control'] = 'private, no-cache'
        return response
