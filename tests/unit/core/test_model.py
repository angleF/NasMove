import pytest

from nasmove.core.errors import DomainValidationError
from tests.fixtures.builders import build_connection_config


def test_connection_config_defaults_to_two_parallel_items() -> None:
    assert build_connection_config().max_parallel_items == 2


@pytest.mark.parametrize("value", (1, 4))
def test_connection_config_accepts_parallel_item_bounds(value: int) -> None:
    assert build_connection_config(max_parallel_items=value).max_parallel_items == value


@pytest.mark.parametrize("value", (0, 5, True, "2"))
def test_connection_config_rejects_invalid_parallel_item_count(value: object) -> None:
    with pytest.raises(DomainValidationError, match="max_parallel_items"):
        build_connection_config(max_parallel_items=value)  # type: ignore[arg-type]
