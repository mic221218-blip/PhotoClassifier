"""创建 / 更新桌面快捷方式（Windows）。

为什么图标要复制到 ``%LOCALAPPDATA%``：

本项目目录在当前开发机上带有 Windows 的 **Low 完整性标签**（沙箱创建的文件），
资源管理器（Explorer）不会渲染来自该目录的 .ico，快捷方式会显示成一张白纸。
把图标复制到 ``%LOCALAPPDATA%\\PhotoClassifier`` 后重新写入快捷方式即可正常显示。

用法（项目根目录）::

    python tools/install_shortcut.py

重复运行是安全的：会覆盖图标与快捷方式，并刷新图标缓存。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ICON_SOURCE = ROOT / "assets" / "photoclassifier.ico"
SHORTCUT_NAME = "PhotoClassifier.lnk"


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _copy_via_powershell(source: Path, target: Path) -> None:
    script = (
        f"Copy-Item -LiteralPath {_ps_quote(str(source))} "
        f"-Destination {_ps_quote(str(target))} -Force"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
    )


def _ensure_medium_integrity(executables: list[Path]) -> None:
    """把带 Low 完整性标签的 Python 解释器改成 Medium。

    Windows 会按可执行文件的完整性级别启动进程：如果项目目录（连同 .venv）
    被沙箱打上了 Low 标签，从快捷方式启动的 GUI 也是 Low，系统会拒绝
    “让资源管理器定位文件”（0x80070005）。把解释器改成 Medium 后按钮才能真正生效。
    """
    existing = [str(path) for path in executables if path.exists()]
    if not existing:
        return
    quoted = ", ".join(_ps_quote(item) for item in existing)
    script = f"""
foreach ($p in @({quoted})) {{
  if (-not (Test-Path -LiteralPath $p)) {{ continue }}
  $acl = (icacls $p) -join "`n"
  if ($acl -match 'Low Mandatory Level|S-1-16-4096') {{
    icacls $p /setintegritylevel Medium | Out-Null
    Write-Output "RELABELED $p"
  }} elseif ($acl -match 'Medium Mandatory Level|S-1-16-8192') {{
    Write-Output "MEDIUM $p"
  }} else {{
    Write-Output "UNLABELED $p"
  }}
}}
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("RELABELED"):
            print("完整性标签已改为 Medium：", line.removeprefix("RELABELED").strip())
        elif line.startswith("UNLABELED"):
            print("提示：解释器没有完整性标签（通常也没问题）：", line.removeprefix("UNLABELED").strip())
    if result.returncode != 0:
        print("警告：检查/修改完整性格失败：", result.stderr.strip())


def main() -> int:
    if not ICON_SOURCE.exists():
        raise SystemExit(f"图标不存在：{ICON_SOURCE}")

    local_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    icon_dir = local_appdata / "PhotoClassifier"
    icon_dir.mkdir(parents=True, exist_ok=True)

    # 用内容哈希命名：图标内容变了才换新文件（也是刷新 Explorer 图标缓存的可靠办法）；
    # 同一个文件可能正被 Explorer 占用，已存在时不要覆盖。
    digest = hashlib.sha1(ICON_SOURCE.read_bytes()).hexdigest()[:8]
    icon_path = icon_dir / f"app-{digest}.ico"
    if not icon_path.exists():
        # 用 PowerShell 复制：本仓库里的文件带 Low 完整性标签，
        # 由低完整性进程（本项目 venv 里的 python）直接写到 Medium 的
        # AppData 会被 Windows 的 no-write-up 策略拒绝。
        _copy_via_powershell(ICON_SOURCE, icon_path)

    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        pythonw = Path("pythonw.exe")
    launcher = ROOT / "run_gui.pyw"

    # “打开文件位置”需要 Medium 权限；Low 标签的 venv 会被系统拒绝
    _ensure_medium_integrity(
        [ROOT / ".venv" / "Scripts" / "pythonw.exe", ROOT / ".venv" / "Scripts" / "python.exe"]
    )

    script = f"""
$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$path = Join-Path $desktop {_ps_quote(SHORTCUT_NAME)}
$lnk = $shell.CreateShortcut($path)
$lnk.TargetPath = {_ps_quote(str(pythonw))}
$lnk.Arguments = {_ps_quote('"' + str(launcher) + '"')}
$lnk.WorkingDirectory = {_ps_quote(str(ROOT))}
$lnk.IconLocation = {_ps_quote(str(icon_path) + ',0')}
$lnk.Description = 'PhotoClassifier - 摄影照片整理与筛选（V2.5）'
$lnk.WindowStyle = 1
$lnk.Save()
Write-Output $path
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
    )
    print("快捷方式：", result.stdout.strip())

    ie4uinit = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "ie4uinit.exe"
    if ie4uinit.exists():
        subprocess.run([str(ie4uinit), "-show"], check=False)
        print("图标缓存已刷新")
    print("图标：", icon_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
