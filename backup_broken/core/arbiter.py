# core/arbiter.py
from time import time
from collections import defaultdict
import json
import logging

def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

log = logging.getLogger("ARBITER")
# Init handler local si aucun n'est configuré par ailleurs
if not log.handlers:
    log.setLevel(logging.INFO)
    import os
    os.makedirs("logs", exist_ok=True)
    fh = logging.FileHandler("logs/arbiter.log", mode="a", encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - [ARBITER] %(message)s")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    log.propagate = False

class Arbiter:
    def __init__(self, cfg_path="config/arbiter.json"):
        self.cfg = load_json(cfg_path)
        self.mode = self.cfg.get("mode", "enforce")  # "monitor" ou "enforce"
        self.state = {}                 # symbol -> {"owner": str, "dir": "BUY"/"SELL"}
        self.cooldowns = {}             # symbol -> ts
        self.rate = defaultdict(list)   # f"{symbol}|{strategy}" -> [ts,...]

    def _allow(self, ok: bool, reason: str):
        if self.mode == "monitor":
            log.info(f"[MONITOR] would_block={not ok} reason={reason}")
            return True, "MONITOR_OK"
        return ok, reason

    def can_open(self, symbol: str, direction: str, strategy: str):
        now = time()

        # Cooldown global post-trade
        if now < self.cooldowns.get(symbol, 0):
            return self._allow(False, "COOLDOWN")

        # Exclusivité par symbole (si activée)
        if self.cfg.get("exclusive_symbol", True) and symbol in self.state:
            owner = self.state[symbol]["owner"]
            if owner != strategy:
                return self._allow(False, "EXCLUSIVE_SYMBOL")

        # Anti flip-flop (inversion trop rapide)
        last = self.state.get(symbol)
        if last and last.get("dir") and last["dir"] != direction:
            if now - self.cooldowns.get(f"{symbol}:flip", 0) < self.cfg.get("flip_flop_block_s", 60):
                return self._allow(False, "FLIPFLOP")

        # Rate limit /h par stratégie
        bucket = f"{symbol}|{strategy}"
        self.rate[bucket] = [t for t in self.rate[bucket] if now - t < 3600]
        cap = int(self.cfg.get("max_trades_per_hour", {}).get(strategy, 9999))
        if len(self.rate[bucket]) >= cap:
            return self._allow(False, "RATE_LIMIT")

        return True, "OK"

    def register_trade(self, symbol: str, direction: str, strategy: str):
        now = time()
        self.state[symbol] = {"owner": strategy, "dir": direction}
        self.cooldowns[symbol] = now + int(self.cfg.get("cooldown_after_trade_s", 30))
        self.cooldowns[f"{symbol}:flip"] = now
        self.rate[f"{symbol}|{strategy}"].append(now)
        log.info(f"[REG] {symbol} {direction} owner={strategy} cool={self.cooldowns[symbol]}")
