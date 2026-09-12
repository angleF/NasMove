[app]
title = NasMove
project_dir = @PROJECT_ROOT@
input_file = @PROJECT_ROOT@/src/nasmove/ui/desktop_app.py
exec_directory = @PROJECT_ROOT@/dist
project_file =
icon = @PROJECT_ROOT@/deployment/nasmove.icns

[python]
python_path = @PYTHON@
packages = Nuitka==4.1.1
android_packages = buildozer==1.5.0,cython==0.29.33

[qt]
qml_files =
excluded_qml_plugins =
modules = Core,DBus,Gui,Widgets
plugins = networkinformation

[android]
wheel_pyside =
wheel_shiboken =
plugins =

[nuitka]
macos.permissions =
mode = onefile
extra_args = --quiet --noinclude-qt-translations --macos-app-name=NasMove --macos-app-version=0.1.0 --macos-signed-app-name=com.nasmove.app

[buildozer]
mode = debug
recipe_dir =
jars_dir =
local_libs =
arch =
