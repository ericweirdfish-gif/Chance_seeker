from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chance_seeker.config import load_config  # noqa: E402
from chance_seeker.storage import Database  # noqa: E402


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """把示例配置复制到临时目录，得到一个干净的独立工程。"""
    (tmp_path / "config").mkdir()
    shutil.copy(ROOT / "config" / "config.example.yaml", tmp_path / "config" / "config.yaml")
    (tmp_path / "data").mkdir()
    return tmp_path


@pytest.fixture()
def config(project: Path):
    return load_config(project / "config" / "config.yaml", root=project)


@pytest.fixture()
def db(config):
    database = Database(config.db_path)
    yield database
    database.close()


@pytest.fixture()
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
