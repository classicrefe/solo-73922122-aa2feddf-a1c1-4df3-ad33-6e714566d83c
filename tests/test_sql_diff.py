"""库内判定差分测试：sqlgen 生成的 PostgreSQL 16 表达式 vs 纯 Python 判定。

需要 PostgreSQL（默认读环境变量 DIFF_DATABASE_URL 或 DATABASE_URL）：
    DIFF_DATABASE_URL=postgresql://water:water@127.0.0.1:55432/contracts \
        python3 tests/test_sql_diff.py

覆盖：
- 手写边界电池：整数写法 1 / 1.0 / 1e0 / -0 / 大整数、缺省 vs 显式 null、
  anyOf 各分支、非对象报文、多余字段、布尔、字符串枚举、空 properties 等；
- 随机模糊：随机契约（含特殊字符字段名/枚举值）× 随机报文文本，
  逐条对照 message_valid 与库内表达式结果，并保证表达式对任何行不报错。
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import psycopg2  # noqa: E402

from schema_spec import message_valid, parse_schema  # noqa: E402
from sqlgen import schema_sql  # noqa: E402

DB_URL = os.environ.get("DIFF_DATABASE_URL") or os.environ.get(
    "DATABASE_URL", "postgresql://water:water@127.0.0.1:55432/contracts")
SEED = int(os.environ.get("DIFF_SEED", "20261121"))
ROUNDS = int(os.environ.get("DIFF_ROUNDS", "400"))      # 随机契约份数
MSGS_PER_ROUND = 60                                      # 每份契约的报文数

PASS = []
FAIL = []


def ok(name, cond, extra=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append(f"{name} {extra}")
        print(f"  ✘ {name} {extra}")


# ---------- 报文文本构造（保留数字写法，json 列存原文） ----------

def J(value):
    """把 Python 值序列化为紧凑 JSON 文本（数字写法由调用方给定）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sql_batch_check(cur, expr, lines):
    """批量库内判定：返回每行 True/False；任何异常向上抛。"""
    vals = ", ".join(f"({i}, CAST(%s AS json))" for i in range(len(lines)))
    cur.execute(
        f"SELECT t.idx, ({expr}) AS ok FROM (VALUES {vals}) AS t(idx, payload)"
        f" ORDER BY t.idx",
        lines)
    got = cur.fetchall()
    assert [i for i, _ in got] == list(range(len(lines))), "行序错乱"
    return [bool(v) for _, v in got]


def compare_battery(cur, name, doc, lines):
    schema = parse_schema(doc)
    expr = schema_sql(schema)
    sql_res = sql_batch_check(cur, expr, lines)
    py_res = [message_valid(json.loads(ln), schema) for ln in lines]
    bad = [(ln, p, s) for ln, p, s in zip(lines, py_res, sql_res) if p != s]
    ok(name, not bad, f"分歧: {bad[:3]}")


# ---------- 手写边界电池 ----------

def hand_battery(cur):
    temp_qc = {
        "type": "object",
        "properties": {
            "temp": {"type": "integer", "minimum": -40, "maximum": 100},
            "site": {"type": "string", "enum": ["WQ-01", "WQ-02"]},
            "qc": {"enum": ["N", "Q", None]},
        },
        "required": ["temp", "site"],
        "additionalProperties": False,
    }
    compare_battery(cur, "电池:温度+质控码", temp_qc, [
        '{"temp": 21, "site": "WQ-01"}',
        '{"temp": -40, "site": "WQ-02", "qc": "N"}',
        '{"temp": 100, "site": "WQ-01", "qc": null}',
        '{"temp": 21, "site": "WQ-01", "qc": "Z"}',
        '{"temp": 21.0, "site": "WQ-01"}',          # 浮点写法 ≠ 整数
        '{"temp": 2e1, "site": "WQ-01"}',           # 指数写法 ≠ 整数
        '{"temp": 1e2, "site": "WQ-01"}',           # 100 的指数写法仍非整数
        '{"temp": -0, "site": "WQ-01"}',            # -0 是整数 0
        '{"temp": 0.0, "site": "WQ-01"}',
        '{"temp": 99999999999999999999, "site": "WQ-01"}',   # 超 bigint，仍按整数比较
        '{"temp": -99999999999999999999, "site": "WQ-01"}',
        '{"temp": 9007199254740992, "site": "WQ-01"}',       # 2^53，越界
        '{"temp": 21, "site": "WQ-01", "qc": 1}',
        '{"temp": 21, "site": "WQ-01", "qc": true}',
        '{"temp": 21, "site": "WQ-01", "qc": []}',
        '{"temp": 21, "site": "WQ-01", "qc": {}}',
        '{"temp": 21, "site": "WQ-01", "extra": 1}',  # 多余字段
        '{"temp": 21}',                                # 缺 required
        '{"site": "WQ-01"}',
        '{"temp": null, "site": "WQ-01"}',             # 显式 null ≠ 缺省
        '{"temp": 21, "site": null}',
        '{"temp": true, "site": "WQ-01"}',
        '{"temp": "21", "site": "WQ-01"}',
        '{"temp": 21, "site": "WQ-01", "site": "WQ-02"}',  # 重复键取最后
        '{"temp": 21, "site": "wq-01"}',               # 大小写敏感
        '{"temp": 21, "site": "WQ-01 "}',
        '{"temp": 21, "site": "WQ-01"}',
        '{"temp": 21, "site": "WQ-01"}',
        '[1, 2]', 'null', 'true', '5', '"x"', '{}',
        '{"temp": 1e999999, "site": "WQ-01"}',         # 大指数：非整数写法
        '{"temp": 21, "site": "WQ-01", "qc": "N"}',
    ])

    anyof_doc = {
        "anyOf": [
            {"type": "object",
             "properties": {"a": {"type": "integer", "minimum": 0, "maximum": 5}},
             "required": ["a"], "additionalProperties": False},
            {"type": "object",
             "properties": {"b": {"enum": ["x", "y", None]},
                            "n": {"type": "null"}},
             "required": ["b"], "additionalProperties": False},
        ]
    }
    compare_battery(cur, "电池:顶层anyOf", anyof_doc, [
        '{"a": 3}', '{"a": 6}', '{"a": 3.0}', '{"a": "3"}', '{"a": null}',
        '{"b": "x"}', '{"b": "y", "n": null}', '{"b": null}', '{"b": "z"}',
        '{"b": "x", "n": 1}', '{"b": "x", "n": null, "c": 1}',
        '{"a": 3, "b": "x"}',   # 两个分支都不接受（各有多余字段）
        '{}', '[]', 'null',
    ])

    empty_props = {"type": "object", "properties": {}, "additionalProperties": False}
    compare_battery(cur, "电池:空properties", empty_props,
                    ['{}', '{"a": 1}', '{"a": null}', '[]', 'null', '0'])

    no_required = {
        "type": "object",
        "properties": {"q": {"type": "integer", "enum": [7, -3, 0]}},
        "additionalProperties": False,
    }
    compare_battery(cur, "电池:整数枚举+全可选", no_required, [
        '{}', '{"q": 7}', '{"q": -3}', '{"q": 0}', '{"q": 0.0}', '{"q": -0}',
        '{"q": 8}', '{"q": "7"}', '{"q": null}', '{"q": true}',
    ])

    tricky_names = {
        "type": "object",
        "properties": {
            "we'ird": {"type": "integer"},
            "温度": {"type": "integer", "minimum": 0},
            "a b": {"enum": ["x'y", "", "换行\n符", "😀"]},
            "back\\slash": {"type": "null"},
        },
        "required": ["we'ird"],
        "additionalProperties": False,
    }
    compare_battery(cur, "电池:特殊字符字段名/枚举值", tricky_names, [
        J({"we'ird": 5}),
        J({"we'ird": 5, "温度": 0}),
        J({"we'ird": 5, "温度": -1}),
        J({"we'ird": 5, "a b": "x'y"}),
        J({"we'ird": 5, "a b": ""}),
        J({"we'ird": 5, "a b": "换行\n符"}),
        J({"we'ird": 5, "a b": "😀"}),
        J({"we'ird": 5, "a b": "😀x"}),
        J({"we'ird": 5, "back\\slash": None}),
        J({"we'ird": 5, "back\\slash": 0}),
        J({"we'ird": None}),
        J({"温度": 1}),
        '{"we\'ird": 5, "温度": 1}',
    ])

    unbounded = {
        "type": "object",
        "properties": {"v": {"type": "integer"}},
        "required": ["v"],
        "additionalProperties": False,
    }
    compare_battery(cur, "电池:无界整数", unbounded, [
        '{"v": 0}', '{"v": -0}', '{"v": 123456789012345678901234567890}',
        '{"v": -123456789012345678901234567890}', '{"v": 1.0}', '{"v": 1e0}',
        '{"v": "1"}', '{"v": null}', '{"v": true}', '{"v": 1e999999}',
    ])


def staging_table_check(cur):
    """端到端：真实暂存表（含 SQL NULL 行）上用 WHERE 筛不符合契约的行。"""
    doc = {
        "anyOf": [
            {"type": "object",
             "properties": {"temp": {"type": "integer", "minimum": -40, "maximum": 100},
                            "site": {"type": "string", "enum": ["WQ-01", "WQ-02"]},
                            "qc": {"enum": ["N", "Q", None]}},
             "required": ["temp", "site"], "additionalProperties": False},
            {"type": "object",
             "properties": {"level": {"type": "integer", "enum": [0, 1, 2, 3]},
                            "site": {"type": "string", "enum": ["WQ-01", "WQ-02"]}},
             "required": ["level", "site"], "additionalProperties": False},
        ]
    }
    schema = parse_schema(doc)
    expr = schema_sql(schema)
    lines = [
        '{"temp": 21, "site": "WQ-01"}',
        '{"temp": 21, "site": "WQ-01", "qc": null}',
        '{"temp": 21.0, "site": "WQ-01"}',
        '{"temp": 2e1, "site": "WQ-01"}',
        '{"level": 3, "site": "WQ-02"}',
        '{"level": 3.0, "site": "WQ-02"}',
        '{"temp": 21, "site": "WQ-01", "extra": 1}',
        '{"temp": -40, "site": "WQ-02", "qc": "Q"}',
        '[1, 2]', 'null', '"s"',
        None,  # SQL NULL 行
    ]
    cur.execute("DROP TABLE IF EXISTS staging_diff")
    cur.execute("CREATE TABLE staging_diff (id serial primary key, payload json)")
    for i, ln in enumerate(lines):
        if ln is None:
            cur.execute("INSERT INTO staging_diff (payload) VALUES (NULL)")
        else:
            cur.execute("INSERT INTO staging_diff (payload) VALUES (CAST(%s AS json))", (ln,))
    cur.execute(f"SELECT id, payload FROM staging_diff WHERE NOT ({expr}) ORDER BY id")
    bad_rows = cur.fetchall()
    cur.execute("DROP TABLE staging_diff")
    cur.connection.commit()

    expect_bad = {i + 1 for i, ln in enumerate(lines)
                  if ln is None or not message_valid(json.loads(ln), schema)}
    got_bad = {r[0] for r in bad_rows}
    ok("暂存表:NOT表达式筛出不符合行", got_bad == expect_bad,
       f"期望{sorted(expect_bad)} 实际{sorted(got_bad)}")
    # 每行只返回真或假：SQL NULL 行、JSON null 行均判假，无 NULL、无报错
    cur.execute("CREATE TEMP TABLE st_null (payload json)")
    cur.execute("INSERT INTO st_null VALUES (NULL), (CAST('null' AS json)),"
                " (CAST('{\"temp\": 21, \"site\": \"WQ-01\"}' AS json))")
    cur.execute(f"SELECT ({expr}) FROM st_null")
    got = [r[0] for r in cur.fetchall()]
    cur.execute("DROP TABLE st_null")
    cur.connection.commit()
    ok("暂存表:SQL NULL判假且只返真/假", got == [False, False, True], str(got))


# ---------- 随机模糊 ----------

FIELD_POOL = ["temp", "site", "qc", "level", "ph", "do", "浊度", "a b",
              "x'y", "back\\slash", "n"]
STR_POOL = ["WQ-01", "WQ-02", "N", "Q", "", "x'y", "换行\n符", "😀", "a b",
            "0", "-1", "null", "true"]


def rand_field_spec(rng):
    kind = rng.randrange(6)
    if kind == 0:  # 整数范围
        lo = rng.choice([None, rng.randint(-100, 0)])
        hi = rng.choice([None, rng.randint(0, 100)])
        if lo is not None and hi is not None and lo > hi:
            lo, hi = hi, lo
        spec = {"type": "integer"}
        if lo is not None:
            spec["minimum"] = lo
        if hi is not None:
            spec["maximum"] = hi
        return spec
    if kind == 1:  # 整数枚举
        vals = rng.sample(range(-10, 30), rng.randint(1, 5))
        return {"type": "integer", "enum": vals}
    if kind == 2:  # 字符串枚举
        return {"enum": rng.sample(STR_POOL, rng.randint(1, 4))}
    if kind == 3:  # 混合枚举（含 null）
        vals = rng.sample(STR_POOL, rng.randint(1, 3)) + [None]
        if rng.random() < 0.5:
            vals += rng.sample(range(-5, 5), rng.randint(1, 3))
        rng.shuffle(vals)
        return {"enum": vals}
    if kind == 4:  # 纯 null
        return {"type": "null"}
    # 无界整数
    return {"type": "integer"}


def rand_schema_doc(rng):
    branches = []
    for _ in range(rng.randint(1, 3) if rng.random() < 0.4 else 1):
        names = rng.sample(FIELD_POOL, rng.randint(0, 5))
        props = {n: rand_field_spec(rng) for n in names}
        required = [n for n in names if rng.random() < 0.5]
        branches.append({"type": "object", "properties": props,
                         "required": required, "additionalProperties": False})
    if len(branches) == 1:
        return branches[0]
    return {"anyOf": branches}


def rand_number_text(rng):
    """随机数字文本：混合整数写法与非整数写法。"""
    kind = rng.randrange(8)
    if kind == 0:
        return str(rng.randint(-100, 100))
    if kind == 1:
        return str(rng.randint(-2**60, 2**60))
    if kind == 2:
        return str(rng.randint(-10**30, 10**30))
    if kind == 3:
        return "-0"
    if kind == 4:
        return f"{rng.randint(-100, 100)}.0"
    if kind == 5:
        return f"{rng.randint(0, 100)}e{rng.randint(0, 3)}"
    if kind == 6:
        return f"{rng.randint(-1000, 1000) / 4}"
    return "1e999999"


def rand_value_text(rng, depth=0):
    kind = rng.randrange(10)
    if kind <= 3:
        return rand_number_text(rng)
    if kind == 4:
        return J(rng.choice(STR_POOL))
    if kind == 5:
        return "null"
    if kind == 6:
        return rng.choice(["true", "false"])
    if kind == 7 and depth < 1:
        return "[" + ", ".join(rand_value_text(rng, depth + 1)
                               for _ in range(rng.randrange(3))) + "]"
    if depth < 1:
        pairs = []
        for _ in range(rng.randrange(3)):
            k = rng.choice(FIELD_POOL)
            pairs.append(f"{J(k)}: {rand_value_text(rng, depth + 1)}")
        return "{" + ", ".join(pairs) + "}"
    return str(rng.randint(-10, 10))


def rand_message_text(rng, doc):
    """围绕契约字段名生成报文：一半命中字段，一半随机。"""
    if rng.random() < 0.12:
        return rng.choice(['5', '"s"', '[1, 2]', 'null', 'true', '{}', '[null]',
                           '{"a": [1, {"b": 2}]}'])
    names = set()

    def collect(d):
        for b in (d.get("anyOf") or [d]):
            names.update(b.get("properties", {}))
    collect(doc)
    pool = list(names) + ["ghost", "extra", "temp"]
    pairs = []
    for _ in range(rng.randrange(0, 6)):
        k = rng.choice(pool)
        pairs.append(f"{J(k)}: {rand_value_text(rng)}")
        if rng.random() < 0.05:  # 偶发重复键
            pairs.append(f"{J(k)}: {rand_value_text(rng)}")
    return "{" + ", ".join(pairs) + "}"


def fuzz(cur):
    rng = random.Random(SEED)
    total = 0
    for rnd in range(ROUNDS):
        doc = rand_schema_doc(rng)
        try:
            schema = parse_schema(doc)
        except Exception as e:
            ok(f"fuzz契约{rnd}可解析", False, str(e))
            continue
        expr = schema_sql(schema)
        lines = [rand_message_text(rng, doc) for _ in range(MSGS_PER_ROUND)]
        try:
            sql_res = sql_batch_check(cur, expr, lines)
        except Exception as e:
            ok(f"fuzz{rnd}:表达式不报错", False,
               f"{e}\n契约={J(doc)}\n首批报文={lines[:3]}")
            cur.connection.rollback()
            continue
        py_res = [message_valid(json.loads(ln), schema) for ln in lines]
        bad = [(ln, p, s) for ln, p, s in zip(lines, py_res, sql_res) if p != s]
        ok(f"fuzz{rnd}:{len(lines)}条对照一致", not bad,
           f"契约={J(doc)} 分歧={bad[:3]}")
        total += len(lines)
    print(f"随机模糊：{ROUNDS} 份契约 × {MSGS_PER_ROUND} 条 = 约 {total} 条对照")


def main():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute("select version()")
    ver = cur.fetchone()[0]
    assert "PostgreSQL 16" in ver, f"需要 PostgreSQL 16，当前：{ver}"
    print(f"库内差分测试 @ {ver.split(',')[0]}，种子 {SEED}")

    hand_battery(cur)
    staging_table_check(cur)
    fuzz(cur)

    conn.close()
    if FAIL:
        print(f"\n失败 {len(FAIL)} 项：")
        for f in FAIL[:10]:
            print("  -", f)
        sys.exit(1)
    print(f"\n全部 {len(PASS)} 组差分对照通过")


if __name__ == "__main__":
    main()
