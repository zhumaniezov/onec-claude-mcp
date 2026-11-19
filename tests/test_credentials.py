import getpass
import hashlib
import os
import sys
import unittest
import warnings
from unittest.mock import patch
from uuid import uuid4

from onec_mcp import credentials

URL = "https://publication.invalid/onec"


class CredentialTests(unittest.TestCase):
    def test_target_is_namespaced_and_bound_to_the_publication(self):
        target = credentials.credential_target("Demo_1", URL)
        self.assertEqual(target, "OneC-Claude-MCP/demo_1/" + hashlib.sha256(URL.encode()).hexdigest())
        self.assertNotEqual(target, credentials.credential_target("Demo_1", URL + "2"))
        for name in ("", "../outside", "url/other", "x" * 65, None):
            with self.assertRaises(ValueError):
                credentials.credential_target(name, URL)

    def test_rejects_incorrect_or_unmarked_blobs_without_disclosing_content(self):
        marker = "synthetic-private"
        for data in (
            "not JSON " + marker,
            '{"v":true,"mode":"bearer","secret":"' + marker + '"}',
            '{"v":1,"mode":"basic","username":"a:b","secret":"' + marker + '"}',
            '{"v":1,"mode":"bearer","secret":"' + marker + '","username":"user"}',
            '{"v":1,"mode":"bearer","secret":"token with spaces"}',
            '{"v":1,"mode":"bearer","secret":"токен"}',
            '{"v":1,"mode":"bearer","secret":"two\\nlines"}',
            '{"v":1,"mode":"bearer","secret":"=invalid-prefix"}',
            '{"v":1,"mode":"basic","username":"user","secret":"\\ud800"}',
        ):
            with self.assertRaises(ValueError) as exc:
                credentials._decode(data.encode("utf-16-le"))
            self.assertNotIn(marker, str(exc.exception))

    def test_noninteractive_and_echoing_fallback_are_rejected(self):
        with (
            patch.object(sys.stdin, "isatty", return_value=False),
            patch.object(getpass, "getpass") as prompt,
        ):
            with self.assertRaises(ValueError):
                credentials._prompt("test")
            prompt.assert_not_called()

        def attempted_fallback(*args, **kwargs):
            warnings.warn("would echo input", getpass.GetPassWarning)
            self.fail("getpass fallback ran")

        with (
            patch.object(credentials, "_console_attached", return_value=True),
            patch.object(getpass, "getpass", side_effect=attempted_fallback) as prompt,
        ):
            with self.assertRaises(ValueError):
                credentials._prompt("test")
            prompt.assert_called_once()

    def test_other_platforms_refuse_wincred_without_os_import(self):
        with (
            patch.object(credentials.sys, "platform", "linux"),
            self.assertRaisesRegex(ValueError, "Windows"),
        ):
            credentials.read_windows_credential("demo", URL)

    @unittest.skipUnless(os.name == "nt", "native Windows Credential Manager")
    def test_native_round_trip_replacement_and_cleanup(self):
        import pywintypes
        import win32cred

        alias = "test-" + uuid4().hex
        alternate = URL + "/other"
        key = credentials.credential_target(alias, URL)
        username = "тест" + uuid4().hex
        password = "пароль-🙂-" + uuid4().hex
        token = "fixture." + uuid4().hex
        # Register cleanup only after confirming this generated target did not pre-exist.
        with self.assertRaises(pywintypes.error) as initial:
            win32cred.CredRead(key, win32cred.CRED_TYPE_GENERIC, 0)
        if initial.exception.winerror == 1312:
            self.skipTest("This Windows logon session has no credential set.")
        self.assertEqual(initial.exception.winerror, 1168)

        def cleanup():
            try:
                win32cred.CredDelete(key, win32cred.CRED_TYPE_GENERIC, 0)
            except pywintypes.error as exc:
                if exc.winerror != 1168:
                    raise
            with self.assertRaises(pywintypes.error) as removed:
                win32cred.CredRead(key, win32cred.CRED_TYPE_GENERIC, 0)
            self.assertEqual(removed.exception.winerror, 1168)

        self.addCleanup(cleanup)
        with patch.object(credentials, "_prompt", side_effect=(username, password)):
            credentials.provision_windows_credential(alias, URL, "basic")
        result = credentials.read_windows_credential(alias, URL)
        self.assertTrue(result == {"username": username, "password": password, "token": None})
        record = win32cred.CredRead(key, win32cred.CRED_TYPE_GENERIC, 0)
        self.assertEqual(record["Persist"], win32cred.CRED_PERSIST_LOCAL_MACHINE)
        with self.assertRaises(ValueError):
            credentials.read_windows_credential(alias, alternate)
        with patch.object(credentials, "_prompt") as prompt:
            with self.assertRaisesRegex(ValueError, "replace"):
                credentials.provision_windows_credential(alias, URL, "bearer")
            prompt.assert_not_called()
        with patch.object(credentials, "_prompt", return_value=token):
            credentials.provision_windows_credential(alias, URL, "bearer", replace=True)
        for invalid in ("abc\r\ndef", "тест", "x" * 1281):
            with (
                patch.object(credentials, "_prompt", return_value=invalid),
                self.assertRaises(ValueError) as err,
            ):
                credentials.provision_windows_credential(alias, URL, "bearer", replace=True)
            self.assertNotIn(invalid, str(err.exception))
        self.assertTrue(
            credentials.read_windows_credential(alias, URL)
            == {"username": None, "password": None, "token": token}
        )
