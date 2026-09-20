"""Pruebas para normalize_database_url."""

import pytest
from dian_automation.db.database import normalize_database_url


def test_normalize_database_url_postgres_scheme():
    url = "postgres://usuario:clave@localhost:5432/kontable"
    assert normalize_database_url(url) == "postgresql+psycopg://usuario:clave@localhost:5432/kontable"


def test_normalize_database_url_postgresql_scheme():
    url = "postgresql://usuario:clave@localhost:5432/kontable"
    assert normalize_database_url(url) == "postgresql+psycopg://usuario:clave@localhost:5432/kontable"


def test_normalize_database_url_sqlite():
    url = "sqlite:///./kontable.db"
    assert normalize_database_url(url) == "sqlite:///./kontable.db"


def test_normalize_database_url_already_normalized():
    url = "postgresql+psycopg://usuario:clave@localhost:5432/kontable"
    assert normalize_database_url(url) == "postgresql+psycopg://usuario:clave@localhost:5432/kontable"


def test_normalize_database_url_with_sslmode_query_param():
    url = "postgresql://usuario:clave@db.host.com:5432/kontable?sslmode=require"
    assert (
        normalize_database_url(url)
        == "postgresql+psycopg://usuario:clave@db.host.com:5432/kontable?sslmode=require"
    )
