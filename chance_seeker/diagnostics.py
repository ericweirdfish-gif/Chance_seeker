"""把真实 API 响应的结构打印出来，用来校验解析逻辑。

写解析器的时候只能照着文档猜字段，文档和线上实现对不上是常态。
`probe --schema` 会把每个数据源的原始响应结构摊开在日志里，
对着看一眼就知道哪个字段名错了、哪个层级少了一层。
"""

from __future__ import annotations

from typing import Any

MAX_STR = 48


def _typename(value: Any) -> str:
    if value is None:
        return "null"
    return {bool: "bool", int: "int", float: "float", str: "str", list: "[]", dict: "{}"}.get(
        type(value), type(value).__name__
    )


def _preview(value: Any) -> str:
    if isinstance(value, str):
        text = value if len(value) <= MAX_STR else value[:MAX_STR] + "…"
        return f' = "{text}"'
    if isinstance(value, (int, float, bool)) or value is None:
        return f" = {value}"
    return ""


def describe(value: Any, max_depth: int = 4, max_keys: int = 40, _depth: int = 0, _prefix: str = "") -> list[str]:
    """把任意 JSON 渲染成缩进的结构树。

    列表只展开第一个元素——同一个接口返回的数组元素结构是一致的，
    展开全部只会把日志刷爆。
    """
    indent = "  " * _depth
    lines: list[str] = []

    if isinstance(value, dict):
        lines.append(f"{indent}{_prefix}{{}} ({len(value)} keys)")
        if _depth >= max_depth:
            lines.append(f"{indent}  …（已达最大深度）")
            return lines
        for i, (key, item) in enumerate(value.items()):
            if i >= max_keys:
                lines.append(f"{indent}  …还有 {len(value) - max_keys} 个字段")
                break
            if isinstance(item, (dict, list)):
                lines.extend(describe(item, max_depth, max_keys, _depth + 1, f"{key}: "))
            else:
                lines.append(f"{indent}  {key}: {_typename(item)}{_preview(item)}")
        return lines

    if isinstance(value, list):
        lines.append(f"{indent}{_prefix}[] ({len(value)} items)")
        if not value:
            return lines
        if _depth >= max_depth:
            lines.append(f"{indent}  …（已达最大深度）")
            return lines
        lines.extend(describe(value[0], max_depth, max_keys, _depth + 1, "[0] "))
        return lines

    lines.append(f"{indent}{_prefix}{_typename(value)}{_preview(value)}")
    return lines


def render(title: str, payload: Any, max_depth: int = 4) -> str:
    body = "\n".join(describe(payload, max_depth=max_depth))
    return f"\n===== {title} =====\n{body}"


def missing_fields(payload: Any, expected: dict[str, str]) -> list[str]:
    """检查解析器依赖的字段是否真的存在。

    expected 是 {点分路径: 说明}，路径里的 ``[]`` 表示取数组第一个元素，
    例如 ``pairs[].baseToken.symbol``。
    """
    problems = []
    for path, description in expected.items():
        if not _resolve(payload, path):
            problems.append(f"{path}（{description}）")
    return problems


def _resolve(node: Any, path: str) -> bool:
    for part in path.split("."):
        is_array = part.endswith("[]")
        key = part[:-2] if is_array else part

        if key:
            if not isinstance(node, dict) or key not in node:
                return False
            node = node[key]
        if is_array:
            if not isinstance(node, list) or not node:
                return False
            node = node[0]
    return node is not None
