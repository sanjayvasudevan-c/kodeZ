import time
import uuid

from jose import jwt

from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)


def test_password_hash_roundtrip():
    password = "correct horse battery staple"
    hashed = hash_password(password)

    assert hashed != password
    assert verify_password(password, hashed)
    assert not verify_password("wrong password", hashed)


def test_password_hash_is_argon2id():
    hashed = hash_password("some-password")
    assert hashed.startswith("$argon2id$")


def test_access_token_roundtrip():
    user_id = uuid.uuid4()
    token = create_access_token(user_id)

    assert decode_access_token(token) == user_id


def test_access_token_rejects_garbage():
    assert decode_access_token("not-a-jwt") is None


def test_access_token_rejects_wrong_type():
    from app.config import settings

    payload = {"sub": str(uuid.uuid4()), "type": "refresh"}
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)

    assert decode_access_token(token) is None


def test_access_token_rejects_bad_signature():
    payload = {"sub": str(uuid.uuid4()), "type": "access"}
    token = jwt.encode(payload, "wrong-secret-key", algorithm="HS256")

    assert decode_access_token(token) is None


def test_refresh_token_is_unique_and_high_entropy():
    tokens = {generate_refresh_token() for _ in range(100)}
    assert len(tokens) == 100
    assert all(len(t) >= 40 for t in tokens)


def test_refresh_token_hash_is_deterministic_and_not_reversible():
    raw = generate_refresh_token()
    hashed_once = hash_refresh_token(raw)
    hashed_twice = hash_refresh_token(raw)

    assert hashed_once == hashed_twice
    assert hashed_once != raw
    assert len(hashed_once) == 64  # sha256 hex digest
