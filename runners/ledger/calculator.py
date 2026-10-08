from __future__ import annotations

import ast
import re
from decimal import Decimal, DivisionByZero, InvalidOperation


CALC_RE = re.compile(r"^[\d\s+\-*/().xX×÷]+$")
CALC_HAS_OPERATOR_RE = re.compile(r"[\d)]\s*[+\-*/xX×÷]\s*[\d(]")
CALC_TRANSLATION = str.maketrans(
    "０１２３４５６７８９＋－＊／（）×÷",
    "0123456789+-*/()* /".replace(" ", ""),
)


def calculate_expression(text: str) -> str | None:
    expression = normalize_calc_expression(text)
    if not expression or not CALC_RE.fullmatch(expression) or not CALC_HAS_OPERATOR_RE.search(expression):
        return None
    try:
        tree = ast.parse(expression, mode="eval")
        value = _eval_calc_node(tree.body)
    except (SyntaxError, ValueError, InvalidOperation, DivisionByZero, OverflowError):
        return None
    display_expression = re.sub(r"\s+", "", expression)
    return f"{display_expression}={format_expression_result(value)}"


def normalize_calc_expression(text: str) -> str:
    return text.strip().translate(CALC_TRANSLATION).replace("x", "*").replace("X", "*")


def format_calc_result(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def format_expression_result(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.01"))
    if rounded == rounded.to_integral_value():
        return format(rounded, ".0f")
    return format(rounded, ".2f")


def _eval_calc_node(node: ast.AST) -> Decimal:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return Decimal(str(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_calc_node(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        left = _eval_calc_node(node.left)
        right = _eval_calc_node(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if right == 0:
            raise DivisionByZero
        return left / right
    raise ValueError("unsupported expression")
