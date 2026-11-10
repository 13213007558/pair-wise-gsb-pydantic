"""安全的“注解字符串 -> 类型”加载器。

核心原则：
1. 绝不调用 eval/exec/compile；只用 ast 解析成语法树，再按节点白名单手工映射。
2. 用户字符串先转成受限的“数据描述”（纯 dataclass 树），再映射到进程内固定的类型表。
3. 任何白名单外的节点（Call/Attribute/Lambda/Subscript 里的未知泛型等）一律拒绝。
4. 限制嵌套深度与参数个数，防止资源耗尽。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Annotated, Any, Literal, Optional, Union


class UnsafeAnnotation(ValueError):
    """注解字符串不在受支持的有限集合内。"""


@dataclass(frozen=True)
class TypeDescriptor:
    """受限数据描述：与具体 Python 对象解耦，可序列化为 JSON、可审计、可重放。"""

    kind: str
    args: tuple["TypeDescriptor", ...] = ()
    literal: Any = None
    max_depth: int = 0


MAX_DEPTH = 5
MAX_GENERIC_ARGS = 8
_ALLOWED_SCALARS = {
    "int": int,
    "str": str,
    "bool": bool,
    "float": float,
    "bytes": bytes,
    "date": date,
    "datetime": datetime,
    "time": time,
    "UUID": __import__("uuid").UUID,
}
_ALLOWED_CONTAINERS = {"list": list, "dict": dict, "set": set, "tuple": tuple}
_SENTINEL_NONE = type(None)


def _parse_node(node: ast.AST, depth: int) -> TypeDescriptor:
    if depth > MAX_DEPTH:
        raise UnsafeAnnotation(f"嵌套深度超过 {MAX_DEPTH}")

    # 裸名字：int / str / bool / None ...
    if isinstance(node, ast.Name):
        if node.id == "None":
            return TypeDescriptor("none")
        if node.id in _ALLOWED_SCALARS:
            return TypeDescriptor("scalar", (), node.id)
        raise UnsafeAnnotation(f"不支持的类型名: {node.id!r}")

    # 常量 None（如 Optional 里的 X | None）
    if isinstance(node, ast.Constant):
        if node.value is None:
            return TypeDescriptor("none")
        if node.value is Ellipsis:
            return TypeDescriptor("ellipsis")
        raise UnsafeAnnotation("仅允许 None / ... 常量，字面量请走 Literal[...]")

    # 下标泛型：list[int] / dict[str, int] / Optional[X] / Union[...] / Literal[...] / Annotated[...]
    if isinstance(node, ast.Subscript):
        return _parse_subscript(node, depth)

    # X | Y 联合（BinOp with BitOr）
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _parse_node(node.left, depth + 1)
        right = _parse_node(node.right, depth + 1)
        return TypeDescriptor("union", (left, right))

    raise UnsafeAnnotation(f"不支持的语法节点: {type(node).__name__}")


def _slice_nodes(node: ast.Subscript) -> list[ast.AST]:
    sl = node.slice
    if isinstance(sl, ast.Tuple):
        return list(sl.elts)
    return [sl]


def _parse_subscript(node: ast.Subscript, depth: int) -> TypeDescriptor:
    if not isinstance(node.value, ast.Name):
        raise UnsafeAnnotation("泛型外层必须是受信任名字，如 list/dict/Optional")
    outer = node.value.id
    slices = _slice_nodes(node)
    if len(slices) > MAX_GENERIC_ARGS:
        raise UnsafeAnnotation(f"泛型参数个数超过 {MAX_GENERIC_ARGS}")

    if outer in _ALLOWED_CONTAINERS:
        args = tuple(_parse_node(s, depth + 1) for s in slices)
        return TypeDescriptor("container", args, outer)

    if outer in ("Optional",):
        if len(slices) != 1:
            raise UnsafeAnnotation("Optional 只接受一个参数")
        return TypeDescriptor("union", (_parse_node(slices[0], depth + 1), TypeDescriptor("none")))

    if outer in ("Union",):
        return TypeDescriptor("union", tuple(_parse_node(s, depth + 1) for s in slices))

    if outer == "Literal":
        values = []
        for s in slices:
            if not isinstance(s, ast.Constant) or not isinstance(s.value, (str, int, float, bool)):
                raise UnsafeAnnotation("Literal 仅支持 str/int/float/bool 字面量")
            values.append(s.value)
        return TypeDescriptor("literal", tuple(), tuple(values))

    raise UnsafeAnnotation(f"不支持的泛型/构造: {outer!r}")


def parse_annotation(text: str) -> TypeDescriptor:
    """字符串 -> 数据描述（安全，不执行任何用户表达式）。"""
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        raise UnsafeAnnotation(f"语法无法解析: {e.msg}") from e
    return _parse_node(tree.body, 0)


def build_type(desc: TypeDescriptor) -> Any:
    """数据描述 -> 进程内已有类型对象（映射，不 eval）。"""
    if desc.kind == "none":
        return _SENTINEL_NONE
    if desc.kind == "scalar":
        return _ALLOWED_SCALARS[desc.literal]
    if desc.kind == "container":
        origin = _ALLOWED_CONTAINERS[desc.literal]
        if desc.literal == "tuple" and len(desc.args) == 2 and desc.args[1].kind == "ellipsis":
            return tuple[build_type(desc.args[0]), ...]
        if any(a.kind == "ellipsis" for a in desc.args):
            raise UnsafeAnnotation("省略号 ... 仅允许出现在 tuple[X, ...] 形式中")
        return origin[tuple(build_type(a) for a in desc.args)]
    if desc.kind == "union":
        return Union[tuple(build_type(a) for a in desc.args)]
    if desc.kind == "literal":
        return Literal[desc.literal]  # type: ignore[valid-type]
    raise UnsafeAnnotation(f"未知描述类型: {desc.kind}")


def safe_annotation(text: str) -> Any:
    """对外主入口：字符串直接得到受信任类型，或抛 UnsafeAnnotation。"""
    return build_type(parse_annotation(text))
