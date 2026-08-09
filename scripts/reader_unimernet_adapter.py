"""Local-only official UniMERNet base adapter for the PC OCR worker.

No model is downloaded here.  The model directory must already contain an
official UniMERNet base checkpoint and tokenizer/config files.
"""

from __future__ import annotations

import argparse
import gc
import importlib
import os
from pathlib import Path


class UniMERNetUnavailable(RuntimeError):
    pass


CHECKPOINT_NAMES = ("pytorch_model.pth", "unimernet_base.pth")


def _checkpoint_path(model_dir: Path) -> Path:
    """Prefer the official snapshot name while accepting older installs."""
    for name in CHECKPOINT_NAMES:
        candidate = model_dir / name
        if candidate.is_file():
            return candidate
    raise UniMERNetUnavailable(
        "formula-model-unavailable: UniMERNet checkpoint is missing"
    )


def _default_model_dir() -> Path:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return local / "BWReader" / "models" / "unimernet_base"


def _default_config_path() -> Path:
    return Path(__file__).resolve().with_name("reader_unimernet_base.yaml")


def _official_runtime():
    """Import only on first formula, never while the worker is idle."""
    try:
        torch = importlib.import_module("torch")
        config_module = importlib.import_module("unimernet.common.config")
        tasks = importlib.import_module("unimernet.tasks")
        processors = importlib.import_module("unimernet.processors")
    except Exception as exc:
        raise UniMERNetUnavailable(
            "formula-model-unavailable: official UniMERNet runtime is not installed"
        ) from exc
    return torch, config_module.Config, tasks, processors.load_processor


class OfficialUniMERNetBase:
    device = "cuda:0"

    def __init__(self, model_dir: Path, config_path: Path, device: str = "cuda"):
        self.model_dir = model_dir.resolve()
        self.config_path = config_path.resolve()
        self.model = None
        self.processor = None
        self.torch = None
        if str(device).lower() != "cuda":
            raise UniMERNetUnavailable(
                "formula-model-unavailable: UniMERNet quality profile requires CUDA"
            )
        if not self.model_dir.is_dir():
            raise UniMERNetUnavailable(
                "formula-model-unavailable: UniMERNet model directory is missing"
            )
        checkpoint = _checkpoint_path(self.model_dir)
        if not self.config_path.is_file():
            raise UniMERNetUnavailable(
                "formula-model-unavailable: UniMERNet config is missing"
            )

        # Fail closed if a dependency tries to resolve a missing artifact from
        # the network.  Models are installed explicitly, outside this worker.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_DATASETS_OFFLINE"] = "1"

        torch, Config, tasks, load_processor = _official_runtime()
        if not bool(torch.cuda.is_available()):
            raise UniMERNetUnavailable(
                "formula-model-unavailable: CUDA is unavailable"
            )
        self.torch = torch
        model_path = self.model_dir.as_posix()
        checkpoint_path = checkpoint.as_posix()
        args = argparse.Namespace(
            cfg_path=str(self.config_path),
            options=[
                f"model.model_config.model_name={model_path}",
                f"model.pretrained={checkpoint_path}",
                f"model.tokenizer_config.path={model_path}",
                "run.device=cuda",
                "run.distributed=false",
                "run.world_size=1",
            ],
        )
        try:
            cfg = Config(args)
            task = tasks.setup_task(cfg)
            model = task.build_model(cfg).to(torch.device("cuda:0"))
            model.eval()
            processor = load_processor(
                "formula_image_eval",
                cfg.config.datasets.formula_rec_eval.vis_processor.eval,
            )
        except Exception as exc:
            self.close()
            raise UniMERNetUnavailable(
                "formula-model-unavailable: UniMERNet base could not load local artifacts"
            ) from exc
        self.model = model
        self.processor = processor

    def __call__(self, image) -> str:
        if self.model is None or self.processor is None or self.torch is None:
            raise UniMERNetUnavailable("formula-model-unavailable: adapter is closed")
        try:
            tensor = self.processor(image.convert("RGB")).unsqueeze(0).to(
                self.torch.device("cuda:0")
            )
            with self.torch.inference_mode():
                output = self.model.generate(
                    {"image": tensor},
                    # Sampling is disabled; neutral values avoid passing the
                    # package defaults (0.2/0.95) into deterministic generate.
                    temperature=1.0,
                    top_p=1.0,
                    do_sample=False,
                )
            predictions = output.get("pred_str") if isinstance(output, dict) else None
            return str(predictions[0] if predictions else "").strip()
        except Exception as exc:
            raise UniMERNetUnavailable("formula recognition failed") from exc

    def close(self) -> None:
        self.model = None
        self.processor = None
        gc.collect()
        try:
            if self.torch is not None and self.torch.cuda.is_available():
                self.torch.cuda.empty_cache()
        except Exception:
            pass


def create_model(model_name: str, device: str):
    if str(model_name).lower() != "unimernet-base":
        raise UniMERNetUnavailable("formula-model-unavailable: unsupported UniMERNet model")
    model_dir = Path(
        os.environ.get("BW_READER_PC_UNIMERNET_MODEL_DIR") or _default_model_dir()
    ).expanduser()
    config_path = Path(
        os.environ.get("BW_READER_PC_UNIMERNET_CONFIG") or _default_config_path()
    ).expanduser()
    return OfficialUniMERNetBase(model_dir, config_path, device=device)
