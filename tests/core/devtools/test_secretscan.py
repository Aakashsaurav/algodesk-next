"""Tests for devtools secret scanner (INV-9, NFR-14)."""

from __future__ import annotations

from pathlib import Path

from algodesk.api.checks import Severity
from algodesk.devtools.secretscan import (
    _is_lock_or_binary_file,
    _is_placeholder_or_secret_ref,
    format_secretscan_json,
    format_secretscan_text,
    load_secretscan_allow,
    make_secretscan_check,
    run_secretscan,
)


def _setup_scan_env(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    pk_pattern = "-----BEGIN (?:[A-Z0-9_-]+ )?PRIVATE KEY-----"
    ak_pattern = r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    gh_pattern = r"\b(?:(?:ghp|gho|ghu|ghs|ghr)_[0-9a-zA-Z]{36}|github_pat_[0-9a-zA-Z_]{22,})\b"
    tg_pattern = r"\b\d{8,10}:[a-zA-Z0-9_-]{35}\b"
    qs_pattern = (
        r"(?i)\b(token|secret|password|passwd|api_key|apikey|credential)\b\s*[:=]\s*"
        r"[\"']([^\"']{16,})[\"']"
    )
    ph_keys = (
        "['placeholder', 'example', 'dummy', 'sample', "
        "'changeme', 'your_', 'default', 'xxxx', 'fake-secret']"
    )
    content = (
        "[patterns]\n"
        f"private_key = '{pk_pattern}'\n"
        f"cloud_access_key = '{ak_pattern}'\n"
        f"github_token = '{gh_pattern}'\n"
        f"telegram_token = '{tg_pattern}'\n"
        f"quoted_secret_regex = '{qs_pattern}'\n\n"
        "[placeholders]\n"
        f"keywords = {ph_keys}\n\n"
        "[exclusions]\n"
        "directories = ['.git', '.venv', '_workbench', 'data', 'logs']\n"
        "file_patterns = ['*.lock']\n"
    )
    (cfg / "secretscan.toml").write_text(content)
    (cfg / "secretscan_allow.toml").write_text("# empty\n")


def test_private_key_detected(tmp_path: Path) -> None:
    _setup_scan_env(tmp_path)
    pem_data = (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEogIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----\n"
    )
    (tmp_path / "key.pem").write_text(pem_data)
    rep = run_secretscan(tmp_path)
    assert len(rep.findings) == 1
    assert rep.findings[0].rule == "secrets.private_key"
    assert rep.exit_code == 1


def test_cloud_access_key_detected(tmp_path: Path) -> None:
    _setup_scan_env(tmp_path)
    (tmp_path / "aws.env").write_text("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n")
    rep = run_secretscan(tmp_path)
    assert any(f.rule == "secrets.cloud_access_key" for f in rep.findings)


def test_github_token_detected(tmp_path: Path) -> None:
    _setup_scan_env(tmp_path)
    fake_gh = "ghp_123456789012345678901234567890123456"
    (tmp_path / "deploy.sh").write_text(f"GITHUB_TOKEN={fake_gh}\n")
    rep = run_secretscan(tmp_path)
    assert any(f.rule == "secrets.github_token" for f in rep.findings)


def test_telegram_token_detected(tmp_path: Path) -> None:
    _setup_scan_env(tmp_path)
    fake_tg = "123456789:abcdefghijklmnopqrstuvwxyz123456789"
    (tmp_path / "bot.py").write_text(f"bot_key = '{fake_tg}'\n")
    rep = run_secretscan(tmp_path)
    assert any(f.rule == "secrets.telegram_token" for f in rep.findings)


def test_quoted_secret_detected_and_value_never_leaked(tmp_path: Path) -> None:
    _setup_scan_env(tmp_path)
    secret_value = "super_high_entropy_secret_string_998877"
    (tmp_path / "settings.toml").write_text(f"api_key = '{secret_value}'\n")

    rep = run_secretscan(tmp_path)
    assert len(rep.findings) == 1
    assert rep.findings[0].rule == "secrets.quoted_secret"

    # CRITICAL: prove secret value never appears in text or json output
    text_out = format_secretscan_text(rep)
    assert secret_value not in text_out
    assert "1 findings" in text_out

    json_out = format_secretscan_json(rep)
    assert secret_value not in json_out


def test_secret_reference_prefix_ignored(tmp_path: Path) -> None:
    _setup_scan_env(tmp_path)
    (tmp_path / "config.toml").write_text("api_key = 'secret:upstox_api_key_production'\n")
    rep = run_secretscan(tmp_path)
    assert len(rep.findings) == 0


def test_fake_secret_marker_ignored(tmp_path: Path) -> None:
    _setup_scan_env(tmp_path)
    (tmp_path / "test_env.py").write_text(
        "api_key = 'raw_exposed_secret_string_12345'  # fake-secret\n"
    )
    rep = run_secretscan(tmp_path)
    assert len(rep.findings) == 0


def test_obvious_placeholder_ignored(tmp_path: Path) -> None:
    _setup_scan_env(tmp_path)
    (tmp_path / "example.env").write_text(
        "api_key = 'your_api_key_here_please_replace'\n"
        "password = 'changeme_dummy_password'\n"
        "token = 'placeholder_token_value_123'\n"
    )
    rep = run_secretscan(tmp_path)
    assert len(rep.findings) == 0


def test_allow_list_suppresses_finding(tmp_path: Path) -> None:
    _setup_scan_env(tmp_path)
    (tmp_path / "fixture.json").write_text('{"api_key": "raw_exposed_secret_string_12345"}\n')

    # Add to allow list
    cfg_allow = tmp_path / "config" / "secretscan_allow.toml"
    cfg_allow.write_text(
        "[[allow]]\n"
        "path = 'fixture.json'\n"
        "reason = 'Test fixture containing simulated credential'\n"
    )
    rep = run_secretscan(tmp_path)
    assert len(rep.findings) == 0


def test_exclusions_directories_and_locks_skipped(tmp_path: Path) -> None:
    _setup_scan_env(tmp_path)
    wb = tmp_path / "_workbench"
    wb.mkdir()
    (wb / "notes.md").write_text("api_key = 'super_secret_exposed_credential_in_workbench'\n")

    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("password = 'super_secret_in_git_config_file'\n")

    (tmp_path / "poetry.lock").write_text("token = 'super_secret_in_lock_file_123456'\n")

    rep = run_secretscan(tmp_path)
    # Only the 2 config files should be scanned; excluded dirs and lock files skipped
    assert rep.scanned == 2
    assert len(rep.findings) == 0


def test_make_secretscan_check_doctor(tmp_path: Path) -> None:
    class FakeRuntime:
        root = tmp_path

    _setup_scan_env(tmp_path)
    (tmp_path / "app.py").write_text("clean = True\n")
    rt = FakeRuntime()
    chk = make_secretscan_check(rt)
    assert chk.id == "secrets.scan"
    assert chk.severity == Severity.CRITICAL

    res_ok = chk.run()
    assert res_ok.ok is True

    (tmp_path / "bad.env").write_text("api_key = 'raw_exposed_secret_string_12345'\n")
    res_fail = chk.run()
    assert res_fail.ok is False
    assert "Secret scan found" in res_fail.message


def test_secretscan_skips_fake_secret_comment(tmp_path: Path) -> None:
    _setup_scan_env(tmp_path)
    # Line marked with # fake-secret must not trigger finding
    (tmp_path / "app.py").write_text("api_key = 'exposed_value_which_is_exempt' # fake-secret\n")
    rep = run_secretscan(tmp_path)
    assert len(rep.findings) == 0


def test_secretscan_skips_placeholder_example(tmp_path: Path) -> None:
    _setup_scan_env(tmp_path)
    (tmp_path / "app.py").write_text("api_key = 'placeholder_dummy_key_sample_12345'\n")
    rep = run_secretscan(tmp_path)
    assert len(rep.findings) == 0


def test_secretscan_placeholder_repeated_chars_and_caps() -> None:
    keywords = ("placeholder", "example", "dummy")
    assert _is_placeholder_or_secret_ref("secret:my_vault_key", keywords) is True
    assert _is_placeholder_or_secret_ref("example_token_value", keywords) is True
    assert _is_placeholder_or_secret_ref("xxxxxxxxxxxxxxxxx", keywords) is True
    assert _is_placeholder_or_secret_ref("API_KEY_ENVIRONMENT_VARIABLE", keywords) is True
    assert _is_placeholder_or_secret_ref("real_secret_token_value_abc", keywords) is False


def test_secretscan_zero_leakage_guarantee(tmp_path: Path) -> None:
    _setup_scan_env(tmp_path)
    secret_val = "sensitive_token_payload_xyz_123"
    (tmp_path / "leak.py").write_text(f"token = '{secret_val}'\n")
    rep = run_secretscan(tmp_path)
    assert len(rep.findings) == 1
    # Check that neither text nor json outputs print the sensitive token payload
    text_out = format_secretscan_text(rep)
    json_out = format_secretscan_json(rep)
    assert secret_val not in text_out
    assert secret_val not in json_out
    assert secret_val not in rep.findings[0].format()


def test_secretscan_binary_file_skipped(tmp_path: Path) -> None:
    bin_file = tmp_path / "binary.dat"
    bin_file.write_bytes(b"some\x00data\x00with\x00nulls")
    assert _is_lock_or_binary_file(bin_file, {"*.lock"}) is True


def test_secretscan_allowlist_loading_and_filtering(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "secretscan_allow.toml").write_text(
        "[[allow]]\npath = 'fixtures/data.txt'\nreason = 'Test fixture'\n"
    )
    allows = load_secretscan_allow(tmp_path)
    assert allows.get("fixtures/data.txt") == "Test fixture"
