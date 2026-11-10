import pytest

from sdvrig.vehicle import build_vehicle


@pytest.fixture
def vehicle():
    return build_vehicle()
