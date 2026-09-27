"""Pyodide JsProxy 与普通 Python 对象的统一访问。

Workers 上的 bindings（D1 / R2 / env）是 JsProxy，测试替身是 dict 或普通对象；
这里的三个函数让调用方不必区分两者。
"""
from __future__ import annotations

import inspect
from typing import Any


async def maybe_await(value: Any) -> Any:
    """JS Promise（JsProxy）与协程都 await，普通值原样返回。"""
    if inspect.isawaitable(value):
        return await value
    return value


def js_field(obj: Any, name: str, default: Any = None) -> Any:
    """读取 JS 对象属性或 dict 键；缺失时返回 default。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def to_py(value: Any) -> Any:
    """JsProxy → Python 原生对象（深转换）；非 JsProxy 原样返回。"""
    convert = getattr(value, "to_py", None)
    return convert() if callable(convert) else value
