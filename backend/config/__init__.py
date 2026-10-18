"""Django project package.

Django's MySQL backend requires ``MySQLdb``; the project depends on PyMySQL, so install
the compatibility shim before any database connection is created.
"""

from __future__ import annotations

try:  # pragma: no cover - depends on which driver is installed
    import MySQLdb  # noqa: F401
except ImportError:  # pragma: no cover
    import pymysql

    pymysql.install_as_MySQLdb()
