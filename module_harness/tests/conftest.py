"""conftest — 共享 fixture。"""

import pytest
from unittest.mock import MagicMock


@pytest.fixture
def mock_llm():
    return MagicMock()
