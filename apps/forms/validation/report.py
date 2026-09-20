"""
Result objects returned by the validation engine.

``ValidationReport``  — the outcome of validating one submission.
``DefinitionReport``  — the outcome of validating a form's own definition.

Both are plain dataclasses (no Django, no DRF) so they can be unit-tested and
reused from any layer. ``ValidationReport.as_dict()`` produces the exact JSON
body the submission endpoint returns on a 400.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FieldError:
    """A single validation failure, tied to a field where one applies."""
    code: str
    message: str
    field_id: int | None = None
    label: str | None = None
    rule: str | None = None
    context: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "field_id": self.field_id,
            "label": self.label,
            "code": self.code,
            "message": self.message,
        }
        if self.rule is not None:
            out["rule"] = self.rule
        if self.context:
            out["context"] = self.context
        return out


@dataclass
class ValidationReport:
    """
    Outcome of ``engine.validate_submission``.

    ``cleaned_answers`` maps FormField id -> the coerced/normalized value that
    should actually be persisted (hidden-field answers are already dropped).
    It is only meaningful when ``ok`` is True (or in partial mode, when only
    warnings remain).
    """
    errors: list[FieldError] = field(default_factory=list)
    warnings: list[FieldError] = field(default_factory=list)
    cleaned_answers: dict[int, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add_error(self, err: FieldError) -> None:
        self.errors.append(err)

    def add_warning(self, err: FieldError) -> None:
        self.warnings.append(err)

    def merge(self, other: "ValidationReport") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        self.cleaned_answers.update(other.cleaned_answers)

    def as_dict(self) -> dict[str, Any]:
        n = len(self.errors)
        return {
            "detail": (
                "Validation passed."
                if n == 0
                else f"Validation failed for {n} field(s)."
            ),
            "code": "VALIDATION_PASSED" if n == 0 else "VALIDATION_FAILED",
            "errors": [e.as_dict() for e in self.errors],
            "warnings": [w.as_dict() for w in self.warnings],
        }


@dataclass
class DefinitionReport:
    """
    Outcome of ``definition.validate_form_definition``.

    ``errors``   — structural problems that block PUBLISHED / SCHEDULED.
    ``warnings`` — soft issues surfaced to the builder but never blocking.
    """
    errors: list[FieldError] = field(default_factory=list)
    warnings: list[FieldError] = field(default_factory=list)

    @property
    def publishable(self) -> bool:
        return not self.errors

    def add_error(self, err: FieldError) -> None:
        self.errors.append(err)

    def add_warning(self, err: FieldError) -> None:
        self.warnings.append(err)

    def as_dict(self) -> dict[str, Any]:
        return {
            "publishable": self.publishable,
            "detail": (
                "Form definition is valid."
                if self.publishable
                else f"Form definition has {len(self.errors)} blocking problem(s)."
            ),
            "errors": [e.as_dict() for e in self.errors],
            "warnings": [w.as_dict() for w in self.warnings],
        }
