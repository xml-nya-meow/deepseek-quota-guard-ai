"""Windows path aliases must retain all recovery ownership and boundary checks."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from quota_guard.profile_recovery import ownership, plain_directory, recover_profiles, write_marker


@unittest.skipUnless(os.name == 'nt', 'Windows 8.3 path alias regression')
class ProfileAliasTests(unittest.TestCase):
    def test_short_root_alias_removes_only_proven_owned_synthetic_session(self):
        import ctypes
        from ctypes import wintypes

        with tempfile.TemporaryDirectory(prefix='quota-profile-alias-') as temporary:
            owned = Path(temporary).resolve(strict=True)
            root = owned / 'application-browser-only'
            root.mkdir()
            api = ctypes.WinDLL('kernel32', use_last_error=True).GetShortPathNameW
            api.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
            api.restype = wintypes.DWORD
            size = api(str(root), None, 0)
            self.assertGreater(size, 0)
            buffer = ctypes.create_unicode_buffer(size)
            count = api(str(root), buffer, size)
            self.assertGreater(count, 0)
            self.assertLess(count, size)
            alias = Path(buffer.value)
            if alias == root:
                self.skipTest('This Windows volume does not expose a distinct 8.3 alias')
            self.assertEqual(alias.resolve(strict=True), root)
            self.assertTrue(root.is_relative_to(owned))
            self.assertEqual(plain_directory(alias), plain_directory(root))

            session = root / ('a' * 32)
            session.mkdir()
            profile = session / 'synthetic-profile'
            profile.mkdir()
            (profile / 'fixture.txt').write_text('synthetic only', encoding='utf-8')
            records = [{'pid': pid, 'created': pid * 100} for pid in (111, 222, 333)]
            record = ownership(session, *records)
            write_marker(session, record)
            write_marker(session, {**record, 'state': 'settled'}, previous=record)
            unmarked = root / ('b' * 32)
            unmarked.mkdir()
            (unmarked / 'keep.txt').write_text('unproven synthetic owner', encoding='utf-8')
            probe = Mock()
            probe.exited.return_value = True

            self.assertEqual(recover_profiles(alias, probe=probe), {'removed': 1, 'retained': 1})
            self.assertFalse(session.exists())
            self.assertEqual((unmarked / 'keep.txt').read_text(encoding='utf-8'),
                             'unproven synthetic owner')
            self.assertEqual(probe.exited.call_count, 3)


if __name__ == '__main__':
    unittest.main()
