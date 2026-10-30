from app.auth_security import hash_password, new_session_token, session_token_hash, verify_password


def test_password_hash_verifies_only_matching_password() -> None:
    encoded = hash_password("correct horse battery staple")
    assert encoded.startswith("scrypt$")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("incorrect password", encoded)


def test_session_tokens_are_random_and_stored_as_digests() -> None:
    first = new_session_token()
    second = new_session_token()
    assert first != second
    assert session_token_hash(first) != first
    assert len(session_token_hash(first)) == 64
