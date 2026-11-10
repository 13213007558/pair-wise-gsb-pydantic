"""案例 D：局部 / 模块 / builtins 命名空间的解析来源与顺序。仅用无害对象，无 I/O。"""
import typing
from pydantic import TypeAdapter, create_model

def fr_eval(expr, g=None, l=None):
    return typing.ForwardRef(expr)._evaluate(g or {}, l or {}, type_params=(), recursive_guard=frozenset())

# 1) 空命名空间：CPython 在 ForwardRef._evaluate 内自动注入 builtins
print("1) 空 ns 解析 'int' ->", fr_eval("int").__name__)
print("   空 ns 解析 builtins 函数 'len' ->", fr_eval("len").__name__)
print("   空 ns 解析 '__import__' ->", fr_eval("__import__").__name__)

# 2) 解析顺序 locals 优先于 globals（用合法类型 int/str 做遮蔽）
G, L = {"shadow": str}, {"shadow": int}
print("2) 仅 globals ->", fr_eval("shadow", G, {}).__name__)
print("   locals 覆盖 globals ->", fr_eval("shadow", G, L).__name__)

# 3) Pydantic 侧：模块 globals 是默认解析域（TypeAdapter 取调用帧 f_globals/f_locals）
SOME_LOCAL_ALIAS = int
ta = TypeAdapter("SOME_LOCAL_ALIAS")
print("3) TypeAdapter 经调用帧命名空间解析 ->", ta.validate_python("42"),
      type(ta.validate_python("42")).__name__)

# 4) create_model + rebuild 注入 _types_namespace，控制 local 层
M = create_model("NS", x=("shadow", ...))
M.model_rebuild(force=True, _types_namespace={"shadow": int})
print("4) rebuild 注入 local 命名空间 ->", M.model_fields["x"].annotation.__name__)

# 5) 注解里可以是任意表达式，而不只是类型名（用 len 证明可执行函数调用，无副作用）
expr = "len('abcd') or int"
print("5) 表达式注解可求值 ->", fr_eval(expr), "（值为 int，因为 len 结果为真值）")
