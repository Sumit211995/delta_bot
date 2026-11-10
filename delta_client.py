"""Thin Delta Exchange India REST client (signed requests, no automatic retry on orders)."""
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import urlencode

import requests

log = logging.getLogger("delta")


class DeltaError(Exception):
    pass


def sign(secret: str, method: str, ts: str, path: str, query: str, payload: str) -> str:
    """signature = HMAC-SHA256(secret, method + timestamp + path + query_string + payload), hex."""
    msg = f"{method}{ts}{path}{query}{payload}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


class DeltaClient:
    def __init__(self, base_url: str, api_key: str = "", api_secret: str = ""):
        self.base = base_url.rstrip("/")
        self.key, self.secret = api_key, api_secret
        self.s = requests.Session()

    # ------------------------------------------------------------------ core
    def request(self, method, path, params=None, body=None, auth=False, retries=3):
        query = ("?" + urlencode(params)) if params else ""
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        attempts = retries if method == "GET" else 1  # NEVER auto-retry writes (duplicate orders)
        last = None
        for i in range(attempts):
            headers = {"Accept": "application/json", "Content-Type": "application/json",
                       "User-Agent": "personal-delta-bot/1.0"}
            if auth:
                if not (self.key and self.secret):
                    raise DeltaError("API key/secret missing for a private call")
                ts = str(int(time.time()))  # signature is valid ~5s: keep system clock synced (NTP)
                headers.update({"api-key": self.key, "timestamp": ts,
                                "signature": sign(self.secret, method, ts, path, query, payload)})
            try:
                r = self.s.request(method, self.base + path + query, data=payload or None,
                                   headers=headers, timeout=10)
            except requests.RequestException as e:
                last = DeltaError(f"network error: {e}")
                if i + 1 < attempts:
                    time.sleep(2 ** i)
                    continue
                raise last
            if (r.status_code == 429 or r.status_code >= 500) and i + 1 < attempts:
                time.sleep(2 ** i)
                continue
            try:
                data = r.json()
            except ValueError:
                raise DeltaError(f"non-JSON response {r.status_code}: {r.text[:200]}")
            if r.status_code >= 400 or data.get("success") is False:
                raise DeltaError(f"{method} {path} -> {r.status_code}: {json.dumps(data)[:400]}")
            return data
        raise last or DeltaError("request failed")

    # ---------------------------------------------------------------- public
    def get_product(self, symbol):
        return self.request("GET", f"/v2/products/{symbol}")["result"]

    def get_ticker(self, symbol):
        return self.request("GET", f"/v2/tickers/{symbol}")["result"]

    def get_candles(self, symbol, resolution, start, end):
        data = self.request("GET", "/v2/history/candles", params={
            "resolution": resolution, "symbol": symbol, "start": int(start), "end": int(end)})
        return sorted(data["result"], key=lambda c: c["time"])

    # --------------------------------------------------------------- private
    def get_balances(self):
        return self.request("GET", "/v2/wallet/balances", auth=True)["result"]

    def get_wallet_equity(self, asset):
        res = self.get_balances()
        for row in res:
            if str(row.get("asset_symbol", "")).upper() == asset.upper():
                return float(row.get("balance", 0))
        return None

    def get_position(self, product_id):
        res = self.request("GET", "/v2/positions", params={"product_id": product_id}, auth=True)["result"]
        if isinstance(res, list):
            res = res[0] if res else {}
        return res or {}

    def position_size(self, product_id) -> float:
        """Signed contracts: >0 long, <0 short, 0 flat."""
        return float(self.get_position(product_id).get("size", 0) or 0)

    def get_open_orders(self, product_id):
        # Delta ignores `product_id` here (returns ALL coins' orders); `product_ids` filters correctly.
        # Filter again client-side so another coin's orders can never be mistaken for this one's.
        res = self.request("GET", "/v2/orders", params={"product_ids": product_id,
                           "states": "open,pending"}, auth=True)["result"]
        return [o for o in res if int(o.get("product_id", -1)) == int(product_id)]

    def set_leverage(self, product_id, leverage):
        return self.request("POST", f"/v2/products/{product_id}/orders/leverage",
                            body={"leverage": str(leverage)}, auth=True)

    def place_bracket_market(self, product_id, size, side, sl, tp, client_order_id):
        body = {"product_id": product_id, "size": int(size), "side": side,
                "order_type": "market_order", "client_order_id": client_order_id,
                "bracket_stop_loss_price": str(sl), "bracket_take_profit_price": str(tp),
                "bracket_stop_trigger_method": "mark_price"}
        return self.request("POST", "/v2/orders", body=body, auth=True)

    def cancel_all(self, product_id):
        return self.request("DELETE", "/v2/orders/all", body={
            "product_id": product_id, "cancel_limit_orders": True, "cancel_stop_orders": True}, auth=True)

    def close_position(self, product_id):
        size = self.position_size(product_id)
        if size == 0:
            return None
        side = "sell" if size > 0 else "buy"
        body = {"product_id": product_id, "size": int(abs(size)), "side": side,
                "order_type": "market_order", "reduce_only": True}
        try:
            return self.request("POST", "/v2/orders", body=body, auth=True)
        except DeltaError:
            body["reduce_only"] = "true"  # some API versions expect a string
            return self.request("POST", "/v2/orders", body=body, auth=True)
