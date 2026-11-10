"""案例 F：为什么“关掉某一个 hook”不等于通用 Python 沙箱。

即使禁止自定义 __get_pydantic_core_schema__，字符串前向引用的 eval 入口依然存在，
TypeAdapter / create_model / model_rebuild 是相互独立的触发面，且 builtins 默认注入。
所有 payload 只用 len/int 等无害符号证明“表达式可执行”，不触碰系统资源。
"""
import builtins
import typing
from pydantic import TypeAdapter, create_model

def fr_eval(expr):
    return typing.ForwardRef(expr)._evaluate({}, {}, type_params=(), recursive_guard=frozenset())

# 1) 不依赖任何自定义 hook，纯内置名字即可在注解里执行函数调用
print("1) 无自定义 hook，注解表达式仍可调用 builtins.len ->", fr_eval("len('side-effect-proof')"))

# 2) TypeAdapter 是独立于 create_model 的 eval 触发面；用 (0 and len()) or int 保证回落到类型
safe_expr = "(0 and len('abc')) or int"
ta = TypeAdapter(safe_expr)
print("2) TypeAdapter 对表达式注解完成建模 ->", ta.validate_python("7"),
      "| 解析得到类型:", fr_eval(safe_expr).__name__)

# 3) create_model 同样直接 eval 表达式，无需任何用户类型/hook
M = create_model("M", x=(safe_expr, ...))
print("3) create_model 字段表达式已求值，complete =", M.__pydantic_complete__)

# 4) builtins 里默认可达的“能力面”（仅列举存在性，不调用）
dangerous = ["__import__", "eval", "exec", "compile", "getattr", "setattr",
             "globals", "locals", "open", "breakpoint"]
reachable = [n for n in dangerous if hasattr(builtins, n)]
print("4) builtins 中可被注解直接按名引用的危险符号:", reachable)

print("""
结论：
- 关闭 __get_pydantic_core_schema__ / __get_validators__ 只消除“用户类型回调”这一条路径；
- ForwardRef._evaluate 的 eval、TypeAdapter、create_model、model_rebuild 仍是独立触发面；
- builtins 自动注入，使 __import__/eval/exec/open 等默认可按名引用；
- 故不存在“关掉单个 hook 即安全”的开关。安全边界应建立在“不让用户字符串进入 eval”，
  即 AST 白名单 + 受限数据描述映射，而非禁用某个 Pydantic 特性。
""")
