"""Runs the shared conditional-engine test vectors (also run by the frontend's TypeScript engine)."""

import json
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase

from decimal import Decimal

from apps.forms.validation.conditional import compute_layout, evaluate_operator
from apps.forms.validation.formula import evaluate as evaluate_formula

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "conditional_vectors.json").read_text(encoding="utf-8"))


def _field(spec):
    return SimpleNamespace(
        id=spec["id"], type=spec.get("type", "TEXT"), options=spec.get("options", []),
        is_required=spec.get("is_required", False), conditional_logic=spec.get("conditional_logic", {}),
    )


class OperatorVectorTests(SimpleTestCase):
    def test_every_operator_vector(self):
        for case in VECTORS["operators"]:
            with self.subTest(case["name"]):
                self.assertIs(evaluate_operator(case["op"], case["left"], case["right"]), case["expect"])


class LayoutVectorTests(SimpleTestCase):
    def test_every_layout_vector(self):
        for scenario in VECTORS["layouts"]:
            fields = [_field(f) for f in scenario["fields"]]
            for case in scenario["cases"]:
                with self.subTest(f"{scenario['name']} / {case['values']}"):
                    values = {int(k): v for k, v in case["values"].items()}
                    layout = compute_layout(fields, values)
                    self.assertEqual(sorted(layout.visible_ids), case["visible"])
                    if "required" in case:
                        self.assertEqual({str(k): v for k, v in layout.required_overrides.items()}, case["required"])
                    if "set_values" in case:
                        self.assertEqual({str(k): v for k, v in layout.set_values.items()}, case["set_values"])
                    if "allowed_options" in case:
                        self.assertEqual({str(k): v for k, v in layout.allowed_options.items()}, case["allowed_options"])
                    if "skipped_pages" in case:
                        self.assertEqual(sorted(layout.skipped_pages), case["skipped_pages"])
                    if "messages" in case:
                        self.assertEqual({str(k): v for k, v in layout.messages.items()}, case["messages"])


class FormulaVectorTests(SimpleTestCase):
    def test_every_formula_vector(self):
        for case in VECTORS["formulas"]:
            with self.subTest(case["name"]):
                values = {int(k): v for k, v in case["values"].items()}
                result = evaluate_formula(case["formula"], values, case["decimals"])
                if case["expect"] is None:
                    self.assertIsNone(result)
                else:
                    self.assertEqual(result, Decimal(str(case["expect"])))
