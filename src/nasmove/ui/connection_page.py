from __future__ import annotations

import hashlib
from typing import Any, cast

from PySide6.QtCore import QSignalBlocker, Qt, QThread, Signal, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
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
    configuration_changed = Signal()

    def __init__(
        self,
        *,
        tester: object | None = None,
        credential_store: object | None = None,
        profile_store: object | None = None,
    ) -> None:
        super().__init__()
        self._tester = tester
        self._credential_store = credential_store
        self._profile_store = profile_store
        self._thread: QThread | None = None
        self._worker: BackgroundCommandWorker | None = None
        self.profile_id: ConnectionProfileId | None = None
        self._tested_config: ConnectionConfig | None = None

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
        self.toggle_password_action = QAction("👁", self.password_lineedit)
        self.toggle_password_action.setToolTip("显示/隐藏密码")
        self.toggle_password_action.triggered.connect(self._toggle_password_visibility)
        self.password_lineedit.addAction(
            self.toggle_password_action, QLineEdit.ActionPosition.TrailingPosition
        )
        self.parallel_items_spinbox = QSpinBox()
        self.parallel_items_spinbox.setRange(1, 4)
        self.parallel_items_spinbox.setValue(2)
        self.parallel_items_spinbox.setToolTip(
            "同一迁移任务内同时处理的文件数；任务队列仍顺序执行"
        )
        self.remember_password_checkbox = QCheckBox("保存到本机数据库")
        self.remember_password_checkbox.setChecked(True)
        self.require_encryption_checkbox = QCheckBox("要求 SMB 加密")
        self.require_encryption_checkbox.setChecked(True)
        self.test_button = QPushButton("测试连接")
        self.test_button.setProperty("themeRole", "primary")
        self.test_button.setObjectName("connectionTestButton")
        self.test_button.setMinimumHeight(38)
        self.error_details_button = QPushButton("显示技术详情")
        self.error_details_button.setProperty("themeRole", "secondary")
        self.error_details_button.setObjectName("connectionDetailsButton")
        self.error_details_button.setCheckable(True)
        self.error_details = QTextEdit()
        self.error_details.setReadOnly(True)
        self.error_details.setHidden(True)
        self.stage_labels = {name: QLabel("未测试") for name in ("地址解析", "TCP", "SMB 协商", "认证", "共享访问")}
        self._build_ui()
        self.address_lineedit.textEdited.connect(self._split_address)
        self.test_button.clicked.connect(self.test_connection)
        self.error_details_button.toggled.connect(self.error_details.setVisible)
        self._restore_last_successful_connection()
        for field in (
            self.display_name_lineedit,
            self.address_lineedit,
            self.share_lineedit,
            self.username_lineedit,
            self.domain_lineedit,
            self.password_lineedit,
        ):
            field.textChanged.connect(lambda _text: self.configuration_changed.emit())
        self.port_spinbox.valueChanged.connect(lambda _value: self.configuration_changed.emit())
        self.parallel_items_spinbox.valueChanged.connect(
            lambda _value: self.configuration_changed.emit()
        )
        self.require_encryption_checkbox.toggled.connect(
            lambda _checked: self.configuration_changed.emit()
        )

    def _build_ui(self) -> None:
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(20)
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
            widget.setMinimumHeight(max(34, widget.fontMetrics().height() + 16))
            widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            form.addRow(label, widget)
        stages = QGroupBox("连接测试")
        stages.setObjectName("connectionStages")
        stage_layout = QFormLayout(stages)
        for stage_name, stage_label in self.stage_labels.items():
            stage_layout.addRow(stage_name, stage_label)
        stage_layout.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        stage_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        stage_layout.setVerticalSpacing(8)
        stages.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        performance = QGroupBox("传输性能")
        performance.setObjectName("connectionPerformance")
        performance_layout = QFormLayout(performance)
        performance_layout.addRow("并发文件数", self.parallel_items_spinbox)
        card = QWidget()
        card.setMaximumWidth(760)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addLayout(form)
        layout.addWidget(performance)
        layout.addWidget(self.remember_password_checkbox)
        layout.addWidget(self.require_encryption_checkbox)
        layout.addWidget(self.test_button)
        layout.addWidget(stages)
        layout.addWidget(self.error_details_button)
        layout.addWidget(self.error_details)
        self.error_details.setMinimumHeight(90)
        self.error_details.setMaximumHeight(130)
        self.test_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addStretch()
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setWidget(card)
        self.scroll_area.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.scroll_area)

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
            profile_id=self.profile_id
            or ConnectionProfileId(
                self._profile_key(host_text, share, self.username_lineedit.text())
            ),
            display_name=display_name,
            host=host_text,
            share=share,
            username=self.username_lineedit.text().strip(),
            port=self.port_spinbox.value(),
            domain=self.domain_lineedit.text().strip() or None,
            require_encryption=self.require_encryption_checkbox.isChecked(),
            max_parallel_items=self.parallel_items_spinbox.value(),
        )
        self.profile_id = config.profile_id
        return config

    def connection_config(self) -> ConnectionConfig:
        """Return the validated, non-secret connection fields for task planning."""
        return self._config()

    @property
    def is_testing(self) -> bool:
        return self._thread is not None

    def load_profile(self, config: ConnectionConfig) -> None:
        if self.is_testing:
            raise RuntimeError("connection test is still running")
        blockers = [QSignalBlocker(control) for control in self._editable_controls()]
        try:
            self.profile_id = config.profile_id
            self._tested_config = None
            self.display_name_lineedit.setText(config.display_name)
            self.address_lineedit.setText(config.host)
            self.port_spinbox.setValue(config.port)
            self.parallel_items_spinbox.setValue(config.max_parallel_items)
            self.share_lineedit.setText(config.share)
            self.username_lineedit.setText(config.username)
            self.domain_lineedit.setText(config.domain or "")
            self.require_encryption_checkbox.setChecked(config.require_encryption)
            self.password_lineedit.clear()
            getter = getattr(self._credential_store, "get_password", None)
            if callable(getter):
                try:
                    password = getter(config.profile_id)
                except Exception:  # noqa: BLE001 - never expose credential storage details
                    self.error_details.setPlainText("无法从本机数据库读取密码")
                else:
                    if password:
                        self.password_lineedit.setText(str(password))
            self._show_untested_stages()
        finally:
            del blockers

    def clear_profile(self) -> None:
        if self.is_testing:
            raise RuntimeError("connection test is still running")
        blockers = [QSignalBlocker(control) for control in self._editable_controls()]
        try:
            self.profile_id = None
            self._tested_config = None
            self.display_name_lineedit.clear()
            self.address_lineedit.clear()
            self.port_spinbox.setValue(445)
            self.parallel_items_spinbox.setValue(2)
            self.share_lineedit.clear()
            self.username_lineedit.clear()
            self.domain_lineedit.clear()
            self.password_lineedit.clear()
            self.remember_password_checkbox.setChecked(True)
            self.require_encryption_checkbox.setChecked(True)
            self._show_untested_stages()
        finally:
            del blockers

    def _editable_controls(self) -> tuple[QWidget, ...]:
        return (
            self.display_name_lineedit,
            self.address_lineedit,
            self.port_spinbox,
            self.parallel_items_spinbox,
            self.share_lineedit,
            self.username_lineedit,
            self.domain_lineedit,
            self.password_lineedit,
            self.remember_password_checkbox,
            self.require_encryption_checkbox,
        )

    def _show_untested_stages(self) -> None:
        for label in self.stage_labels.values():
            label.setText("未测试")
        self.error_details.clear()
        self.error_details_button.setChecked(False)

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
        if self._thread is not None:
            return
        self._reset_stages()
        try:
            config = self._config()
        except Exception as error:  # noqa: BLE001
            self._show_error(str(error))
            return
        request = ConnectionRequest(config=config, password=self.password_lineedit.text())
        self._tested_config = config
        tester = cast(Any, self._tester)
        if tester is None:
            self._show_error("未配置连接测试服务")
            return
        self.test_button.setEnabled(False)
        self.setEnabled(False)
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
        self.setEnabled(True)
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
            self._save_successful_profile()
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
                try:
                    setter(self.profile_id, password)
                except Exception:  # noqa: BLE001 - connection stays usable if the password is not remembered
                    self.error_details.append("无法保存密码到本机数据库")
        else:
            deleter = getattr(store, "delete_password", None)
            removed = True
            if callable(deleter):
                try:
                    deleter(self.profile_id)
                except Exception:  # noqa: BLE001 - connection stays usable if cleanup fails
                    removed = False
                    self.error_details.append("无法清除本机数据库中的密码")
            if removed:
                self.password_lineedit.clear()

    def _save_successful_profile(self) -> None:
        store = self._profile_store
        config = self._tested_config
        if store is None or config is None:
            return
        saver = getattr(store, "save_successful_connection", None)
        if callable(saver):
            try:
                saver(config)
            except Exception:  # noqa: BLE001 - connection remains valid if local history fails
                self.error_details.append("无法保存最近连接配置")

    def _restore_last_successful_connection(self) -> None:
        store = self._profile_store
        loader = getattr(store, "last_successful_connection", None)
        if not callable(loader):
            return
        try:
            config = loader()
        except Exception:  # noqa: BLE001 - startup must remain usable
            self.error_details.setPlainText("无法读取最近连接配置")
            return
        if not isinstance(config, ConnectionConfig):
            return
        self.load_profile(config)

    def _show_error(self, message: object) -> None:
        self.setEnabled(True)
        self.error_details.setPlainText(str(message))
        self.error_details_button.setChecked(True)
        self.test_button.setEnabled(True)

    @Slot()
    def _thread_finished(self) -> None:
        self.setEnabled(True)
        self._worker = None
        self._thread = None
        self.test_button.setEnabled(True)

    def _toggle_password_visibility(self) -> None:
        if self.password_lineedit.echoMode() == QLineEdit.EchoMode.Password:
            self.password_lineedit.setEchoMode(QLineEdit.EchoMode.Normal)
            self.toggle_password_action.setText("🔒")
        else:
            self.password_lineedit.setEchoMode(QLineEdit.EchoMode.Password)
            self.toggle_password_action.setText("👁")


__all__ = ["ConnectionPage"]
