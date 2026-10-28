"""Rule conditions (`observed >= baseline + 0.02 AND n >= 1000`) evaluated without `eval`.

A condition is parsed once into a Python AST and only arithmetic, comparisons, boolean logic, numbers,
variable names and `abs`/`min`/`max` are allowed. A variable the observation does not carry makes the
condition *unknown* (None), never true: an anti-noise gate then keeps its current state instead of guessing.
"""
from __future__ import annotations

import ast
import operator
import re
from collections.abc import Mapping
from typing import Any

from analystos.core.errors import InvalidInput

MAX_LENGTH = 500
_KEYWORDS = re.compile(r"\b(AND|OR|NOT)\b")
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_CMP = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Eq: operator.eq,
        ast.NotEq: operator.ne}
_FUNCS = {"abs": abs, "min": min, "max": max}


class _Unknown(Exception):
    """A name the observation does not carry, or a value that is not a number."""


def parse(expression: str) -> ast.Expression:
    """Parse and check a condition; InvalidInput names what is not allowed."""
    if not isinstance(expression, str) or not expression.strip():
        raise InvalidInput("a condition must be a non-empty expression")
    if len(expression) > MAX_LENGTH:
        raise InvalidInput(f"a condition is at most {MAX_LENGTH} characters")
    source = _KEYWORDS.sub(lambda m: m.group(1).lower(), expression)
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise InvalidInput(f"condition {expression!r} is not a valid expression: {exc.msg}") from None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd, ast.Load)):
            continue
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
            continue
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            continue
        if isinstance(node, ast.Compare) and all(type(op) in _CMP for op in node.ops):
            continue
        if type(node) in _BIN or type(node) in _CMP:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, bool)):
            continue
        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                raise InvalidInput(f"condition {expression!r}: names may not start with an underscore")
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            continue
        raise InvalidInput(f"condition {expression!r} uses {type(node).__name__}, which is not allowed "
                           "(numbers, names, + - * /, comparisons, and/or/not, abs/min/max only)")
    return tree


def names(expression: str) -> set[str]:
    tree = parse(expression)
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id not in _FUNCS}


def _num(value: Any) -> float | bool:
    if isinstance(value, (bool, int, float)):
        return value
    raise _Unknown


def _eval(node: ast.AST, env: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, env)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in env or env[node.id] is None:
            raise _Unknown
        return _num(env[node.id])
    if isinstance(node, ast.BoolOp):
        values = [bool(_eval(v, env)) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, env)
        return (not v) if isinstance(node.op, ast.Not) else (-v if isinstance(node.op, ast.USub) else +v)
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left, env), _eval(node.right, env)
        if isinstance(node.op, ast.Div) and right == 0:
            raise _Unknown
        return _BIN[type(node.op)](left, right)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, env)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval(comparator, env)
            if not _CMP[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        return _FUNCS[node.func.id](*[_eval(a, env) for a in node.args])  # type: ignore[union-attr]
    raise _Unknown


def evaluate(expression: str, env: Mapping[str, Any]) -> bool | None:
    """True/False, or None when the observation lacks a value the condition needs."""
    tree = parse(expression)
    try:
        return bool(_eval(tree, env))
    except _Unknown:
        return None
    except (TypeError, ValueError, OverflowError):
        return None
