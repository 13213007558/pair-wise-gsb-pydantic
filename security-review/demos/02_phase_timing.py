"""案例 A：字符串注解在 定义期 / 首次验证期 / 显式 rebuild 各自触发什么。

用一个仅写内存日志的表达式 __import__('os').environ.get('X','mark-...') 太接近真实危险，
这里改用纯函数 marker()（只 append 日志），观察求值时机，不触碰任何系统资源。
"""
import importlib.util
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("marker_kit", os.path.join(_here, "01_marker_kit.py"))
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)

from pydantic import create_model  # noqa: E402

# 把只写内存的 marker 暴露到本模块 globals，供字符串前向引用解析时查找
marker = kit.mark
reset = kit.reset
snapshot = kit.snapshot


def show(title):
    print(f"\n== {title} ==")
    for line in snapshot("  "):
        print(line)
    reset()


reset()
print("阶段0 干净日志:", snapshot())

# 字符串里是一个“带函数调用的注解表达式”。marker(...) 只写内存，无其它副作用。
ann = "marker('DEFER:eval-at-resolution') or int"

print("\n[1] create_model（默认 defer_build=False）")
M = create_model("DynModel", field=(ann, ...))
show("定义期副作用")

print("__pydantic_complete__ =", M.__pydantic_complete__)

print("\n[2] 首次 model_validate")
M(field=5)
show("首次验证期副作用")

print("\n[3] 显式 model_rebuild(force=True)")
M.model_rebuild(force=True)
show("显式 rebuild 副作用")
