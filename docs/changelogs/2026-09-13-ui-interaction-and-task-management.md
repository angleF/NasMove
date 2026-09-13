# NasMove 改动记录与交接文档

更新日期：2026-09-13  
归档目录：`/Users/fuzhaoliang/codex-documents/NasMove/`

---

## 1. 改动概述

本次更新主要集中在 UI 交互缺陷修复、任务生命周期管理（删除/归档功能）、列表及按钮视觉排版优化，以及完整打包构建：

1. **修复 UI 信号绑定导致的“点击无反应”缺陷**（PyQt/PySide 信号参数隐式传递问题）。
2. **新增已完成/失败任务的删除与归档能力**（涵盖数据库、命令层、控制器与 UI 的全链路贯通）。
3. **消除横向滚动条并优化主题样式**。
4. **重构微型任务队列的按钮排版**，彻底解决宽度受限导致文字挤压变形的问题。
5. **完成 macOS 独立应用程序打包与签名验证**。

---

## 2. 详细变更说明

### 2.1 PyQt/PySide 信号绑定修复（解决点击无反应）

* **问题背景**：
  用户在界面点击“暂停”、“取消”、“置顶”、“编辑配置”等按钮时没有任何响应。
* **根因定位**：
  `QPushButton.clicked` 信号在 Qt 规范中默认会发射一个 `bool` 参数（表示控件的 `checked` 状态）。原先代码在连接信号时使用了无参匿名函数（例如 `clicked.connect(lambda: ...)`），当用户点击按钮时，Python 运行时抛出 `TypeError: <lambda>() takes 0 positional arguments but 1 was given`，导致槽函数被静默终止，界面无任何响应。
  此外，“编辑配置”在任务处于运行状态时，受底层安全策略保护被拦截（提示“任务处理中，请暂停任务后切换账号”）。
* **修改文件**：
  * `src/nasmove/ui/queue_panel.py`：所有按钮连接改为 `lambda *_: ...`
  * `src/nasmove/ui/task_page.py`：所有控制按钮连接改为 `lambda *_: ...`
  * `src/nasmove/ui/device_sidebar.py`：配置添加、编辑、移除、测试等按钮改为 `lambda *_: ...`
  * `src/nasmove/ui/source_page.py`：清空按钮改为 `lambda *_: ...`
  * `src/nasmove/ui/transfer_workspace.py`：上传/移动按钮改为 `lambda *_: ...`
  * `src/nasmove/ui/main_window.py`：新建任务按钮改为 `lambda *_: ...`

---

### 2.2 任务删除与归档功能实现

* **功能目标**：
  对任务队列中处于终态（已完成、完成但有警告、已失败、已取消）的任务，提供主动删除/归档的能力，从列表中清除并彻底级联清理相关存储，释放空间。
* **架构分层实现**：
  1. **持久化层 (`src/nasmove/persistence/sqlite_repository.py`)**：
     * 新增 `delete_task(self, task_id: TaskId) -> None` 方法。
     * 基于 SQLite 表结构中定义的 `ON DELETE CASCADE` 外键约束，在删除 `tasks` 记录时，自动级联删除对应的 `transfer_items`、`checkpoints`、`attempts` 及 `events` 记录。
  2. **命令服务层 (`src/nasmove/ui/task_commands.py`)**：
     * 在 `TaskCommandService` 中新增 `delete(self, task_id: object) -> None` 命令。
     * 添加状态防护：仅当任务处于 `COMPLETED`, `COMPLETED_WITH_WARNINGS`, `FAILED`, `CANCELED` 状态时才执行物理删除。
  3. **任务控制器 (`src/nasmove/ui/task_controller.py`)**：
     * 接入 `delete_requested` 信号。
     * 在 `_dispatch_for` 中特化处理 `delete` 命令：删除成功后立即从本地 `_tasks` 内存元组中剔除该任务，同步更新 `_page` 和 `_queue_panel`，避免由于任务已被删除而在调用 `get_task` 时抛出异常。
  4. **视图层 (`src/nasmove/ui/queue_panel.py` & `src/nasmove/ui/task_page.py`)**：
     * 新增 `delete_requested` 信号与 `delete_button`（按钮文字“删除”）。
     * 在状态更新逻辑中增加判定，只在任务处于终态时启用并显示“删除”按钮。

---

### 2.3 列表横向滚动条优化与主题补充

* **问题背景**：
  任务列表项因文本较长超出可视区域，出现非常粗糙的原生 Windows/X11 风格白底黑框横向滚动条，严重破坏视觉一致性。
* **修复措施**：
  1. **禁用横向滚动策略**：
     * 在 `QueuePanel` 的 `self.list_widget` 上调用 `setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)`。
     * 在 `TaskPage` 的 `self.queue_list` 上调用 `setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)`。
     * 列表超长文字采用默认截断显示，保持队列界面的整洁紧凑。
  2. **完善主题 QSS 样式 (`src/nasmove/ui/theme.py`)**：
     * 为全局样式表补全 `QScrollBar:horizontal` 及 `QScrollBar::handle:horizontal` 样式规则（透明轨道、细圆角滑块、与竖向滚动条相同的调色板契合度）。
     * 规范 f-string 转义，保持双大括号 `{{`、`}}` 的正确性。

---

### 2.4 侧边栏按钮排版重构

* **问题背景**：
  微型队列面板在加入“删除”按钮后共有 5 个按钮（置顶、暂停、继续/重试、取消、删除）。在窄侧边栏中强行单行排布导致按钮宽度严重不足，按钮内的两个中文字被挤压变形重叠。
* **排版调整 (`src/nasmove/ui/queue_panel.py`)**：
  * 将单行 `QHBoxLayout` 拆分为两行流式排布：
    * **第一行**：`置顶`、`暂停`、`继续/重试`（间距 6px）
    * **第二行**：`取消`、`删除`（间距 6px）
  * 按钮恢复正常的内边距与字体显示，视觉舒适度大幅改善。

---

### 2.5 应用程序打包与验证

* **构建环境**：
  * Python 解释器：`/Users/fuzhaoliang/miniconda3/envs/nasmove-py312/bin/python` (Python 3.12)
  * 打包工具：`pyside6-deploy` / Nuitka 4.1.1
  * 脚本：`scripts/build_macos_app.sh`
* **打包结果**：
  * 产物路径：`dist/NasMove.app`
  * 签名与校验：通过 `codesign --verify --deep --strict` 验证，系统状态为 `valid on disk` 并满足 `Designated Requirement`。

---

## 3. 修改文件清单

| 文件路径 | 变更性质 | 变更说明 |
|---|---|---|
| `src/nasmove/persistence/sqlite_repository.py` | 代码新增 | 新增 `delete_task` 级联删除任务数据方法 |
| `src/nasmove/ui/task_commands.py` | 代码新增 | 新增 `TaskCommandService.delete` 终态删除保护逻辑 |
| `src/nasmove/ui/task_controller.py` | 逻辑增强 | 串联 `delete_requested` 信号并在调度器中安全移除内存状态 |
| `src/nasmove/ui/queue_panel.py` | 修复/重构 | 修复 lambda 绑定；新增删除按钮；关掉横向滚动条；重构为两行排版 |
| `src/nasmove/ui/task_page.py` | 修复/增强 | 修复 lambda 绑定；新增删除按钮；关掉列表横向滚动条 |
| `src/nasmove/ui/theme.py` | 样式补充 | 增加 `QScrollBar:horizontal` 扁平化极简样式，修复模板转义 |
| `src/nasmove/ui/device_sidebar.py` | 缺陷修复 | 修复按钮 clicked 连接的 lambda 参数丢失错误 |
| `src/nasmove/ui/source_page.py` | 缺陷修复 | 修复清空按钮 clicked 连接的 lambda 参数丢失错误 |
| `src/nasmove/ui/transfer_workspace.py` | 缺陷修复 | 修复上传/移动按钮 clicked 连接的 lambda 参数丢失错误 |
| `src/nasmove/ui/main_window.py` | 缺陷修复 | 修复新建任务按钮 clicked 连接的 lambda 参数丢失错误 |
