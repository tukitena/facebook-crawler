import os
import subprocess
import sys


def test_help_works_with_legacy_windows_pipe_encoding():
    env = {**os.environ, 'PYTHONIOENCODING': 'cp1252'}
    result = subprocess.run([sys.executable, 'main.py', '--help'], env=env,
                            capture_output=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'bài viết' in result.stdout
