"""Local CLIP embeddings shared by crop verification and whole-photo analysis."""
from __future__ import annotations

from pathlib import Path


class ClipEncoder:
    def __init__(self, models: Path, options) -> None:
        import numpy as np
        import onnxruntime as ort
        from PIL import Image
        from tokenizers import Tokenizer

        self.np = np
        self.resize = Image.Resampling.BICUBIC
        self.text_path = models / "clip/int8/text_model_int8.onnx"
        self.options = options
        self.tokenizer = Tokenizer.from_file(str(models / "clip/tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=77)
        self.tokenizer.enable_padding(length=77)
        self.vision = ort.InferenceSession(str(models / "clip/int8/vision_model_int8.onnx"),
                                          options, providers=["CPUExecutionProvider"])
        self.input_name = self.vision.get_inputs()[0].name
        self.mean = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
        self.std = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

    def text_vectors(self, prompts):
        import onnxruntime as ort

        np = self.np
        session = ort.InferenceSession(str(self.text_path), self.options, providers=["CPUExecutionProvider"])
        tokens = self.tokenizer.encode_batch(list(prompts))
        ids = np.asarray([item.ids for item in tokens], dtype=np.int64)
        vectors = session.run(None, {session.get_inputs()[0].name: ids})[0]
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)

    def image_vector(self, image):
        np = self.np
        pixels = np.asarray(image.resize((224, 224), self.resize), dtype=np.float32)
        pixels = (pixels / 255.0 - self.mean) / self.std
        tensor = np.ascontiguousarray(pixels.transpose(2, 0, 1)[None])
        vector = self.vision.run(None, {self.input_name: tensor})[0][0]
        norm = np.linalg.norm(vector)
        if not np.isfinite(norm) or not norm:
            raise ValueError("图像特征无效")
        return vector / norm
