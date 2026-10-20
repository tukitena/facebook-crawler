from threading import Event, current_thread, get_ident
from unittest.mock import Mock

import pytest
from playwright.sync_api import Error, TimeoutError

from src.config import AppConfig
from src.crawl.session import START_MESSAGE, SessionManager, navigate_with_retry
from src.errors import AppError, CrawlCancelled
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
    def confirm(message):
        events.append('enter')
        return 'y' if message == START_MESSAGE else ''
    prompt = Mock(side_effect=confirm)
    manager = SessionManager(config, chromium, adapter, input_fn=prompt,
                             is_interactive=lambda: interactive, sleep_fn=lambda _: None)
    return manager, chromium, contexts, launcher, events


def test_context_closed_before_manual_launch_and_rechecked(tmp_path, monkeypatch):
    manager, chromium, contexts, launcher, events = setup(tmp_path, monkeypatch, [S.LOGIN_REQUIRED, S.AUTHENTICATED])
    assert manager.ensure_authenticated() is contexts[-1]
    assert events == ['enter', 'closed', 'manual', 'enter', 'enter']
    assert manager.adapter.check_session.call_count == 2
    assert chromium.launch_persistent_context.call_args.kwargs['user_data_dir'] == str(manager.config.chrome.user_data_dir)
    contexts[-1].close.assert_not_called()


def test_fresh_profile_goes_straight_to_manual_login(tmp_path, monkeypatch):
    manager, _, _, launcher, events = setup(tmp_path, monkeypatch, [S.AUTHENTICATED], fresh=True)
    manager.ensure_authenticated()
    assert events == ['manual', 'enter', 'enter']
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


@pytest.mark.parametrize('answer', ['y', 'Y', ' y '])
def test_user_confirms_after_navigation_before_session_check(tmp_path, monkeypatch, answer):
    manager, _, contexts, _, events = setup(tmp_path, monkeypatch, [])
    ready = False

    def confirm(message):
        nonlocal ready
        contexts[0].pages[0].goto.assert_called_once()
        manager.adapter.check_session.assert_not_called()
        ready = True
        events.append('confirm')
        return answer

    def check_session(page):
        events.append('check')
        return S.AUTHENTICATED if ready else S.UNKNOWN

    manager.input_fn = Mock(side_effect=confirm)
    manager.adapter.check_session.side_effect = check_session

    assert manager.ensure_authenticated() is contexts[0]
    assert events == ['confirm', 'check']
    manager.adapter.check_session.assert_called_once_with(contexts[0].pages[0])


def test_start_confirmation_pumps_browser_events_on_calling_thread(tmp_path, monkeypatch):
    manager, chromium, contexts, _, _ = setup(tmp_path, monkeypatch, [])
    main_thread_id = get_ident()
    browser_serviced = Event()
    input_threads = []
    pump_threads = []
    launch = chromium.launch_persistent_context.side_effect

    def service_events(milliseconds):
        assert 0 < milliseconds <= 250
        manager.adapter.check_session.assert_not_called()
        pump_threads.append(get_ident())
        browser_serviced.set()

    def launch_context(**kwargs):
        context = launch(**kwargs)
        context.pages[0].wait_for_timeout.side_effect = service_events
        return context

    def confirm(message):
        input_threads.append(current_thread())
        browser_serviced.wait(1)
        return 'y'

    chromium.launch_persistent_context.side_effect = launch_context
    manager.input_fn = confirm
    manager.adapter.check_session.side_effect = lambda _: S.AUTHENTICATED if browser_serviced.is_set() else S.UNKNOWN

    assert manager.ensure_authenticated() is contexts[0]
    assert pump_threads and set(pump_threads) == {main_thread_id}
    assert input_threads[0].ident != main_thread_id
    assert input_threads[0].daemon
    manager.adapter.check_session.assert_called_once()


def test_interrupt_during_confirmation_pump_closes_context_without_waiting_for_input(tmp_path, monkeypatch):
    manager, chromium, contexts, _, _ = setup(tmp_path, monkeypatch, [S.AUTHENTICATED])
    release_input = Event()
    launch = chromium.launch_persistent_context.side_effect

    def launch_context(**kwargs):
        context = launch(**kwargs)
        context.pages[0].wait_for_timeout.side_effect = KeyboardInterrupt()
        return context

    def confirm(message):
        release_input.wait(1)
        return 'y'

    chromium.launch_persistent_context.side_effect = launch_context
    manager.input_fn = confirm
    try:
        with pytest.raises(KeyboardInterrupt):
            manager.ensure_authenticated()
        assert not release_input.is_set()
        manager.adapter.check_session.assert_not_called()
        contexts[0].close.assert_called_once()
    finally:
        release_input.set()


def test_start_confirmation_reprompts_until_y(tmp_path, monkeypatch):
    manager, _, contexts, _, _ = setup(tmp_path, monkeypatch, [S.AUTHENTICATED])
    answers = iter(['', '   ', 'no', 'yes', 'x', 'y'])

    def confirm(message):
        manager.adapter.check_session.assert_not_called()
        return next(answers)

    manager.input_fn = Mock(side_effect=confirm)
    assert manager.ensure_authenticated() is contexts[0]
    assert manager.input_fn.call_count == 6
    assert all(call.args == (START_MESSAGE,) for call in manager.input_fn.call_args_list)
    manager.adapter.check_session.assert_called_once()


@pytest.mark.parametrize('answer', ['n', 'N', ' n '])
def test_declining_start_closes_context_without_checking_session(tmp_path, monkeypatch, answer):
    manager, _, contexts, launcher, _ = setup(tmp_path, monkeypatch, [S.AUTHENTICATED])
    manager.input_fn.side_effect = ['', 'invalid', answer]
    with pytest.raises(CrawlCancelled):
        manager.ensure_authenticated()
    assert manager.input_fn.call_count == 3
    manager.adapter.check_session.assert_not_called()
    contexts[0].close.assert_called_once()
    launcher.assert_not_called()


def test_confirmation_checks_unknown_session_once_without_polling(tmp_path, monkeypatch):
    manager, _, contexts, _, _ = setup(tmp_path, monkeypatch, [S.UNKNOWN, S.AUTHENTICATED])
    manager.config.crawl.action_timeout_seconds = 5
    with pytest.raises(AppError) as error:
        manager.ensure_authenticated()
    assert error.value.code == 'PAGE_UNAVAILABLE'
    manager.adapter.check_session.assert_called_once()
    contexts[0].close.assert_called_once()


def test_existing_profile_requires_interactive_start_confirmation(tmp_path, monkeypatch):
    manager, _, contexts, launcher, _ = setup(tmp_path, monkeypatch, [S.AUTHENTICATED], interactive=False)
    with pytest.raises(AppError) as error:
        manager.ensure_authenticated()
    assert error.value.code == 'AUTH_REQUIRED'
    manager.input_fn.assert_not_called()
    manager.adapter.check_session.assert_not_called()
    contexts[0].close.assert_called_once()
    launcher.assert_not_called()


def test_start_confirmation_eof_closes_context(tmp_path, monkeypatch):
    manager, _, contexts, _, _ = setup(tmp_path, monkeypatch, [S.AUTHENTICATED])
    manager.input_fn.side_effect = EOFError()
    with pytest.raises(AppError) as error:
        manager.ensure_authenticated()
    assert error.value.code == 'AUTH_REQUIRED'
    manager.adapter.check_session.assert_not_called()
    contexts[0].close.assert_called_once()


def test_start_confirmation_interrupt_closes_context(tmp_path, monkeypatch):
    manager, _, contexts, _, _ = setup(tmp_path, monkeypatch, [S.AUTHENTICATED])
    manager.input_fn.side_effect = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        manager.ensure_authenticated()
    manager.adapter.check_session.assert_not_called()
    contexts[0].close.assert_called_once()


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
    assert page.goto.call_args.kwargs['wait_until'] == 'commit'


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
