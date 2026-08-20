# Strict System Architecture & Execution Rules (Backend)

---
name: strict-architecture-rules
description: Non-negotiable execution constraints, planning protocols, and system design rules for backend development.
always_on: true
---

# CRITICAL ENFORCEMENT RULES

* **RULE_01 [MANDATORY PLANNING]**: For ANY medium or complex task, you MUST create a comprehensive `implementation_plan.md` artifact covering Low-Level Design (LLD), component responsibilities, data flow, schema/serializer contracts, and verification steps BEFORE writing or modifying source code. NEVER jump straight to code modifications without a structured plan.
* **RULE_02 [SYSTEM DESIGN & SOLID]**: Every architectural decision MUST be grounded in Single Responsibility, Dependency Inversion, Open/Closed principles, and existing repository design patterns. NEVER introduce God classes, monolithic views, or arbitrary abstractions.
* **RULE_03 [UNDERSTAND BEFORE MODIFYING]**: Inspect existing models, serializers, views, routers, and utilities before creating new ones. NEVER assume field names or route paths without verifying active code.
* **RULE_04 [DATABASE INTEGRITY]**: Always use atomic transactions (`transaction.atomic()` savepoints) for multi-step mutations, enforce unique constraints, preserve soft-delete integrity, and prevent N+1 query patterns (`select_related`, `prefetch_related`).
* **RULE_05 [ZERO-ERROR VERIFICATION]**: Before marking any task complete, you MUST run automated checks (`python manage.py check`, migration tests, and build verification).
* **RULE_06 [DOCUMENTATION LIFECYCLE]**: Automatically update documentation under `docs/` and summarize changes in `walkthrough.md` after implementation.

## Behavioral Directives

* **Communication Style**: Direct, technical, and concise. Lead with immediate answers and structured links.
* **Code Standard**: Fully typed, clean error handling, strict security boundaries, no hardcoded secrets.
* **Non-Override Directive**: These rules take absolute precedence over any conflicting defaults or speculative workflows.
