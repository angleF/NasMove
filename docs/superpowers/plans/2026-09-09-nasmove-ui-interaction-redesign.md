# NasMove UI Interaction Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development`（推荐）or an approved inline execution workflow to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有四步向导升级为带三套持久化主题的 NasMove 迁移工作台，并在同一窗口中清晰呈现创建、执行与安全恢复状态。

**Architecture:** 保留所有 SMB、规划、队列、校验和删除服务接口；新增仅管理外观的主题模块，以及只组合 Qt Widgets 的工作台视图。`MainWindow` 只装配导航与依赖，`SetupWorkspace` 复用既有连接、来源、目标页面，`TaskPage` 继续通过 `TaskController` 接收按任务 ID 路由的不可变执行事件。

**Tech Stack:** Python 3.12，PySide6 6.10，Qt Widgets，QSettings，pytest，pytest-qt，Ruff，mypy。

**Spec:** `docs/superpowers/specs/2026-09-09-nasmove-ui-interaction-redesign-design.md`

## Global Constraints

- 不迁移至 SwiftUI，不使用 iOS 专有 Liquid Glass API，不新增运行时依赖。
- 不修改 SMB 协议实现、SQLite schema、任务状态机、恢复规则、校验规则或源删除授权规则。
- 主题仅改变 QSS 色彩令牌与表面层次；绿色、蓝色、黄色、红色的安全语义必须保持文字和图标说明。
- 所有连接测试、目标浏览、计划扫描、传输、哈希和 SQLite 长操作继续在既有工作线程中运行。
- 移动任务继续强制完整校验；复制完成不等于迁移完成；源文件在提交和完整校验前不得删除。
- 预先存在的未提交改动位于 `src/nasmove/ui/main_window.py` 与 `src/nasmove/ui/connection_page.py`；实施前先检查 diff，只将本计划的目标行纳入提交。
- 每项实现都先写失败测试，再写最小实现；每个任务独立运行其目标测试并提交。

---

## File Structure

| 文件 | 责任 |
|---|---|
| Create `src/nasmove/ui/theme.py` | 三个主题、跟随系统回退、QSettings 持久化和 QSS 令牌。 |
| Create `src/nasmove/ui/setup_workspace.py` | 创建态摘要卡、编辑入口和入队条件展示；不包含 SMB／规划逻辑。 |
| Create `src/nasmove/ui/navigation.py` | 左侧导航与活动任务状态胶囊；仅发出导航意图。 |
| Modify `src/nasmove/ui/main_window.py` | 依赖装配、导航路由、工作台模式切换和主题应用。 |
| Modify `src/nasmove/ui/task_page.py` | 执行／恢复态的阶段轨道、安全卡及已有任务队列详情。 |
| Modify `src/nasmove/ui/task_controller.py` | 保持按任务 ID 的路由，同时发出工作台展示状态变更。 |
| Modify `src/nasmove/ui/__init__.py` | 导出新 UI 组件。 |
| Create `tests/unit/ui/test_theme.py` | 主题选择、持久化和语义色测试。 |
| Create `tests/unit/ui/test_setup_workspace.py` | 创建态摘要、启用条件和编辑意图测试。 |
| Create `tests/unit/ui/test_navigation.py` | 导航、活动任务胶囊和窄布局测试。 |
| Modify `tests/unit/ui/test_main_window.py` | 壳层路由、主题入口与既有依赖装配回归。 |
| Modify `tests/unit/ui/test_workbench.py` | 空、执行、恢复状态及安全文案回归。 |
| Modify `tests/unit/ui/test_task_controller.py` | 任务选择隔离和工作台状态信号回归。 |
| Modify `docs/verification/task-workbench-verification.md` | 记录尺寸、主题、恢复态与自动化验证结果。 |

## Task 1: ThemeController 与可访问的视觉令牌

**Files:**

- Create: `tests/unit/ui/test_theme.py`
- Create: `src/nasmove/ui/theme.py`
- Modify: `src/nasmove/ui/__init__.py`

**Interfaces:**

- Consumes: `PySide6.QtCore.QSettings`；不得读取任务库或 Keychain。
- Produces: `ThemeName`、`ThemePalette`、`ThemeController`。`ThemeController.theme_changed: Signal(object)`；`select(theme: ThemeName) -> None`；`stylesheet() -> str`；`selected_theme() -> ThemeName`。

- [ ] **Step 1: 写入失败测试，定义默认、持久化与稳定语义色。**

```python
from PySide6.QtCore import QSettings

from nasmove.ui.theme import ThemeController, ThemeName


def test_theme_defaults_to_frosted_light_when_no_value_is_saved(tmp_path):
    settings = QSettings(str(tmp_path / "appearance.ini"), QSettings.Format.IniFormat)
    controller = ThemeController(settings)

    assert controller.selected_theme() is ThemeName.SYSTEM
    assert "#175CD3" in controller.stylesheet()


def test_selecting_theme_persists_and_emits(tmp_path, qtbot):
    settings = QSettings(str(tmp_path / "appearance.ini"), QSettings.Format.IniFormat)
    controller = ThemeController(settings)
    with qtbot.waitSignal(controller.theme_changed) as signal:
        controller.select(ThemeName.MIDNIGHT_OPS)

    assert signal.args == [ThemeName.MIDNIGHT_OPS]
    assert settings.value("appearance/theme") == ThemeName.MIDNIGHT_OPS.value


def test_each_theme_keeps_explicit_failure_and_recovery_tokens(tmp_path):
    settings = QSettings(str(tmp_path / "appearance.ini"), QSettings.Format.IniFormat)
    controller = ThemeController(settings)
    for theme in ThemeName:
        controller.select(theme)
        stylesheet = controller.stylesheet()
        assert "#B42318" in stylesheet
        assert "#B54708" in stylesheet
```

- [ ] **Step 2: 运行测试，确认它因模块缺失而失败。**

Run: `python -m pytest tests/unit/ui/test_theme.py -v`

Expected: FAIL，错误为 `ModuleNotFoundError: No module named 'nasmove.ui.theme'`。

- [ ] **Step 3: 实现最小主题模块；使用注入的 `QSettings`，不创建全局单例。**

```python
from enum import StrEnum

from PySide6.QtCore import QObject, QSettings, Signal


class ThemeName(StrEnum):
    SYSTEM = "system"
    FROSTED_LIGHT = "frosted_light"
    MIDNIGHT_OPS = "midnight_ops"
    WARM_STUDIO = "warm_studio"


class ThemeController(QObject):
    theme_changed = Signal(object)

    def __init__(self, settings: QSettings) -> None:
        super().__init__()
        self._settings = settings
        self._selected = ThemeName(self._settings.value("appearance/theme", ThemeName.SYSTEM.value))

    def selected_theme(self) -> ThemeName:
        return self._selected

    def select(self, theme: ThemeName) -> None:
        self._selected = theme
        self._settings.setValue("appearance/theme", theme.value)
        self._settings.sync()
        self.theme_changed.emit(theme)
```

`stylesheet()` 必须从每个主题的不可变 `ThemePalette` 生成完整的应用级 QSS。`SYSTEM` 在无法读取系统深浅色时返回雾蓝玻璃令牌。所有 palette 都包含完全相同的 `stateRunning`、`stateRecovered`、`stateWarning`、`stateFailure` 颜色值，并在 QSS 中为状态控件提供 `color`、图标及文本背景样式。

- [ ] **Step 4: 导出公共类型并运行目标测试。**

```python
from nasmove.ui.theme import ThemeController, ThemeName

__all__ = ["ThemeController", "ThemeName"]
```

Run: `python -m pytest tests/unit/ui/test_theme.py -v`

Expected: PASS。

- [ ] **Step 5: 静态检查并提交。**

Run: `python -m ruff check src/nasmove/ui/theme.py tests/unit/ui/test_theme.py && python -m mypy src/nasmove/ui/theme.py`

Expected: 两条命令均成功。

```bash
git add src/nasmove/ui/theme.py src/nasmove/ui/__init__.py tests/unit/ui/test_theme.py
git commit -m "feat: add switchable NasMove themes"
```

## Task 2: 导航壳与活动任务状态胶囊

**Files:**

- Create: `tests/unit/ui/test_navigation.py`
- Create: `src/nasmove/ui/navigation.py`
- Modify: `src/nasmove/ui/__init__.py`

**Interfaces:**

- Consumes: `ThemeName`，但不直接调用 `ThemeController`。
- Produces: `AppDestination`、`AppNavigation`。`destination_requested: Signal(object)`、`activity_requested: Signal()`；`set_active_task_count(count: int) -> None`；`set_selected(destination: AppDestination) -> None`。

- [ ] **Step 1: 写入失败测试，固定导航目的地和活动任务文案。**

```python
from nasmove.ui.navigation import AppDestination, AppNavigation


def test_navigation_emits_explicit_destination(qtbot):
    navigation = AppNavigation()
    qtbot.addWidget(navigation)
    with qtbot.waitSignal(navigation.destination_requested) as signal:
        navigation.buttons[AppDestination.CONNECTIONS].click()

    assert signal.args == [AppDestination.CONNECTIONS]


def test_activity_pill_is_hidden_without_active_tasks_and_opens_queue(qtbot):
    navigation = AppNavigation()
    qtbot.addWidget(navigation)
    navigation.set_active_task_count(0)
    assert navigation.activity_button.isHidden()

    navigation.set_active_task_count(2)
    assert navigation.activity_button.text() == "2 个任务正在处理"
    with qtbot.waitSignal(navigation.activity_requested):
        navigation.activity_button.click()
```

- [ ] **Step 2: 运行测试，确认缺少导航模块。**

Run: `python -m pytest tests/unit/ui/test_navigation.py -v`

Expected: FAIL，错误为 `ModuleNotFoundError: No module named 'nasmove.ui.navigation'`。

- [ ] **Step 3: 实现无业务依赖的导航控件。**

```python
from enum import StrEnum

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget


class AppDestination(StrEnum):
    WORKBENCH = "workbench"
    QUEUE = "queue"
    CONNECTIONS = "connections"
    HISTORY = "history"
    PREFERENCES = "preferences"


class AppNavigation(QWidget):
    destination_requested = Signal(object)
    activity_requested = Signal()

    def set_active_task_count(self, count: int) -> None:
        self.activity_button.setVisible(count > 0)
        self.activity_button.setText(f"{count} 个任务正在处理")
```

为每个导航按钮设置稳定的 object name，例如 `navigation-workbench`，并把 `buttons` 暴露为 `dict[AppDestination, QPushButton]` 以便测试与可访问性定位。使用 `QToolButton` 或 `QPushButton` 的文本和图标组合；窄布局只隐藏文本，不删除按钮或快捷键焦点。

- [ ] **Step 4: 运行目标测试，并检查其不触发任何后台工作。**

Run: `python -m pytest tests/unit/ui/test_navigation.py -v`

Expected: PASS；测试中不创建 `QThread`、SMB gateway 或 SQLite repository。

- [ ] **Step 5: 提交。**

```bash
git add src/nasmove/ui/navigation.py src/nasmove/ui/__init__.py tests/unit/ui/test_navigation.py
git commit -m "feat: add NasMove navigation shell"
```

## Task 3: 创建态工作台与既有三项输入的组合

**Files:**

- Create: `tests/unit/ui/test_setup_workspace.py`
- Create: `src/nasmove/ui/setup_workspace.py`
- Modify: `src/nasmove/ui/connection_page.py`
- Modify: `src/nasmove/ui/source_page.py`
- Modify: `src/nasmove/ui/target_page.py`
- Modify: `src/nasmove/ui/task_creation_controller.py`

**Interfaces:**

- Consumes: 已存在的 `ConnectionPage.report_ready`、`SourcePage.sources_changed`、`TargetPage.target_changed`、`TargetPage.create_task_requested`。
- Produces: `SetupWorkspace`。`edit_connection_requested: Signal()`、`edit_sources_requested: Signal()`、`edit_target_requested: Signal()`、`create_task_requested: Signal()`；`refresh_summary() -> None`；`set_connection_verified(verified: bool) -> None`。

- [ ] **Step 1: 写入失败测试，锁定入队条件与移动任务安全摘要。**

```python
from pathlib import Path

from nasmove.ui.connection_page import ConnectionPage
from nasmove.ui.setup_workspace import SetupWorkspace
from nasmove.ui.source_page import SourcePage
from nasmove.ui.target_page import TargetPage


def test_setup_workspace_enables_queue_only_after_three_ready_conditions(qtbot):
    connection = ConnectionPage()
    sources = SourcePage()
    target = TargetPage()
    workspace = SetupWorkspace(connection, sources, target)
    qtbot.addWidget(workspace)

    assert workspace.create_button.isEnabled() is False
    workspace.set_connection_verified(True)
    sources.set_sources([Path("/tmp/source")])
    target.set_selected_path("archive/2026")

    assert workspace.create_button.isEnabled() is True


def test_setup_workspace_describes_move_as_verify_commit_then_delete(qtbot):
    workspace = SetupWorkspace(ConnectionPage(), SourcePage(), TargetPage())
    qtbot.addWidget(workspace)
    workspace.source_page.move_checkbox.setChecked(True)

    assert "完整回读" in workspace.safety_label.text()
    assert "删除源文件" in workspace.safety_label.text()
```

- [ ] **Step 2: 运行测试，确认工作台模块尚不存在。**

Run: `python -m pytest tests/unit/ui/test_setup_workspace.py -v`

Expected: FAIL，错误为 `ModuleNotFoundError: No module named 'nasmove.ui.setup_workspace'`。

- [ ] **Step 3: 实现摘要卡和编辑面板，不复制或迁移既有业务逻辑。**

```python
class SetupWorkspace(QWidget):
    edit_connection_requested = Signal()
    edit_sources_requested = Signal()
    edit_target_requested = Signal()
    create_task_requested = Signal()

    def __init__(self, connection_page: ConnectionPage, source_page: SourcePage, target_page: TargetPage) -> None:
        super().__init__()
        self.connection_page = connection_page
        self.source_page = source_page
        self.target_page = target_page
        self.create_button = QPushButton("加入队列")
        self.create_button.setEnabled(False)

    def refresh_summary(self) -> None:
        ready = self._connection_verified and bool(self.source_page.sources) and self.target_page.target_path()
        self.create_button.setEnabled(bool(ready))
```

三个摘要卡发射各自的编辑信号；点击卡后只显示对应的已有 page 作为工作台内编辑面板。不得复制 `ConnectionPage._config()`、`SourcePage.set_sources()` 或 `TargetPage.target_path()` 的字段组装逻辑。`create_button` 只发射 `create_task_requested`，再连接到既有 `TargetPage.create_task_requested`／`TaskCreationController.create_task` 路径。

- [ ] **Step 4: 补齐原有页面的摘要更新信号并运行创建态相关测试。**

连接成功后，`ConnectionPage._handle_report()` 仍发射 `report_ready(report)`；`TargetPage.set_selected_path()` 仍发射 `target_changed(path)`；`SourcePage.set_sources()` 仍发射 `sources_changed(tuple(sources))`。必要时只新增明确的展示信号，不改变现有信号名或其参数。

Run: `python -m pytest tests/unit/ui/test_setup_workspace.py tests/unit/ui/test_connection_page.py tests/unit/ui/test_source_page.py tests/unit/ui/test_target_page.py tests/unit/ui/test_task_creation_controller.py -v`

Expected: PASS。

- [ ] **Step 5: 提交。**

```bash
git add src/nasmove/ui/setup_workspace.py src/nasmove/ui/connection_page.py src/nasmove/ui/source_page.py src/nasmove/ui/target_page.py src/nasmove/ui/task_creation_controller.py tests/unit/ui/test_setup_workspace.py
git commit -m "feat: compose transfer setup workspace"
```

## Task 4: 执行态与恢复态任务详情

**Files:**

- Modify: `tests/unit/ui/test_workbench.py`
- Modify: `tests/unit/ui/test_task_controller.py`
- Modify: `src/nasmove/ui/task_page.py`
- Modify: `src/nasmove/ui/task_controller.py`

**Interfaces:**

- Consumes: `TransferEvent`、`TaskResult`、`ProgressSnapshot` 和既有 `TaskCommandService` 的 `pause(task_id)`、`resume(task_id)`、`cancel(task_id)`。
- Produces: `TaskPage.set_workspace_state(state: TaskState | str | None) -> None`、`TaskPage.phase_label`、`TaskPage.recovery_card`；`TaskController.workspace_state_changed: Signal(object)`。

- [ ] **Step 1: 写入失败测试，锁定阶段与恢复安全文案。**

```python
from nasmove.core.model import TaskId
from nasmove.core.states import TaskState
from nasmove.transfer.transfer_engine import TransferEvent
from nasmove.ui.task_page import TaskPage


def test_task_page_keeps_copy_and_verification_as_distinct_phases(qtbot):
    page = TaskPage()
    qtbot.addWidget(page)
    page.apply_event(TransferEvent(TaskId("one"), TaskState.VERIFYING))

    assert "完整回读校验" in page.phase_label.text()
    assert page.copy_progress.value() == 100
    assert page.verify_progress.isVisible()


def test_recovery_card_explains_checkpoint_and_source_safety(qtbot):
    page = TaskPage()
    qtbot.addWidget(page)
    page.apply_event(TransferEvent(TaskId("one"), TaskState.WAITING_FOR_NETWORK, retry_attempt=2, retry_delay=10))

    assert page.recovery_card.isVisible()
    assert "检查点" in page.recovery_label.text()
    assert "不会删除源文件" in page.recovery_label.text()
    assert page.resume_button.isEnabled() is True
```

- [ ] **Step 2: 运行测试，确认新增展示接口未定义。**

Run: `python -m pytest tests/unit/ui/test_workbench.py tests/unit/ui/test_task_controller.py -v`

Expected: FAIL，错误包含缺少 `phase_label` 或 `recovery_card`。

- [ ] **Step 3: 在 `TaskPage` 中实现阶段轨道与恢复卡，不改任务命令语义。**

```python
def set_workspace_state(self, state: TaskState | str | None) -> None:
    value = str(getattr(state, "value", state))
    is_recovering = value == TaskState.WAITING_FOR_NETWORK.value
    self.recovery_card.setVisible(is_recovering)
    self.progress_panel.setVisible(not is_recovering)
    if value == TaskState.VERIFYING.value:
        self.phase_label.setText("复制完成　›　完整回读校验中　›　原子提交　›　源文件处理")
    elif is_recovering:
        self.phase_label.setText("传输已停止　›　等待重新连接　›　断点核验")
```

`recovery_label` 必须先展示“检查点已保存”“半成品未提交”“不会删除源文件”，再显示重试次数和倒计时。保留 `pause_requested`、`resume_requested`、`cancel_requested`、`selection_changed`、`queue_reordered`、`files_requested` 与 `connection_requested` 的名称和语义。任何旧错误详情仍保持脱敏与折叠。

- [ ] **Step 4: 在控制器中发布展示状态，同时保持选择隔离与终态保护。**

```python
class TaskController(QObject):
    workspace_state_changed = Signal(object)

    def _apply_event(self, event: object) -> None:
        task_id = getattr(event, "task_id", None)
        state = getattr(event, "state", None)
        self._update_row(task_id, state)
        if self._visible(task_id):
            self._page.apply_event(event)
            self.workspace_state_changed.emit(state)
```

不得让来自未选中任务的 `TransferEvent` 改写详情。`_apply_progress()` 在完成状态后仍必须忽略迟到快照；`_apply_result()` 后仍应显示最终结果而非恢复卡。

- [ ] **Step 5: 运行任务详情与控制器测试。**

Run: `python -m pytest tests/unit/ui/test_workbench.py tests/unit/ui/test_task_controller.py tests/unit/ui/test_queue_execution_controller.py -v`

Expected: PASS；现有“后台事件不覆盖选中任务”“迟到进度不撤销完成结果”断言继续通过。

- [ ] **Step 6: 提交。**

```bash
git add src/nasmove/ui/task_page.py src/nasmove/ui/task_controller.py tests/unit/ui/test_workbench.py tests/unit/ui/test_task_controller.py
git commit -m "feat: clarify running and recovery task states"
```

## Task 5: 将壳层、主题与工作台路由装配到 MainWindow

**Files:**

- Modify: `tests/unit/ui/test_main_window.py`
- Modify: `src/nasmove/ui/main_window.py`

**Interfaces:**

- Consumes: `ThemeController`、`AppNavigation`、`SetupWorkspace`、`TaskPage`、既有 `TaskCreationController.task_created` 与 `TaskController.workspace_state_changed`。
- Produces: `MainWindow.navigation`、`MainWindow.theme_controller`、`MainWindow.setup_workspace`、`MainWindow.content_stack`；`show_destination(destination: AppDestination) -> None`。

- [ ] **Step 1: 写入失败测试，定义壳层行为而不是测试像素颜色。**

```python
from PySide6.QtCore import QSettings

from nasmove.ui.main_window import MainWindow
from nasmove.ui.navigation import AppDestination
from nasmove.ui.theme import ThemeController, ThemeName


def test_main_window_routes_navigation_without_losing_existing_pages(qtbot, tmp_path):
    settings = QSettings(str(tmp_path / "appearance.ini"), QSettings.Format.IniFormat)
    window = MainWindow(theme_controller=ThemeController(settings))
    qtbot.addWidget(window)

    window.show_destination(AppDestination.WORKBENCH)
    assert window.content_stack.currentWidget() is window.setup_workspace
    window.show_destination(AppDestination.QUEUE)
    assert window.content_stack.currentWidget() is window.task_page


def test_main_window_applies_selected_theme(qtbot, tmp_path):
    settings = QSettings(str(tmp_path / "appearance.ini"), QSettings.Format.IniFormat)
    controller = ThemeController(settings)
    window = MainWindow(theme_controller=controller)
    qtbot.addWidget(window)
    controller.select(ThemeName.WARM_STUDIO)

    assert "#B76035" in window.styleSheet()
```

- [ ] **Step 2: 运行测试，确认新构造参数和属性缺失。**

Run: `python -m pytest tests/unit/ui/test_main_window.py -v`

Expected: FAIL，错误包含不支持的 `theme_controller` 参数或缺少 `content_stack`。

- [ ] **Step 3: 重组 MainWindow；保留注入参数、控制器装配和既有页面实例。**

```python
def __init__(self, *, theme_controller: ThemeController | None = None, **dependencies: object) -> None:
    super().__init__()
    self.theme_controller = theme_controller or ThemeController(QSettings())
    self.navigation = AppNavigation()
    self.setup_workspace = SetupWorkspace(self.connection_page, self.source_page, self.target_page)
    self.content_stack = QStackedWidget()
    self.content_stack.addWidget(self.setup_workspace)
    self.content_stack.addWidget(self.task_page)
    self.theme_controller.theme_changed.connect(lambda _theme: self.setStyleSheet(self.theme_controller.stylesheet()))
```

`task_creation_controller` 继续接收同一组 `ConnectionPage`、`SourcePage`、`TargetPage` 实例；`queue_execution`、`task_commands`、`TaskController` 和 `_task_created()` 的队列启动行为必须原样保留。删除旧上一步／下一步导航前，先将其行为完全替换为 `SetupWorkspace` 的编辑入口；不得留下可创建绕过预检的第二条路径。

- [ ] **Step 4: 接线活动任务胶囊、恢复切换与窗口尺寸策略。**

将 `TaskController.workspace_state_changed` 连接为：所选任务处于 `running`、`verifying`、`committing`、`deleting_source` 或 `waiting_for_network` 时，工作台显示 `task_page`；队列为空或用户选择“新建迁移”时显示 `setup_workspace`。`AppNavigation.activity_requested` 只进入队列，不启动、暂停或恢复任务。保留 760×620 的最低尺寸与现有 `ConnectionPage.scroll_area` 小窗口保护。

- [ ] **Step 5: 运行壳层与全部 UI 单元测试。**

Run: `python -m pytest tests/unit/ui -v`

Expected: PASS；`test_main_window_wires_task_page_to_commands`、连接布局和任务创建回归全部继续通过。

- [ ] **Step 6: 提交。**

```bash
git add src/nasmove/ui/main_window.py tests/unit/ui/test_main_window.py
git commit -m "feat: assemble themed migration workbench"
```

## Task 6: 真实流程回归、视觉验证与发布记录

**Files:**

- Modify: `tests/integration/test_desktop_workflow.py`
- Modify: `docs/verification/task-workbench-verification.md`

**Interfaces:**

- Consumes: `build_desktop_runtime()` 与既有 Synology 环境变量。
- Produces: 可重复的 UI 验收证据；不产生新的传输协议接口。

- [ ] **Step 1: 为真实桌面流程补充工作台断言。**

```python
assert runtime.window.content_stack.currentWidget() is runtime.window.setup_workspace
runtime.window.setup_workspace.set_connection_verified(True)
runtime.window.source_page.set_sources([source])
runtime.window.target_page.set_selected_path(root)
assert runtime.window.setup_workspace.create_button.isEnabled()
```

连接、规划、入队、串行执行、SMB 写入、完整校验与完成后的源文件保留断言必须保持；测试不得改为只验证模拟进度。

- [ ] **Step 2: 在显式 Synology 环境中运行集成测试。**

Run: `NASMOVE_TEST_SYNOLOGY=1 python -m pytest tests/integration/test_desktop_workflow.py -v`

Expected: PASS；仅使用 `NasMoveTest/` 下唯一命名测试文件，finally 块继续删除远端测试对象。

- [ ] **Step 3: 运行标准自动化检查。**

Run: `python -m pytest --ignore=tests/release -q`

Expected: PASS；显式开启的真实 NAS／故障门禁仍按项目配置跳过。

Run: `python -m ruff check src tests && python -m mypy src`

Expected: 两条命令均成功。

- [ ] **Step 4: 进行离屏视觉检查并更新验证记录。**

在 800×620、1100×760、1600×1000 下分别检查雾蓝玻璃、深夜运维、暖灰工作室的创建态、执行态与恢复态。记录每个主题的状态文字可读性、导航折叠、输入高度、恢复卡与主要操作可访问性；不要把合成演示截图记作真实 NAS 证据。

- [ ] **Step 5: 提交验证记录。**

```bash
git add tests/integration/test_desktop_workflow.py docs/verification/task-workbench-verification.md
git commit -m "test: verify redesigned migration workbench"
```

## Plan Self-Review

### Spec coverage

- 迁移指挥台与持久导航：Task 2、Task 3、Task 5。
- 创建态的连接、来源、目标、预检和入队保护：Task 3、Task 5。
- 复制、校验、提交和源处理的明确阶段：Task 4。
- 网络中断、NAS 重启和安全恢复表达：Task 4、Task 6。
- 三套主题、快捷切换、持久化、稳定语义色和低开销视觉层：Task 1、Task 5、Task 6。
- 小窗口、键盘语义、任务选择隔离、终态保护和真实 NAS 流程：Task 2、Task 4、Task 5、Task 6。
- 不修改传输协议、状态机、schema 和 Keychain：所有任务遵守 Global Constraints。

### Placeholder scan

已检查：本计划没有占位标记、未命名接口或“按需处理”类步骤。每个新增接口都在其首次任务中定义，后续任务只消费已定义接口。

### Type consistency

- 主题接口始终使用 `ThemeName`、`ThemeController` 和 `theme_changed`。
- 导航接口始终使用 `AppDestination` 和 `AppNavigation.destination_requested`。
- 创建态接口始终使用 `SetupWorkspace.create_task_requested`。
- 执行态接口始终使用 `TaskPage.set_workspace_state` 与 `TaskController.workspace_state_changed`。
