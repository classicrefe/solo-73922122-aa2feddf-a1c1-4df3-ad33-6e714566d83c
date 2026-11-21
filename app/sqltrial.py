"""库内试跑：在数据库里执行拟交付的表达式，并与纯 Python 判定逐条对照。

只有全部可对照报文一致、且执行无错时才交付表达式（ok=True 且携带
expression）；任何分歧或执行错误都会说明原因并暂不提供表达式。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select, text

from db import FirmwareFormat, SessionLocal
from schema_spec import Schema, message_valid, parse_schema
from sqlgen import schema_sql

MAX_LINES = 500          # 单次试跑最多报文行数
MAX_LINE_LEN = 65536     # 单行报文最大长度


@dataclass
class TrialRow:
    line_no: int
    text: str
    status: str                     # agree | diverge | skipped | sql-error
    python: Optional[bool] = None
    sql: Optional[bool] = None
    note: str = ""

    @property
    def python_disp(self) -> str:
        return "—" if self.python is None else ("真" if self.python else "假")

    @property
    def sql_disp(self) -> str:
        return "—" if self.sql is None else ("真" if self.sql else "假")


@dataclass
class TrialResult:
    ok: bool                                # True 时才交付表达式
    version: int
    expression: Optional[str] = None        # 仅 ok 时提供
    rows: list = field(default_factory=list)  # list[TrialRow]
    error: Optional[str] = None
    compared: int = 0
    skipped: int = 0
    lines_text: str = ""                    # 回显到表单的原始输入


def _strict_loads(line: str):
    """与 PostgreSQL json 对齐的严格解析：拒绝 NaN/Infinity 等非标准常量。"""

    def _bad_const(x):
        raise ValueError(f"非标准 JSON 常量 {x}（PostgreSQL 无法读取）")

    return json.loads(line, parse_constant=_bad_const)


def _compare_query(expr: str, n: int) -> str:
    vals = ", ".join(f"({i}, CAST(:p{i} AS json))" for i in range(n))
    return (f"SELECT t.idx, ({expr}) AS ok"
            f" FROM (VALUES {vals}) AS t(idx, payload) ORDER BY t.idx")


def _single_query(expr: str) -> str:
    return f"SELECT ({expr}) AS ok FROM (VALUES (CAST(:p AS json))) AS t(payload)"


def run_trial(s, expr: str, schema: Schema, lines: list[str],
              version: int = 0, lines_text: str = "") -> TrialResult:
    """在会话 s 上执行 expr 并与 Python 判定对照。lines 为原始报文文本行。"""
    rows: list[TrialRow] = []
    parsed: list[int] = []  # rows 下标：成功解析、需要库内对照的行
    for no, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            continue  # 空行不计
        if len(line) > MAX_LINE_LEN:
            rows.append(TrialRow(no, line[:120] + "…", "skipped",
                                 note=f"行超过 {MAX_LINE_LEN} 字符，未纳入对照"))
            continue
        try:
            value = _strict_loads(line)
        except (json.JSONDecodeError, ValueError) as e:
            rows.append(TrialRow(no, line, "skipped",
                                 note=f"非法/非标准 JSON，两端均无法读取，未纳入对照：{e}"))
            continue
        rows.append(TrialRow(no, line, "agree", python=message_valid(value, schema)))
        parsed.append(len(rows) - 1)

    res = TrialResult(ok=False, version=version, rows=rows,
                      skipped=sum(1 for r in rows if r.status == "skipped"),
                      lines_text=lines_text)
    if not parsed:
        res.error = "没有可对照的报文：请至少粘贴一条两端均可读取的标准 JSON 报文。"
        return res

    params = {f"p{i}": rows[ridx].text for i, ridx in enumerate(parsed)}
    try:
        got = s.execute(text(_compare_query(expr, len(parsed))), params).all()
        # ORDER BY t.idx 保证返回顺序与 parsed 一致
        for (idx, ok), ridx in zip(got, parsed):
            rows[ridx].sql = bool(ok)
    except Exception as batch_err:
        s.rollback()
        # 批量执行出错：逐行重试以定位问题行并给出原因
        errors = {}
        for i, ridx in enumerate(parsed):
            try:
                ok = s.execute(text(_single_query(expr)),
                               {"p": rows[ridx].text}).scalar()
                rows[ridx].sql = bool(ok)
            except Exception as e:  # 该行导致 SQL 执行错误
                s.rollback()
                rows[ridx].status = "sql-error"
                rows[ridx].note = str(e).strip().split("\n")[0]
                errors.setdefault(rows[ridx].note, []).append(rows[ridx].line_no)
        if errors:
            detail = "；".join(f"第 {','.join(map(str, ns))} 行：{msg}"
                               for msg, ns in errors.items())
            res.error = (f"数据库执行拟交付表达式时报错（{detail}）。"
                         "此类报文不属于两端均可读取的标准 JSON，表达式暂不提供。")
        else:
            res.error = f"数据库批量执行出错（逐行重试均通过）：{batch_err}"
        # 继续走对照，把已得到的逐行结果展示出来

    diverged = 0
    for r in rows:
        if r.status == "sql-error":
            continue
        if r.sql is None:
            continue  # skipped 行
        if r.sql != r.python:
            r.status = "diverge"
            r.note = "库内判定与 Python 判定不一致"
            diverged += 1
    res.compared = sum(1 for r in rows if r.sql is not None)

    if res.error:
        return res
    if diverged:
        res.error = (f"有 {diverged} 条报文库内判定与 Python 判定不一致，"
                     "表达式与现有判定存在分歧，暂不提供。")
        return res
    res.ok = True
    res.expression = expr
    return res


def trial_firmware_sql(model_id: int, version: int, lines: list[str],
                       lines_text: str = "") -> TrialResult:
    """按型号与固件版本号取已发布契约，生成表达式并库内试跑对照。"""
    if len(lines) > MAX_LINES:
        return TrialResult(ok=False, version=version, lines_text=lines_text,
                           error=f"报文行数超过 {MAX_LINES} 行上限，请分批试跑。")
    with SessionLocal() as s:
        fw = s.execute(select(FirmwareFormat)
                       .where(FirmwareFormat.model_id == model_id,
                              FirmwareFormat.version == version)).scalar_one_or_none()
        if fw is None:
            return TrialResult(ok=False, version=version, lines_text=lines_text,
                               error=f"固件写入格式 v{version} 不存在。")
        schema = parse_schema(fw.doc)
        expr = schema_sql(schema)
        if s.bind.dialect.name != "postgresql":
            return TrialResult(ok=False, version=version, lines_text=lines_text,
                               error=("库内试跑需要 PostgreSQL（表达式面向 PostgreSQL 16），"
                                      f"当前数据库为 {s.bind.dialect.name}。"
                                      "请用 docker compose 启动 PostgreSQL 后重试。"))
        return run_trial(s, expr, schema, lines, version=version,
                         lines_text=lines_text)
