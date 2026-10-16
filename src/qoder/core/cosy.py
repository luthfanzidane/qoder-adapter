"""Cosy signing — port of prod chunk 2701.js module 30015 + constants 69704."""
from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from qoder.core import config as config

RSA_PEM = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""

_PUB = serialization.load_pem_public_key(RSA_PEM)


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    n = block - (len(data) % block)
    return data + bytes([n] * n)


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def build_cosy_headers(
    body: bytes | None,
    request_url: str,
    *,
    user_id: str,
    auth_token: str,
    name: str = "",
    email: str = "",
    machine_id: str = "",
) -> dict[str, str]:
    if not user_id:
        raise ValueError("cosy: user id is empty")
    if not auth_token:
        raise ValueError("cosy: auth token is empty")
    body = body or b""
    machine_id = machine_id or str(uuid.uuid4())

    aes_key_str = str(uuid.uuid4())[:16]
    aes_key = aes_key_str.encode("utf-8")
    user_info = json.dumps(
        {
            "uid": user_id,
            "security_oauth_token": auth_token,
            "name": name or "",
            "aid": "",
            "email": email or "",
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(aes_key))
    enc = cipher.encryptor()
    info_b64 = base64.b64encode(enc.update(_pkcs7_pad(user_info)) + enc.finalize()).decode("ascii")
    cosy_key = base64.b64encode(_PUB.encrypt(aes_key, padding.PKCS1v15())).decode("ascii")

    ts = str(int(time.time()))
    payload_obj = {
        "version": "v1",
        "requestId": str(uuid.uuid4()),
        "info": info_b64,
        "cosyVersion": config.COSY_VERSION,
        "ideVersion": "",
    }
    payload_b64 = base64.b64encode(
        json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")

    path = urlparse(request_url).path or ""
    sig_path = path[5:] if path.startswith("/algo") else path

    # Prod: md5(Buffer.from(payload+"\n"+key+"\n"+ts+"\n"+bodyLatin1+"\n"+path, "latin1"))
    sig_input = (
        payload_b64.encode("latin1")
        + b"\n"
        + cosy_key.encode("latin1")
        + b"\n"
        + ts.encode("latin1")
        + b"\n"
        + body
        + b"\n"
        + sig_path.encode("latin1")
    )
    sig = _md5_hex(sig_input)

    return {
        "Authorization": f"Bearer COSY.{payload_b64}.{sig}",
        "Cosy-Key": cosy_key,
        "Cosy-User": user_id,
        "Cosy-Date": ts,
        "Cosy-Version": config.COSY_VERSION,
        "Cosy-Machineid": machine_id,
        "Cosy-Machinetoken": machine_id,
        "Cosy-Machinetype": "5",
        "Cosy-Machineos": "x86_64_windows",
        "Cosy-Clienttype": "5",
        "Cosy-Clientip": "127.0.0.1",
        "Cosy-Bodyhash": _md5_hex(body),
        "Cosy-Bodylength": str(len(body)),
        "Cosy-Sigpath": sig_path,
        "Cosy-Data-Policy": "disagree",
        "Cosy-Organization-Id": "",
        "Cosy-Organization-Tags": "",
        "Login-Version": "v2",
        "X-Request-Id": str(uuid.uuid4()),
    }
