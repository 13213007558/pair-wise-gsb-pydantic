"""案例 C：区分三类用户代码的执行时机。
- 前向引用字符串的 eval
- 类型上的 __get_pydantic_core_schema__ 自定义 schema hook
- 验证期的 validator 方法
全部仅写内存日志。
"""
import importlib.util, os
from typing import Annotated
_here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("marker_kit", os.path.join(_here, "01_marker_kit.py"))
kit = importlib.util.module_from_spec(spec); spec.loader.exec_module(kit)
from pydantic import create_model, field_validator, AfterValidator
marker, reset, snapshot = kit.mark, kit.reset, kit.snapshot

class HookType(int):
    """带自定义 schema hook 的类型：hook 在 schema 生成期运行，不是验证期。"""
    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        marker("C:schema-hook(__get_pydantic_core_schema__)")
        return handler(int)

def check(v):
    marker("C:AfterValidator(validation-time)")
    return v

# 场景1：类型可直接解析 -> 定义期即生成 schema，hook 在定义期运行
print("== 场景1 可解析类型 + 自定义 hook + AfterValidator ==")
reset()
M1 = create_model(
    "M1",
    a=(HookType, ...),
    b=(Annotated[int, AfterValidator(check)], ...),
)
print("定义期日志:", snapshot()); reset()
M1(a=1, b=2)
print("验证期日志:", snapshot()); reset()
M1.model_rebuild(force=True)
print("强制 rebuild 日志:", snapshot()); reset()

# 场景2：前向引用缺失 -> hook 延迟到 rebuild
print("\n== 场景2 前向引用缺失，hook 延迟到 rebuild ==")
M2 = create_model("M2", c=("LateHook", ...))
print("定义期 complete =", M2.__pydantic_complete__, "日志:", snapshot() or "(空)"); reset()
globals()["LateHook"] = HookType
M2.model_rebuild()
print("rebuild 日志:", snapshot()); reset()
M2(c=1)
print("验证期日志:", snapshot() or "(空)")
