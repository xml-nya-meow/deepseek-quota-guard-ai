"""Offline regression cases. Unit tests do not fetch a model or require an account."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from quota_guard import local_ai_runtime as runtime
from quota_guard.local_ai_assistant import EXAMPLES, topics_for


class RuntimeTests(unittest.TestCase):
    def test_case_fields_and_topics_must_belong_to_closed_domains(self):
        case = EXAMPLES[0]
        safe = json.loads(case.model_json())
        bad_inputs = [json.dumps({**safe, "reason": "UntrustedText"}),
                      json.dumps({**safe, "url": "private"}), json.dumps(list(safe.items())),
                      case.model_json().replace('{"fee":', '{"fee":"unknown","fee":')]
        with patch.object(runtime, "verify_assets") as verification:
            for data in bad_inputs:
                with self.subTest(data=data), self.assertRaises(runtime.LocalAIError):
                    runtime.rank_topics(data, topics_for(case))
            for topics in [("verify_platform",), ("not_a_catalog_entry",), ("harness_managed",) * 2]:
                with self.assertRaises(runtime.LocalAIError):
                    runtime.rank_topics(case.model_json(), topics)
            verification.assert_not_called()

    @patch.object(runtime, "verify_assets")
    def test_only_one_approved_model_selection_reorders_existing_topics(self, _verify):
        case = EXAMPLES[0]
        approved = topics_for(case)
        with patch.object(runtime, "_run", return_value=b"read_only_check [end of text]\r\n") as run:
            self.assertEqual(runtime.rank_topics(case.model_json(), approved),
                             ("read_only_check", "harness_managed", "target_identity"))
            command = run.call_args.args[0]
            self.assertIn("--offline", command)
            self.assertIn("--grammar", command)
            self.assertEqual(command[0].split(os.sep)[-1], "llama-completion.exe")
        for invalid in (b"", b"delete", b"harness_managed\nrun something", b"\xff"):
            with patch.object(runtime, "_run", return_value=invalid), self.assertRaises(runtime.LocalAIError):
                runtime.rank_topics(case.model_json(), approved)

    def test_manifest_tampering_missing_and_extra_dll_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {}
            for rel in ("bin/runner.dll", "models/model.gguf", "licenses/license"):
                path = root / rel
                path.parent.mkdir()
                path.write_bytes(b"demo")
                files[rel] = {"size": 4, "sha256": hashlib.sha256(b"demo").hexdigest()}
            lock = (json.dumps({"version": 1, "files": files}, indent=2) + "\n").encode()
            manifest = root / "asset-lock.json"
            manifest.write_bytes(lock.replace(b"\n", b"\r\n"))
            with patch.object(runtime, "LOCK_HASH", hashlib.sha256(lock).hexdigest()):
                runtime.verify_assets(root)
                extra = root / "bin" / "extra.dll"
                extra.touch()
                with self.assertRaises(runtime.LocalAIError):
                    runtime.verify_assets(root)
                extra.unlink()
                model = root / "models" / "model.gguf"
                model.write_bytes(b"fake")
                with self.assertRaises(runtime.LocalAIError):
                    runtime.verify_assets(root)
                model.unlink()
                with self.assertRaises(runtime.LocalAIError):
                    runtime.verify_assets(root)
            with self.assertRaises(runtime.LocalAIError):
                runtime.verify_assets(root)

    @unittest.skipUnless(os.name == "nt", "Windows process job")
    def test_timeout_reaps_runner_and_child(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bin").mkdir()
            witness = root / "pids.txt"
            # A local witness makes this a tree cleanup test, not an assertion about kill() calls.
            code = ("import os,subprocess,sys,time; "
                    "p=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(20)']); "
                    "open(sys.argv[1],'w').write(str(os.getpid())+' '+str(p.pid)); time.sleep(20)")
            with self.assertRaisesRegex(runtime.LocalAIError, "超时"):
                runtime._run([sys.executable, "-I", "-c", code, str(witness)], root, 2.0)
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            api.OpenProcess.restype = ctypes.c_void_p
            api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            api.CloseHandle.argtypes = [ctypes.c_void_p]
            pids = list(map(int, witness.read_text().split()))
            self.assertEqual(len(pids), 2)
            for pid in pids:
                handle = api.OpenProcess(0x100000, False, pid)
                if handle:
                    try:
                        # Windows can drop the Job's active count just before the process
                        # kernel object becomes signalled. Give that final transition time.
                        self.assertEqual(api.WaitForSingleObject(handle, 1500), 0, pid)
                    finally:
                        api.CloseHandle(handle)
                else:
                    self.assertEqual(ctypes.get_last_error(), 87)  # process is gone


if __name__ == "__main__":
    unittest.main()
