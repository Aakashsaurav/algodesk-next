"""Tests for api.Scrubber (INV-9)."""

from __future__ import annotations

import concurrent.futures

from algodesk.api.scrub import Scrubber


def test_scrubber_bearer_pattern() -> None:
    s = Scrubber()
    assert s.scrub("Authorization: Bearer secret_jwt_token_here") == "Authorization: Bearer ***"
    assert s.scrub("Bearer abc123def") == "Bearer ***"
    assert s.scrub('Bearer "my-token"') == "Bearer ***"


def test_scrubber_key_value_patterns() -> None:
    s = Scrubber()
    # token, secret, password, passwd, api_key, apikey, private_key, credential
    assert s.scrub("token=mysecret123") == "token=***"
    assert s.scrub("api_key='abc-xyz'") == "api_key='***'"
    assert s.scrub('password="super password"') == 'password="***"'
    assert s.scrub("db_passwd: mypassword") == "db_passwd: ***"
    assert s.scrub("apikey = 987654") == "apikey = ***"
    assert s.scrub("user_secret: 'val'") == "user_secret: '***'"
    assert s.scrub("private_key=pk123456") == "private_key=***"
    assert s.scrub("user_credential=cred123") == "user_credential=***"


def test_scrubber_base64_hex_32_chars() -> None:
    s = Scrubber()
    # 32 hex chars
    hex_32 = "a" * 32
    assert s.scrub(f"hash is {hex_32}") == "hash is ***"

    # 44-char base64 string
    b64 = "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY="
    assert s.scrub(f"key: {b64}") == "key: ***"

    # 31 chars should NOT be masked as 32+ char pattern
    short_31 = "a" * 31
    assert s.scrub(f"value {short_31}") == f"value {short_31}"


def test_scrubber_registered_secrets() -> None:
    s = Scrubber()
    s.register("my-custom-super-secret")
    assert s.scrub("hello my-custom-super-secret world") == "hello *** world"


def test_scrubber_registered_secrets_shorter_than_6_ignored() -> None:
    s = Scrubber()
    s.register("short")
    s.register("12345")
    s.register("")
    assert s.scrub("short 12345 test") == "short 12345 test"


def test_scrubber_extra_patterns() -> None:
    s = Scrubber(extra_patterns=[r"SESSION-[0-9]+"])
    assert s.scrub("active SESSION-998822") == "active ***"


def test_scrubber_scrub_obj_recursively_and_no_mutation() -> None:
    s = Scrubber()
    s.register("registered_secret_val")

    original_dict = {
        "normal": "hello registered_secret_val",
        "api_key": "raw_key_123",
        "nested_list": ["item1", "registered_secret_val", {"password": "pass"}],
        "nested_tuple": ("Bearer abc", 42, True),
    }

    # Clone for mutation check
    dict_copy = dict(original_dict)

    result = s.scrub_obj(original_dict)

    # Assert no mutation
    assert original_dict == dict_copy
    assert result is not original_dict
    assert result["nested_list"] is not original_dict["nested_list"]

    # Assert scrubbed values
    assert result["normal"] == "hello ***"
    assert result["api_key"] == "***"
    assert result["nested_list"][0] == "item1"
    assert result["nested_list"][1] == "***"
    assert result["nested_list"][2] == {"password": "***"}
    assert result["nested_tuple"] == ("Bearer ***", 42, True)


def test_scrubber_scrub_obj_primitives() -> None:
    s = Scrubber()
    assert s.scrub_obj(123) == 123
    assert s.scrub_obj(45.67) == 45.67
    assert s.scrub_obj(True) is True
    assert s.scrub_obj(None) is None


def test_scrubber_thread_safety() -> None:
    s = Scrubber()
    num_threads = 8
    iterations = 100

    def worker(idx: int) -> None:
        secret = f"secret_val_thread_{idx}_123"
        s.register(secret)
        for _ in range(iterations):
            out = s.scrub(f"Log line with {secret} here")
            assert secret not in out
            assert "***" in out

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker, i) for i in range(num_threads)]
        for f in concurrent.futures.as_completed(futures):
            f.result()


def test_scrubber_multiple_bearer_tokens() -> None:
    s = Scrubber()
    txt = "First Bearer token1 and Second Bearer token2"
    assert s.scrub(txt) == "First Bearer *** and Second Bearer ***"


def test_scrubber_bearer_case_insensitivity() -> None:
    s = Scrubber()
    assert s.scrub("bearer xyz123") == "bearer ***"
    assert s.scrub("BEARER XYZ123") == "BEARER ***"


def test_scrubber_key_value_with_tabs_and_spaces() -> None:
    s = Scrubber()
    assert s.scrub("api_key   =   mysecretval") == "api_key   =   ***"
    assert s.scrub("password \t : \t 'secret_with_tabs'") == "password \t : \t '***'"


def test_scrubber_key_value_various_delimiters() -> None:
    s = Scrubber()
    assert s.scrub("auth_token:secret1") == "auth_token:***"
    assert s.scrub("auth_token=secret2") == "auth_token=***"


def test_scrubber_overlapping_registered_secrets() -> None:
    s = Scrubber()
    s.register("supersecret")
    s.register("supersecret_extended_version")
    txt = "Key is supersecret_extended_version here"
    # Longer secret should be masked first
    assert s.scrub(txt) == "Key is *** here"


def test_scrubber_clean_text_untouched() -> None:
    s = Scrubber()
    txt = "The quick brown fox jumps over the lazy dog."
    assert s.scrub(txt) == txt


def test_scrubber_empty_and_whitespace_strings() -> None:
    s = Scrubber()
    assert s.scrub("") == ""
    assert s.scrub("   \n\t  ") == "   \n\t  "


def test_scrubber_non_string_converted_to_str() -> None:
    s = Scrubber()
    # non-str text argument converted to str
    assert s.scrub(123456) == "123456"  # type: ignore[arg-type]


def test_scrubber_deeply_nested_containers() -> None:
    s = Scrubber()
    s.register("deep_secret_value")
    nested = {"l1": [{"l2": [("deep_secret_value", 99)]}]}
    scrubbed = s.scrub_obj(nested)
    assert scrubbed == {"l1": [{"l2": [("***", 99)]}]}


def test_scrubber_dict_with_tuple_and_int_keys() -> None:
    s = Scrubber()
    d = {1: "val", ("a", "b"): "val2"}
    res = s.scrub_obj(d)
    assert res == {1: "val", ("a", "b"): "val2"}


def test_scrubber_extra_patterns_compiled_regex() -> None:
    import re

    s = Scrubber(extra_patterns=[re.compile(r"ACCOUNT-[0-9]{4}")])
    assert s.scrub("User has ACCOUNT-1234 active") == "User has *** active"


def test_scrubber_extra_patterns_string() -> None:
    s = Scrubber(extra_patterns=[r"PIN-[0-9]{4}"])
    assert s.scrub("User has PIN-4321 active") == "User has *** active"


def test_scrubber_unicode_and_symbols() -> None:
    s = Scrubber()
    s.register("₹_secret_symbol_₹")
    assert s.scrub("Price is ₹_secret_symbol_₹") == "Price is ***"


def test_scrubber_boundary_exactly_32_chars() -> None:
    s = Scrubber()
    hex_32 = "f" * 32
    assert s.scrub(f"token: {hex_32}") == "token: ***"


def test_scrubber_boundary_31_chars_not_masked() -> None:
    s = Scrubber()
    hex_31 = "f" * 31
    assert s.scrub(f"value: {hex_31}") == f"value: {hex_31}"
