import config


def test_comment_only_env_value_is_empty(monkeypatch):
    monkeypatch.setattr(config, "_load_env", lambda: None)
    monkeypatch.setenv("ML_MODEL_PATH", "# e.g. models/filter.joblib (optional)")
    monkeypatch.setenv("LIVE_CONFIRM", "   # must be exactly ...")
    monkeypatch.setenv("SYMBOLS", "BTCUSD,ETHUSD # two coins")
    c = config.load_config()
    assert c.ml_model_path == "" and c.live_confirm == ""
    assert c.symbols == ("BTCUSD", "ETHUSD")
