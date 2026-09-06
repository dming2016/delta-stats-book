import hashlib
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from account_storage import (
    account_id,
    cache_file,
    migrate_legacy_data,
    preferences_file,
    raw_dir,
)
from auth_registry import (
    VAULT_BACKUP_FILE,
    VAULT_ENTROPY,
    VAULT_FILE,
    activate_account,
    get_account_auth,
    get_account_identity,
    get_active_identity,
    record_validation,
    registry_summary,
    remove_account,
    safe_candidate_summary,
    upsert_account,
)
from delta_api import SavedAuthError, cookie_header, load_auth
from miniapp_storage import (
    AUTH_ENTROPY,
    AUTH_FILE,
    auth_payload_from_storage,
    auth_status,
    has_required_fields,
    save_auth,
)
from secure_store import InterProcessFileLock, read_json, write_json


def wechat_auth(
    openid: str = "wx-openid",
    *,
    session: str = "wx-session",
    display_name: str | None = None,
) -> dict:
    payload = {
        "provider": "wechat-miniapp",
        "account_type": "wechat",
        "openid": openid,
        "acctype": "mini",
        "appid": "wx1c36464bbea2507a",
        "ieg_ams_session_token": session,
        "ieg_ams_token": "wx-token",
        "ieg_ams_token_time": "123",
        "saved_at_utc": "2026-08-09T00:00:00+00:00",
    }
    if display_name:
        payload["display_name"] = display_name
    return payload


def qq_auth(
    openid: str = "qq-openid",
    *,
    token: str = "qq-session",
    display_name: str | None = None,
) -> dict:
    payload = {
        "provider": "wechat-miniapp",
        "account_type": "qq",
        "openid": openid,
        "acctype": "qc",
        "appid": "qq-appid",
        "access_token": token,
        "saved_at_utc": "2026-08-09T00:00:00+00:00",
    }
    if display_name:
        payload["display_name"] = display_name
    return payload


class AuthTests(unittest.TestCase):
    def test_wechat_storage_is_normalized_with_role_display_name(self) -> None:
        payload = auth_payload_from_storage(
            {
                "openid": "wx-openid",
                "ieg_ams_session_token": "wx-session",
                "ieg_ams_token": "wx-token",
                "ieg_ams_token_time": "123",
                "ieg_ams_token_v2": "wx-token-v2",
                "unionid": "wx-union",
                "role": {"nickname": "微信角色"},
            }
        )

        self.assertEqual(payload["provider"], "wechat-miniapp")
        self.assertEqual(payload["account_type"], "wechat")
        self.assertEqual(payload["acctype"], "mini")
        self.assertEqual(payload["openid"], "wx-openid")
        self.assertEqual(payload["display_name"], "微信角色")
        self.assertTrue(has_required_fields(payload))

    def test_role_display_name_decodes_miniapp_url_encoding(self) -> None:
        payload = auth_payload_from_storage(
            {
                "openid": "wx-openid",
                "ieg_ams_session_token": "wx-session",
                "ieg_ams_token": "wx-token",
                "ieg_ams_token_time": "123",
                "role": {"nickname": "%E8%BF%98%E8%A6%81%E5%86%8D%E5%90%83%E7%82%B9"},
            }
        )

        self.assertEqual(payload["display_name"], "还要再吃点")

    def test_qq_storage_is_normalized_from_qc_fields(self) -> None:
        payload = auth_payload_from_storage(
            {
                "currentLoginType": "qq",
                "acctype": "qc",
                "qq_openid": "qq-openid",
                "qq_ieg_ams_session_token": "qq-session",
                "qq_ieg_ams_appid": "qq-appid",
                "role": '{"userName":"QQ角色"}',
            }
        )

        self.assertEqual(payload["account_type"], "qq")
        self.assertEqual(payload["acctype"], "qc")
        self.assertEqual(payload["openid"], "qq-openid")
        self.assertEqual(payload["access_token"], "qq-session")
        self.assertEqual(payload["appid"], "qq-appid")
        self.assertEqual(payload["display_name"], "QQ角色")
        self.assertTrue(has_required_fields(payload))

    def test_qq_storage_requires_all_qc_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "qq_ieg_ams_appid"):
            auth_payload_from_storage(
                {
                    "currentLoginType": "qq",
                    "acctype": "qc",
                    "qq_openid": "qq-openid",
                    "qq_ieg_ams_session_token": "qq-session",
                }
            )

    def test_explicit_wechat_selection_ignores_stale_qc_marker(self) -> None:
        payload = auth_payload_from_storage(
            {
                "currentLoginType": "wx",
                "acctype": "qc",
                "openid": "wx-openid",
                "ieg_ams_session_token": "wx-session",
                "ieg_ams_token": "wx-token",
                "ieg_ams_token_time": "123",
            }
        )

        self.assertEqual(payload["account_type"], "wechat")
        self.assertEqual(payload["acctype"], "mini")

    def test_canonical_payload_requires_cookie_identity_fields(self) -> None:
        self.assertFalse(
            has_required_fields(
                {
                    "openid": "qq-openid",
                    "appid": "qq-appid",
                    "access_token": "qq-session",
                }
            )
        )

    def test_qc_cookie_uses_access_token_only(self) -> None:
        header = cookie_header(
            {
                "openid": "qq-openid",
                "acctype": "qc",
                "appid": "qq-appid",
                "access_token": "qq-session",
                "ieg_ams_token": "must-not-leak",
            }
        )

        self.assertEqual(
            header,
            "openid=qq-openid; acctype=qc; appid=qq-appid; access_token=qq-session;",
        )
        self.assertNotIn("ieg_ams_token", header)

    def test_account_id_accepts_safe_identity_without_exposing_openid(self) -> None:
        auth = {"openid": "secret-openid", "acctype": "qc", "appid": "qq-appid"}
        stored_id = account_id(auth)

        self.assertEqual(len(stored_id), 24)
        self.assertNotIn("secret-openid", stored_id)
        self.assertEqual(account_id({"account_id": stored_id}), stored_id)
        self.assertEqual(
            account_id({"openid": "wx-id", "acctype": "mini"}),
            hashlib.sha256(b"wechat:wx-id").hexdigest()[:24],
        )

    def test_legacy_data_is_copied_once_to_the_previous_account(self) -> None:
        old_auth = wechat_auth()
        new_auth = qq_auth()
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            legacy_cache = app_dir / "cache" / "matches.json"
            legacy_raw = app_dir / "cache" / "raw" / "detail.json"
            legacy_preferences = app_dir / "preferences.json"
            legacy_cache.parent.mkdir(parents=True)
            legacy_raw.parent.mkdir(parents=True)
            legacy_cache.write_text('{"matches": []}', encoding="utf-8")
            legacy_raw.write_text('{"ok": true}', encoding="utf-8")
            legacy_preferences.write_text('{"favorite_friends": []}', encoding="utf-8")

            result = migrate_legacy_data(old_auth, app_dir=app_dir)

            self.assertTrue(result["migrated"])
            self.assertTrue(cache_file(old_auth, app_dir=app_dir).is_file())
            self.assertTrue((raw_dir(old_auth, app_dir=app_dir) / "detail.json").is_file())
            self.assertTrue(preferences_file(old_auth, app_dir=app_dir).is_file())
            self.assertTrue(legacy_cache.is_file())
            second = migrate_legacy_data(new_auth, app_dir=app_dir)
            self.assertFalse(second["migrated"])
            self.assertFalse(cache_file(new_auth, app_dir=app_dir).exists())

    def test_legacy_migration_can_retry_after_raw_copy_failure(self) -> None:
        auth = wechat_auth()
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            legacy_cache = app_dir / "cache" / "matches.json"
            legacy_raw = app_dir / "cache" / "raw" / "detail.json"
            legacy_preferences = app_dir / "preferences.json"
            legacy_raw.parent.mkdir(parents=True)
            legacy_cache.write_text('{"matches": []}', encoding="utf-8")
            legacy_raw.write_text('{"ok": true}', encoding="utf-8")
            legacy_preferences.write_text(
                '{"favorite_friends": []}', encoding="utf-8"
            )

            with patch("account_storage.shutil.copytree", side_effect=OSError("disk")):
                with self.assertRaisesRegex(OSError, "disk"):
                    migrate_legacy_data(auth, app_dir=app_dir)

            self.assertFalse((app_dir / "account-storage-v1.json").exists())
            result = migrate_legacy_data(auth, app_dir=app_dir)
            self.assertTrue(result["migrated"])
            self.assertTrue((raw_dir(auth, app_dir=app_dir) / "detail.json").is_file())

    def test_legacy_auth_migrates_once_and_remains_compatibility_mirror(self) -> None:
        legacy = wechat_auth(display_name="旧微信")
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            write_json(AUTH_FILE, legacy, AUTH_ENTROPY, app_dir=app_dir)

            summary = registry_summary(app_dir=app_dir)
            stored_id = account_id(legacy)

            self.assertEqual(summary["active_account_id"], stored_id)
            self.assertEqual(summary["accounts"][0]["auth_state"], "unverified")
            self.assertTrue((app_dir / VAULT_FILE).is_file())
            self.assertEqual(
                read_json(AUTH_FILE, AUTH_ENTROPY, app_dir=app_dir), legacy
            )
            self.assertEqual(get_account_auth(app_dir=app_dir)["openid"], "wx-openid")

    def test_multiple_accounts_switch_locally_and_update_compatibility_mirror(self) -> None:
        wechat = wechat_auth(display_name="微信一号")
        qq = qq_auth(display_name="QQ一号")
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            wx_summary = upsert_account(wechat, app_dir=app_dir)
            qq_summary = upsert_account(qq, activate=False, app_dir=app_dir)

            summary = registry_summary(app_dir=app_dir)
            self.assertEqual(len(summary["accounts"]), 2)
            self.assertEqual(summary["active_account_id"], wx_summary["id"])
            active = next(account for account in summary["accounts"] if account["active"])
            self.assertTrue(active["is_active"])
            self.assertTrue(active["can_sync"])
            self.assertEqual(active["auth"]["state"], "valid")

            activate_account(qq_summary["id"], app_dir=app_dir)

            self.assertEqual(get_account_auth(app_dir=app_dir)["openid"], "qq-openid")
            self.assertEqual(
                read_json(AUTH_FILE, AUTH_ENTROPY, app_dir=app_dir)["openid"],
                "qq-openid",
            )
            self.assertEqual(
                get_active_identity(app_dir=app_dir)["account_id"], qq_summary["id"]
            )
            self.assertNotIn(
                "openid", get_account_identity(wx_summary["id"], app_dir=app_dir)
            )

    def test_same_account_upsert_preserves_metadata_and_other_accounts(self) -> None:
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            first = upsert_account(
                wechat_auth(display_name="保留昵称"), app_dir=app_dir
            )
            upsert_account(qq_auth(), activate=False, app_dir=app_dir)
            created_at = first["created_at_utc"]

            updated = upsert_account(
                wechat_auth(session="fresh-session"),
                activate=False,
                app_dir=app_dir,
            )

            self.assertEqual(updated["display_name"], "保留昵称")
            self.assertEqual(updated["created_at_utc"], created_at)
            self.assertEqual(len(registry_summary(app_dir=app_dir)["accounts"]), 2)
            self.assertEqual(
                get_account_auth(updated["id"], app_dir=app_dir)[
                    "ieg_ams_session_token"
                ],
                "fresh-session",
            )

    def test_remove_active_clears_credential_but_retains_identity_and_cache(self) -> None:
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            account = upsert_account(
                wechat_auth(display_name="缓存账号"), app_dir=app_dir
            )
            path = cache_file(
                {"account_id": account["id"], "account_type": "wechat"},
                app_dir=app_dir,
            )
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "updated_at": "2026-08-10T01:00:00+00:00",
                        "matches": [{"id": "one"}, {"id": "two"}],
                    }
                ),
                encoding="utf-8",
            )

            removed = remove_account(account["id"], app_dir=app_dir)
            summary = registry_summary(app_dir=app_dir)

            self.assertEqual(removed["auth_state"], "missing")
            self.assertEqual(summary["active_account_id"], None)
            self.assertEqual(summary["accounts"][0]["cache"]["matches"], 2)
            self.assertTrue(summary["accounts"][0]["cache"]["exists"])
            self.assertEqual(summary["accounts"][0]["display_name"], "缓存账号")
            self.assertFalse((app_dir / AUTH_FILE).exists())
            self.assertIsNone(get_active_identity(app_dir=app_dir))
            self.assertEqual(
                get_account_identity(account["id"], app_dir=app_dir)["auth_state"],
                "missing",
            )
            with self.assertRaisesRegex(ValueError, "没有保存"):
                get_account_auth(account["id"], app_dir=app_dir)

    def test_existing_account_cache_is_registered_without_credentials(self) -> None:
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            active = upsert_account(wechat_auth(), app_dir=app_dir)
            cached_id = account_id(qq_auth(openid="cached-qq"))
            path = cache_file(
                {"account_id": cached_id, "account_type": "qq"},
                app_dir=app_dir,
            )
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "account_id": cached_id,
                        "account_type": "qq",
                        "updated_at": "2026-08-10T01:00:00+00:00",
                        "matches": [{"id": "cached-match"}],
                    }
                ),
                encoding="utf-8",
            )

            summary = registry_summary(app_dir=app_dir)
            cached = next(item for item in summary["accounts"] if item["id"] == cached_id)
            first_revision = summary["revision"]

            self.assertEqual(summary["active_account_id"], active["id"])
            self.assertEqual(cached["account_type"], "qq")
            self.assertEqual(cached["auth_state"], "missing")
            self.assertFalse(cached["has_credential"])
            self.assertFalse(cached["can_sync"])
            self.assertEqual(cached["cache"]["matches"], 1)
            self.assertEqual(registry_summary(app_dir=app_dir)["revision"], first_revision)

            activate_account(cached_id, app_dir=app_dir)
            self.assertEqual(
                get_active_identity(app_dir=app_dir)["account_id"], cached_id
            )
            with self.assertRaisesRegex(ValueError, "没有保存"):
                get_account_auth(cached_id, app_dir=app_dir)

    def test_cache_registration_ignores_untrusted_or_existing_records(self) -> None:
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            saved = upsert_account(qq_auth(openid="saved-qq"), app_dir=app_dir)
            saved_path = cache_file(
                {"account_id": saved["id"], "account_type": "qq"},
                app_dir=app_dir,
            )
            saved_path.parent.mkdir(parents=True)
            saved_path.write_text(
                json.dumps(
                    {
                        "account_id": saved["id"],
                        "account_type": "qq",
                        "matches": [],
                    }
                ),
                encoding="utf-8",
            )

            mismatched_id = account_id(wechat_auth(openid="mismatched"))
            mismatched_path = cache_file(
                {"account_id": mismatched_id, "account_type": "wechat"},
                app_dir=app_dir,
            )
            mismatched_path.parent.mkdir(parents=True)
            mismatched_path.write_text(
                json.dumps(
                    {
                        "account_id": account_id(wechat_auth(openid="other")),
                        "account_type": "wechat",
                        "matches": [],
                    }
                ),
                encoding="utf-8",
            )

            malformed_id = account_id(wechat_auth(openid="malformed"))
            malformed_path = cache_file(
                {"account_id": malformed_id, "account_type": "wechat"},
                app_dir=app_dir,
            )
            malformed_path.parent.mkdir(parents=True)
            malformed_path.write_text(
                json.dumps(
                    {
                        "account_id": malformed_id,
                        "account_type": "wechat",
                        "matches": "not-a-list",
                    }
                ),
                encoding="utf-8",
            )

            summary = registry_summary(app_dir=app_dir)

            self.assertEqual([item["id"] for item in summary["accounts"]], [saved["id"]])
            self.assertTrue(summary["accounts"][0]["has_credential"])
            self.assertEqual(summary["accounts"][0]["auth_state"], "valid")

    def test_only_explicit_rejection_marks_account_expired(self) -> None:
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            account = upsert_account(qq_auth(), app_dir=app_dir)

            for result in ("network_error", "busy", "error"):
                current = record_validation(
                    account["id"], result, "temporary", app_dir=app_dir
                )
                self.assertEqual(current["auth_state"], "valid")

            rejected = record_validation(
                account["id"], "rejected", "请先登录", app_dir=app_dir
            )
            self.assertEqual(rejected["auth_state"], "expired")
            with self.assertRaisesRegex(ValueError, "已过期"):
                get_account_auth(account["id"], app_dir=app_dir)

            still_expired = record_validation(
                account["id"], "network_error", "offline", app_dir=app_dir
            )
            self.assertEqual(still_expired["auth_state"], "expired")
            restored = record_validation(
                account["id"], "success", app_dir=app_dir
            )
            self.assertEqual(restored["auth_state"], "valid")

    def test_reread_credential_survives_busy_validation_until_later_success(self) -> None:
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            original = upsert_account(
                qq_auth(token="expired-session"), app_dir=app_dir
            )
            expired = record_validation(
                original["id"], "rejected", "请先登录", app_dir=app_dir
            )
            self.assertEqual(expired["auth_state"], "expired")

            pending = upsert_account(
                qq_auth(token="fresh-session"),
                validation_result="busy",
                validation_error="访问人数太多",
                app_dir=app_dir,
            )

            self.assertEqual(pending["auth_state"], "pending_verification")
            self.assertTrue(pending["can_sync"])
            self.assertEqual(
                get_account_auth(original["id"], app_dir=app_dir)["access_token"],
                "fresh-session",
            )

            still_pending = record_validation(
                original["id"], "network_error", "offline", app_dir=app_dir
            )
            self.assertEqual(still_pending["auth_state"], "pending_verification")

            restored = record_validation(
                original["id"], "success", app_dir=app_dir
            )
            self.assertEqual(restored["auth_state"], "valid")

    def test_safe_validation_error_is_exposed_without_credentials(self) -> None:
        secret = qq_auth(
            openid="error-secret-openid",
            token="error-secret-token",
        )
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            account = upsert_account(secret, app_dir=app_dir)
            record_validation(
                account_id=account["id"],
                result="network_error",
                error="failed error-secret-openid error-secret-token",
                app_dir=app_dir,
            )

            current = registry_summary(app_dir=app_dir)["accounts"][0]
            encoded = json.dumps(current, ensure_ascii=False)

            self.assertEqual(current["last_error"], "failed [redacted] [redacted]")
            self.assertEqual(
                current["last_validation"]["error"], current["last_error"]
            )
            self.assertEqual(
                current["auth"]["last_validation"]["error"], current["last_error"]
            )
            self.assertNotIn("error-secret-openid", encoded)
            self.assertNotIn("error-secret-token", encoded)

    def test_safe_summaries_never_expose_credentials(self) -> None:
        secret = qq_auth(
            openid="qq-secret-openid",
            token="qq-secret-token",
            display_name="公开昵称",
        )
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            upsert_account(secret, app_dir=app_dir)
            registry = registry_summary(app_dir=app_dir)
            candidate = safe_candidate_summary(secret)
            encoded = json.dumps(
                {
                    "registry": registry,
                    "candidate": candidate,
                    "identity": get_active_identity(app_dir=app_dir),
                },
                ensure_ascii=False,
            )

        self.assertNotIn("qq-secret-openid", encoded)
        self.assertNotIn("qq-secret-token", encoded)
        self.assertNotIn("access_token", encoded)
        self.assertIn("公开昵称", encoded)
        self.assertEqual(
            registry["accounts"][0]["credential_revision"],
            candidate["credential_revision"],
        )

    def test_candidate_credential_revision_is_stable_and_opaque(self) -> None:
        original = qq_auth(
            openid="revision-secret-openid",
            token="revision-secret-token",
            display_name="初始昵称",
        )
        metadata_changed = {
            **original,
            "saved_at_utc": "2026-08-10T00:00:00+00:00",
            "display_name": "更新昵称",
        }
        credential_changed = {
            **metadata_changed,
            "access_token": "revision-secret-token-new",
        }

        original_summary = safe_candidate_summary(original)
        metadata_summary = safe_candidate_summary(metadata_changed)
        changed_summary = safe_candidate_summary(credential_changed)
        encoded = json.dumps(
            [original_summary, metadata_summary, changed_summary],
            ensure_ascii=False,
        )

        self.assertRegex(original_summary["credential_revision"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            original_summary["credential_revision"],
            metadata_summary["credential_revision"],
        )
        self.assertNotEqual(
            original_summary["credential_revision"],
            changed_summary["credential_revision"],
        )
        self.assertNotIn("revision-secret-openid", encoded)
        self.assertNotIn("revision-secret-token", encoded)
        self.assertNotIn("access_token", encoded)

    def test_cache_summary_reports_corruption_without_mutating_cache(self) -> None:
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            account = upsert_account(wechat_auth(), app_dir=app_dir)
            path = cache_file(
                {"account_id": account["id"], "account_type": "wechat"},
                app_dir=app_dir,
            )
            path.parent.mkdir(parents=True)
            path.write_text("not json", encoding="utf-8")

            cache = registry_summary(app_dir=app_dir)["accounts"][0]["cache"]

            self.assertTrue(cache["has_cache"])
            self.assertFalse(cache["viewable"])
            self.assertTrue(cache["warning"])
            self.assertEqual(path.read_text(encoding="utf-8"), "not json")

    def test_load_auth_reads_vault_only_and_never_imports_miniapp(self) -> None:
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            with patch("miniapp_storage.import_auth") as import_mock:
                with self.assertRaisesRegex(SavedAuthError, "尚未选择账号"):
                    load_auth(app_dir=app_dir)
            import_mock.assert_not_called()

    def test_save_auth_upserts_and_activates_without_overwriting_other_accounts(self) -> None:
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            save_auth(wechat_auth(), app_dir=app_dir)
            returned = save_auth(qq_auth(), app_dir=app_dir)

            summary = registry_summary(app_dir=app_dir)
            self.assertEqual(returned, app_dir / AUTH_FILE)
            self.assertEqual(len(summary["accounts"]), 2)
            self.assertEqual(
                get_account_auth(app_dir=app_dir)["openid"], "qq-openid"
            )

    def test_auth_status_is_safe_and_reports_active_account(self) -> None:
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            save_auth(
                qq_auth(
                    openid="status-secret-openid",
                    token="status-secret-token",
                    display_name="状态账号",
                ),
                app_dir=app_dir,
            )

            status = auth_status(app_dir=app_dir)
            encoded = json.dumps(status, ensure_ascii=False)

            self.assertTrue(status["exists"])
            self.assertEqual(status["account_type"], "qq")
            self.assertEqual(status["auth_state"], "valid")
            self.assertEqual(status["display_name"], "状态账号")
            self.assertNotIn("status-secret-openid", encoded)
            self.assertNotIn("status-secret-token", encoded)

    def test_incomplete_legacy_auth_is_preserved_but_not_loadable(self) -> None:
        legacy = {
            "provider": "wechat-miniapp",
            "account_type": "wechat",
            "openid": "incomplete-openid",
            "acctype": "mini",
            "appid": "wx1c36464bbea2507a",
        }
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            write_json(AUTH_FILE, legacy, AUTH_ENTROPY, app_dir=app_dir)

            account = registry_summary(app_dir=app_dir)["accounts"][0]

            self.assertEqual(account["auth_state"], "incomplete")
            self.assertTrue((app_dir / AUTH_FILE).is_file())
            with self.assertRaisesRegex(ValueError, "不完整"):
                get_account_auth(account["id"], app_dir=app_dir)

    def test_corrupted_main_vault_recovers_from_encrypted_backup(self) -> None:
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            first = upsert_account(wechat_auth(), app_dir=app_dir)
            second = upsert_account(qq_auth(), activate=False, app_dir=app_dir)
            activate_account(second["id"], app_dir=app_dir)
            self.assertTrue((app_dir / VAULT_BACKUP_FILE).is_file())

            (app_dir / VAULT_FILE).write_bytes(b"corrupted")
            recovered = registry_summary(app_dir=app_dir)

            self.assertEqual(len(recovered["accounts"]), 2)
            self.assertEqual(recovered["active_account_id"], first["id"])
            restored_main = read_json(
                VAULT_FILE,
                VAULT_ENTROPY,
                app_dir=app_dir,
            )
            self.assertEqual(restored_main["revision"], recovered["revision"])
            self.assertEqual(
                read_json(AUTH_FILE, AUTH_ENTROPY, app_dir=app_dir)["openid"],
                "wx-openid",
            )

    def test_interprocess_lock_excludes_contender_and_releases_cross_thread(self) -> None:
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            owner = InterProcessFileLock(
                "operation.lock", timeout=0.2, app_dir=app_dir
            )
            owner.acquire()
            contender = InterProcessFileLock(
                "operation.lock", timeout=0.05, app_dir=app_dir
            )
            with self.assertRaises(TimeoutError):
                contender.acquire()

            release_thread = threading.Thread(target=owner.release)
            release_thread.start()
            release_thread.join(timeout=1)
            self.assertFalse(release_thread.is_alive())
            self.assertFalse(owner.is_locked())

            contender.acquire(timeout=0.2)
            contender.release()

    def test_secure_store_parallel_writes_use_collision_safe_temp_files(self) -> None:
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            errors = []
            barrier = threading.Barrier(4)

            def writer(value: int) -> None:
                try:
                    barrier.wait(timeout=1)
                    write_json(
                        "parallel.dat",
                        {"value": value},
                        "parallel-test",
                        app_dir=app_dir,
                    )
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(value,)) for value in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

            self.assertEqual(errors, [])
            self.assertIn(
                read_json("parallel.dat", "parallel-test", app_dir=app_dir)["value"],
                range(4),
            )
            self.assertEqual(list(app_dir.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
