# Under the Hood: Backend Technical Learning Guide

This guide explains **how Django, DRF, PostgreSQL, Redis, Celery, Centralized RBAC, and the Dynamic Form Builder engine work under the hood** in the SRKR Coding Club Backend.

---

## 1. How Django & Request Processing Works Under the Hood

When an HTTP request hits the Django REST Framework server:

```
HTTP Request ──► WSGI / ASGI Middleware ──► URL Dispatcher (urls.py) ──► ViewSet / View ──► Serializer / ORM ──► PostgreSQL ──► JSON Response
```

1. **WSGI/ASGI Entrypoint** (`config/wsgi.py`):
   - Accepts raw socket requests and converts the HTTP environment into a Django `HttpRequest` object.
2. **Middleware Pipeline** (`config/settings.py -> MIDDLEWARE`):
   - **`SecurityMiddleware`**: Enforces SSL, HSTS, and X-Content-Type security headers.
   - **`CorsMiddleware`**: Validates cross-origin headers against `CORS_ALLOWED_ORIGINS` (`http://localhost:3000`) and allows credentials (`CORS_ALLOW_CREDENTIALS = True`).
   - **`SessionMiddleware` & `AuthenticationMiddleware`**: Reads headers/cookies, inspects SimpleJWT tokens, and populates `request.user` with either an instance of `apps.accounts.models.User` or `AnonymousUser`.
3. **URL Routing** (`config/urls.py`):
   - Resolves the requested path (e.g. `/api/feature-flags/`) against registered URL patterns and routes to the matching `ViewSet`.

---

## 2. Centralized Role-Based Access Control (RBAC) Under the Hood

Permissions are declared centrally in [apps/core/permissions.py](file:///c:/Users/chall/OneDrive/Desktop/SRKRCC-Main_APPLICATION_BACKEND/apps/core/permissions.py):

```python
class IsAdminOrClubLead(permissions.BasePermission):
    """
    Evaluated by DRF during check_permissions():
    - Permits Django staff or superusers
    - Permits authenticated users whose User.role is 'ADMIN' or 'CLUB_LEAD'
    - Rejects unauthenticated requests with 401 Unauthorized
    - Rejects other roles (MEMBER, JUDGE) with 403 Forbidden
    """
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.user.is_staff or request.user.is_superuser:
            return True
        return getattr(request.user, 'role', None) in ['ADMIN', 'CLUB_LEAD']
```

---

## 3. Dynamic Database Profile Calculations Under the Hood

When `GET /api/auth/me/` is requested, `UserProfileDetailSerializer` in [apps/accounts/serializers.py](file:///c:/Users/chall/OneDrive/Desktop/SRKRCC-Main_APPLICATION_BACKEND/apps/accounts/serializers.py) evaluates real-time database relationships:
- **`streak`**: Fetches `user.streak.current_streak` from `apps.codequest.models.UserStreak`.
- **`points`**: Computes total XP dynamically from correct algorithm submissions (`Submission.is_correct=True`) and verified form registrations.
- **`events_count`**: Aggregates verified `Response` records submitted by this user.
- **`registered_events`**: Queries `Response.objects.filter(user=user).select_related('form')` and returns live submission dates, status, and direct form slugs (`/forms/[slug]`).
- **`badges`**: Generates dynamic achievement objects based on user activity thresholds.

---

## 4. Automated Audit Logging Pipeline Under the Hood

Platform mutations (form publishing, unpublishing, closing, scheduling, offline entries, and feature flag toggles) invoke the non-blocking helper in [apps/audit/utils.py](file:///c:/Users/chall/OneDrive/Desktop/SRKRCC-Main_APPLICATION_BACKEND/apps/audit/utils.py):

```python
def log_audit_event(actor=None, action="", target_model="", target_id="", details=None, request=None):
    # Extracts IP address, resolves authenticated actor, and creates AuditLog record
```

---

## 5. How Django ORM & PostgreSQL Work Under the Hood

Django ORM bridges Python code with PostgreSQL SQL queries:

* **Avoiding N+1 Queries**:
  - `select_related(*fields)`: Performs a SQL `INNER JOIN` or `LEFT OUTER JOIN` for foreign keys (1-to-1 or Many-to-1). Used extensively in `ResponseViewSet` and `AuditLogViewSet`.
  - `prefetch_related(*fields)`: Executes batch lookups for reverse foreign keys and Many-to-Many relationships (`answers__field`).
* **Transactions & ACID Consistency**:
  - Multi-step operations (e.g. form submission with answers in `ResponseViewSet.create`, CSV bulk ingest in `bulk_ingest`) are wrapped in `with transaction.atomic():` so partial failures are rolled back cleanly.
