import base64
import json
import time
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from httpx import ASGITransport, AsyncClient
from vps_one.main import app
from vps_one.security import decrypt, encrypt, hash_password, verify_password
from vps_one.services.clicd import plan_payload
from vps_one.services.hashpay import HashPay


def test_security_roundtrip():
    password_hash = hash_password("StrongPassword123")
    assert verify_password(password_hash, "StrongPassword123")
    assert not verify_password(password_hash, "wrong")
    assert decrypt(encrypt("secret-value")) == "secret-value"


def test_clicd_payload_contract():
    class Plan:
        virtualization = "lxc"; clicd_image = "debian-bookworm"; cpu = 2; memory_mb = 2048; disk_gb = 40
        assign_nat = True; port_mapping_count = 2; assign_ipv4 = False; ipv4_count = 0
        assign_ipv6 = True; ipv6_count = 1; network_down_mbps = 200; network_up_mbps = 100
        io_read_mbps = 120; io_write_mbps = 80; traffic_gb = 1000
    payload = plan_payload(Plan(), "VP123", "2027-01-01T00:00:00")
    assert payload["vcpu"] == 2
    assert payload["template_id"] == "debian-bookworm"
    assert payload["assign_nat"] is True
    assert payload["network_up_mbps"] == 100
    assert payload["ssh_password"] == ""
    assert payload["ssh_public_key"] == ""
    assert "monthly_traffic_gb" not in payload


def test_hashpay_encrypted_callback():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    aes_key = AESGCM.generate_key(bit_length=256)
    iv = b"123456789012"
    message = json.dumps({"timestamp": int(time.time()), "payload": {"merchantNo": "VP1", "amount": 10, "status": "paid"}}).encode()
    encrypted = AESGCM(aes_key).encrypt(iv, message, None)
    wrapped = private.public_key().encrypt(aes_key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    envelope = {"alg": "RSA-OAEP-256+A256GCM", "key": base64.b64encode(wrapped).decode(), "iv": base64.b64encode(iv).decode(), "data": base64.b64encode(encrypted).decode()}
    assert HashPay("", "", pem).decrypt_callback(envelope)["merchantNo"] == "VP1"


@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
