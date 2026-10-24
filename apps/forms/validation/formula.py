"""
Safe arithmetic for CALCULATED questions.

A formula is numbers, ``{<question id>}`` references, ``+ - * /`` and parentheses, nothing else - no
names, calls or attribute access, so there is nothing to escape from. It is parsed into a small tree and
evaluated with ``Decimal``.

Rules the browser's evaluator (``src/lib/formula.ts``) follows too, and the shared test vectors check:
  * a blank or missing reference counts as 0;
  * a reference to something that is not a number makes the whole result empty;
  * division by zero, or a result of 10**15 or more, makes the result empty;
  * the result is rounded half-up to ``decimals`` places (0-6).
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from typing import Any

from .values import CoercionError, is_empty, parse_number

MAX_FORMULA_LENGTH = 300
MAX_REFERENCES = 30
MAX_DEPTH = 20
MAX_RESULT = Decimal(10) ** 15
MAX_DECIMALS = 6

_TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|\{(\d+)\}|([-+*/()]))")


class FormulaError(ValueError):
    pass


# A node is ("num", Decimal) | ("ref", int) | ("neg", node) | (op, left, right)
Node = tuple


def _tokenize(text: str) -> list[tuple[str, Any]]:
    tokens: list[tuple[str, Any]] = []
    position = 0
    text = text.rstrip()
    while position < len(text):
        match = _TOKEN.match(text, position)
        if not match:
            raise FormulaError(f"Unexpected character at position {position + 1}.")
        number, ref, symbol = match.groups()
        if number is not None:
            tokens.append(("num", Decimal(number)))
        elif ref is not None:
            tokens.append(("ref", int(ref)))
        else:
            tokens.append(("sym", symbol))
        position = match.end()
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, Any]]):
        self.tokens = tokens
        self.index = 0
        self.depth = 0

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self):
        token = self.peek()
        self.index += 1
        return token

    def parse(self) -> Node:
        if not self.tokens:
            raise FormulaError("The formula is empty.")
        node = self.expression()
        if self.peek() is not None:
            raise FormulaError("Unexpected token after the end of the formula.")
        return node

    def expression(self) -> Node:
        node = self.term()
        while (token := self.peek()) and token[0] == "sym" and token[1] in "+-":
            self.take()
            node = (token[1], node, self.term())
        return node

    def term(self) -> Node:
        node = self.factor()
        while (token := self.peek()) and token[0] == "sym" and token[1] in "*/":
            self.take()
            node = (token[1], node, self.factor())
        return node

    def factor(self) -> Node:
        token = self.take()
        if token is None:
            raise FormulaError("The formula ends unexpectedly.")
        kind, value = token
        if kind == "num":
            return ("num", value)
        if kind == "ref":
            return ("ref", value)
        if value == "-":
            return ("neg", self.factor())
        if value == "(":
            self.depth += 1
            if self.depth > MAX_DEPTH:
                raise FormulaError("Too many nested brackets.")
            node = self.expression()
            closing = self.take()
            if closing != ("sym", ")"):
                raise FormulaError("A bracket is not closed.")
            self.depth -= 1
            return node
        raise FormulaError(f"Unexpected '{value}'.")


@lru_cache(maxsize=512)
def parse(formula: str) -> Node:
    if len(formula) > MAX_FORMULA_LENGTH:
        raise FormulaError(f"A formula can be at most {MAX_FORMULA_LENGTH} characters.")
    node = _Parser(_tokenize(formula)).parse()
    if len(references(node)) > MAX_REFERENCES:
        raise FormulaError(f"A formula can use at most {MAX_REFERENCES} questions.")
    return node


def references(node: Node) -> set[int]:
    kind = node[0]
    if kind == "ref":
        return {node[1]}
    if kind == "num":
        return set()
    if kind == "neg":
        return references(node[1])
    return references(node[1]) | references(node[2])


class _Empty(Exception):
    """The result cannot be a number (not a number in, divide by zero, too large)."""


def _value(node: Node, values: dict[int, Any]) -> Decimal:
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "ref":
        raw = values.get(node[1])
        if raw is None or is_empty(raw):
            return Decimal(0)
        if isinstance(raw, (list, dict)):
            raise _Empty()
        try:
            return parse_number(raw)
        except CoercionError:
            raise _Empty() from None
    if kind == "neg":
        return -_value(node[1], values)
    left, right = _value(node[1], values), _value(node[2], values)
    if kind == "+":
        return left + right
    if kind == "-":
        return left - right
    if kind == "*":
        return left * right
    if right == 0:
        raise _Empty()
    return left / right


def evaluate(formula: str, values: dict[int, Any], decimals: int = 2) -> Decimal | None:
    """The formula's value for ``values`` ({question id: answer}), or None when it has no value."""
    try:
        node = parse(formula)
        result = _value(node, values)
    except (FormulaError, _Empty, ArithmeticError):
        return None
    if abs(result) >= MAX_RESULT:
        return None
    places = min(max(int(decimals), 0), MAX_DECIMALS)
    return result.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
