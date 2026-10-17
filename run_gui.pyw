"""PhotoClassifier GUI 启动器（无控制台窗口）。

桌面快捷方式的目标即为本文件（由 pythonw.exe 运行）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.gui import main  # noqa: E402

sys.exit(main())
