"""Portable model checks must stay fast and offline (no actual model load)."""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import model_assets
from forced_aligner_runtime import QwenForcedAlignerRuntime


class ModelAssetsTests(unittest.TestCase):
    def test_no_weights_or_missing_shard_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "config.json").write_text("{}")
            self.assertIn("weights", model_assets.model_error(directory))
            (directory / "model.safetensors.index.json").write_text(
                json.dumps({"weight_map": {"layer": "part-1.safetensors"}})
            )
            self.assertIn("part-1", model_assets.model_error(directory))
            (directory / "part-1.safetensors").write_bytes(b"data")
            self.assertIsNone(model_assets.model_error(directory))

    def test_import_cached_snapshot_materializes_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            cache.mkdir()
            (cache / "config.json").write_text("{}")
            (cache / "model.safetensors").write_bytes(b"weights")
            destination = root / "portable" / "models" / "forced_aligner"
            calls = []

            def fake_snapshot_download(repo_id, **kwargs):
                calls.append((repo_id, kwargs))
                return str(cache)

            module = types.SimpleNamespace(snapshot_download=fake_snapshot_download)
            with patch.object(model_assets, "MODEL_DIR", destination), patch.dict(sys.modules, {"huggingface_hub": module}):
                result = model_assets.import_cache()
            self.assertEqual(result, destination)
            self.assertEqual((destination / "model.safetensors").read_bytes(), b"weights")
            self.assertEqual(calls, [(model_assets.MODEL_ID, {"local_files_only": True})])

    def test_local_forced_aligner_override_requires_no_hf_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"TTS_VISEME_ALIGNER_MODEL": tmp}):
                runtime = QwenForcedAlignerRuntime(backend="qwen_asr")
            self.assertEqual(runtime.model_id, tmp)


if __name__ == "__main__":
    unittest.main()
