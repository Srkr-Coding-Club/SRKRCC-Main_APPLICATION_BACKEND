# AGENTS.md — Agent Guidelines & Architecture Rules (Backend)

This document governs the behavior, standards, coding practices, and documentation lifecycle for AI agents operating on `SRKRCC-Main_APPLICATION_BACKEND`.

---

## General Agent Coding Guidelines

Whenever writing, modifying, or refactoring code, agents MUST follow the existing architecture and engineering conventions of this repository.

### 1. Understand Before Changing

Before implementing a change:

* Understand the relevant feature, module, and execution flow.
* Follow the existing feature documentation and bounded-context workflow defined in this `AGENTS.md` and `docs/`.
* Inspect existing implementations, utilities, services, components, hooks, APIs, and abstractions before creating new ones.
* Reuse existing functionality whenever it is appropriate.
* Identify dependencies and potential consumers before modifying shared code.
* Do not modify unrelated parts of the codebase.

Agents MUST NOT make architectural decisions based only on generic best practices. Decisions must be grounded in the actual repository architecture.

### 2. Clean Code

Write code that is readable, predictable, maintainable, and easy for another developer to understand.

* Use meaningful and descriptive names.
* Keep functions, classes, components, and modules focused.
* Follow the Single Responsibility Principle.
* Avoid deeply nested logic.
* Prefer simple and explicit implementations over clever code.
* Avoid unnecessary comments; code should communicate its intent through structure and naming.
* Remove dead code and unused imports when working in the affected area.
* Avoid magic numbers and unexplained constants.
* Keep error handling explicit and consistent with existing project conventions.

### 3. SOLID Principles

Apply SOLID principles pragmatically.

* **Single Responsibility:** Keep responsibilities focused and avoid God classes/components/functions.
* **Open/Closed:** Introduce extension points only when the code genuinely requires multiple implementations or future extensibility.
* **Liskov Substitution:** Implementations must respect the behavior expected by their abstractions.
* **Interface Segregation:** Avoid unnecessarily large interfaces or contracts.
* **Dependency Inversion:** Keep business/application logic independent from infrastructure details where an abstraction provides real value.

Do NOT introduce abstractions merely to satisfy SOLID terminology.

### 4. LLD and Design Patterns

For non-trivial functionality, consider the Low-Level Design before implementation.

Identify:

* Responsibilities
* Dependencies
* Interfaces/contracts
* Data flow
* Error flow
* State ownership
* Extension points
* Integration boundaries

Use established design patterns such as Strategy, Factory, Adapter, Repository, Command, State, or Dependency Injection only when they solve a real problem.

Do NOT introduce a design pattern when a simple function, class, component, or conditional provides a clearer solution.

The objective is maintainable design, not maximum abstraction.

### 5. DRY — Avoid Accidental Duplication

Before creating new code, search the repository for existing functionality that solves the same or a closely related problem.

* Reuse existing utilities, services, hooks, components, validators, clients, and abstractions where appropriate.
* Consolidate genuinely duplicated business logic.
* Do not create parallel implementations of existing functionality.
* Extract shared logic only when the duplicated behavior represents the same responsibility or business concept.

Do NOT create generic `Utils`, `Helpers`, `Managers`, base classes, or abstractions solely because two pieces of code look similar.

Avoid both:
* Unnecessary duplication
* Premature abstraction

### 6. Minimal and Focused Changes

Prefer the smallest clean change that correctly solves the requested problem.

Agents MUST NOT:

* Rewrite working modules without justification.
* Refactor unrelated code.
* Rename unrelated files or symbols.
* Introduce unnecessary dependencies.
* Change established architecture without a concrete reason.
* Mix large refactoring with unrelated feature work.

When refactoring, preserve existing behavior unless the requested task explicitly requires a behavior change.

### 7. Security First

Security MUST be considered whenever code is added or modified.

Depending on the affected area, consider:

* Authentication (SimpleJWT + Django AllAuth)
* Authorization & RBAC (`MEMBER`, `VOLUNTEER`, `JUDGE`, `CLUB_LEAD`, `ADMIN`)
* Resource ownership and tenant isolation
* Input validation via DRF Serializers
* SQL/ORM injection risks
* Command injection & Path traversal
* Unsafe file handling
* Sensitive data exposure
* Secret management (`.env`)
* Secure logging (do not log tokens or secrets)
* Rate limiting where appropriate

Never trust security decisions made exclusively by the frontend.

Never hardcode:
* Passwords
* API keys
* Access tokens
* Private keys
* Database credentials

### 8. Backend-Specific Rules

For backend changes:

* Keep ViewSets and views focused on request/response concerns and delegation.
* Keep business logic in DRF Serializers or service functions according to the existing architecture (`apps/`).
* Reuse existing validation and authorization permissions (`permissions.IsAuthenticatedOrReadOnly`, custom RBAC permissions).
* Use safe Django ORM queries and avoid N+1 query patterns by using `select_related()` and `prefetch_related()`.
* Consider transactions (`django.db.transaction.atomic`) for multi-step database mutations.
* Maintain PostgreSQL compatibility (`dj-database-url`, `psycopg2-binary`).
* Protect user/role boundaries and never expose internal database stack traces to clients.
* Background job processing uses plain Python threads (`apps/core/tasks.py::run_in_background`), not Celery/Redis — this is a deliberate choice for the app's current scale (see that module's docstring). Don't introduce a Celery/Redis dependency without a concrete scale-driven reason; follow the existing thread-based pattern for new background work.

### 9. Testing and Validation

Before considering a change complete:

* Inspect existing tests related to the affected functionality.
* Add or update tests for meaningful behavior changes (`python manage.py test`).
* Test important edge cases and failure paths.
* Validate authorization and security-sensitive behavior where applicable.
* Run Django system check (`python manage.py check`) and migrations check (`python manage.py makemigrations --check`).

### 10. Architecture Preservation & Automated Documentation Lifecycle

The existing architecture is the default unless there is a documented reason to change it.

**MANDATORY DOCUMENTATION UPDATE PROTOCOL**:
Whenever an agent creates a new feature, updates an API endpoint, modifies a database schema, or changes module visibility/RBAC rules:
1. **Identify Gaps**: Inspect `docs/` (e.g. `docs/architecture/`, `docs/modules/`, `docs/features/`).
2. **Update Documentation**: Automatically update the corresponding documentation files to reflect the code changes.
3. **Maintain Learning Guides**: Ensure the technical learning guides explaining *how things work under the hood* (Django ORM, DRF execution pipeline, Celery background workers, Dynamic Form Builder metadata model) are up to date.

### 11. Universal Backup Engine & Raw Vault Architectural Invariants

Whenever handling file backups, bulk ingestion, or spreadsheet restoration:

1. **Universal Backup ≠ Universal Import**: Every uploaded file begins life strictly as an immutable `BackupJob` artifact (SHA-256 hash, raw row extraction, disk storage).
2. **No Implicit Domain Mutation**: Uploading a backup file alone **never** creates, updates, or deletes application-domain records (`User`, `Event`, `Hackathon`, `FormSubmission`).
3. **Decoupled Statuses**: `BackupJob` and `ImportAttempt` are separate entities. A single backup job may undergo multiple import attempts without re-uploading.
4. **Strict 50% Rule & Required Fields**:
   - Structured domain import is prohibited unless `Required Fields Satisfied = True` AND `Schema Match Confidence >= 50%`.
   - Confidence = `(Unique Canonical Fields Matched / Total Expected Canonical Fields) * 100`. Canonical fields are counted at most once regardless of duplicate aliases.
5. **Zero Legacy Data Loss**: Unmapped source columns must always be preserved in `ImportRow.raw_data` provenance.
6. **Schemaless Raw Vault**: `UNKNOWN_RAW` backups enforce zero schema constraints and save raw rows into `RawBackupArchive` + `RawBackupRow`.
### 12. Credential Safety & Password Setup Lifecycle Invariants

Whenever handling member authentication, account restoration, or spreadsheet ingestion:

1. **Credential Safety Invariant**: Backup restoration may restore account identity, permanent Club IDs, and profile data, but it must **NEVER** silently grant, forge, or overwrite authentication credentials.
2. **Account Existence vs. Password Readiness**:
   - New members created via backup import receive `set_unusable_password()` and `password_status = 'NEEDS_SETUP'`.
   - Existing active members keep their existing passwords **byte-for-byte untouched**; never call `set_unusable_password()` on existing members during imports.
   - Forbidden credential columns (`password`, `pwd`, `pass`, `password_hash`, etc.) are systematically purged by `UserBackupImporter` and `UserAccountService`.
3. **Consistency Invariant**:
   - `password_status == 'ACTIVE'` $\iff$ stored password is a valid, usable hash.
   - `password_status == 'NEEDS_SETUP'` $\iff$ stored password is an unusable password (`!`).
4. **Zero Raw Token Logging Invariant**:
   - Raw setup tokens, setup URLs, passwords, or password hashes must **NEVER** appear in application logs, audit logs, exception messages, or frontend telemetry.
5. **Anti-Enumeration & Rate Limiting**:
   - Setup link requests are rate-limited (5/hr/email, 20/hr/IP) and return generic non-enumerating responses.
   - Login responses intentionally return `PASSWORD_SETUP_REQUIRED` without the email to provide immediate actionable UX.
6. **Atomic Concurrency Protection**:
   - Password confirmation MUST execute inside `transaction.atomic()` using `PasswordSetupToken.objects.select_for_update().select_related('user')`.
   - On confirmation, invalidate all remaining unused setup tokens for that user.

### 13. Agent Decision Rule

Before implementing a non-trivial change, the agent should be able to answer:

> Does this functionality already exist?  
> Which component/layer owns this responsibility?  
> Can existing code be reused?  
> Am I introducing duplication?  
> Am I introducing unnecessary abstraction?  
> What existing consumers could this change affect?  
> What security boundary does this change cross?  
> How will this change be tested?  
> What is the smallest clean implementation?  

If these questions cannot be answered from the repository, inspect the relevant code and documentation in `docs/` before implementing.

---

## Under the Hood: Technical Learning Guide

To accelerate developer onboarding and understand the internal workings of this backend, refer to:
* **[docs/architecture/README.md](file:///c:/Users/chall/OneDrive/Desktop/SRKRCC-Main_APPLICATION_BACKEND/docs/architecture/README.md)** — Architectural Overview
* **[docs/architecture/tech-stack.md](file:///c:/Users/chall/OneDrive/Desktop/SRKRCC-Main_APPLICATION_BACKEND/docs/architecture/tech-stack.md)** — Tech Stack Details
* **[docs/architecture/data-model-dynamic-forms.md](file:///c:/Users/chall/OneDrive/Desktop/SRKRCC-Main_APPLICATION_BACKEND/docs/architecture/data-model-dynamic-forms.md)** — Dynamic Form Builder metadata model under the hood
* **[docs/architecture/technical-learning-guide.md](file:///c:/Users/chall/OneDrive/Desktop/SRKRCC-Main_APPLICATION_BACKEND/docs/architecture/technical-learning-guide.md)** — In-depth guide on Django ORM, DRF pipelines, SimpleJWT auth flow, PostgreSQL indexing, and Celery background workers.
