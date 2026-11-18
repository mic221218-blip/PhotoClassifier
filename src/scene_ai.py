"""Rank six photographic subjects using whole-frame and localized evidence."""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from src.ai_analysis import analyze_batch
from src.person_ai import PersonAnalyzer
from src.preview import load_color_preview
from src.sky_ai import SkyAnalyzer

MODEL_VERSION = "clip-segformer-yolox-subjects-v1"
SUBJECTS = {"person": "人物", "sky": "天空", "building": "建筑", "city": "城市",
            "flowers": "花卉", "landscape": "风光"}


class Role(IntEnum):
    BACKGROUND = 1
    SECONDARY = 2
    PRIMARY = 3


ROLE_NAMES = {Role.PRIMARY: "主要内容", Role.SECONDARY: "次要内容", Role.BACKGROUND: "背景元素"}

# Prompts describe a photographic subject, including the scale of the scene.
PROMPTS = {
    "person": ("a portrait photograph of a person", "a photo focusing on people"),
    "sky": ("a photograph of clouds in the sky", "a dramatic sunrise or sunset sky"),
    "building": ("an architectural photograph of a single prominent building",
                 "a detailed photograph of a building facade, tower, or bridge"),
    "city": ("a photo of a city", "a photo of a city skyline"),
    "flowers": ("a photo of flowers", "a photo of a flower garden"),
    "landscape": ("a photo of a natural landscape", "a photo of mountains and nature"),
}

# ADE20K class indices for the pinned SegFormer weights. City and landscape
# are scene-level concepts. Their masks measure supporting elements.
BUILDINGS = (1, 25, 48, 61, 79, 84)
CLASS_IDS = {"person": (12,), "building": BUILDINGS, "flowers": (66,),
             "city": (*BUILDINGS, 6, 11, 20, 80, 87, 136),
             "landscape": (4, 9, 13, 16, 21, 26, 29, 34, 46, 60, 68, 72, 94, 113, 128)}


@dataclass(frozen=True)
class Evidence:
    similarity: float
    area: float
    center: float
    detail: float  # share of local image gradients inside this region


def rank_subjects(evidence: dict[str, Evidence], person_detected: bool) -> dict[str, Role]:
    """Assign roles independently of which filters the viewer has selected."""
    scores = {}
    # Similarity scales differ by prompt. The two scene labels need both
    # semantic and spatial support. Isolated buildings need no city label.
    for key, item in evidence.items():
        area, similarity = item.area, item.similarity
        if key == "person":
            if not person_detected:
                continue
            # A verified distant visitor is still searchable as background.
            score = (2.3 * area + 0.8 * item.center + 0.35 * item.detail
                     + 0.12 * _unit((similarity - 0.22) / 0.055) * _unit(area / 0.10))
        elif key == "sky":
            if area < 0.025:
                continue
            semantic = _unit((similarity - 0.19) / 0.09)
            score = 0.52 * area + 0.35 * semantic + 0.13 * item.detail
        elif key == "building":
            if area < 0.016:
                continue
            semantic = _unit((similarity - 0.19) / 0.09)
            coverage = min(1., area * 2 + item.center * 0.3)
            score = 0.38 * semantic * min(1., coverage * 3) + 0.32 * coverage + 0.30 * item.detail
        elif key == "city":
            if area < 0.025 or similarity < 0.25:
                continue
            score = 0.75 * _unit((similarity - 0.214) / 0.065) + 0.25 * min(1., area * 2)
        elif key == "flowers":
            if area < 0.006 and similarity < 0.245:
                continue
            score = (0.50 * _unit((similarity - 0.20) / 0.065)
                     + 0.32 * min(1., area * 3) + 0.18 * min(1., item.center * 2))
            # A patch of flowers in sharp foreground detail can be secondary
            # even in a wide landscape. Small, indistinct patches stay low.
            if area >= 0.025 and similarity >= 0.225:
                score += 0.18 * _unit(2 * (item.detail / area - 1))
        else:  # landscape
            if area < 0.06 or similarity < 0.22:
                continue
            score = (0.63 * _unit((similarity - 0.20) / 0.07)
                     + 0.37 * min(1., area * 1.5))
        scores[key] = _unit(score)

    # A distant row of buildings can strongly support "city" while covering
    # little of the frame as architecture in its own right.
    if "city" in scores and "building" in scores:
        building = evidence["building"]
        if building.area < 0.20 and evidence["city"].similarity > building.similarity:
            scores["building"] *= 0.72
        if building.detail > 0.65 and building.center > 0.48:
            scores["city"] *= 0.80

    # Trees behind blossoms and water in front of a skyline are evidence
    # for a setting. Reduce its rank if a more specific subject explains the frame.
    if "landscape" in scores:
        rivals = [evidence[key].similarity for key in ("flowers", "city", "sky") if key in scores]
        advantage = max(rivals, default=0.) - evidence["landscape"].similarity
        scores["landscape"] -= 0.24 * _unit((advantage - 0.015) / 0.025)

    roles = {}
    strongest = max(scores.values(), default=0.0)
    ordered = sorted(scores, key=scores.get, reverse=True)
    for position, key in enumerate(ordered):
        score = scores[key]
        if position < 2 and score >= max(0.43, strongest - 0.12):
            roles[key] = Role.PRIMARY
        elif score >= max(0.22, strongest * 0.46):
            roles[key] = Role.SECONDARY
        else:
            roles[key] = Role.BACKGROUND
    return roles


def _unit(value: float) -> float:
    return min(1., max(0., value))


class SceneAnalyzer:
    def __init__(self):
        self.person = PersonAnalyzer()
        self.segmenter = SkyAnalyzer()
        self.np = self.segmenter.np
        vectors = self.person.clip.text_vectors(prompt for pair in PROMPTS.values() for prompt in pair)
        means = vectors.reshape(len(PROMPTS), 2, -1).mean(axis=1)
        self.embeddings = means / self.np.linalg.norm(means, axis=1, keepdims=True)

    def evidence(self, image):
        np = self.np
        gray = np.asarray(image.convert("L").resize((128, 128), self.segmenter.bilinear), dtype=np.float32)
        if np.mean((gray > 8) & (gray < 248)) < 0.01:
            return {}, False
        classes = self.segmenter.segment(image)
        frame = self.person.clip.image_vector(image)
        similarities = self.embeddings @ frame
        if not np.isfinite(similarities).all():
            raise ValueError("场景分析结果无效")
        dy, dx = np.gradient(gray)
        edges = np.hypot(dx, dy)
        total = max(float(edges.sum()), 1.0)
        evidence = {}
        for key, similarity in zip(PROMPTS, similarities, strict=True):
            mask = (self.segmenter.visible_sky_mask(image, classes) if key == "sky"
                    else np.isin(classes, CLASS_IDS[key]))
            evidence[key] = Evidence(float(similarity), float(mask.mean()),
                                     float(mask[32:96, 32:96].mean()), float(edges[mask].sum()) / total)
        return evidence, self.person.analyze_image(image).detected

    def analyze(self, info):
        return rank_subjects(*self.evidence(load_color_preview(info)))


def analyze_selection(infos, db, *, progress, cancelled, make_analyzer=SceneAnalyzer, workers=2):
    return analyze_batch(
        infos, make_analyzer=make_analyzer,
        has_result=lambda info, mtime: db.has_scene_result(info, MODEL_VERSION, mtime),
        save_result=lambda info, mtime, result: db.save_scene_result(info, MODEL_VERSION, mtime, result),
        progress=progress, cancelled=cancelled, workers=workers,
    )
