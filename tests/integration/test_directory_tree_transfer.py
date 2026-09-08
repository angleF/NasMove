from __future__ import annotations

import pytest

pytestmark = pytest.mark.synology


def test_nested_tree_and_empty_directory_are_preserved_on_synology(
    synology_fixture,
) -> None:
    result = synology_fixture.transfer_directory_tree()

    assert result.nested_file_verified is True
    assert result.empty_directory_created is True
    assert result.source_tree_retained is True
