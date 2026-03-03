import os
import tempfile

import pytest

from app.config import Settings


@pytest.fixture
def settings(tmp_path):
    db_path = str(tmp_path / "test.db")
    return Settings(
        PORT=8000,
        INTERNAL_API_SECRET="test-secret",
        USE_SQLITE=True,
        SQLITE_DB_PATH=db_path,
        PROXYJET_HOST="proxy.proxyjet.io",
        PROXYJET_PORT=8080,
        PROXYJET_USERNAME="user123",
        PROXYJET_PASSWORD="pass456",
    )
