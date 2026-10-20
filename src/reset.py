"""Remove only managed dataset outputs; preserve login and operator-owned files."""

import logging
from pathlib import Path

from src.config import AppConfig
from src.errors import AppError
from src.storage import resource_lock

logger = logging.getLogger('crawler')
PROCESSED_FILES = ('confessions.csv', 'sources.csv', 'excluded.csv', 'preprocess.manifest.json')
LABELED_FILES = ('labeled.csv', 'classification_failures.csv', 'labeled.meta.json')


def validate_reset_paths(config: AppConfig, config_path: Path) -> tuple[list[Path], Path]:
    """Validate the complete deletion set before touching any dataset file."""
    try:
        paths = config.paths
        data_dir = paths.data_dir.resolve()
        lock_path = data_dir / '.dataset.lock'
        targets = [paths.raw_csv, paths.raw_csv.with_suffix('.meta.json'),
                   *(paths.processed_dir / name for name in PROCESSED_FILES),
                   *(paths.labeled_dir / name for name in LABELED_FILES)]
        profile = config.chrome.user_data_dir
        inputs = [config_path, config.classification.prompt_path]
        if config.preprocess.overrides_csv is not None:
            inputs.append(config.preprocess.overrides_csv)
        inputs = [path.resolve() for path in inputs]
        log_files = [paths.log_dir / 'crawl.jsonl',
                     *(paths.log_dir / f'crawl.jsonl.{index}'
                       for index in range(1, config.logging.backup_count + 1))]
        if any(path.resolve() in inputs for path in log_files):
            raise ValueError('File log hoặc bản xoay vòng trùng config, prompt hoặc override thủ công')
        protected = [*inputs, paths.reports_dir, paths.log_dir, profile,
                     profile.with_name(profile.name + '.lock'), lock_path]
        protected = [path.resolve() for path in protected]
        if lock_path.resolve() != lock_path:
            raise ValueError(f'Khóa dataset bị chuyển hướng: {lock_path}')
        for target in targets:
            resolved = target.resolve()
            if data_dir not in resolved.parents or target != resolved:
                raise ValueError(f'Đích reset phải nằm trong data_dir và không chuyển hướng: {target}')
            if any(resolved == path or resolved in path.parents or path in resolved.parents
                   for path in protected):
                raise ValueError(f'Đích reset trùng đường dẫn cần giữ: {target}')
        return targets, lock_path
    except (OSError, RuntimeError, ValueError) as exc:
        raise AppError('CONFIG_ERROR', f'Đường dẫn reset không hợp lệ: {exc}', 2) from exc


def reset_dataset(config: AppConfig, *, config_path: Path) -> None:
    targets, lock_path = validate_reset_paths(config, config_path)
    with resource_lock(lock_path):
        # Recheck resolved paths after acquiring ownership, before any deletion.
        targets, _ = validate_reset_paths(config, config_path)
        logger.info('reset_started', extra={'event': 'reset_started'})
        for target in targets:
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                raise AppError('STORAGE_ERROR', f'Chưa reset được {target}; đóng ứng dụng đang giữ file, '
                               'kiểm tra quyền ghi rồi chạy lại reset để hoàn tất.') from exc
        for target in targets:
            try:
                target.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise AppError('STORAGE_ERROR', f'Không kiểm tra được {target}; chạy lại reset để hoàn tất.') from exc
            raise AppError('STORAGE_ERROR', f'File vẫn tồn tại: {target}; chạy lại reset để hoàn tất.')
        logger.info('reset_finished', extra={'event': 'reset_finished', 'status': 'complete'})
