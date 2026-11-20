"""Alert helper tests: once-per-condition semantics with markers."""

from __future__ import annotations

from bot import main as bot_main


class _FakeTelegram:
    def __init__(self, fail: bool = False):
        self.sent: list[str] = []
        self._fail = fail

    def send_alert(self, text: str) -> bool:
        if self._fail:
            return False
        self.sent.append(text)
        return True


def test_alert_once_sends_single_time(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_main, "_alert_marker_dir", lambda: tmp_path)
    tg = _FakeTelegram()
    assert bot_main._alert_once(tg, "session_linkedin", "Título", "Detalhe") is True
    assert bot_main._alert_once(tg, "session_linkedin", "Título", "Detalhe") is False
    assert len(tg.sent) == 1
    assert (tmp_path / "session_linkedin").exists()


def test_alert_resends_after_clear_and_on_send_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_main, "_alert_marker_dir", lambda: tmp_path)
    failing = _FakeTelegram(fail=True)
    assert bot_main._alert_once(failing, "analyzer_down", "T", "D") is False
    assert not (tmp_path / "analyzer_down").exists()

    tg = _FakeTelegram()
    assert bot_main._alert_once(tg, "analyzer_down", "T", "D") is True
    bot_main._clear_alert("analyzer_down")
    assert bot_main._alert_once(tg, "analyzer_down", "T", "D") is True
    assert len(tg.sent) == 2


def test_session_lost_flag_reported_by_linkedin_scraper():
    from bot.scrapers.linkedin import LinkedInScraper

    scraper = LinkedInScraper(browser=None, search_terms=["dev"])
    assert scraper.session_lost is False
