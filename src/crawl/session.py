import logging
import math
import sys
import time

from playwright.sync_api import BrowserContext, BrowserType, Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeout

from src.config import AppConfig
from src.crawl.chrome import launch_manual_chrome, resolve_chrome
from src.crawl.facebook_adapter import FacebookAdapter
from src.errors import AppError
from src.models import SessionState

logger = logging.getLogger('crawler')
LOGIN_MESSAGE = ('Hãy đăng nhập Facebook trong cửa sổ Chrome vừa mở, hoàn thành xác minh nếu có, '
                 'đóng các cửa sổ Chrome dùng profile này rồi nhấn Enter. ')
BUSY_MESSAGE = 'Profile đang được Chrome sử dụng. Đóng các cửa sổ Chrome dùng profile này rồi nhấn Enter. '


def navigate_with_retry(page: Page, url: str, timeout_ms: int, sleep_fn=time.sleep) -> None:
    for attempt in range(1, 4):
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
            return
        except PlaywrightTimeout as exc:
            if attempt == 3:
                raise AppError('NAVIGATION_TIMEOUT', 'Tải page timeout sau 3 lần; kiểm tra mạng và URL.') from exc
            logger.warning('navigation_retry', extra={'event': 'navigation_retry', 'attempt': attempt})
            sleep_fn(attempt)


class SessionManager:
    def __init__(self, config: AppConfig, chromium: BrowserType, adapter: FacebookAdapter, *,
                 input_fn=None, is_interactive=None, sleep_fn=time.sleep):
        self.config = config
        self.chromium = chromium
        self.adapter = adapter
        self.input_fn = input_fn if input_fn is not None else input
        self.is_interactive = is_interactive if is_interactive is not None else sys.stdin.isatty
        self.sleep_fn = sleep_fn

    def _route(self, route):
        if route.request.resource_type in self.config.crawl.blocked_resource_types:
            route.abort()
        else:
            route.continue_()

    def _check_ready(self, page):
        # Re-check actual session signals while the SPA renders after DOMContentLoaded.
        for attempt in range(max(1, math.ceil(self.config.crawl.action_timeout_seconds / 0.25))):
            state = self.adapter.check_session(page)
            if state != SessionState.UNKNOWN:
                return state
            page.wait_for_timeout(250)
        return SessionState.UNKNOWN

    def ensure_authenticated(self) -> BrowserContext:
        config = self.config
        executable = resolve_chrome(config.chrome.executable_path)
        profile = config.chrome.user_data_dir
        reason = 'login' if not profile.exists() or not any(profile.iterdir()) else None
        prompts = 0
        while True:
            if reason:
                code = 'PROFILE_IN_USE' if reason == 'busy' else 'AUTH_REQUIRED'
                if prompts >= config.chrome.login_attempts:
                    raise AppError(code, 'Không xác minh được phiên sau số lần cho phép; đóng Chrome và chạy lại.', 2)
                if not self.is_interactive():
                    raise AppError(code, 'Cần đăng nhập/đóng Chrome trong terminal tương tác rồi chạy lại.', 2)
                logger.info('auth_required', extra={'event': 'auth_required', 'attempt': prompts + 1})
                if reason != 'busy':
                    launch_manual_chrome(executable, profile, config.facebook.page_url)
                try:
                    self.input_fn(BUSY_MESSAGE if reason == 'busy' else LOGIN_MESSAGE)
                except EOFError as exc:
                    raise AppError('AUTH_REQUIRED', 'Terminal đã đóng; hãy chạy lại trong terminal tương tác.', 2) from exc
                prompts += 1
            try:
                context = self.chromium.launch_persistent_context(
                    user_data_dir=str(profile), executable_path=str(executable),
                    headless=config.chrome.headless, service_workers=config.crawl.service_workers,
                    handle_sigint=False, accept_downloads=False,
                    args=['--no-first-run', '--no-default-browser-check'],
                )
            except PlaywrightError as exc:
                diagnostic = str(exc).casefold()
                if any(word in diagnostic for word in ('processsingleton', 'singletonlock', 'profile is already in use', 'user data directory is already in use')):
                    reason = 'busy'
                    continue
                raise AppError('BROWSER_ERROR', 'Chrome không khởi động được; kiểm tra executable/profile và phiên bản.') from exc
            returned = False
            try:
                context.set_default_timeout(config.crawl.action_timeout_seconds * 1000)
                context.set_default_navigation_timeout(config.crawl.navigation_timeout_seconds * 1000)
                context.route('**/*', self._route)
                page = context.pages[0] if context.pages else context.new_page()
                for extra_page in context.pages[1:]:
                    extra_page.close()
                navigate_with_retry(page, config.facebook.page_url,
                                    int(config.crawl.navigation_timeout_seconds * 1000), self.sleep_fn)
                state = self._check_ready(page)
                if state == SessionState.AUTHENTICATED:
                    logger.info('auth_verified', extra={'event': 'auth_verified'})
                    returned = True
                    return context
                if state == SessionState.UNKNOWN:
                    raise AppError('PAGE_UNAVAILABLE', 'Không xác minh được page/phiên; kiểm tra URL, quyền truy cập hoặc cập nhật adapter.', 2)
                reason = 'login'
            finally:
                if not returned:
                    active_error = sys.exc_info()[0] is not None
                    try:
                        context.close()
                    except PlaywrightError:
                        if not active_error:
                            raise AppError('BROWSER_ERROR', 'Không đóng được Chrome; đóng cửa sổ profile trước khi chạy lại.')
