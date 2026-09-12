# 任务工作台验证记录

验证日期：2026-09-09。

## 问题与修复

- 真实 SQLite 连接在主线程创建，在规划和执行线程使用时触发 `sqlite3.ProgrammingError`。新增真实数据库跨线程回归测试，确认修复前失败、修复后通过；现在每个线程独立使用数据库连接，工作线程结束时释放连接。
- 执行异常原先仅发出无参数信号，任务页清空详情。现在传递安全错误码，显示中文原因与建议，并保存错误事件；保存失败明确提示导出报告。
- 原先任务事件更新当前选中行，可能覆盖其他任务。现在事件、结果和进度按任务 ID 关联，终止结果不被迟到进度覆盖。
- 原先大文件完成后才显示复制／校验进度。现在复制按持久化检查点更新，远端校验按读取块更新；百分比不作为完成证明。
- 工作台支持状态详情、有效操作、文件结果分页、Finder 目标入口和导出摘要。暂停、取消、重连与成功分别展示；成功历史保留。
- 排队顺序以数据库记录为准；继续请求遇到工作线程退出窗口时不会丢失。

## 自动验证

`python -m pytest --ignore=tests/release -q`：859 passed，12 skipped。

跳过项为显式开启的真实 NAS／故障注入／规模门禁；其中新的桌面真实 NAS 流程已单独开启并通过。该次修改未重新执行 NAS 重启、50 GiB、100,000 文件门禁。

Ruff 通过；mypy 检查 45 个源码文件通过。应用包检查在构建结束后独立执行，避免构建替换包目录时出现无效失败；结果参见发布检查清单。

## 真实 NAS 桌面流程

`tests/integration/test_desktop_workflow.py`：1 passed。

使用临时本地状态库、内存测试凭据和已授权的 `NasMoveTest/` 下专用目录。实际点击连接测试及创建按钮，覆盖 Qt 线程、SQLite 规划持久化、串行执行、SMB 写入、完整校验与完成页面。再次读取目标验证内容一致，检查本地源文件仍保留；清理本次唯一命名的远端测试文件。

## 视觉检查

已渲染运行、执行异常和完成三种场景，检查 1100×760 与 800×620 窗口。任务列表和详情状态一致；小窗口详情可滚动；错误原因有文字说明；完成阶段显示校验与源文件处理结果。演示截图使用合成数据，不作为真实 NAS 传输证据。

## 连接页布局补充验证（2026-09-09）

展开技术详情时，原布局将地址输入框压缩到 12 像素，而字体高度为 17 像素，导致文字裁切。连接表单现在保留最小行高、限制详情高度，并在空间不足时滚动；步骤标题允许换行。修复后离屏渲染测得输入框高度为 34 像素。

新增 800×620、1100×760、1600×1000 三种尺寸回归测试；界面单元测试 50 passed。此次改动仅涉及布局，不改变连接、凭据或传输逻辑。

## 使用边界

未知执行异常不会直接强行重启迁移；用户可检查连接、查看文件结果和导出摘要。已暂停任务通过既有恢复协议继续。源文件变动、目标状态不确定等安全检查仍由传输协议决定，界面不会绕过它们。

本次未修改数据库 schema，也未加入新依赖。回滚代码时保留原任务库、已保存凭据和 NAS 临时文件。凭据后端后来从 macOS Keychain 改为应用本机数据库；升级时旧的 Keychain 密码不会自动迁移，用户需要重新输入一次，期间无凭据的任务按既有协议暂停。

## 三区迁移工作台验证（2026-09-10）

新的默认首页由当前 SMB 配置侧栏、Mac／NAS 双目录面板和任务级常驻队列组成。目录读取在后台线程执行，并使用请求编号拒绝迟到响应；修改连接配置时会立即失效并清空旧 NAS 目录。任务创建继续复用 `SourcePage`、`TargetPage`、`TaskCreationController`、现有任务规划器和串行队列，没有修改数据库 schema 或可靠传输协议。

自动化结果：

- `python -m pytest -q`：903 passed，12 skipped。
- `python -m pytest tests/unit/ui -q`：89 passed。
- `python -m ruff check src tests`：通过。
- `python -m mypy src`：54 个源码文件通过。
- `python -m pytest tests/integration/test_desktop_workflow.py tests/unit/test_package.py -v`：1 passed，1 skipped；真实桌面 SMB 流程需要显式提供隔离 Synology 测试目录。

使用合成目录和任务数据完成雾蓝玻璃、深夜运维两种主题，以及 800×620、1100×760、1300×780、1600×1000 四种尺寸的真实 Qt 渲染检查。长中文文件名保持单行省略，窄屏下配置栏和队列栏自动折叠，中央双栏、搜索框、路径、状态反馈和上传／移动按钮没有关键裁切。截图保存在 `/Users/fuzhaoliang/codex-documents/NasMove/screenshots/`，截图中的文件、连接和任务均为合成数据，不代表真实 NAS 传输结果。

本机未找到 `NasMove Local Signing` 或其他有效持久代码签名身份，`scripts/build_macos_app.sh` 在编译和删除现有应用包前按设计停止。原有 `dist/NasMove.app` 得到保留且 `codesign --verify --deep --strict` 通过，但该旧包为 x86_64 ad-hoc 签名，不能作为本次改造的新构建，也不能视为正式发布就绪。

当前产品边界仍为 SMB、Mac→NAS、串行任务，以及关闭应用时安全暂停并退出。NAS→Mac、多协议、并行传输和关窗后台运行没有作为可操作功能展示。

## 多 NAS 配置与连接隔离验证（2026-09-10）

连接配置数据库已从 schema 3 原子迁移到 schema 4，并增加软删除标记。配置侧栏现在展示所有未归档的成功连接，支持新增、选择、编辑和移除。存在未完成任务引用时拒绝移除；只有终态历史引用时保留完整任务数据并隐藏配置。重新保存相同 profile 会恢复配置。

配置切换会先让远端目录请求失效，再重置 SMB 会话、清空 NAS 目录、容量、目标选择和连接验证状态，最后载入新配置及对应已保存密码。连接测试、任务创建或队列执行期间的切换与移除请求会被拒绝。数据库归档成功后才删除已保存密码；凭据删除失败只显示固定的安全提示，不回滚任务历史，也不暴露底层异常。

自动化结果：

- `python -m pytest tests/unit/ui -q`：104 passed。
- `python -m pytest -q --ignore=tests/release`：923 passed，12 skipped。
- `python -m ruff check src tests`：通过。
- `python -m mypy src`：55 个源码文件通过。
- `git diff --check`：通过。

使用合成的两个 NAS 配置重新渲染雾蓝玻璃、深夜运维两种主题和 800×620、1100×760、1600×1000 三种尺寸。宽屏配置列表、选中状态、新增／移除／编辑入口正常；窄屏配置栏和任务队列折叠后仍可读取当前设备和任务数量。截图位于 `/Users/fuzhaoliang/codex-documents/NasMove/screenshots/multi-profile-*.png`。

当前 `dist/NasMove.app` 不存在，因此未通过依赖现有应用包的两项发布测试；这不作为本阶段源码与 UI 通过的证据。新包仍受持久签名身份门禁约束。

## 可取消的真实预检会话验证（2026-09-10）

任务规划已拆分为预检、确认和取消三个阶段。预检将源文件扫描、空间检查与冲突命名写入独立临时 SQLite 文件，不创建正式任务；确认阶段不再扫描本地文件或查询 NAS 目录，而是将预先规划的条目交给仓储单事务持久化。取消、预检异常以及确认结束都会关闭并清理临时会话。

预检取消使用线程安全事件，在源路径之间、每个扫描条目前、进度回调后、空间查询前后以及冲突规划期间检查。进度快照提供已发现条目数、文件数、总字节数和当前路径。冲突数量在预检结果中返回，可供后续确认界面展示。

自动化结果：

- `python -m pytest tests/unit/planning/test_task_planner.py -q`：24 passed。
- `QT_QPA_PLATFORM=offscreen python -m pytest -q --ignore=tests/release`：927 passed，12 skipped。
- `python -m ruff check src tests`：通过。
- `python -m mypy src`：通过。
- 相关文件 `git diff --check`：通过。

本阶段没有改变正式数据库 schema、传输执行协议或任务执行状态机。预检临时库不承担崩溃恢复；应用在用户确认前退出时，临时目录由运行时清理，且正式任务库不会出现半成品任务。

## 预检确认界面验证（2026-09-10）

生产任务创建路径现在使用可取消的后台预检。扫描期间显示窗口模态进度，包含当前条目、已发现条目数、文件数和累计大小；点击取消会触发规划器的线程安全取消令牌。预检结束后显示真实来源数、文件数、扫描条目、总大小、安全余量、所需空间、NAS 可用空间、冲突数以及复制／移动语义。

确认前正式任务库没有新任务。点击“确认加入队列”后，控制器在后台确认临时会话，再沿用既有的 `PREFLIGHT → QUEUED` 状态迁移和串行队列入口；取消或关闭摘要会调用 `cancel_preflight()`，销毁临时数据且不入队。仅实现 `plan()` 的离线演示替身继续走兼容路径。

自动化结果：

- `QT_QPA_PLATFORM=offscreen python -m pytest tests/unit/ui/test_task_creation_controller.py -q`：9 passed。
- `QT_QPA_PLATFORM=offscreen python -m pytest tests/unit/ui -q`：107 passed。
- `QT_QPA_PLATFORM=offscreen python -m pytest -q --ignore=tests/release`：930 passed，12 skipped。
- 全量 Ruff、mypy 与 `git diff --check`：通过。

本阶段没有改变传输执行状态机、正式数据库 schema 或串行队列语义。废纸篓源文件处置已在后续独立子任务中完成。

## 完整冲突策略验证（2026-09-11）

冲突策略已扩展为保留两者、覆盖、跳过、仅源文件较新时覆盖、每次创建时询问。schema 5 使用独立 `conflict_strategy` 字段保存新语义，同时保留旧 `conflict_policy=auto_rename` 兼容列；schema 3 和 schema 4 只有在结构及指纹完全匹配时才会原子迁移，旧任务默认映射为保留两者。

预检阶段按策略生成确定结果，跳过条目不计入待传输文件数和字节数。提交阶段重新检查预检后的目标竞态：覆盖调用 SMB 同共享原子替换；跳过清理临时对象并保持源文件；仅较新覆盖重新比较源和目标修改时间；未解析的询问策略拒绝猜测。工作台的“每次创建时询问”会在启动预检前解析为具体策略，避免从后台线程弹出逐文件窗口，也避免未决任务进入队列。

自动化结果：

- 冲突规划、状态迁移、提交竞态、schema 与 UI 相关回归：465 passed。
- SMB 网关单元测试：21 passed。
- `QT_QPA_PLATFORM=offscreen python -m pytest -q --ignore=tests/release`：954 passed，12 skipped。
- 全量 Ruff、mypy 与 `git diff --check`：通过。

本阶段没有执行真实 Synology 覆盖测试；真实目标竞态门禁仍需要显式隔离测试目录。原子替换返回结果不确定时统一进入 `rename_outcome_unknown` 恢复路径，不会把未知结果报告为成功。

## macOS 废纸篓源文件处置验证（2026-09-11）

移动任务不再永久删除源文件。只有完整回读校验、NAS 目标原子提交、源文件指纹复核和删除授权全部通过后，本地文件网关才调用 PySide6 `QFile.moveToTrash` 将源文件或空目录交给 macOS 系统废纸篓。该实现复用现有 PySide6 依赖，不调用 Finder 自动化，也不新增第三方包。

入篓前继续拒绝符号链接、变化后的文件和非空目录。系统废纸篓拒绝操作或抛出错误时，本地源文件保持不变，条目转为 `SOURCE_RETAINED`，已提交 NAS 目标保留并返回警告。入篓成功但状态提交前进程退出时，恢复流程会重新核对目标完整性，并把已不存在的源文件幂等收敛到 `DONE`。

界面的移动选项、二次确认、预检摘要、执行阶段、安全提示和文件明细已经统一为“移入废纸篓”语义。复制任务仍明确保留本地源文件。

自动化结果：

- 本地文件、删除服务、传输引擎、崩溃恢复与 UI 定向回归：95 passed。
- `QT_QPA_PLATFORM=offscreen python -m pytest -q --ignore=tests/release`：957 passed，12 skipped。
- 全量 Ruff、mypy 与 `git diff --check`：通过。

本阶段没有向用户真实废纸篓写入测试文件；系统调用边界通过可注入替身验证。真实 Finder 恢复操作属于人工发布验收项。

## 新应用包与发布门禁复核（2026-09-11）

使用项目 Python 3.12 虚拟环境重新执行 PySide6／Nuitka 构建，生成新的 `dist/NasMove.app`。bundle ID 为 `com.nasmove.app`，全量回归 `959 passed，12 skipped`；`codesign --verify --deep --strict` 对 Nuitka 生成的 ad-hoc 包通过，离屏启动五秒后进程仍正常运行，主可执行文件为 `x86_64`。

登录钥匙串原有同名证书缺少私钥，因此新建了一个仅用于本机开发验证的持久自签名身份。`security find-identity -v -p codesigning` 现返回 1 个有效身份。构建时又发现同名旧证书会使按名称签名产生歧义，构建脚本已改为从有效身份列表解析唯一 SHA-1 指纹并按指纹调用 `codesign`。

首次使用新私钥签名时，macOS 要求在原生钥匙串授权窗口中确认访问；当前自动化通道不能代输登录口令或代点“始终允许”。未采用 `-A` 放宽私钥 ACL，也未回退到脚本内 ad-hoc 签名。因此现有新包仍是 Nuitka ad-hoc 签名，`spctl --assess` 返回 rejected，不是正式发布候选。

仍阻塞的外部门禁包括：

- 在钥匙串窗口对 `NasMove Local Signing` 私钥授予 `codesign` 持久访问后，重新完成最终签名。
- Apple Silicon 原生或经验证的 universal 构建。
- Developer ID Application 签名、公证和 stapling。
- 隔离 Synology 环境中的桌面闭环、50 GiB／10 次断连和 100,000 小文件门禁。
