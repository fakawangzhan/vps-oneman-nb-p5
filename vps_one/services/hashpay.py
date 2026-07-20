import base64
import json
import time
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class HashPay:
    def __init__(self, base_url: str, merchant_id: str, private_key: str, public_key: str = ""):
        self.base = base_url.rstrip("/")
        self.mid = merchant_id
        self.private = private_key
        self.public = public_key

    def sign(self, method: str, path: str, timestamp: str, body: str) -> str:
        key = serialization.load_pem_private_key(self.private.encode(), password=None)
        message = f"{method}\n{path}\n{timestamp}\n{body}".encode()
        return base64.b64encode(key.sign(message, padding.PKCS1v15(), hashes.SHA256())).decode()

    async def request(self, method: str, path: str, payload: dict | None = None):
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) if payload is not None else ""
        timestamp = str(int(time.time()))
        headers = {"X-Merchant-Id": self.mid, "X-Timestamp": timestamp, "X-Signature": self.sign(method, path, timestamp, body), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5)) as client:
            response = await client.request(method, self.base + path, headers=headers, content=body or None)
            response.raise_for_status()
            return response.json()

    async def create(self, payload: dict):
        return await self.request("POST", "/api/merchant/new", payload)

    async def query(self, order_id: str):
        return await self.request("GET", f"/api/order/{order_id}")

    def decrypt_callback(self, envelope: dict) -> dict:
        if envelope.get("alg") != "RSA-OAEP-256+A256GCM":
            raise ValueError("不支持的 HashPay 回调加密算法")
        key = serialization.load_pem_private_key(self.private.encode(), password=None)
        aes_key = key.decrypt(base64.b64decode(envelope["key"]), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        plaintext = AESGCM(aes_key).decrypt(base64.b64decode(envelope["iv"]), base64.b64decode(envelope["data"]), None)
        message = json.loads(plaintext)
        timestamp = int(message.get("timestamp", 0))
        if abs(int(time.time()) - timestamp) > 300:
            raise ValueError("HashPay 回调已过期")
        payload = message.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("HashPay 回调格式错误")
        return payload
