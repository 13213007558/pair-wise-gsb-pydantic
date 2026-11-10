import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from typing import get_args, get_origin, Union
from safe_type_loader import safe_annotation, parse_annotation, UnsafeAnnotation

ACCEPTED = [
    "int", "str | None", "list[int]", "dict[str, list[int]]",
    "Optional[bool]", "Union[int, str]", "Literal['active', 'inactive']",
    "list[dict[str, int | None]]", "tuple[int, ...]", "set[bytes]",
]
REJECTED = [
    "int and len('x')",          # 表达式/调用
    "int().__class__",           # 属性访问
    "__import__",                # 危险 builtin
    "(lambda: int)()",           # lambda
    "getattr(int, '__name__')",  # 反射调用
    "list[os.PathLike]",         # 未知类型名
    "int" + "[" * 8 + "int" + "]" * 8,  # 超深嵌套（视深度限制）
    "compiler_directive",        # 未知名
    "f'{1}'",                    # f-string
    "int | ",                    # 语法错误
]

print("== 应接受 ==")
for s in ACCEPTED:
    t = safe_annotation(s)
    print(f"  OK  {s:<28} -> {t}")

print("\n== 应拒绝 ==")
for s in REJECTED:
    try:
        safe_annotation(s)
        print(f"  !! 错误地接受了: {s!r}")
    except UnsafeAnnotation as e:
        print(f"  拒绝 {s[:30]!r:<34} 原因: {e}")
