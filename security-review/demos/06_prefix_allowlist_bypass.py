"""案例 E：仅按“字符串前缀/子串”做 allowlist 的典型绕过。
全部 payload 都以受信任前缀 'int' 开头，且用无害函数 len/marker 证明任意表达式可执行。
"""
import typing

def fr_eval(expr):
    return typing.ForwardRef(expr)._evaluate({}, {}, type_params=(), recursive_guard=frozenset())

def naive_prefix_allow(expr: str, allowed=("int", "str", "bool", "float", "list", "dict")):
    return any(expr.lstrip().startswith(a) for a in allowed)

payloads = [
    "int",                                   # 合法基线
    "int and len('pwned-bypass-1')",         # 前缀 int，附带任意函数调用
    "int().__class__",                        # 前缀 int，属性下钻
    "int or __import__",                      # 前缀 int，引用危险 builtin
    "int if True else len('x')",             # 前缀 int，三元表达式
    "int  # comment\n",                       # 前缀 int，注释/空白
    "int.from_bytes",                         # 前缀匹配但语义是方法对象
]

print(f"{'naive允许?':<10}{'可求值?':<8}payload")
for p in payloads:
    allowed = naive_prefix_allow(p)
    try:
        val = fr_eval(p)
        evald = f"是 -> {repr(val)[:30]}"
    except Exception as e:
        evald = f"否({type(e).__name__})"
    print(f"{str(allowed):<10}{evald:<8}{p!r}")

print("\n结论：naive 前缀检查对 7 条全部放行，但只有第 1 条是真正想要的类型注解。")
print("再看子串/黑名单同样脆弱（只演示构造，不实际调用危险函数）：")
more = [
    "(len('x'), int)[1]",      # int 只是元组里的一个元素
    "getattr(int, '__name__')",# 不含被禁关键字 eval/exec/open，却能反射
    "(lambda: int)()",         # lambda 产出 int，前缀规则完全不理解结构
]
for p in more:
    print("  ", repr(p), "-> 求值", repr(fr_eval(p))[:40])
