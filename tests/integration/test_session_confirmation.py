import threading
from unittest.mock import Mock

from src.config import AppConfig
from src.crawl.facebook_adapter import FacebookAdapter
from src.crawl.session import SessionManager


def test_facebook_can_finish_loading_while_user_considers_start(browser, tmp_path, monkeypatch):
    """Sync Playwright must dispatch routes even while console input is pending."""
    from src.crawl import session

    config = AppConfig()
    config.chrome.user_data_dir = tmp_path / 'profile'
    config.chrome.user_data_dir.mkdir()
    (config.chrome.user_data_dir / 'Local State').write_text('{}')
    monkeypatch.setattr(session, 'resolve_chrome', lambda _: tmp_path / 'chrome.exe')
    context = browser.new_context()
    page = context.new_page()
    ready = threading.Event()
    page.on('console', lambda message: ready.set() if message.text == 'page-ready' else None)

    def route(request):
        if request.request.resource_type == 'document':
            request.fulfill(content_type='text/html', body='''<html><body><script>
                setTimeout(() => {
                    const script = document.createElement('script');
                    script.src = '/ready.js'; document.body.append(script);
                }, 50);
            </script></body></html>''')
        else:
            request.fulfill(content_type='application/javascript', body='''
                document.body.innerHTML = '<button aria-label="Your profile">Account</button>' +
                    '<h1>UIT Confessions</h1><div role="feed"></div>';
                console.log('page-ready');
            ''')

    def confirm(message):
        assert ready.wait(2), 'Facebook requests stalled while waiting for console input'
        return 'y'

    manager = SessionManager(config, Mock(launch_persistent_context=Mock(return_value=context)),
                             FacebookAdapter(config), input_fn=confirm, is_interactive=lambda: True)
    manager._route = route  # Fulfill all traffic locally while exercising sync route dispatch.
    try:
        assert manager.ensure_authenticated() is context
        assert ready.is_set()
    finally:
        context.close()
