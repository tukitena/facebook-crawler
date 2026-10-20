import os
import shutil
import subprocess
from pathlib import Path

from src.errors import AppError


def resolve_chrome(executable_path: Path | None) -> Path:
    if executable_path is not None:
        if not executable_path.is_file():
            raise AppError('CHROME_NOT_FOUND', f'Chrome không tồn tại: {executable_path}', 2)
        return executable_path.resolve()
    candidates = []
    for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root) / 'Google/Chrome/Application/chrome.exe')
    for name in ('chrome', 'google-chrome', 'google-chrome-stable'):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise AppError('CHROME_NOT_FOUND', 'Cài Google Chrome hoặc đặt chrome.executable_path trong config.', 2)


def launch_manual_chrome(executable: Path, user_data_dir: Path, url: str) -> subprocess.Popen:
    user_data_dir.mkdir(parents=True, exist_ok=True)
    try:
        return subprocess.Popen([str(executable), f'--user-data-dir={user_data_dir.resolve()}',
                                 '--no-first-run', '--no-default-browser-check', '--new-window', url],
                                shell=False)
    except OSError as exc:
        raise AppError('BROWSER_ERROR', 'Không mở được Chrome để đăng nhập.') from exc
