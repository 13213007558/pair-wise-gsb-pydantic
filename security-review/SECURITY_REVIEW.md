# Pydantic 动态建模安全可行性审查（基于源码）

- 环境：Python 3.12.11，Pydantic 2.12.3（工作区 vendored 源码）
- 范围：用户提交 `{字段名: 注解字符串}` 映射，经 `create_model` / `TypeAdapter` 动态建模
- 方法：源码追踪 + 只写内存标记探针，不启动外部命令、不读真实秘密、无网络/文件副作用
- 结论速览：**不能把用户注解字符串当可信输入交给 Pydantic 的默认求值路径。** 字符串会进入
  `ForwardRef._evaluate` 的 `eval`，且 builtins 自动注入。推荐改为 AST 白名单 → 受限数据描述 →
  映射到固定类型表，让用户字符串永远不进入 `eval`。

## 1. 源码中的求值链路

字符串注解在 Pydantic 内部不是“数据”，而是会被求值的前向引用：

- `create_model` 把字符串原样放进 `__annotations__`（`pydantic/main.py:1779`、`pydantic/main.py:1788`）。
- 元类 `ModelMetaclass.__new__` 调用 `set_model_fields` → `rebuild_model_fields`，
  其中 `eval_type(field_info._original_annotation, *ns_resolver.types_namespace)`
  对字符串做求值（`pydantic/_internal/_fields.py:442`、`pydantic/_internal/_fields.py:595`）。
- 求值最终落到 `typing._eval_type` → `ForwardRef._evaluate`（`pydantic/_internal/_typing_extra.py:515`），
  以及 schema 生成期的 `_resolve_forward_ref`（`pydantic/_internal/_generate_schema.py:948`）。
- 命名空间由 `NsResolver` 组装，globalns 取模块 `__dict__`，localns 取类/父帧命名空间
  （`pydantic/_internal/_namespace_utils.py:232`、`pydantic/_internal/_namespace_utils.py:250`）。
- `TypeAdapter` 是独立触发面，默认取调用帧的 `f_globals` / `f_locals`
  （`pydantic/type_adapter.py:235`、`pydantic/type_adapter.py:245`）。
- 自定义 schema hook：`__get_pydantic_core_schema__` 在 schema 生成期被回调
  （`pydantic/_internal/_generate_schema.py:889`、`pydantic/_internal/_generate_schema.py:919`）。

## 2. 三个时机各执行什么（探针实测）

| 用户代码形态 | 模型定义期 `create_model` | 首次验证期 `model_validate` | 显式 `model_rebuild` |
|---|---|---|---|
| 可解析的字符串注解（含表达式） | **立即 eval** | 不执行 | force 时重新 eval |
| `defer_build=True` 的字符串 | **仍 eval**（字段收集阶段，仅推迟 core schema） | 不执行 | 重新走 schema |
| 未解析 ForwardRef（名字缺失） | NameError 被降级为 `MockValSer`，不 eval | 抛 `PydanticUserError` | 名字就绪后**此时才 eval** |
| 类型上的 `__get_pydantic_core_schema__` | schema 生成期调用 | 不调用 | force 时再次调用 |
| `AfterValidator` / `field_validator` | 不调用 | **每次验证调用** | 不调用 |

关键反直觉点：`defer_build=True` **不能**阻止字符串求值——`rebuild_model_fields`
在元类阶段就已运行（见 `demos/03_deferred_and_lazy.py`）。

## 3. 命名空间：局部、模块、内置三层（探针实测）

- 解析顺序为 locals 优先于 globals（`demos/05_namespaces.py` 第 2 项）。
- 即使传入空 `{}` 命名空间，`ForwardRef._evaluate` 也会由 CPython 自动注入 `__builtins__`，
  因此 `int`、`len`、`__import__` 等默认可按名解析（同脚本第 1 项）。
- `TypeAdapter` 默认借用调用帧的 globals/locals，意味着注解字符串能看到调用现场可见的模块级名字。
- `model_rebuild(_types_namespace=...)` / `TypeAdapter.rebuild` 可显式注入 local 层，
  但同样无法移除 builtins。
- 注解位置接受的是任意 Python 表达式而非仅类型名，`len('x') or int` 这类函数调用可被求值。

## 4. 为什么“字符串前缀 allowlist”不足

前缀/子串匹配不理解语法结构，无法区分“类型名”与“以该名开头的任意表达式”。
`demos/06_prefix_allowlist_bypass.py` 中以下 payload 全部以受信任前缀 `int` 开头却被放行：

- `int and len('...')`（BoolOp 附带任意调用）
- `int().__class__`（Attribute 下钻）
- `int or __import__`（引用危险 builtin）
- `int if True else len('x')`（三元）
- `int  # comment`（注释/空白）
- `getattr(int, '__name__')`、`(lambda: int)()`（无黑名单关键字仍可反射/匿名函数）

黑名单同理脆弱：无法枚举所有危险内省路径，且 Python 语法糖（元组下标、lambda、f-string 等）
可轻易改写表面形态。**正确边界是语法级白名单（AST 节点类型），不是词法级字符串匹配。**

## 5. “关掉某个 hook”不是通用沙箱

禁用 `arbitrary_types_allowed` 或不使用自定义 `__get_pydantic_core_schema__` /
`__get_validators__`，只消除了“用户类型回调”这一条路径。下列攻击面相互独立、仍然存在
（见 `demos/07_no_single_hook_sandbox.py`）：

- `ForwardRef._evaluate` 的 `eval` 入口；
- `create_model`、`TypeAdapter`、`model_rebuild` 三个独立触发点；
- builtins 自动注入带来的 `__import__ / eval / exec / compile / open / breakpoint ...`。

因此不存在“关闭单一开关即获得 Python 沙箱”的配置。真正的边界必须保证用户字符串不进入 `eval`。

## 6. 推荐方案：受限数据描述 + 固定类型映射

实现见 `safe_integration/safe_type_loader.py`：

1. `ast.parse(..., mode="eval")` 仅做语法解析，不执行；
2. 递归遍历，只接受白名单节点：`Name`（固定标量）、`Subscript`（固定容器/`Optional`/`Union`/`Literal`）、
   `BinOp(BitOr)` 联合、`None`/`...` 常量；
3. `Call`/`Attribute`/`Lambda`/`JoinedStr`/未知名等一律抛 `UnsafeAnnotation`；
4. 限制嵌套深度（默认 5）与泛型参数个数（默认 8），防资源耗尽；
5. 先得到纯数据 `TypeDescriptor`（可 JSON 化、可审计、可重放），再映射到进程内固定类型对象；
6. 全部字段通过后才调用 `create_model`，此时传入的是受信任类型对象，Pydantic 的字符串 eval 路径不被触发。

### 明确支持的组合

- 标量：`int str bool float bytes date datetime time UUID`
- 容器：`list[...] dict[K, V] set[...] tuple[X, ...]`
- 联合：`X | Y`、`Optional[X]`、`Union[X, Y]`
- 字面量：`Literal['a', 1, True]`（仅 str/int/float/bool）
- 以上可在深度/参数上限内嵌套，如 `dict[str, list[int | None]]`

### 明确拒绝方式

- 任何函数调用、属性访问、lambda、f-string、未知类型名 → `UnsafeAnnotation`，整份 schema 不建模
  （`build_model_from_mapping` 聚合所有字段错误后抛 `RejectedSchema`）。
- 语法错误、超深、超参数个数同样拒绝。
- 验收样例：`safe_integration/test_safe_loader.py`（10 接受 / 10 拒绝）、
  `safe_integration/platform_integration.py`（端到端，含恶意 payload 被 AST 拦截）。

## 7. 哪些输入适合转为数据描述

适合：字段类型本身可枚举、结构规则化的场景——表单/API 契约、ETL 列类型、低代码建模、
配置校验等。把“类型”表达成 JSON AST（如 `{"kind":"container","name":"list","args":[...]}`）
比传 Python 源码字符串更稳，可跨语言、可做权限审计。

不适合直接支持、应在平台侧另建受控能力的：自定义类、带业务方法的校验器、`Annotated[..., 约束]`
中任意可调用对象、泛型 TypeVar、递归模型。若确需约束（如字符串长度、数值范围），应提供**预置、
不可被用户指定函数**的约束枚举（如 `{"base":"str","max_length":100}`），由平台映射到
`pydantic.Field` / `Annotated`，而不是让用户提交可执行表达式。

## 8. 复现方式

```bash
cd security-review
./run_all_demos.sh        # 依次重放 demos/0*.py 与 safe_integration 两个脚本
```

所有脚本仅在解释器进程内运行，副作用统一写入内存 list（`demos/01_marker_kit.py`），
无 subprocess/socket/文件写入；危险符号仅作为“会被拒绝的字符串样例”出现。
