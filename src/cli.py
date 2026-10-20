import argparse
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from src.config import load_config
from src.crawl.service import crawl
from src.errors import AppError
from src.logging_setup import close_logging, configure_logging
from src.models import CrawlStats


class ConsoleProgress:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.last = float('-inf')

    def __call__(self, stats: CrawlStats, *, force=False):
        now = self.clock()
        if force or now - self.last >= 1:
            fields = ' '.join(f'{key}={value}' for key, value in asdict(stats).items())
            print(f'{fields} elapsed={now - self.started:.1f}s', file=sys.stderr)
            self.last = now


def main(argv: Sequence[str] | None = None) -> int:
    # Windows redirected streams may otherwise use cp1252 and reject Vietnamese.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Crawl bài viết từ Facebook page bằng Chrome profile riêng.')
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parent.parent / 'config.yaml',
                        help='File YAML (đặt trước subcommand); mặc định config.yaml tại project root.')
    commands = parser.add_subparsers(dest='command', required=True)
    crawler = commands.add_parser('crawl', help='Thu thập bài thô; chạy lại tự bỏ nội dung đã lưu.')
    crawler.add_argument('--max-posts', type=int, help='Số bài mới được lưu tối đa trong lần chạy.')
    crawler.add_argument('--max-scrolls', type=int, help='Số lần cuộn tối đa trong lần chạy.')
    args = parser.parse_args(argv)
    run_id = str(uuid.uuid4())
    try:
        overrides = {key: value for key in ('max_posts', 'max_scrolls')
                     if (value := getattr(args, key)) is not None}
        config = load_config(args.config, overrides)
        configure_logging(config, run_id, args.command)
        progress = ConsoleProgress()
        result = crawl(config, progress=progress, run_id=run_id)
        progress(result.stats, force=True)
        print(f'Dừng: {result.stop_reason}; trạng thái={result.status}; report={config.paths.reports_dir / (run_id + ".json")}',
              file=sys.stderr)
        if result.stop_reason.startswith('session_'):
            print('Phiên/page không còn sẵn sàng. Chạy lại để xác minh hoặc đăng nhập.', file=sys.stderr)
        print('candidate_segments là số marker tạm đếm, chưa phải số confession đã tiền xử lý.', file=sys.stderr)
        return result.exit_code
    except KeyboardInterrupt:
        print('Đã nhận Ctrl+C. Các bản ghi đã commit được giữ lại; chạy lại để tiếp tục. (exit 130)', file=sys.stderr)
        return 130
    except AppError as exc:
        print(f'{exc.code}: {exc}', file=sys.stderr)
        return exc.exit_code
    except Exception as exc:
        # Browser exception strings can contain DOM snippets or session details.
        print(f'RUNTIME_ERROR ({type(exc).__name__}): lệnh đã dừng; kiểm tra cấu hình, Chrome và file report.', file=sys.stderr)
        return 1
    finally:
        close_logging()
