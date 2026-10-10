import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "fixtures"
# The private profile comes from the generator's own resolution
# ($CVGEN_PROFILE_DIR, else <repo>/profile): personal photos never live in the
# project tree any more.
from cv_generator.paths import DEFAULT_OUTPUT_DIR, PROFILE_PIC_DIR  # noqa: E402

PROFILE_PIC = PROFILE_PIC_DIR
OUTPUTS = DEFAULT_OUTPUT_DIR

CROPPED_PHOTO = PROFILE_PIC / "placeholder_photo.jpg"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def en_data() -> dict:
    return load_fixture("en_historical.json")


@pytest.fixture
def de_data() -> dict:
    return load_fixture("de_minimal.json")


@pytest.fixture
def de_full_data() -> dict:
    return load_fixture("de_full.json")


@pytest.fixture
def cropped_photo() -> Path:
    return CROPPED_PHOTO
