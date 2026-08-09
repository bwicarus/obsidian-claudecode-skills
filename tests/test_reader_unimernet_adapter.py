from __future__ import annotations

import contextlib
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import reader_unimernet_adapter as adapter  # noqa: E402


class FakeTensor:
    def __init__(self):
        self.moves = []

    def unsqueeze(self, value):
        self.unsqueeze_value = value
        return self

    def to(self, device):
        self.moves.append(str(device))
        return self


class FakeModel:
    def __init__(self):
        self.device = None
        self.evaluated = False
        self.generated = []

    def to(self, device):
        self.device = str(device)
        return self

    def eval(self):
        self.evaluated = True
        return self

    def generate(self, value, **options):
        self.generated.append((value, options))
        return {"pred_str": [r"x^2+y^2"]}


class FakeImage:
    def convert(self, mode):
        self.mode = mode
        return self


class UniMERNetAdapterTest(unittest.TestCase):
    def test_official_config_pipeline_is_cuda_local_only_and_callable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model_dir = root / "unimernet_base"
            model_dir.mkdir()
            (model_dir / "pytorch_model.pth").write_bytes(b"local-weight")
            config_path = root / "demo.yaml"
            config_path.write_text("local: true\n", "utf-8")
            fake_model = FakeModel()
            captured = {}

            class FakeConfig:
                def __init__(self, args):
                    captured["args"] = args
                    self.config = SimpleNamespace(
                        datasets=SimpleNamespace(
                            formula_rec_eval=SimpleNamespace(
                                vis_processor=SimpleNamespace(
                                    eval={"name": "formula_image_eval"}
                                )
                            )
                        )
                    )

            class FakeTask:
                def build_model(self, cfg):
                    captured["cfg"] = cfg
                    return fake_model

            tasks = SimpleNamespace(setup_task=lambda cfg: FakeTask())
            tensor = FakeTensor()

            def load_processor(name, config):
                captured["processor"] = (name, config)
                return lambda _image: tensor

            torch = SimpleNamespace(
                cuda=SimpleNamespace(
                    is_available=lambda: True,
                    empty_cache=lambda: captured.setdefault("emptied", True),
                ),
                device=lambda value: value,
                inference_mode=lambda: contextlib.nullcontext(),
            )
            with patch.object(
                adapter,
                "_official_runtime",
                return_value=(torch, FakeConfig, tasks, load_processor),
            ):
                value = adapter.OfficialUniMERNetBase(
                    model_dir, config_path, device="cuda"
                )
                self.assertEqual(value(FakeImage()), r"x^2+y^2")
                value.close()

            options = captured["args"].options
            self.assertIn(
                "model.model_config.model_name=" + model_dir.resolve().as_posix(),
                options,
            )
            self.assertIn(
                "model.pretrained="
                + (model_dir / "pytorch_model.pth").resolve().as_posix(),
                options,
            )
            self.assertEqual(fake_model.device, "cuda:0")
            self.assertTrue(fake_model.evaluated)
            self.assertEqual(
                fake_model.generated[0][1],
                {"temperature": 1.0, "top_p": 1.0, "do_sample": False},
            )
            self.assertEqual(captured["processor"][0], "formula_image_eval")
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
            self.assertEqual(os.environ["TRANSFORMERS_OFFLINE"], "1")
            self.assertTrue(captured["emptied"])

    def test_missing_local_checkpoint_fails_before_runtime_import(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "demo.yaml"
            config.write_text("local: true\n", "utf-8")
            with patch.object(adapter, "_official_runtime") as runtime:
                with self.assertRaisesRegex(
                    adapter.UniMERNetUnavailable, "UniMERNet checkpoint is missing"
                ):
                    adapter.OfficialUniMERNetBase(root, config)
            runtime.assert_not_called()

    def test_checkpoint_prefers_official_snapshot_and_accepts_legacy_name(self):
        with tempfile.TemporaryDirectory() as temp:
            model_dir = Path(temp)
            legacy = model_dir / "unimernet_base.pth"
            legacy.write_bytes(b"legacy")
            self.assertEqual(adapter._checkpoint_path(model_dir), legacy)

            official = model_dir / "pytorch_model.pth"
            official.write_bytes(b"official")
            self.assertEqual(adapter._checkpoint_path(model_dir), official)

    def test_factory_uses_explicit_environment_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model_dir = root / "model"
            config = root / "config.yaml"
            with patch.dict(
                os.environ,
                {
                    "BW_READER_PC_UNIMERNET_MODEL_DIR": str(model_dir),
                    "BW_READER_PC_UNIMERNET_CONFIG": str(config),
                },
            ), patch.object(
                adapter, "OfficialUniMERNetBase", return_value="model"
            ) as factory:
                self.assertEqual(adapter.create_model("unimernet-base", "cuda"), "model")
            factory.assert_called_once_with(model_dir, config, device="cuda")


if __name__ == "__main__":
    unittest.main()
