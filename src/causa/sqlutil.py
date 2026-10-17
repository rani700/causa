"""Small SQL helpers shared by the engines (classification and fingerprinting of statements)."""

from __future__ import annotations

import re
from datetime import date, datetime

import polars as pl

_COMMANDS = (
    (re.compile(r"^\s*CREATE\s+(OR\s+REPLACE\s+)?TABLE\b.*\bAS\b", re.I | re.S), "CREATE TABLE AS"),
    (re.compile(r"^\s*CREATE\s+(OR\s+REPLACE\s+)?TABLE\b", re.I), "CREATE TABLE"),
    (re.compile(r"^\s*CREATE\s+(OR\s+REPLACE\s+)?(FORCE\s+)?VIEW\b", re.I), "CREATE VIEW"),
    (re.compile(r"^\s*ALTER\s+TABLE\b", re.I), "ALTER TABLE"),
    (re.compile(r"^\s*TRUNCATE\b", re.I), "TRUNCATE"),
    (re.compile(r"^\s*(WITH\b.*?\)\s*)?SELECT\b", re.I | re.S), "SELECT"),
    (re.compile(r"^\s*INSERT\b", re.I), "INSERT"),
    (re.compile(r"^\s*MERGE\b", re.I), "MERGE"),
    (re.compile(r"^\s*DELETE\b", re.I), "DELETE"),
    (re.compile(r"^\s*UPDATE\b", re.I), "UPDATE"),
    (re.compile(r"^\s*IMPORT\b", re.I), "IMPORT"),
    (re.compile(r"^\s*EXPORT\b", re.I), "EXPORT"),
    (re.compile(r"^\s*DROP\b", re.I), "DROP"),
)


def command_name(sql: str) -> str:
    for pattern, name in _COMMANDS:
        if pattern.match(sql):
            return name
    return sql.split(None, 1)[0].upper() if sql.strip() else "UNKNOWN"


def normalize_expr(col: pl.Expr) -> pl.Expr:
    """Statement text with literals and whitespace normalised, so daily runs of the same job collapse
    to one template. Vectorised because audit logs run to millions of rows."""
    return (
        col.str.replace_all(r"'(?:[^']|'')*'", "?")
        .str.replace_all(r"\b\d+(?:\.\d+)?\b", "?")
        .str.replace_all(r"\s+", " ")
        .str.strip_chars()
        .str.to_uppercase()
    )


def fingerprint_expr(col: pl.Expr) -> pl.Expr:
    return normalize_expr(col).hash(seed=0).cast(pl.String)


def ts(value: datetime) -> str:
    return f"TIMESTAMP '{value:%Y-%m-%d %H:%M:%S}'"


def dt(value: date) -> str:
    return f"DATE '{value:%Y-%m-%d}'"
