import json
import os
from typing import Optional
from core.common import log, read_json, write_json_atomic, ROOT_DIR

VALORANT_STORE_PATH = os.path.join(ROOT_DIR, "valorant_accounts.json")
VALORANT_KEY_PATH = os.path.join(ROOT_DIR, "valorant.key")

class CryptoUnavailable(RuntimeError):
    pass

def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:
        raise CryptoUnavailable("Thiếu thư viện 'cryptography' (pip install cryptography).") from e

    key = os.getenv("VALORANT_SECRET_KEY")
    if not key and os.path.exists(VALORANT_KEY_PATH):
        with open(VALORANT_KEY_PATH, "r", encoding="utf-8") as f:
            key = f.read().strip()
    if not key:
        key = Fernet.generate_key().decode()
        fd = os.open(VALORANT_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key)
        log("Đã tạo khoá mã hoá valorant.key — nhớ sao lưu, mất khoá là mọi người phải đăng nhập lại!", "warn")
    return Fernet(key.encode())

class ValorantStore:
    def __init__(self, path: str = VALORANT_STORE_PATH):
        self.path = path
        self._data: Optional[dict] = None

    def available(self) -> bool:
        try:
            _fernet()
            return True
        except CryptoUnavailable:
            return False

    def _load(self) -> dict:
        if self._data is None:
            self._data = read_json(self.path, {})
        return self._data

    def _save(self):
        try:
            write_json_atomic(self.path, self._data, mode=0o600)
        except OSError as e:
            log(f"Không lưu được valorant_accounts.json: {e}", "warn")

    def has(self, uid: str) -> bool:
        return uid in self._load()

    def get(self, uid: str) -> Optional[dict]:
        rec = self._load().get(uid)
        if rec is None:
            return None
        try:
            cookies = json.loads(_fernet().decrypt(rec["cookies"].encode()).decode())
        except Exception as e:
            log(f"Không giải mã được phiên Valorant ({type(e).__name__}), cần đăng nhập lại", "warn")
            return None
        return {"puuid": rec["puuid"], "shard": rec["shard"], "name": rec["name"], "cookies": cookies}

    def set(self, uid: str, account: dict):
        enc = _fernet().encrypt(json.dumps(account["cookies"]).encode()).decode()
        self._load()[uid] = {"puuid": account["puuid"], "shard": account["shard"],
                             "name": account["name"], "cookies": enc}
        self._save()

    def remove(self, uid: str):
        if self._load().pop(uid, None) is not None:
            self._save()


valorant_store = ValorantStore()
