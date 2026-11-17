"""Optional offline person detection and crop verification."""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from src.ai_analysis import analyze_batch
from src.paths import model_directories
from src.photo_info import PhotoInfo
from src.preview import load_color_preview

MODEL_VERSION = "yolox-tiny-clip-int8-person-v1"
DETECTION_THRESHOLD = 0.72
CROP_THRESHOLD = -0.014
ROOT = Path(__file__).resolve().parent.parent
MODEL_PATHS = (
    Path("yolox_tiny.onnx"),
    Path("clip/int8/vision_model_int8.onnx"),
    Path("clip/int8/text_model_int8.onnx"),
    Path("clip/tokenizer.json"),
)
PERSON_PROMPTS = (
    "a candid photograph of a real person",
    "a photo of a real live human being",
    "a real person standing or sitting in front of the camera",
    "a photo of actual people outdoors or indoors",
)
OTHER_PROMPTS = (
    "a stone or metal sculpture of a person",
    "a life size mannequin or wax figure",
    "a painting, poster, or photograph of a person on a wall",
    "a relief or mural representing a human figure",
    "an inanimate object or an empty room with no people",
)


@dataclass(frozen=True)
class PersonResult:
    detected: bool
    margin: float | None


def model_directory() -> Path:
    folders = model_directories(ROOT)
    for folder in folders:
        if all((folder / asset).is_file() for asset in MODEL_PATHS):
            return folder
    raise RuntimeError(f"未找到完整的人物分析模型。请按 README 将模型放到 {folders[0]}。")


class PersonAnalyzer:
    """Keep one CPU session per model for the duration of a batch."""

    def __init__(self, models: Path | None = None) -> None:
        try:
            import numpy as np
            import onnxruntime as ort
            from PIL import Image, ImageOps
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise RuntimeError("请安装人物分析的可选依赖（requirements-ai.txt）。") from exc
        self.np = np
        self.image = Image
        self.image_ops = ImageOps
        weights = models or model_directory()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        providers = ["CPUExecutionProvider"]
        detector, vision, text, tokenizer_data = (weights / name for name in MODEL_PATHS)
        self.detector = ort.InferenceSession(str(detector), options, providers=providers)
        self.vision = ort.InferenceSession(str(vision), options, providers=providers)
        text_session = ort.InferenceSession(str(text), options, providers=providers)
        detector_input = self.detector.get_inputs()[0]
        if detector_input.shape[-2:] != [416, 416]:
            raise ValueError("人物检测模型的输入尺寸不匹配")
        self.detector_input = detector_input.name
        self.vision_input = self.vision.get_inputs()[0].name
        tokenizer = Tokenizer.from_file(str(tokenizer_data))
        tokenizer.enable_truncation(max_length=77)
        tokenizer.enable_padding(length=77)
        encodings = tokenizer.encode_batch(list(PERSON_PROMPTS + OTHER_PROMPTS))
        tokens = np.asarray([item.ids for item in encodings], dtype=np.int64)
        vectors = text_session.run(None, {text_session.get_inputs()[0].name: tokens})[0]
        self.embeddings = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
        self.mean = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
        self.std = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
        grids, strides = [], []
        for stride in (8, 16, 32):
            cells = 416 // stride
            y, x = np.indices((cells, cells), dtype=np.float32)
            grids.append(np.column_stack((x.ravel(), y.ravel())))
            strides.append(np.full((cells * cells, 1), stride, dtype=np.float32))
        self.grid, self.strides = np.concatenate(grids), np.concatenate(strides)

    def analyze(self, info: PhotoInfo) -> PersonResult:
        image = load_color_preview(info)
        best = None
        for crop in self._person_crops(image):
            margin = self._crop_margin(crop)
            if margin >= CROP_THRESHOLD:
                return PersonResult(True, margin)
            best = margin if best is None else max(best, margin)
        return PersonResult(False, best)

    def _person_crops(self, image):
        np = self.np
        scale = 416 / max(image.size)
        width, height = (max(1, int(value * scale)) for value in image.size)
        resized = image.resize((width, height), self.image.Resampling.BILINEAR)
        canvas = np.full((416, 416, 3), 114, dtype=np.float32)
        canvas[:height, :width] = np.asarray(resized)[:, :, ::-1]
        tensor = np.ascontiguousarray(canvas.transpose(2, 0, 1)[None])
        output = self.detector.run(None, {self.detector_input: tensor})[0][0]
        if output.shape != (len(self.grid), 85) or not np.isfinite(output).all():
            raise ValueError("人物检测结果无效")
        scores = output[:, 4] * output[:, 5]
        mask = (output[:, 5:].argmax(axis=1) == 0) & (scores >= DETECTION_THRESHOLD)
        predictions = output[mask]
        strides = self.strides[mask]
        centers = (predictions[:, :2] + self.grid[mask]) * strides
        half = np.exp(predictions[:, 2:4]) * strides / 2
        first = np.clip(centers - half, 0, [width, height]) / [width, height]
        last = np.clip(centers + half, 0, [width, height]) / [width, height]
        accepted = []
        dimensions = np.array(image.size)
        for index in np.argsort(scores[mask])[::-1]:
            lo, hi = first[index], last[index]
            span = hi - lo
            if np.any(span <= 0):
                continue
            if any(self._overlap(lo, hi, a, b) > 0.45 for a, b in accepted):
                continue
            accepted.append((lo, hi))
            extra = span * 0.12
            left = np.maximum((lo - extra) * dimensions, 0)
            right = np.minimum((hi + extra) * dimensions, dimensions)
            yield image.crop((*map(math.floor, left), *map(math.ceil, right)))
            if len(accepted) == 3:
                break

    def _overlap(self, lo, hi, other_lo, other_hi) -> float:
        np = self.np
        intersection = np.prod(np.maximum(np.minimum(hi, other_hi) - np.maximum(lo, other_lo), 0))
        union = np.prod(hi - lo) + np.prod(other_hi - other_lo) - intersection
        return float(intersection / union)

    def _crop_margin(self, crop) -> float:
        np = self.np
        thumbnail = self.image_ops.pad(crop, (224, 224), method=self.image.Resampling.BICUBIC, color=(114, 114, 114))
        pixels = (np.asarray(thumbnail, dtype=np.float32) / 255.0 - self.mean) / self.std
        tensor = np.ascontiguousarray(pixels.transpose(2, 0, 1)[None])
        vector = self.vision.run(None, {self.vision_input: tensor})[0][0]
        similarities = self.embeddings @ (vector / np.linalg.norm(vector))
        if not np.isfinite(similarities).all():
            raise ValueError("人物复核结果无效")
        positives, negatives = np.split(similarities, [len(PERSON_PROMPTS)])
        return float(positives.max() - negatives.max())


def analyze_selection(infos, db, *, progress, cancelled, make_analyzer=PersonAnalyzer, workers=2):
    return analyze_batch(
        infos,
        make_analyzer=make_analyzer,
        has_result=lambda info, mtime: db.has_person_result(info, MODEL_VERSION, mtime),
        save_result=lambda info, mtime, result: db.save_person_result(
            info, MODEL_VERSION, mtime, result.detected, result.margin
        ),
        progress=progress,
        cancelled=cancelled,
        workers=workers,
    )
