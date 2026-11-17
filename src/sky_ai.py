"""Optional offline sky detection using the evaluated SegFormer model."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import file_digest
from pathlib import Path

from src.ai_analysis import analyze_batch
from src.paths import model_directories
from src.photo_info import PhotoInfo
from src.preview import load_color_preview

MODEL_VERSION = "segformer-b0-visible-sky-v1"
MODEL_FILE = Path("segformer-b0/model.onnx")
MODEL_SHA256 = "3e5c18a4be395f16646438d54c42377ddc202edfa33d5eced0c9506de75c44c2"
ROOT = Path(__file__).resolve().parent.parent
SKY_CLASS = 2
SKY_THRESHOLD = 0.40


@dataclass(frozen=True)
class SkyResult:
    detected: bool
    visible_fraction: float


def model_path() -> Path:
    folders = model_directories(ROOT)
    for folder in folders:
        path = folder / MODEL_FILE
        if path.is_file():
            return path
    raise RuntimeError(f"未找到天空分析模型。请按 README 将模型放到 {folders[0] / MODEL_FILE}。")


class SkyAnalyzer:
    def __init__(self, model: Path | None = None) -> None:
        try:
            import numpy as np
            import onnxruntime as ort
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("请安装天空分析的可选依赖（requirements-ai.txt）。") from exc

        path = model if model is not None else model_path()
        with path.open("rb") as handle:
            if file_digest(handle, "sha256").hexdigest() != MODEL_SHA256:
                raise RuntimeError("天空模型与已验收的版本不一致，请按 README 获取模型。")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.np = np
        self.bilinear = Image.Resampling.BILINEAR
        self.mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        self.std = np.array([0.229, 0.224, 0.225], dtype=np.float32)

    def analyze(self, info: PhotoInfo) -> SkyResult:
        image = load_color_preview(info)
        np = self.np
        model_pixels = np.asarray(image.resize((512, 512), self.bilinear), dtype=np.float32) / 255
        tensor = np.ascontiguousarray(((model_pixels - self.mean) / self.std).transpose(2, 0, 1)[None])
        logits = self.session.run(None, {self.input_name: tensor})[0][0]
        if logits.shape != (150, 128, 128) or not np.isfinite(logits).all():
            raise ValueError("天空分析结果无效")
        sky = logits.argmax(axis=0) == SKY_CLASS
        pixels = np.asarray(image.resize((128, 128), self.bilinear), dtype=np.float32) / 255
        # Exclude near-black and near-white pixels from the segmented sky area.
        brightness = pixels[sky].mean(axis=1)
        fraction = float(np.count_nonzero((brightness > 0.12) & (brightness < 0.95)) / sky.size)
        return SkyResult(fraction >= SKY_THRESHOLD, fraction)


def analyze_selection(infos, db, *, progress, cancelled, make_analyzer=SkyAnalyzer, workers=2):
    return analyze_batch(
        infos,
        make_analyzer=make_analyzer,
        has_result=lambda info, mtime: db.has_sky_result(info, MODEL_VERSION, mtime),
        save_result=lambda info, mtime, result: db.save_sky_result(
            info, MODEL_VERSION, mtime, result.detected, result.visible_fraction
        ),
        progress=progress,
        cancelled=cancelled,
        workers=workers,
    )
