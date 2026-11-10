"""接入示例：接口平台收到 {字段名: 注解字符串} 映射后安全动态建模。

关键：先用 safe_annotation 把每个字符串映射成受信任类型对象，
只有全部字段通过后才调用 create_model；create_model 收到的是真实类型，而非字符串，
因此 Pydantic 的 ForwardRef eval 路径根本不会被用户输入触发。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pydantic import create_model, ValidationError  # noqa: E402
from safe_type_loader import safe_annotation, UnsafeAnnotation  # noqa: E402


class RejectedSchema(ValueError):
    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))


def build_model_from_mapping(model_name: str, mapping: dict[str, str]):
    """mapping: {字段名: 注解字符串}。任一字段不安全则整体拒绝，不创建任何模型。"""
    resolved: dict[str, type] = {}
    errors: dict[str, str] = {}
    for field_name, ann_text in mapping.items():
        try:
            resolved[field_name] = safe_annotation(ann_text)
        except UnsafeAnnotation as e:
            errors[field_name] = str(e)
    if errors:
        raise RejectedSchema(errors)
    # 此处传入的全是受信任类型对象，不含任何用户字符串
    return create_model(model_name, **{k: (v, ...) for k, v in resolved.items()})


def demo():
    good_mapping = {
        "user_id": "int",
        "email": "str | None",
        "tags": "list[str]",
        "status": "Literal['active', 'inactive']",
        "meta": "dict[str, list[int]]",
    }
    User = build_model_from_mapping("User", good_mapping)
    print("== 合法映射建模成功 ==")
    print("  fields:", {k: str(v.annotation) for k, v in User.model_fields.items()})
    u = User(user_id="42", email=None, tags=["a"], status="active", meta={"x": [1, 2]})
    print("  验证通过:", u.model_dump())

    print("\n== 非法输入触发校验错误（不建模）==")
    try:
        User(user_id="not-int", email=None, tags=[], status="nope", meta={})
    except ValidationError as e:
        for err in e.errors():
            print(f"  字段 {'.'.join(map(str, err['loc']))}: {err['type']}")

    print("\n== 恶意映射在 AST 阶段被整体拒绝 ==")
    evil_mapping = {
        "ok_field": "int",
        "attack": "int and __import__('os').system('echo pwned')",
    }
    try:
        build_model_from_mapping("Evil", evil_mapping)
    except RejectedSchema as e:
        print("  拒绝原因:", e.errors)
    print("  （未调用任何系统命令；字符串从未进入 eval）")


if __name__ == "__main__":
    demo()
