import base64
import ctypes
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import key_store
from key_store import KeyStore, KeyStoreError


class KeyStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "keys" / "api-key.json"
        self.store = KeyStore(self.path)

    def tearDown(self):
        self.folder.cleanup()

    def envelope(self, **changes):
        result = {
            "format": "classroom-assistant-key", "version": 1,
            "protection": "windows-dpapi-current-user",
            "ciphertext": base64.b64encode(b"encrypted-test-value").decode("ascii"),
        }
        result.update(changes)
        return result

    def write_envelope(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(value), encoding="ascii")

    def test_missing_and_repeated_delete_are_noops(self):
        self.assertIsNone(self.store.load())
        self.store.delete()
        self.store.delete()

    def test_save_only_writes_versioned_ciphertext_then_loads(self):
        secret = "synthetic-api-key-for-unit-test"
        with patch("key_store._require_windows"), patch("key_store._dpapi", return_value=b"encrypted") as crypt:
            self.store.save(secret)
        crypt.assert_called_once_with(secret.encode("ascii"), decrypt=False)
        disk = self.path.read_bytes()
        self.assertNotIn(secret.encode("ascii"), disk)
        envelope = json.loads(disk)
        self.assertEqual(envelope["version"], 1)
        self.assertEqual(envelope["protection"], "windows-dpapi-current-user")
        self.assertEqual(base64.b64decode(envelope["ciphertext"]), b"encrypted")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        with patch("key_store._require_windows"), patch("key_store._dpapi", return_value=secret.encode("ascii")) as crypt:
            self.assertEqual(self.store.load(), secret)
        crypt.assert_called_once_with(b"encrypted", decrypt=True)
        self.store.delete()
        self.assertFalse(self.path.exists())

    def test_invalid_keys_do_not_call_encryption_or_touch_disk(self):
        for value in (None, 7, b"bytes", "", " ", " token", "token ", "to ken", "a\nb", "a\rb", "a\tb", "a\0b", "密钥", "a\x7fb", "a" * 8193):
            with self.subTest(value_type=type(value).__name__, length=len(value) if isinstance(value, str) else None):
                with patch("key_store._dpapi") as crypt:
                    with self.assertRaises(KeyStoreError) as caught:
                        self.store.save(value)
                self.assertEqual(caught.exception.kind, "invalid_key")
                crypt.assert_not_called()
                self.assertFalse(self.path.exists())

    def test_non_windows_never_saves_even_with_mocked_encryption(self):
        with patch("key_store.os.name", "posix"), patch("key_store._dpapi") as crypt:
            with self.assertRaises(KeyStoreError) as caught:
                self.store.save("synthetic-key")
            self.assertEqual(caught.exception.kind, "unsupported")
            self.assertIsNone(self.store.load())
            crypt.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_non_windows_cannot_load_but_can_delete_existing_store(self):
        self.write_envelope(self.envelope())
        with patch("key_store.os.name", "posix"):
            with self.assertRaises(KeyStoreError) as caught:
                self.store.load()
            self.assertEqual(caught.exception.kind, "unsupported")
            self.store.delete()
        self.assertFalse(self.path.exists())

    def test_rejected_formats_are_not_passed_to_dpapi(self):
        invalid = [[], {}, self.envelope(version=2), self.envelope(version=True),
                   self.envelope(protection="plaintext"), self.envelope(ciphertext="***"),
                   self.envelope(ciphertext=""), self.envelope(ciphertext=7),
                   self.envelope(unexpected="field"), self.envelope(format="other")]
        for value in invalid:
            with self.subTest(value=value):
                self.write_envelope(value)
                with patch("key_store._require_windows"), patch("key_store._dpapi") as crypt:
                    with self.assertRaises(KeyStoreError) as caught:
                        self.store.load()
                self.assertEqual(caught.exception.kind, "invalid_format")
                crypt.assert_not_called()

    def test_corrupt_oversize_and_plaintext_files_are_rejected(self):
        self.path.parent.mkdir(parents=True)
        for value in (b"not json", b"synthetic-api-key", b"\xff", b"x" * 65537):
            with self.subTest(length=len(value)):
                self.path.write_bytes(value)
                with patch("key_store._require_windows"), patch("key_store._dpapi") as crypt:
                    with self.assertRaises(KeyStoreError) as caught:
                        self.store.load()
                self.assertEqual(caught.exception.kind, "invalid_format")
                crypt.assert_not_called()

    def test_atomic_replace_failure_preserves_previous_file_and_removes_temp(self):
        self.write_envelope(self.envelope())
        previous = self.path.read_bytes()
        raw_error = f"synthetic-sensitive-token at {self.path}"
        with patch("key_store._require_windows"), patch("key_store._dpapi", return_value=b"new-encrypted"), patch("key_store.os.replace", side_effect=OSError(raw_error)):
            with self.assertRaises(KeyStoreError) as caught:
                self.store.save("synthetic-new-key")
        self.assertEqual(caught.exception.kind, "write_failed")
        self.assertNotIn("synthetic", str(caught.exception))
        self.assertNotIn(str(self.path), str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_fsync_failure_does_not_replace_existing_key(self):
        self.write_envelope(self.envelope())
        previous = self.path.read_bytes()
        with patch("key_store._require_windows"), patch("key_store._dpapi", return_value=b"new-encrypted"), patch("key_store.os.fsync", side_effect=OSError("synthetic-private-path")), patch("key_store.os.replace") as replace:
            with self.assertRaises(KeyStoreError) as caught:
                self.store.save("synthetic-new-key")
        self.assertEqual(caught.exception.kind, "write_failed")
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        replace.assert_not_called()

    def test_read_and_delete_errors_are_sanitized(self):
        for method, patched_method, kind in ((self.store.load, "open", "read_failed"), (self.store.delete, "unlink", "delete_failed")):
            with self.subTest(kind=kind):
                with patch.object(Path, patched_method, side_effect=OSError(f"synthetic-token {self.path}")):
                    with self.assertRaises(KeyStoreError) as caught:
                        method()
                self.assertEqual(caught.exception.kind, kind)
                self.assertNotIn("synthetic", str(caught.exception))
                self.assertNotIn(str(self.path), str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_bad_decrypted_data_is_sanitized(self):
        self.write_envelope(self.envelope())
        for value in (b"", b"\xff", b"contains space"):
            with self.subTest(value=value):
                with patch("key_store._require_windows"), patch("key_store._dpapi", return_value=value):
                    with self.assertRaises(KeyStoreError) as caught:
                        self.store.load()
                self.assertEqual(caught.exception.kind, "decrypt_failed")
                self.assertNotIn(str(self.path), str(caught.exception))

    def test_dpapi_failure_is_sanitized_and_does_not_write(self):
        with patch("key_store._require_windows"), patch("key_store.ctypes.WinDLL", side_effect=OSError("synthetic-sensitive-token private-path"), create=True):
            with self.assertRaises(KeyStoreError) as caught:
                self.store.save("synthetic-key")
        self.assertEqual(caught.exception.kind, "encrypt_failed")
        self.assertNotIn("synthetic", str(caught.exception))
        self.assertNotIn("private-path", str(caught.exception))
        self.assertFalse(self.path.exists())

    def test_dpapi_uses_current_user_ui_forbidden_and_frees_output(self):
        for decrypt in (False, True):
            with self.subTest(decrypt=decrypt):
                crypt32, kernel32 = MagicMock(), MagicMock()
                output_buffer = ctypes.create_string_buffer(b"result", 6)
                observed = {}

                def crypt(source_pointer, description, entropy, reserved, prompt, flags, output_pointer):
                    source = ctypes.cast(source_pointer, ctypes.POINTER(key_store._DataBlob)).contents
                    observed["data"] = ctypes.string_at(source.pbData, source.cbData)
                    observed["flags"] = flags
                    observed["description"] = description
                    observed["entropy"] = entropy
                    output = ctypes.cast(output_pointer, ctypes.POINTER(key_store._DataBlob)).contents
                    output.cbData = 6
                    output.pbData = ctypes.cast(output_buffer, ctypes.POINTER(ctypes.c_ubyte))
                    return 1

                function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
                function.side_effect = crypt
                with patch("key_store._require_windows"), patch("key_store.ctypes.WinDLL", side_effect=[crypt32, kernel32], create=True):
                    self.assertEqual(key_store._dpapi(b"synthetic-input", decrypt=decrypt), b"result")
                self.assertEqual(observed, {"data": b"synthetic-input", "flags": 1, "description": None, "entropy": None})
                kernel32.LocalFree.assert_called_once()
                self.assertEqual(output_buffer.raw, b"\0" * 6)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_real_windows_dpapi_roundtrip_and_corruption(self):
        # This disposable value is synthetic; no real configuration is read.
        secret = "synthetic-dpapi-roundtrip-not-a-real-api-key"
        self.store.save(secret)
        self.assertNotIn(secret.encode("ascii"), self.path.read_bytes())
        self.assertEqual(KeyStore(self.path).load(), secret)
        envelope = json.loads(self.path.read_bytes())
        encrypted = bytearray(base64.b64decode(envelope["ciphertext"]))
        encrypted[-1] ^= 0x01
        envelope["ciphertext"] = base64.b64encode(encrypted).decode("ascii")
        self.write_envelope(envelope)
        with self.assertRaises(KeyStoreError) as caught:
            self.store.load()
        self.assertEqual(caught.exception.kind, "decrypt_failed")
        self.assertNotIn(secret, str(caught.exception))
        self.store.delete()
        self.assertIsNone(self.store.load())


if __name__ == "__main__":
    unittest.main()
