from importlib.metadata import version

import nasmove


def test_package_and_module_versions_match() -> None:
    assert nasmove.__version__ == version("nasmove")
