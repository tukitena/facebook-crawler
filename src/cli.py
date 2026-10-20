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
from src.preprocess.models import PreprocessStats
from src.preprocess.service import preprocess
from src.preprocess.storage import validate_preprocess_paths
from src.reset import reset_dataset, validate_reset_paths


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


class PreprocessConsoleProgress:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.last = float('-inf')

    def __call__(self, stats: PreprocessStats, *, force: bool = False) -> None:
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
    parser = argparse.ArgumentParser(description='Crawl bài viết và quản lý dữ liệu Facebook page bằng Chrome profile riêng.')
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parent.parent / 'config.yaml',
                        help='File YAML (đặt trước subcommand); mặc định config.yaml tại project root.')
    commands = parser.add_subparsers(dest='command', required=True)
    crawler = commands.add_parser('crawl', help='Thu thập bài thô; chạy lại tự bỏ nội dung đã lưu.')
    crawler.add_argument('--max-posts', type=int, help='Số bài mới được lưu tối đa trong lần chạy.')
    crawler.add_argument('--max-scrolls', type=int, help='Số lần cuộn tối đa trong lần chạy.')
    commands.add_parser('reset', help='Xóa raw, metadata và output dẫn xuất; giữ profile, config, log và report.',
                        description='Xóa lịch sử/output của dataset theo config để crawl lại. '
                                    'Giữ profile đăng nhập, config, log, report và file thủ công; không tự chạy crawl.')
    commands.add_parser('preprocess', help='Tách, làm sạch và dedup confession từ raw; chạy offline.',
                        description='Rebuild confessions, sources và excluded từ raw; giữ nguyên raw và kiểm tra nhãn cũ.')
    args = parser.parse_args(argv)
    run_id = str(uuid.uuid4())
    try:
        overrides = {key: value for key in ('max_posts', 'max_scrolls')
                     if (value := getattr(args, key, None)) is not None}
        config = load_config(args.config, overrides)
        if args.command == 'reset':
            validate_reset_paths(config, args.config)
        elif args.command == 'preprocess':
            validate_preprocess_paths(config, args.config)
        configure_logging(config, run_id, args.command)
        if args.command == 'reset':
            reset_dataset(config, config_path=args.config)
            print('Đã reset dữ liệu. Chạy python main.py crawl để thu thập lại '
                  '(dùng cùng --config nếu có).', file=sys.stderr)
            return 0
        if args.command == 'preprocess':
            progress = PreprocessConsoleProgress()
            result = preprocess(config, progress=progress, run_id=run_id)
            progress(result.stats, force=True)
            print(f'preprocess: {result.status}; generation={result.generation_id}; '
                  f'report={config.paths.reports_dir / (run_id + ".json")}', file=sys.stderr)
            return result.exit_code
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
        if args.command == 'preprocess':
            print('Đã ngắt preprocess; raw giữ nguyên. Xem report để biết generation đã publish hay chưa; '
                  'chạy lại preprocess để rebuild. (exit 130)', file=sys.stderr)
            return 130
        if args.command == 'reset':
            print('Đã ngắt reset; dữ liệu có thể mới xóa một phần. '
                  'Chạy lại reset để hoàn tất trước khi crawl. (exit 130)', file=sys.stderr)
            return 130
        print('Đã nhận Ctrl+C. Các bản ghi đã commit được giữ lại; chạy lại để tiếp tục. (exit 130)', file=sys.stderr)
        return 130
    except AppError as exc:
        print(f'{exc.code}: {exc}', file=sys.stderr)
        return exc.exit_code
    except Exception as exc:
        # Browser exception strings can contain DOM snippets or session details.
        if args.command == 'preprocess':
            print(f'RUNTIME_ERROR ({type(exc).__name__}): preprocess đã dừng; kiểm tra cấu hình và report.', file=sys.stderr)
        else:
            print(f'RUNTIME_ERROR ({type(exc).__name__}): lệnh đã dừng; kiểm tra cấu hình, Chrome và file report.', file=sys.stderr)
        return 1
    finally:
        close_logging()
