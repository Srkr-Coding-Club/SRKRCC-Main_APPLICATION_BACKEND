"""
apps.forms.validation
======================

Production-grade validation engine for the Dynamic Form Builder.

    Question Type -> Validation Rules -> Response Type -> Conditional Rules
                  -> Dependencies -> Cross-Field Rules -> Final Submission Validity

Public API
----------
``validate_submission(form, raw_answers, *, mode, is_edit, existing_answers)``
    -> ``ValidationReport`` (``.ok``, ``.errors``, ``.warnings``, ``.cleaned_answers``).
    Enforces every stored form-definition rule on an incoming response. ``mode``
    is ``"strict"`` (public submit / edit) or ``"partial"`` (admin manual entry /
    CSV import — required + constraint failures become warnings).

``validate_form_definition(form_or_payload)``
    -> ``DefinitionReport`` (``.publishable``, ``.errors``, ``.warnings``).
    Validates a form's OWN configuration; structural errors block PUBLISHED /
    SCHEDULED.

``normalize_validation_rules`` / ``normalize_conditional_logic``
    Canonicalize the two JSON config blobs on write.

Backward-compat re-exports (``evaluate_condition`` / ``evaluate_visible_fields``)
let existing callers in ``apps.forms.serializers`` / ``apps.forms.views`` keep
importing the old names.
"""

from .report import FieldError, ValidationReport, DefinitionReport
from .engine import validate_submission, STRICT, PARTIAL
from .definition import validate_form_definition
from .schema import normalize_validation_rules, normalize_conditional_logic
from .conditional import (
    evaluate_condition,
    evaluate_visible_fields,
    compute_layout,
    evaluate_operator,
)

__all__ = [
    "FieldError",
    "ValidationReport",
    "DefinitionReport",
    "validate_submission",
    "validate_form_definition",
    "normalize_validation_rules",
    "normalize_conditional_logic",
    "evaluate_condition",
    "evaluate_visible_fields",
    "compute_layout",
    "evaluate_operator",
    "STRICT",
    "PARTIAL",
]
