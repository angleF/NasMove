from PySide6.QtWidgets import QLineEdit

from nasmove.ui.connection_page import ConnectionPage
from tests.fixtures.builders import build_connection_config
from tests.fixtures.ui import FakeCredentialStore


def test_expanded_connection_details_do_not_compress_input_text(qtbot):
    from nasmove.ui.main_window import MainWindow
    window = MainWindow()
    qtbot.addWidget(window)
    window.new_task_button.click()
    window.show()
    page = window.connection_page
    page.error_details_button.setChecked(True)
    for width, height in ((800, 620), (1100, 760), (1600, 1000)):
        window.resize(width, height)
        window.layout().activate()
        page.scroll_area.widget().layout().activate()
        for field in (page.address_lineedit, page.password_lineedit, page.port_spinbox):
            assert field.height() >= field.fontMetrics().height() + 12
        assert page.error_details.maximumHeight() <= 130


class ProfileStore:
    def __init__(self, profile=None) -> None:
        self.profile = profile
        self.saved = []

    def last_successful_connection(self):
        return self.profile

    def save_successful_connection(self, config) -> None:
        self.saved.append(config)


def test_connection_page_defaults_to_port_445_keychain_and_encryption(qtbot) -> None:
    page = ConnectionPage()
    qtbot.addWidget(page)
    assert page.port_spinbox.value() == 445
    assert page.remember_password_checkbox.isChecked()
    assert page.require_encryption_checkbox.isChecked()
    assert page.test_button.property("themeRole") == "primary"
    assert page.error_details_button.property("themeRole") == "secondary"


def test_connection_page_password_toggle(qtbot) -> None:
    page = ConnectionPage()
    qtbot.addWidget(page)

    assert page.password_lineedit.echoMode() == QLineEdit.EchoMode.Password
    page.toggle_password_action.trigger()
    assert page.password_lineedit.echoMode() == QLineEdit.EchoMode.Normal
    page.toggle_password_action.trigger()
    assert page.password_lineedit.echoMode() == QLineEdit.EchoMode.Password


def test_connection_page_edits_and_restores_parallel_item_count(qtbot) -> None:
    page = ConnectionPage()
    qtbot.addWidget(page)

    assert page.parallel_items_spinbox.minimum() == 1
    assert page.parallel_items_spinbox.maximum() == 4
    assert page.parallel_items_spinbox.value() == 2

    page.parallel_items_spinbox.setValue(4)
    assert page.connection_config().max_parallel_items == 4

    page.load_profile(build_connection_config(max_parallel_items=1))
    assert page.parallel_items_spinbox.value() == 1

    page.clear_profile()
    assert page.parallel_items_spinbox.value() == 2


def test_failed_stage_shows_technical_error_code_in_collapsible_area(qtbot, ui_fixture) -> None:
    from nasmove.ui.view_models import ConnectionStageResult, ConnectionTestReport

    ui_fixture.fake_service.report = ConnectionTestReport(
        (
            ConnectionStageResult("地址解析", True, None),
            ConnectionStageResult("TCP", True, None),
            ConnectionStageResult("SMB 协商", True, None),
            ConnectionStageResult("认证", False, "authentication_failed"),
            ConnectionStageResult("共享访问", False, None),
        )
    )
    page = ui_fixture.connection_page
    page.test_button.click()
    qtbot.waitUntil(lambda: "未测试" not in page.stage_labels["认证"].text())
    assert "失败" in page.stage_labels["认证"].text()
    assert page.error_details.isHidden()
    page.error_details_button.click()
    assert not page.error_details.isHidden()
    assert "authentication_failed" in page.error_details.toPlainText()


def test_smb_url_address_is_split_into_editable_fields(qtbot) -> None:
    page = ConnectionPage()
    qtbot.addWidget(page)
    page.address_lineedit.textEdited.emit("smb://nas.local/media/folder")
    assert page.address_lineedit.text() == "nas.local"
    assert page.share_lineedit.text() == "media"
    assert page.port_spinbox.value() == 445


def test_remember_password_saves_to_credential_store_after_successful_test(
    qtbot, ui_fixture
) -> None:
    page = ui_fixture.connection_page
    page.address_lineedit.setText("nas.local")
    page.share_lineedit.setText("media")
    page.username_lineedit.setText("user")
    page.password_lineedit.setText("secret")
    page.test_button.click()
    qtbot.waitUntil(lambda: bool(ui_fixture.credential_store.set_calls))
    assert page.profile_id is not None
    assert ui_fixture.credential_store.passwords[str(page.profile_id)] == "secret"


def test_unchecked_remember_password_keeps_password_process_local(qtbot, ui_fixture) -> None:
    page = ui_fixture.connection_page
    ui_fixture.credential_store.passwords[str(page.profile_id)] = "old-secret"
    page.remember_password_checkbox.setChecked(False)
    page.test_button.click()
    qtbot.waitUntil(lambda: bool(ui_fixture.credential_store.delete_calls))
    assert str(page.profile_id) not in ui_fixture.credential_store.passwords
    assert page.password_lineedit.text() == ""


def test_credential_write_failure_degrades_without_breaking_connection(
    qtbot, ui_fixture, tmp_path
) -> None:
    from nasmove.persistence.sqlite_repository import SqliteTaskRepository
    from nasmove.security.sqlite_store import SqliteCredentialStore

    # No profile row exists (no profile store wired here), so the credentials
    # foreign key genuinely rejects the write.  The connection must still finish
    # successfully; only the password is not remembered.
    repository = SqliteTaskRepository(tmp_path / "nasmove.db")
    store = SqliteCredentialStore(repository)
    page = ConnectionPage(tester=ui_fixture.fake_service, credential_store=store)
    qtbot.addWidget(page)
    page.password_lineedit.setText("secret")
    reports: list[object] = []
    page.report_ready.connect(reports.append)

    with qtbot.waitSignal(page.report_ready, timeout=5000):
        page.test_button.click()
    qtbot.waitUntil(lambda: not page.is_testing, timeout=5000)

    assert reports[0].success is True
    assert "无法保存密码到本机数据库" in page.error_details.toPlainText()
    assert store.get_password(page.profile_id) is None
    repository.close()


def test_last_successful_connection_and_keychain_password_are_restored(qtbot) -> None:
    from tests.fixtures.ui import FakeCredentialStore

    config = build_connection_config()
    store = ProfileStore(config)
    credentials = FakeCredentialStore()
    credentials.passwords[str(config.profile_id)] = "saved-secret"

    page = ConnectionPage(credential_store=credentials, profile_store=store)
    qtbot.addWidget(page)

    assert page.address_lineedit.text() == config.host
    assert page.share_lineedit.text() == config.share
    assert page.username_lineedit.text() == config.username
    assert page.password_lineedit.text() == "saved-secret"
    assert page.profile_id == config.profile_id


def test_successful_connection_saves_non_secret_profile(qtbot, ui_fixture) -> None:
    store = ProfileStore()
    page = ConnectionPage(
        tester=ui_fixture.fake_service,
        credential_store=ui_fixture.credential_store,
        profile_store=store,
    )
    qtbot.addWidget(page)
    page.password_lineedit.setText("secret")

    page.test_button.click()

    qtbot.waitUntil(lambda: bool(store.saved))
    assert store.saved[0].host == page.address_lineedit.text()


def test_loading_profile_restores_credential_without_emitting_user_change(qtbot) -> None:
    config = build_connection_config()
    credentials = FakeCredentialStore()
    credentials.passwords[str(config.profile_id)] = "saved-secret"
    page = ConnectionPage(credential_store=credentials)
    qtbot.addWidget(page)
    changes: list[bool] = []
    page.configuration_changed.connect(lambda: changes.append(True))

    page.load_profile(config)

    assert changes == []
    assert page.profile_id == config.profile_id
    assert page.address_lineedit.text() == config.host
    assert page.password_lineedit.text() == "saved-secret"


def test_editing_loaded_profile_keeps_stable_profile_id(qtbot) -> None:
    config = build_connection_config()
    page = ConnectionPage()
    qtbot.addWidget(page)
    page.load_profile(config)

    page.address_lineedit.setText("nas-new.local")

    assert page.connection_config().profile_id == config.profile_id


def test_clearing_profile_starts_a_new_identity_and_removes_password(qtbot) -> None:
    config = build_connection_config()
    page = ConnectionPage()
    qtbot.addWidget(page)
    page.load_profile(config)

    page.clear_profile()

    assert page.profile_id is None
    assert page.display_name_lineedit.text() == ""
    assert page.address_lineedit.text() == ""
    assert page.password_lineedit.text() == ""
    assert page.port_spinbox.value() == 445
    assert page.require_encryption_checkbox.isChecked() is True
