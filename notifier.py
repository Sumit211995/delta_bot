import logging

import requests

log = logging.getLogger("notify")


def send(cfg, text: str) -> None:
    log.info("NOTIFY: %s", text)
    if not (cfg.telegram_token and cfg.telegram_chat_id):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
                      json={"chat_id": cfg.telegram_chat_id, "text": text[:3500]}, timeout=8)
    except requests.RequestException:
        pass
