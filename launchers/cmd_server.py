"""cmd_server launcher — 编译为 cmd_server.exe 后双击运行"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from cmd_server import main
if __name__ == "__main__":
    main()
