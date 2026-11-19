import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from onec_mcp.profiles import connection_options, load_profiles, parse_profiles


def config(**fields):
    return {"bases": {"demo": {"publicationUrl": "https://example.test/base/", **fields}}}


class ProfilesTests(unittest.TestCase):
    def test_urls_defaults_and_bom(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(config()), encoding="utf-8-sig")
            p = load_profiles(path)["demo"]
        self.assertEqual(p["name"], "demo")
        self.assertEqual(p["odataUrl"], "https://example.test/base/odata/standard.odata")
        self.assertEqual(p["extensionUrl"], "https://example.test/base/hs/onec_mcp/v1/")
        self.assertEqual(p["writeCollections"], [])
        self.assertFalse(p["physicalDelete"])
        self.assertNotIn("label", p)
        self.assertEqual(
            connection_options(p),
            {
                "username": None,
                "password": None,
                "token": None,
                "timeout_ms": 20000,
                "allow_insecure_http": False,
                "max_bytes": 32 * 1024 * 1024,
            },
        )

    def test_strict_validation_never_prints_auth_secrets(self):
        for bad in [
            {"publicationUrl": "http://example.test", "allowLoopbackHttp": True},
            {"publicationUrl": "https://user:secret123@example.test"},
            {"password": "secret123"},
            {"physicalDelete": "false"},
            {"physicalDelete": True},
            {"timeoutMs": True},
            {"usernameEnv": "USER"},
            {"tokenEnv": "BAD X"},
            {"writeCollections": "Catalog_X"},
            {"publicationUrl": "https://example.test/demo?token=secret123"},
            {"publicationUrl": "https://example.test:99999/demo"},
        ]:
            with self.subTest(fields=list(bad)), self.assertRaises(ValueError) as error:
                parse_profiles(config(**bad))
            self.assertNotIn("secret123", str(error.exception))

    @patch("onec_mcp.profiles.sys.platform", "linux")
    def test_legacy_environment_credentials_on_non_windows(self):
        p = parse_profiles(config(usernameEnv="TEST_USER", passwordEnv="TEST_PASS"))["demo"]
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "TEST_USER"):
                connection_options(p)
        with patch.dict("os.environ", {"TEST_USER": "testuser", "TEST_PASS": "pass"}, clear=True):
            self.assertEqual(connection_options(p)["password"], "pass")
        self.assertNotIn("pass", p.values())

    def test_vault_reference_is_lazy_and_bound_to_the_publication(self):
        auth = {"username": "reader", "password": "fake-password", "token": None}
        with patch("onec_mcp.profiles.read_windows_credential", return_value=auth) as read:
            profile = parse_profiles(config(publicationUrl="https://EXAMPLE.test:443/temp/../base/",
                                            credentialRef="demo"))["demo"]
            read.assert_not_called()
            self.assertNotIn("password", profile)
            self.assertEqual(connection_options(profile, {})["password"], auth["password"])
            read.assert_called_once_with("demo", "https://example.test/base")

    def test_missing_vault_record_fails_without_using_environment(self):
        profile = parse_profiles(config(credentialRef="demo"))["demo"]
        with patch("onec_mcp.profiles.read_windows_credential", side_effect=ValueError("missing")):
            with self.assertRaisesRegex(ValueError, "missing"):
                connection_options(profile, {"ONEC_DEMO_PASSWORD": "unused"})

    @patch("onec_mcp.profiles.sys.platform", "win32")
    def test_windows_environment_profiles_require_migration(self):
        for legacy in ({"usernameEnv": "U", "passwordEnv": "P"}, {"tokenEnv": "T"}):
            with self.subTest(keys=list(legacy)), self.assertRaisesRegex(ValueError, "credentialRef"):
                parse_profiles(config(**legacy))

    def test_mixed_and_invalid_references_are_rejected_without_echoing_values(self):
        for fields in ({"credentialRef": "demo", "tokenEnv": "T"}, {"credentialRef": None},
                       {"credentialRef": "secret/sentinel"}):
            with self.subTest(keys=list(fields)), self.assertRaises(ValueError) as error:
                parse_profiles(config(**fields))
            self.assertNotIn("sentinel", str(error.exception))

    def test_loopback_and_unicode_paths(self):
        p = parse_profiles(config(publicationUrl="http://[::1]:8888/база", allowLoopbackHttp=True))["demo"]
        self.assertEqual(p["odataUrl"], "http://[::1]:8888/%D0%B1%D0%B0%D0%B7%D0%B0/odata/standard.odata")

    def test_empty_and_bad_top_level(self):
        for raw in [None, [], {"bases": {}}, {"bases": {}, "extra": True}, {"bases": {"a b": {}}}]:
            with self.assertRaises(ValueError):
                parse_profiles(raw)
