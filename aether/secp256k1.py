"""secp256k1 + BIP340 Schnorr 的纯 Python 实现。

为什么要自己写：Nostr 的协议签名是 secp256k1/x-only Schnorr，而标准库与
cryptography 都不提供该曲线。签名频率极低（服务广告每几分钟一次），
纯 Python 的实现速度完全够用，换来零编译依赖与可审计性。

实现对照 BIP340 参考流程，使用 Jacobian 坐标减少模逆次数。
"""

from __future__ import annotations

import hashlib
import os

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

INF = (0, 0, 0)  # Jacobian 无穷远点


def _tagged_hash(tag: str, msg: bytes) -> bytes:
    th = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(th + th + msg).digest()


def _jac_double(p1):
    x, y, z = p1
    if z == 0 or y == 0:
        return INF
    yy = y * y % P
    s = 4 * x * yy % P
    m = 3 * x * x % P
    x3 = (m * m - 2 * s) % P
    y3 = (m * (s - x3) - 8 * yy * yy) % P
    z3 = 2 * y * z % P
    return (x3, y3, z3)


def _jac_add(p1, p2):
    if p1[2] == 0:
        return p2
    if p2[2] == 0:
        return p1
    x1, y1, z1 = p1
    x2, y2, z2 = p2
    z1z1 = z1 * z1 % P
    z2z2 = z2 * z2 % P
    u1 = x1 * z2z2 % P
    u2 = x2 * z1z1 % P
    s1 = y1 * z2 * z2z2 % P
    s2 = y2 * z1 * z1z1 % P
    if u1 == u2:
        return _jac_double(p1) if s1 == s2 else INF
    h = (u2 - u1) % P
    r = (s2 - s1) % P
    h2 = h * h % P
    h3 = h * h2 % P
    u1h2 = u1 * h2 % P
    x3 = (r * r - h3 - 2 * u1h2) % P
    y3 = (r * (u1h2 - x3) - s1 * h3) % P
    z3 = h * z1 * z2 % P
    return (x3, y3, z3)


def _jac_mul(point_aff, scalar: int):
    if scalar % N == 0:
        return INF
    result = INF
    addend = (point_aff[0], point_aff[1], 1)
    for bit in bin(scalar)[2:]:
        result = _jac_double(result)
        if bit == "1":
            result = _jac_add(result, addend)
    return result


def _to_affine(p1):
    if p1[2] == 0:
        return None
    zinv = pow(p1[2], P - 2, P)
    zinv2 = zinv * zinv % P
    return (p1[0] * zinv2 % P, p1[1] * zinv2 * zinv % P)


def _affine_mul(point_aff, scalar: int):
    return _to_affine(_jac_mul(point_aff, scalar))


def _has_even_y(point) -> bool:
    return point[1] % 2 == 0


def _xonly(point) -> bytes:
    return point[0].to_bytes(32, "big")


def _lift_x(x: int):
    if x >= P:
        return None
    c = (x * x * x + 7) % P
    y = pow(c, (P + 1) // 4, P)
    if y * y % P != c:
        return None
    return (x, y if y % 2 == 0 else P - y)


# ---- 对外接口 ----

def pubkey_xonly(seckey: bytes) -> bytes:
    d0 = int.from_bytes(seckey, "big")
    if not 0 < d0 < N:
        raise ValueError("私钥超出范围")
    point = _affine_mul((GX, GY), d0)
    return _xonly(point)


def schnorr_sign(msg32: bytes, seckey: bytes, aux: bytes | None = None) -> bytes:
    if len(msg32) != 32:
        raise ValueError("消息必须是 32 字节摘要")
    aux = aux if aux is not None else os.urandom(32)
    d0 = int.from_bytes(seckey, "big")
    if not 0 < d0 < N:
        raise ValueError("私钥超出范围")
    point = _affine_mul((GX, GY), d0)
    d = d0 if _has_even_y(point) else N - d0
    t = bytes(a ^ b for a, b in zip(d.to_bytes(32, "big"), _tagged_hash("BIP0340/aux", aux), strict=True))
    px = _xonly(point)
    k0 = int.from_bytes(_tagged_hash("BIP0340/nonce", t + px + msg32), "big") % N
    if k0 == 0:
        raise ValueError("nonce 退化")
    r_point = _affine_mul((GX, GY), k0)
    k = k0 if _has_even_y(r_point) else N - k0
    rx = _xonly(r_point)
    e = int.from_bytes(_tagged_hash("BIP0340/challenge", rx + px + msg32), "big") % N
    return rx + ((k + e * d) % N).to_bytes(32, "big")


def schnorr_verify(msg32: bytes, pubkey: bytes, sig: bytes) -> bool:
    if len(msg32) != 32 or len(pubkey) != 32 or len(sig) != 64:
        return False
    point = _lift_x(int.from_bytes(pubkey, "big"))
    if point is None:
        return False
    r = int.from_bytes(sig[:32], "big")
    s = int.from_bytes(sig[32:], "big")
    if r >= P or s >= N:
        return False
    e = int.from_bytes(
        _tagged_hash("BIP0340/challenge", sig[:32] + pubkey + msg32), "big"
    ) % N
    # R = s*G - e*P，用 s*G + (N-e)*P 计算
    neg_e = N - e if e else 0
    sj = _jac_mul((GX, GY), s)
    ej = _jac_mul(point, neg_e)
    r_point = _to_affine(_jac_add(sj, ej))
    if r_point is None or not _has_even_y(r_point):
        return False
    return r_point[0] == r


# ---- 展示用编码 ----

def to_npub(pubkey: bytes) -> str:
    import bech32  # 延迟导入，缺失则退回 hex

    return bech32.encode("npub", bech32.convertbits(pubkey, 8, 5))
