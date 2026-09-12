from PySide6.QtCore import Qt

from nasmove.ui.directory_models import (
    DirectoryEntryViewModel,
    DirectorySide,
    DirectorySnapshot,
    DirectoryTableModel,
)


def test_directory_model_orders_directories_before_files_and_filters() -> None:
    model = DirectoryTableModel(DirectorySide.LOCAL)
    model.replace(
        DirectorySnapshot(
            side=DirectorySide.LOCAL,
            location="/tmp",
            entries=(
                DirectoryEntryViewModel("z.mov", False, 20, None, "/tmp/z.mov"),
                DirectoryEntryViewModel("Album", True, 0, None, "/tmp/Album"),
                DirectoryEntryViewModel("a.mov", False, 10, None, "/tmp/a.mov"),
            ),
            request_id=1,
            profile_id=None,
        )
    )

    assert [model.entry_at(row).name for row in range(model.rowCount())] == [
        "Album",
        "a.mov",
        "z.mov",
    ]
    model.set_filter("z.")
    assert model.rowCount() == 1
    assert model.entry_at(0).name == "z.mov"


def test_directory_model_exposes_name_size_and_modified_columns() -> None:
    model = DirectoryTableModel(DirectorySide.REMOTE)
    model.replace(
        DirectorySnapshot(
            side=DirectorySide.REMOTE,
            location="Video",
            entries=(DirectoryEntryViewModel("movie.mov", False, 1024, None, "Video/movie.mov"),),
            request_id=2,
            profile_id="profile-1",
        )
    )

    assert model.index(0, 0).data() == "movie.mov"
    assert "KB" in model.index(0, 1).data()
    assert model.index(0, 2).data() == "—"
    assert model.index(0, 0).data(Qt.ItemDataRole.ToolTipRole) == "movie.mov"


def test_directory_model_formats_modified_time_for_file_browser_readability() -> None:
    model = DirectoryTableModel(DirectorySide.LOCAL)
    past_ns = 1_700_000_000_000_000_000
    model.replace(
        DirectorySnapshot(
            side=DirectorySide.LOCAL,
            location="/tmp",
            entries=(
                DirectoryEntryViewModel(
                    "movie.mov", False, 1024, past_ns, "/tmp/movie.mov"
                ),
            ),
            request_id=1,
            profile_id=None,
        )
    )

    displayed = model.index(0, 2).data()

    assert displayed != str(past_ns)
    assert isinstance(displayed, str)
    assert "月" in displayed
    assert displayed.endswith("日")
    assert len(displayed) <= 7
