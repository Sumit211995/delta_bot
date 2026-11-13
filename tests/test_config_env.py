import config


def test_comment_only_env_value_is_empty(monkeypatch):
    monkeypatch.setattr(config, "_load_env", lambda: None)
    monkeypatch.setenv("ML_MODEL_PATH", "# e.g. models/filter.joblib (optional)")
    monkeypatch.setenv("LIVE_CONFIRM", "   # must be exactly ...")
    monkeypatch.setenv("SYMBOLS", "BTCUSD,ETHUSD # two coins")
    c = config.load_config()
    assert c.ml_model_path == "" and c.live_confirm == ""
    assert c.symbols == ("BTCUSD", "ETHUSD")


def test_times_for_people_are_ist():
    import bot
    t = 1794544200                                              # 2026-11-13 04:30 UTC
    assert bot.fmt_ist(t) == "13 Nov 2026 10:00 IST"
    assert bot.fmt_t(t) == "2026-11-13 04:30 UTC"              # journal format unchanged


def test_log_lines_are_ist(tmp_path, monkeypatch, capsys):
    import dataclasses
    import logging
    import time
    import bot
    import config
    monkeypatch.setattr(config, "_load_env", lambda: None)
    cfg = dataclasses.replace(config.load_config(), log_dir=str(tmp_path))
    logging.getLogger().handlers.clear()
    bot.setup_logging(cfg)
    logging.info("hello")
    line = (tmp_path / "bot.log").read_text().strip().splitlines()[-1]
    want = time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 19800))
    assert line.startswith(want) and " IST INFO " in line
