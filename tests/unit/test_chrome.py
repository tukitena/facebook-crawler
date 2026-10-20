from pathlib import Path
from unittest.mock import Mock

import pytest

from src.crawl.chrome import launch_manual_chrome, resolve_chrome
from src.errors import AppError


def test_explicit_path_and_argv_with_spaces(tmp_path, monkeypatch):
    executable = tmp_path / 'Chrome Folder' / 'chrome.exe'
    executable.parent.mkdir()
    executable.touch()
    popen = Mock()
    monkeypatch.setattr('src.crawl.chrome.subprocess.Popen', popen)
    assert resolve_chrome(executable) == executable.resolve()
    profile = tmp_path / 'User Data'
    launch_manual_chrome(executable, profile, 'https://www.facebook.com/UITconfess')
    argv = popen.call_args.args[0]
    assert argv[0] == str(executable)
    assert f'--user-data-dir={profile.resolve()}' in argv
    assert '--new-window' in argv
    assert not any('remote-debugging' in arg for arg in argv)
    assert popen.call_args.kwargs['shell'] is False


def test_bad_explicit_executable_is_not_silently_replaced(tmp_path):
    with pytest.raises(AppError) as error:
        resolve_chrome(tmp_path / 'missing.exe')
    assert error.value.exit_code == 2
