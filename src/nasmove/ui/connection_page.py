from __future__ import annotations

import hashlib
from typing import Any, cast

from PySide6.QtCore import QThread, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from nasmove.core.errors import DomainValidationError
from nasmove.core.model import ConnectionConfig, ConnectionProfileId
from nasmove.planning.addresses import parse_smb_address
from nasmove.ui.view_models import ConnectionRequest, ConnectionTestReport
from nasmove.ui.worker import BackgroundCommandWorker


class ConnectionPage(QWidget):
    report_ready = Signal(object)

    def __init__(self, *, tester: object | None = None, credential_store: object | None = None) -> None:
        super().__init__()
        self._tester = tester
        self._credential_store = credential_store
        self._thread: QThread | None = None
        self._worker: BackgroundCommandWorker | None = None
        self.profile_id: ConnectionProfileId | None = None

        self.display_name_lineedit = QLineEdit()
        self.address_lineedit = QLineEdit()
        self.address_lineedit.setPlaceholderText("nas.local 或 192.168.1.10")
        self.address_lineedit.setText("nas.local")
        self.port_spinbox = QSpinBox()
        self.port_spinbox.setRange(1, 65535)
        self.port_spinbox.setValue(445)
        self.share_lineedit = QLineEdit()
        self.share_lineedit.setText("media")
        self.username_lineedit = QLineEdit()
        self.username_lineedit.setText("user")
        self.domain_lineedit = QLineEdit()
        self.password_lineedit = QLineEdit()
        self.password_lineedit.setEchoMode(QLineEdit.EchoMode.Password)
        self.remember_password_checkbox = QCheckBox("保存到 macOS Keychain")
        self.remember_password_checkbox.setChecked(True)
        self.require_encryption_checkbox = QCheckBox("要求 SMB 加密")
        self.require_encryption_checkbox.setChecked(True)
        self.test_button = QPushButton("测试连接")
        self.error_details_button = QPushButton("显示技术详情")
        self.error_details_button.setCheckable(True)
        self.error_details = QTextEdit()
        self.error_details.setReadOnly(True)
        self.error_details.setHidden(True)
        self.stage_labels = {name: QLabel("未测试") for name in ("地址解析", "TCP", "SMB 协商", "认证", "共享访问")}
        self._build_ui()
        self.address_lineedit.textEdited.connect(self._split_address)
        self.test_button.clicked.connect(self.test_connection)
        self.error_details_button.toggled.connect(self.error_details.setVisible)

    def _build_ui(self) -> None:
        form = QFormLayout()
        fields = (
            ("配置名", self.display_name_lineedit),
            ("SMB 地址", self.address_lineedit),
            ("端口", self.port_spinbox),
            ("共享目录", self.share_lineedit),
            ("用户名", self.username_lineedit),
            ("域（可选）", self.domain_lineedit),
            ("密码", self.password_lineedit),
        )
        for label, widget in fields:
            form.addRow(label, widget)
        stages = QGroupBox("连接测试")
        stage_layout = QFormLayout(stages)
        for stage_name, stage_label in self.stage_labels.items():
            stage_layout.addRow(stage_name, stage_label)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.remember_password_checkbox)
        layout.addWidget(self.require_encryption_checkbox)
        layout.addWidget(self.test_button)
        layout.addWidget(stages)
        layout.addWidget(self.error_details_button)
        layout.addWidget(self.error_details)

    @Slot(str)
    def _split_address(self, value: str) -> None:
        if not value.lower().startswith("smb://"):
            return
        try:
            parsed = parse_smb_address(value)
        except DomainValidationError:
            return
        self.address_lineedit.blockSignals(True)
        self.address_lineedit.setText(parsed.host)
        self.address_lineedit.blockSignals(False)
        self.port_spinbox.setValue(parsed.port)
        self.share_lineedit.setText(parsed.share)

    def _config(self) -> ConnectionConfig:
        display_name = self.display_name_lineedit.text().strip() or self.address_lineedit.text().strip()
        host_text = self.address_lineedit.text().strip()
        share = self.share_lineedit.text().strip()
        config = ConnectionConfig(
            profile_id=ConnectionProfileId(self._profile_key(host_text, share, self.username_lineedit.text())),
            display_name=display_name,
            host=host_text,
            share=share,
            username=self.username_lineedit.text().strip(),
            port=self.port_spinbox.value(),
            domain=self.domain_lineedit.text().strip() or None,
            require_encryption=self.require_encryption_checkbox.isChecked(),
        )
        self.profile_id = config.profile_id
        return config

    def connection_config(self) -> ConnectionConfig:
        """Return the validated, non-secret connection fields for task planning."""
        return self._config()

    @staticmethod
    def _profile_key(host: str, share: str, username: str) -> str:
        value = f"{host}\x00{share}\x00{username}"
        return hashlib.sha256(value.encode()).hexdigest()[:32]

    def _reset_stages(self) -> None:
        for label in self.stage_labels.values():
            # Keep the initial-state marker until the worker publishes a
            # result; this also makes the transition observable to callers.
            label.setText("未测试（测试中…）")
        self.error_details.clear()
        self.error_details_button.setChecked(False)

    @Slot()
    def test_connection(self) -> None:
        self._reset_stages()
        try:
            config = self._config()
        except Exception as error:  # noqa: BLE001
            self._show_error(str(error))
            return
        request = ConnectionRequest(config=config, password=self.password_lineedit.text())
        tester = cast(Any, self._tester)
        if tester is None:
            self._show_error("未配置连接测试服务")
            return
        self.test_button.setEnabled(False)
        worker = BackgroundCommandWorker(lambda: tester.test_connection(request))
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._handle_report)
        worker.failed.connect(self._show_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._thread_finished)
        self._worker = worker
        self._thread = thread
        thread.start()

    @Slot(object)
    def _handle_report(self, report: object) -> None:
        if not isinstance(report, ConnectionTestReport):
            self._show_error("连接测试返回了无效结果")
            return
        for stage in report.stages:
            label = self.stage_labels.get(stage.name)
            if label is not None:
                label.setText("通过" if stage.success else "失败")
            if not stage.success and stage.error_code:
                self.error_details.append(stage.error_code)
        if report.success:
            self._save_password()
        self.report_ready.emit(report)

    def _save_password(self) -> None:
        store = self._credential_store
        if store is None or self.profile_id is None:
            return
        password = self.password_lineedit.text()
        if self.remember_password_checkbox.isChecked() and password:
            setter = getattr(store, "set_password", None)
            if callable(setter):
                setter(self.profile_id, password)
        else:
            deleter = getattr(store, "delete_password", None)
            if callable(deleter):
                deleter(self.profile_id)
            self.password_lineedit.clear()

    def _show_error(self, message: object) -> None:
        self.error_details.setPlainText(str(message))
        self.error_details_button.setChecked(True)
        self.test_button.setEnabled(True)

    @Slot()
    def _thread_finished(self) -> None:
        self._worker = None
        self._thread = None
        self.test_button.setEnabled(True)


__all__ = ["ConnectionPage"]
