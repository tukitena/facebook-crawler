import subprocess
import sys

from src.storage import resource_lock


def test_second_process_cannot_acquire_lock(tmp_path):
    path = tmp_path / 'lock'
    code = '''
import sys
from pathlib import Path
from src.errors import AppError
from src.storage import resource_lock
try:
    with resource_lock(Path(sys.argv[1])):
        pass
except AppError as error:
    sys.exit(error.exit_code)
'''
    with resource_lock(path):
        result = subprocess.run([sys.executable, '-c', code, str(path)], timeout=10, capture_output=True)
        assert result.returncode == 2, result.stderr
    result = subprocess.run([sys.executable, '-c', code, str(path)], timeout=10, capture_output=True)
    assert result.returncode == 0, result.stderr
