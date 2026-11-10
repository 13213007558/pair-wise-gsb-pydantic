"""只写内存标记工具：所有副作用都只 append 到进程内 list，不做任何 I/O / 网络 / 子进程。"""

LOG: list[str] = []


def mark(event: str) -> None:
    LOG.append(event)


def reset() -> None:
    LOG.clear()


def snapshot(prefix: str = "") -> list[str]:
    return [f"{prefix}{e}" for e in LOG]


class Marker:
    """一个在各生命周期点写入内存日志的无害对象，用来观察“什么时机执行了用户代码”。"""

    def __init__(self, label: str):
        self.label = label
        mark(f"{label}:__init__")

    def __get_pydantic_core_schema__(self, source_type, handler):
        from pydantic_core import core_schema
        mark(f"{label}:__get_pydantic_core_schema__")
        # 仍然委托给 int，保证 schema 可用
        return handler(int)

    @classmethod
    def __get_pydantic_json_schema__(cls, schema, handler):
        mark(f"{cls.__name__}:__get_pydantic_json_schema__")
        return handler(schema)

    def __class_getitem__(cls, item):
        mark(f"{cls.__name__}:__class_getitem__({item!r})")
        return cls


def marker_factory(label: str):
    """模拟一个“带副作用的可调用对象/类型”，仅记录被调用。"""

    def _factory():
        mark(f"{label}:called")
        return int

    _factory.__name__ = f"factory_{label}"
    return _factory
