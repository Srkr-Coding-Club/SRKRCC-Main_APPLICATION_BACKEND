# Under the Hood: Backend Technical Learning Guide

This guide explains **how Django, DRF, PostgreSQL, Redis, Celery, and the Dynamic Form Builder engine work under the hood** in the SRKR Coding Club Backend. It is written to flatten the technical learning curve for developers and AI agents alike.

---

## 1. How Django & Request Processing Works Under the Hood

When an HTTP request hits the Django REST Framework server:

```
HTTP Request ──► WSGI / ASGI Middleware ──► URL Dispatcher (urls.py) ──► ViewSet / View ──► Serializer / ORM ──► PostgreSQL ──► JSON Response
```

1. **WSGI/ASGI Entrypoint** (`config/wsgi.py`):
   - Accepts raw socket requests from Gunicorn/uWSGI or Django's dev server.
   - Instantiates `WSGIHandler` which converts the HTTP request environment into a Django `HttpRequest` object.
2. **Middleware Pipeline** (`config/settings.py -> MIDDLEWARE`):
   - **`SecurityMiddleware`**: Enforces SSL, HSTS, and X-Content-Type security headers.
   - **`CorsMiddleware`**: Validates cross-origin headers against `CORS_ALLOWED_ORIGINS` (`http://localhost:3000`).
   - **`SessionMiddleware` & `AuthenticationMiddleware`**: Reads headers/cookies, inspects SimpleJWT tokens, and populates `request.user` with either an instance of `apps.accounts.models.User` or `AnonymousUser`.
3. **URL Routing** (`config/urls.py`):
   - Resolves the requested path (e.g. `/api/feature-flags/`) against registered URL patterns using regex/path matchers and routes to the matching `ViewSet`.

---

## 2. How Django REST Framework (DRF) Works Under the Hood

DRF wraps standard Django views with powerful API abstractions:

* **`APIView` / `ViewSet` Pipeline**:
  ```python
  request -> initial() -> check_permissions() -> check_throttles() -> dispatch method (GET/POST/PUT/DELETE)
  ```
  - `initial()` executes permission checks (`permissions.IsAuthenticatedOrReadOnly`). If a permission check fails, DRF immediately raises an `APIException` resulting in an HTTP 401 or 403 response without executing business logic.
* **Serializers (`serializers.ModelSerializer`)**:
  - Convert complex Python model instances into native Python primitives (`.data`), which are then rendered into JSON strings by `JSONRenderer`.
  - Validate incoming JSON payloads (`.is_valid()`), running field-level validators (`validate_<fieldname>()`) and object-level validators (`validate()`).
  - Perform ORM mutations (`.save()` -> calls `.create()` or `.update()`).

---

## 3. How Django ORM & PostgreSQL Work Under the Hood

Django ORM bridges Python code with PostgreSQL SQL queries via an Abstract Syntax Tree (AST):

* **QuerySets are Lazy**:
  ```python
  # Does NOT hit PostgreSQL yet:
  queryset = Event.objects.filter(category='Workshop')
  
  # Hits PostgreSQL only when evaluated (iterated, sliced, len(), bool()):
  events = list(queryset)
  ```
* **Avoiding N+1 Queries**:
  - `select_related(*fields)`: Performs a SQL `INNER JOIN` or `LEFT OUTER JOIN` for foreign keys (1-to-1 or Many-to-1).
  - `prefetch_related(*fields)`: Executes a separate SQL query for Many-to-Many or reverse foreign key lookups and joins them in Python memory.
* **Transactions & Consistency**:
  - Mutations across multiple models (e.g. submitting a dynamic form response + answers) are wrapped in `transaction.atomic()` to guarantee ACID compliance (if an error occurs, PostgreSQL rolls back all partial writes).

---

## 4. How the Dynamic Form Engine Works Under the Hood

Unlike traditional platforms that demand a new PostgreSQL table per form, our dynamic form builder relies on a **Metadata Entity-Attribute-Value (EAV) variant model**:

```
[Form] ──1:N──► [FormField] (label, type, validation_rules JSON, order)
  │                    ▲
1:N                  1:N
  ▼                    │
[Response] ──1:N──► [Answer] (value JSON)
```

* **No DB Migrations per Form**: Adding a new form or question inserts metadata rows into `forms_formfield`.
* **JSON Validation Rules**: Complex constraints (e.g. `{"min_length": 5, "regex": "^[0-9]+$"}`) are evaluated dynamically in Python during response submission via `AnswerSerializer`.

---

## 5. How Redis & Celery Work Under the Hood

* **Celery Worker & Scheduler**:
  - Celery reads scheduled tasks from Redis (e.g. auto-publishing Codequest daily problems at midnight or sending bulk reminder emails).
  - Tasks execute in background worker processes isolated from the main HTTP thread, preventing request blocking.

---

## 6. Maintenance & Gap Update Protocol

Whenever you update backend logic:
1. Update existing models/serializers/views.
2. Run `python manage.py check` and `python manage.py makemigrations`.
3. Check for documentation gaps in `docs/` and update `docs/architecture/` and `docs/modules/` accordingly.
