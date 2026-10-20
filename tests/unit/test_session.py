from unittest.mock import Mock

import pytest
from playwright.sync_api import Error, TimeoutError

from src.config import AppConfig
from src.crawl.session import SessionManager, navigate_with_retry
from src.errors import AppError
from src.models import SessionState as S


def setup(tmp_path, monkeypatch, states, fresh=False, interactive=True):
    config = AppConfig()
    config.chrome.user_data_dir = tmp_path / 'profile'
    config.crawl.action_timeout_seconds = 0.01
    if not fresh:
        config.chrome.user_data_dir.mkdir()
        (config.chrome.user_data_dir / 'Local State').touch()
    exe = tmp_path / 'Chrome.exe'
    exe.touch()
    monkeypatch.setattr('src.crawl.session.resolve_chrome', lambda value: exe)
    events = []
    launcher = Mock(side_effect=lambda *a: events.append('manual'))
    monkeypatch.setattr('src.crawl.session.launch_manual_chrome', launcher)
    contexts = []
    def launch(**kwargs):
        context = Mock()
        context.pages = [Mock()]
        context.close.side_effect = lambda: events.append('closed')
        contexts.append(context)
        return context
    chromium = Mock()
    chromium.launch_persistent_context.side_effect = launch
    adapter = Mock()
    adapter.check_session.side_effect = states
    prompt = Mock(side_effect=lambda message: events.append('enter'))
    manager = SessionManager(config, chromium, adapter, input_fn=prompt,
                             is_interactive=lambda: interactive, sleep_fn=lambda _: None)
    return manager, chromium, contexts, launcher, events


def test_context_closed_before_manual_launch_and_rechecked(tmp_path, monkeypatch):
    manager, chromium, contexts, launcher, events = setup(tmp_path, monkeypatch, [S.LOGIN_REQUIRED, S.AUTHENTICATED])
    assert manager.ensure_authenticated() is contexts[-1]
    assert events == ['closed', 'manual', 'enter']
    assert manager.adapter.check_session.call_count == 2
    assert chromium.launch_persistent_context.call_args.kwargs['user_data_dir'] == str(manager.config.chrome.user_data_dir)
    contexts[-1].close.assert_not_called()


def test_fresh_profile_goes_straight_to_manual_login(tmp_path, monkeypatch):
    manager, _, _, launcher, events = setup(tmp_path, monkeypatch, [S.AUTHENTICATED], fresh=True)
    manager.ensure_authenticated()
    assert events == ['manual', 'enter']
    assert launcher.call_count == 1


@pytest.mark.parametrize('state', [S.LOGIN_REQUIRED, S.CHECKPOINT])
def test_enter_is_not_authentication_and_attempts_bounded(tmp_path, monkeypatch, state):
    manager, _, contexts, launcher, _ = setup(tmp_path, monkeypatch, [state] * 5, fresh=True)
    with pytest.raises(AppError) as error:
        manager.ensure_authenticated()
    assert error.value.code == 'AUTH_REQUIRED'
    assert launcher.call_count == 3
    assert len(contexts) == 3
    assert all(ctx.close.call_count == 1 for ctx in contexts)


def test_non_tty_does_not_launch_or_prompt(tmp_path, monkeypatch):
    manager, chromium, _, launcher, _ = setup(tmp_path, monkeypatch, [], fresh=True, interactive=False)
    with pytest.raises(AppError) as error:
        manager.ensure_authenticated()
    assert error.value.code == 'AUTH_REQUIRED'
    launcher.assert_not_called()
    chromium.launch_persistent_context.assert_not_called()


def test_profile_busy_retry_is_bounded(tmp_path, monkeypatch):
    manager, chromium, _, launcher, events = setup(tmp_path, monkeypatch, [])
    chromium.launch_persistent_context.side_effect = Error('ProcessSingleton: profile is already in use')
    with pytest.raises(AppError) as error:
        manager.ensure_authenticated()
    assert error.value.code == 'PROFILE_IN_USE'
    assert events == ['enter'] * 3
    launcher.assert_not_called()


def test_unknown_page_is_not_success(tmp_path, monkeypatch):
    manager, _, contexts, launcher, _ = setup(tmp_path, monkeypatch, [S.UNKNOWN] * 10)
    with pytest.raises(AppError) as error:
        manager.ensure_authenticated()
    assert error.value.code == 'PAGE_UNAVAILABLE'
    contexts[0].close.assert_called_once()
    launcher.assert_not_called()


def test_context_closed_on_interrupt(tmp_path, monkeypatch):
    manager, chromium, contexts, _, _ = setup(tmp_path, monkeypatch, [KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        manager.ensure_authenticated()
    contexts[0].close.assert_called_once()


def test_navigation_retries_only_timeouts_with_1_then_2_seconds():
    page = Mock()
    page.goto.side_effect = [TimeoutError('slow'), TimeoutError('slow'), None]
    sleep = Mock()
    navigate_with_retry(page, 'https://www.facebook.com/UITconfess', 30000, sleep)
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]
    assert page.goto.call_count == 3
    assert page.goto.call_args.kwargs['wait_until'] == 'domcontentloaded'


def test_only_media_resource_types_aborted(tmp_path, monkeypatch):
    manager, chromium, contexts, _, _ = setup(tmp_path, monkeypatch, [S.AUTHENTICATED])
    manager.ensure_authenticated()
    route_callback = contexts[0].route.call_args.args[1]
    for resource in ['image', 'media', 'font', 'script', 'xhr', 'fetch', 'document', 'stylesheet']:
        route = Mock()
        route.request.resource_type = resource
        route_callback(route)
        assert route.abort.called == (resource in ['image', 'media', 'font'])
        assert route.continue_.called == (resource not in ['image', 'media', 'font'])
    assert chromium.launch_persistent_context.call_args.kwargs['service_workers'] == 'block'
