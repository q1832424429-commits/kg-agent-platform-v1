"""静态识别用户 Python 脚本入口签名和字段访问，不导入或执行用户代码。"""

from __future__ import annotations

import ast
from typing import Any


def inspect_script_functions(source: bytes) -> dict[str, Any]:
    """返回可确认的顶层函数参数和标准双参数入口存在性。"""

    tree = ast.parse(source.decode("utf-8"))
    functions: list[dict[str, object]] = []
    standard_entrypoint = False
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        names = [item.arg for item in args.args]
        if (
            node.name == "run"
            and names == ["inputs", "parameters"]
            and not (args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg)
        ):
            standard_entrypoint = True
        if node.name.startswith("_") or not 1 <= len(names) <= 6:
            continue
        if args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg:
            continue
        accessed: dict[str, set[str]] = {name: set() for name in names}
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Subscript)
                and isinstance(child.value, ast.Name)
                and child.value.id in accessed
                and isinstance(child.slice, ast.Constant)
                and isinstance(child.slice.value, str)
            ):
                accessed[child.value.id].add(child.slice.value)
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "get"
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id in accessed
                and child.args
                and isinstance(child.args[0], ast.Constant)
                and isinstance(child.args[0].value, str)
            ):
                accessed[child.func.value.id].add(child.args[0].value)
        functions.append(
            {
                "function_name": node.name,
                "parameters": names,
                "async": isinstance(node, ast.AsyncFunctionDef),
                "accessed_keys": {name: sorted(keys) for name, keys in accessed.items() if keys},
            }
        )
    return {"functions": functions, "standard_entrypoint": standard_entrypoint}


def detect_function_field_paths(
    source: bytes, function_name: str, *, required_only: bool = False
) -> dict[str, list[str]]:
    """静态追踪函数参数、局部别名及列表迭代变量的字段读取路径。

    只报告字面量键；动态键、跨函数调用和运行时生成的结构不会猜测。
    路径中的 `*` 表示对列表的每一项读取同一字段。required_only
    只包含方括号必取路径，避免把带默认值的 get 当作必需字段。
    """

    tree = ast.parse(source.decode("utf-8"))
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ),
        None,
    )
    if function is None:
        return {}
    names = [argument.arg for argument in function.args.args]
    aliases: dict[str, tuple[str, ...]] = {name: (name,) for name in names}
    found: dict[str, set[str]] = {name: set() for name in names}

    def path_of(node: ast.AST) -> tuple[str, ...] | None:
        """解析当前别名环境中可确定的字面量访问路径。"""

        if isinstance(node, ast.Name):
            return aliases.get(node.id)
        if isinstance(node, ast.BoolOp):
            return next((path for value in node.values if (path := path_of(value))), None)
        if isinstance(node, ast.Subscript):
            parent = path_of(node.value)
            if (
                parent
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                return (*parent, node.slice.value)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            parent = path_of(node.func.value)
            if parent:
                return (*parent, node.args[0].value)
        return None

    def visit(node: ast.AST) -> None:
        """按源码顺序建立局部别名，并记录所有静态字段访问。"""

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            # AST 把结果表达式排在生成器之前；先绑定逐项变量才能识别
            # [row["id"] for row in payload["items"]] 这类常见旧脚本。
            for generator in node.generators:
                visit(generator)
            visit(node.elt)
            return
        if isinstance(node, ast.DictComp):
            for generator in node.generators:
                visit(generator)
            visit(node.key)
            visit(node.value)
            return
        if isinstance(node, ast.Assign):
            value_path = path_of(node.value)
            if value_path:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases[target.id] = value_path
        if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            iterable = path_of(node.iter)
            if iterable and isinstance(node.target, ast.Name):
                aliases[node.target.id] = (*iterable, "*")
        if isinstance(node, (ast.Subscript, ast.Call)):
            field_path = path_of(node)
            if (
                field_path
                and len(field_path) > 1
                and field_path[0] in found
                and (not required_only or isinstance(node, ast.Subscript))
            ):
                found[field_path[0]].add("/" + "/".join(field_path[1:]))
        for child in ast.iter_child_nodes(node):
            visit(child)

    for statement in function.body:
        visit(statement)
    return {name: sorted(paths) for name, paths in found.items() if paths}
