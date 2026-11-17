"""Optional offline person detection and crop verification."""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.photo_info import PhotoInfo
from src.preview import PreviewError, load_color_preview
from src.work_queue import bounded_results

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
    for folder in (ROOT / "models", ROOT / ".dsh_work" / "models"):
        if all((folder / asset).is_file() for asset in MODEL_PATHS):
            return folder
    raise RuntimeError("未找到完整的人物分析模型。请将模型放到项目的 models 文件夹。")


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
    """Read chosen photos, retaining completed results when a batch is cancelled."""
    analyzer = None
    analyzed = reused = failed = 0
    first_error = None
    def pending():
        nonlocal analyzer, reused, failed, first_error
        for info in infos:
            if cancelled():
                return
            try:
                before = info.path.stat()
                modified_at = datetime.fromtimestamp(before.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                if (before.st_size, modified_at) != (info.size_bytes, info.modified_at):
                    raise PreviewError("原片已变更，请先重新扫描")
            except (OSError, PreviewError) as exc:
                failed += 1
                first_error = first_error or str(exc)
                continue
            if db.has_person_result(info, MODEL_VERSION, before.st_mtime_ns):
                reused += 1
                progress(analyzed + reused + failed, len(infos), info.file_name)
                continue
            if analyzer is None:
                progress(analyzed + reused + failed, len(infos), info.file_name)
                analyzer = make_analyzer()
            if not cancelled():
                yield info, before

    def analyze(item):
        info, before = item
        result = analyzer.analyze(info)
        after = info.path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise PreviewError("分析时原片发生变化，请重新扫描")
        return result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (info, before), future in bounded_results(pool, pending(), analyze, limit=workers, cancelled=cancelled):
            try:
                result = future.result()
            except Exception as exc:
                failed += 1
                first_error = first_error or str(exc)
            else:
                if db.save_person_result(info, MODEL_VERSION, before.st_mtime_ns, result.detected, result.margin):
                    analyzed += 1
                else:
                    failed += 1
                    first_error = first_error or "照片库记录已变化，请刷新后重试"
            progress(analyzed + reused + failed, len(infos), info.file_name)
    return analyzed, reused, failed, first_error
