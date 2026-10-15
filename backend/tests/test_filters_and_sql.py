from __future__ import annotations

import ast
import random

import pytest

from app.services import sql_utils
from app.services.filters import EmbedError, embed_sql, expected_embedded_value, py_docline, verify_embedded_sql

VARS = {"proj": "my-proj", "ds": "raw_01"}


def _real_eval(src: str) -> str:
    """Evaluate the emitted literal the way the generated step does: variables are plain locals."""
    return eval(compile(ast.parse(src.strip(), mode="eval"), "<sql>", "eval"), {}, dict(VARS))  # noqa: S307


CASES = [
    "SELECT * FROM `${proj}.${ds}.t` WHERE REGEXP_CONTAINS(x, r'^[A-Z]{3}\\d+$')",
    'SELECT JSON \'{"a": 1}\' AS j FROM t',
    "SELECT '''multi\nline''' AS a FROM `${proj}.d.t`",
    'SELECT """tq""" AS a, \'\'\'b\'\'\' FROM `${proj}.d.t` WHERE c = \'{}\'',
    "SELECT 1 -- trailing backslash \\",
    "SELECT '\\N{BULLET}' AS a, '{{x}}' AS b FROM t",
    "SELECT 1",
]


@pytest.mark.parametrize("sql", CASES)
def test_embedded_sql_round_trips_exactly(sql):
    src = embed_sql(sql, indent=8)
    verify_embedded_sql(src, sql, VARS, 8)
    assert _real_eval(src) == expected_embedded_value(sql, VARS, 8)


def test_embedding_fuzz():
    alphabet = list("ab{}$\\'\"`\n -_@") + ["${proj}", "${ds}", '"""', "'''", "{{", "}}", "\\{"]
    rnd = random.Random(1234)
    for _ in range(1500):
        sql = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 30)))
        if not sql.strip():
            continue
        src = embed_sql(sql, indent=12)
        verify_embedded_sql(src, sql, VARS, 12)
        assert _real_eval(src) == expected_embedded_value(sql, VARS, 12)


def test_no_placeholders_means_plain_string():
    src = embed_sql("SELECT '{a}' FROM t")
    assert src.startswith('"""') and "{a}" in src  # braces untouched, no f-prefix


def test_variables_become_plain_locals_and_backslashes_make_it_raw():
    assert embed_sql("SELECT * FROM `${proj}.${ds}.t`").startswith('f"""') and "{proj}" in embed_sql("SELECT ${proj}")
    assert embed_sql("SELECT r'\\d' FROM `${proj}.t`").startswith('rf"""')


def test_tampered_literal_is_detected():
    src = embed_sql("SELECT * FROM `${proj}.t`").replace("{proj}", "{__import__('os').getcwd()}")
    with pytest.raises(EmbedError):
        verify_embedded_sql(src, "SELECT * FROM `${proj}.t`", VARS, 8)


def test_docline_cannot_break_out_of_docstring():
    text = py_docline('evil """ + __import__("os").system("x") + """ \\')
    tree = ast.parse(f'def f():\n    """{text}"""\n')
    assert isinstance(tree.body[0].body[0].value, ast.Constant)


def test_facts_sources_targets_and_kinds():
    f = sql_utils.facts(
        "CREATE TEMP TABLE stg AS SELECT a FROM `p.raw.s`;\n"
        "INSERT INTO `p.mart.t` SELECT a FROM stg;\n"
        "INSERT INTO `p.mart.t` SELECT a FROM `p.mart.t`"
    )
    assert f.kind == "SCRIPT"
    assert f.targets == ["p.mart.t"]
    assert f.sources == ["p.raw.s", "p.mart.t"]  # temp table excluded; self-read counted


def test_risk_rules():
    f = sql_utils.facts("DELETE FROM `p.d.t` WHERE TRUE; SELECT * FROM t WHERE d = CURRENT_DATE() AND x = '2024-01-01'")
    messages = " ".join(m for _sev, m in f.risks)
    assert "SELECT *" in messages and "2024-01-01" in messages
    assert "CURRENT_DATE" not in messages  # dates relative to the day the job runs are normal now (no run date)
    f2 = sql_utils.facts("UPDATE `p.d.t` SET a = 1")
    assert any(sev == "error" for sev, _m in f2.risks)


def test_query_params_ignore_system_vars_strings_and_comments():
    assert sql_utils.find_query_params("SELECT @run_date, @@row_count, '@no', @a + @b -- @c\n") == ["run_date", "a", "b"]


def test_substitute_params_leaves_strings_alone():
    out = sql_utils.substitute_params("WHERE d=@run_date AND f='@run_date'", {"run_date": "DATE '2024-01-01'"})
    assert out == "WHERE d=DATE '2024-01-01' AND f='@run_date'"


def test_equivalence_ignores_formatting_but_not_logic():
    assert sql_utils.equivalent("select a from t where d='x' -- c", "SELECT a\nFROM t\nWHERE d = 'x'")[0]
    assert not sql_utils.equivalent("SELECT a FROM t WHERE d = 'x'", "SELECT a FROM t WHERE d <> 'x'")[0]


def test_table_templates_keep_variables():
    sources, targets = sql_utils.table_templates(
        "INSERT INTO `${project_id}.${mart}.t` SELECT * FROM `${project_id}.${raw}.s`"
    )
    assert targets == ["{project_id}.{mart}.t"] and sources == ["{project_id}.{raw}.s"]


def test_attached_sql_files_are_decoded_as_text():
    assert sql_utils.decode_sql_file("\ufeffSELECT 1\r\nFROM t\r\n".encode("utf-8"), "q.sql") == "SELECT 1\nFROM t\n"
    assert sql_utils.decode_sql_file("SELECT 'café'".encode("cp1252"), "q.txt") == "SELECT 'café'"
    for data, name, message in [
        (b"SELECT 1", "q.py", "attach a .sql or .txt file"),
        (b"\x00\x01binary", "q.sql", "not a text file"),
        (b"  \n", "q.sql", "empty"),
        (b"x" * 2048, "big.sql", "the limit is"),
    ]:
        with pytest.raises(ValueError, match=message):
            sql_utils.decode_sql_file(data, name, max_bytes=1024)

