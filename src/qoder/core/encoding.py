"""Encode=1 transform — copied from prod 9router-qoder-patch.cjs / chunk 318."""
from __future__ import annotations

import base64

STD = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
CUSTOM = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"
S2C = [-1] * 128
C2S = [-1] * 128
for i in range(64):
    S2C[ord(STD[i])] = ord(CUSTOM[i])
    C2S[ord(CUSTOM[i])] = ord(STD[i])
S2C[ord("=")] = ord("$")
C2S[ord("$")] = ord("=")


_ENCODE_TABLE = bytes.maketrans((STD + "=").encode("ascii"), (CUSTOM + "$").encode("ascii"))

def encode_body(plaintext: bytes | str) -> bytes:
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    encoded = base64.b64encode(plaintext)
    n = len(encoded)
    a = n // 3
    rearranged = encoded[n - a:] + encoded[a:n - a] + encoded[:a]
    return rearranged.translate(_ENCODE_TABLE)
