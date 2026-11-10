"""Double-click to open the control board with the project's Python environment."""
import ctypes
import subprocess
import sys
from pathlib import Path


def main():
    project = Path(__file__).resolve().parent
    python = project / ".venv" / "Scripts" / "pythonw.exe"
    try:
        subprocess.Popen([python, project / "control_board.py", *sys.argv[1:]], cwd=project)
    except OSError as error:
        message = ("The Pi-lingual Python environment is missing. Follow the setup in README.md."
                   if not python.is_file() else str(error))
        ctypes.windll.user32.MessageBoxW(None, message, "Pi-lingual Control Board", 0x10)


if __name__ == "__main__":
    main()

