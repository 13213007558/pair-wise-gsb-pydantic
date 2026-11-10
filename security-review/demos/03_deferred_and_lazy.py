"""案例 B：defer_build 与未解析 ForwardRef 的真实执行时机。"""
import importlib.util, os
_here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("marker_kit", os.path.join(_here, "01_marker_kit.py"))
kit = importlib.util.module_from_spec(spec); spec.loader.exec_module(kit)

from pydantic import create_model  # noqa: E402

marker, reset, snapshot = kit.mark, kit.reset, kit.snapshot

def show(t):
    print(f"\n== {t} ==")
    print("\n".join("  "+e for e in snapshot()) or "  (空)")
    reset()

print("# B1  defer_build=True：字符串仍在定义期被 eval（字段收集阶段）")
reset()
M1 = create_model("Deferred", __config__={"defer_build": True},
                  field=("marker('B1:eval-during-field-collection') or int", ...))
show("create_model 后")
print("  complete =", M1.__pydantic_complete__, "（schema 被推迟，但字符串已经求值）")
M1(field=1)
show("首次验证（无新 eval）")

print("\n# B2  未解析 ForwardRef：定义期 NameError 被降级，rebuild 时才真正求值")
reset()
M2 = create_model("Lazy", field=("NotYetDefined", ...))
show("create_model 后（未解析，无 eval）")
print("  complete =", M2.__pydantic_complete__, " validator =", type(M2.__pydantic_validator__).__name__)
try:
    M2(field=1)
except Exception as e:
    print("  首次验证 ->", type(e).__name__)

# 之后才把名字放进模块 globals（模拟“前向引用后定义”）
globals()["NotYetDefined"] = int
ok = M2.model_rebuild()
show("显式 model_rebuild 后")
print("  rebuild 返回 =", ok, " complete =", M2.__pydantic_complete__)
M2(field="123")
show("rebuild 后验证（正常 int 强转，无 eval）")
