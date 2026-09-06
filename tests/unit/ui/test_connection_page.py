from nasmove.ui.connection_page import ConnectionPage


def test_connection_page_defaults_to_port_445_keychain_and_encryption(qtbot) -> None:
    page = ConnectionPage()
    qtbot.addWidget(page)
    assert page.port_spinbox.value() == 445
    assert page.remember_password_checkbox.isChecked() is True
    assert page.require_encryption_checkbox.isChecked() is True


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
