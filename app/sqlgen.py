"""把已解析的受限契约编译为 PostgreSQL 16 布尔表达式。

报文列约定：暂存表中 `json` 类型的 `payload` 列。生成的表达式可直接放入
WHERE 子句，对每行返回真或假（SQL NULL 判假），语义与
schema_spec.message_valid 一致：

- json 类型保留数字原始文本：用 ->> 取文本 + 整数写法正则区分 1 与 1.0/1e0；
- -> 取字段：键缺省得 SQL NULL，显式 null 得 'null'::json —— 两者天然区分；
- additionalProperties:false 用 CASE 保护的 json_object_keys 做键集合包含判断；
  不用 ::jsonb（大指数数字如 1e999999 会让 jsonb 报 value overflows numeric）；
- ::numeric 转换一律置于 CASE 保护内，保证任何行都不报错、只返真/假；
- 最外层 COALESCE(..., false)：payload 为 SQL NULL 时判假。

已知边界（不属于"两端均可读取的标准 JSON"，试跑会报错并暂不提供表达式）：
- 含未成对代理项转义（\\ud800 等）的报文：PostgreSQL 无法处理，Python 可以；
- 重复键：PostgreSQL 与 Python 均取最后出现的值，行为一致，但非标准 JSON。
"""
from __future__ import annotations

from schema_spec import Branch, FieldSpec, Schema

# JSON 整数写法（RFC 8259：int = zero / (digit1-9 *DIGIT)，可选负号）。
# 1.0、1e0、0.5 等写法不匹配，与 Python json.loads 得到 float 被判定拒绝一致。
INT_TEXT_RE = r"^-?(0|[1-9][0-9]*)$"


def _lit(s: str) -> str:
    """SQL 字符串字面量（standard_conforming_strings=on，只需双写单引号）。"""
    return "'" + s.replace("'", "''") + "'"


def _field_expr(name: str, spec: FieldSpec) -> str:
    """字段存在时的取值判定（调用方保证仅在字段存在时才会生效）。"""
    j = f"(payload -> {_lit(name)})"   # json 值；键缺省时为 SQL NULL
    t = f"(payload ->> {_lit(name)})"  # 文本；JSON null / 键缺省时为 SQL NULL
    opts: list[str] = []
    if spec.allow_null:
        opts.append(f"json_typeof({j}) = 'null'")
    if spec.int_kind == "range":
        cmps = []
        if spec.int_lo is not None:
            cmps.append(f"({t})::numeric >= {spec.int_lo}")
        if spec.int_hi is not None:
            cmps.append(f"({t})::numeric <= {spec.int_hi}")
        body = " AND ".join(cmps) if cmps else "true"
        opts.append(
            f"CASE WHEN json_typeof({j}) = 'number' AND {t} ~ {_lit(INT_TEXT_RE)}"
            f" THEN {body} ELSE false END")
    elif spec.int_kind == "set":
        vals = ", ".join(str(v) for v in sorted(spec.int_set))
        opts.append(
            f"CASE WHEN json_typeof({j}) = 'number' AND {t} ~ {_lit(INT_TEXT_RE)}"
            f" THEN ({t})::numeric IN ({vals}) ELSE false END")
    if spec.str_set:
        vals = ", ".join(_lit(s) for s in sorted(spec.str_set))
        opts.append(f"json_typeof({j}) = 'string' AND {t} IN ({vals})")
    if not opts:  # 受限语法下不可达；防御性返回
        return "false"
    return "(" + " OR ".join(opts) + ")"


def _branch_expr(b: Branch) -> str:
    names = sorted(b.properties)
    if names:
        key_arr = "ARRAY[" + ", ".join(_lit(n) for n in names) + "]::text[]"
    else:
        key_arr = "ARRAY[]::text[]"
    conds = [
        "json_typeof(payload) = 'object'",
        # additionalProperties: false —— 顶层键集合必须 ⊆ 已声明字段。
        # CASE 保护：json_object_keys 只接受对象，对非标量/数组会报错。
        "CASE WHEN json_typeof(payload) = 'object'"
        " THEN NOT EXISTS (SELECT 1 FROM json_object_keys(payload) AS _keys(key)"
        f" WHERE _keys.key <> ALL ({key_arr}))"
        " ELSE false END",
    ]
    for n in names:
        j = f"(payload -> {_lit(n)})"
        if n in b.required:
            conds.append(f"{j} IS NOT NULL")
        conds.append(f"({j} IS NULL OR {_field_expr(n, b.properties[n])})")
    return "(" + "\n  AND ".join(conds) + ")"


def schema_sql(schema: Schema) -> str:
    """编译为可直接放入 WHERE 的布尔表达式；每行必返真/假（SQL NULL 判假）。"""
    alts = [_branch_expr(b) for b in schema.branches]
    body = alts[0] if len(alts) == 1 else "(" + "\n   OR ".join(alts) + ")"
    return f"COALESCE({body}, false)"
