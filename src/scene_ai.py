"""Rank six photographic subjects using whole-frame and localized evidence."""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from src.ai_analysis import analyze_batch
from src.person_ai import PersonAnalyzer
from src.preview import load_color_preview
from src.sky_ai import SkyAnalyzer

PREVIOUS_MODEL_VERSION = "clip-segformer-yolox-subjects-v1"
MODEL_VERSION = "clip-segformer-yolox-subjects-v2"
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

# Contrast the subject of a composition with the elements visible in it.
# The final four captions can veto a primary role without adding search tags.
COMPOSITION_PROMPTS = {
    "city": ("an urban skyline composed of a group of high rise buildings",
             "a cityscape photograph of many buildings seen together"),
    "building": ("a photo of one particular building, a pavilion or a bridge",
                 "a photograph focusing on the architecture of one structure"),
    "landscape": ("a landscape photograph of an entire natural scene",
                  "a scenic view across a garden, lake, river, or coast"),
    "flowers": ("a photograph focusing on flowers in bloom", "a flowering branch with visible colorful blossoms"),
    "sky": ("a photograph of the clouds and sky overhead", "a dramatic sunrise or sunset sky"),
    "person": ("a portrait of a person posing for the camera", "a photograph focusing on people and their activities"),
    "animals": ("a photograph focusing on a bird or another animal", "a wildlife photograph with animals as the subject"),
    "object": ("a close view of an object, ornament or exhibit", "a still life photograph of a decorative object"),
    "foliage": ("a detail photograph focusing on the leaves of a plant", "a close up photograph of leafy branches without flowers"),
    "moon": ("a photograph of the moon isolated against a plain sky", "a small moon in an otherwise empty sky"),
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


def _subject_scores(evidence: dict[str, Evidence], person_detected: bool,
                    composition: dict[str, float] | None = None) -> dict[str, float]:
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
            skyline = composition is not None and composition[key] >= 0.27 and area >= 0.08
            if area < 0.025 or similarity < (0.24 if skyline else 0.25):
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

    return scores


def _share_primary(first: str, second: str, scores: dict[str, float], spatial: dict[str, float],
                   composition: dict[str, float] | None) -> bool:
    score_gap = scores[first] - scores[second]
    if composition is None:
        return score_gap <= 0.12
    caption_gap = abs(composition[first] - composition[second])
    weaker_region = min(spatial[first], spatial[second])
    if score_gap <= 0.09 and (caption_gap <= 0.010 or weaker_region >= 0.80):
        return True
    # Buildings and city share pixels. For other pairs, independent regions
    # can support two main subjects even when their combined scores differ.
    if {first, second} == {"building", "city"}:
        return False
    balanced_regions = weaker_region >= 0.54 and abs(spatial[first] - spatial[second]) <= 0.10 and caption_gap <= 0.015
    balanced_composition = caption_gap <= 0.006 and min(composition[first], composition[second]) >= 0.245
    return balanced_regions or (balanced_composition and score_gap <= 0.23)


def rank_subjects(evidence: dict[str, Evidence], person_detected: bool,
                  composition: dict[str, float] | None = None) -> dict[str, Role]:
    """Assign roles independently of which filters the viewer has selected."""
    spatial = _subject_scores(evidence, person_detected, composition)
    if not spatial:
        return {}
    scores = spatial.copy()
    allow_primary = True
    if composition is not None:
        best_subject = max(composition[key] for key in SUBJECTS)
        outside = max(value for key, value in composition.items() if key not in SUBJECTS)
        allow_primary = outside < best_subject + 0.012
        for key, strength in spatial.items():
            relative = _unit(1 - (best_subject - composition[key]) / 0.040)
            semantic = _unit((composition[key] - 0.218) / 0.040) * relative
            support = _unit((strength - 0.12) / 0.25)
            scores[key] = 0.38 * strength + 0.62 * semantic * support
            if key in ("person", "flowers") and evidence[key].area >= 0.15:
                scores[key] = max(scores[key], 0.80 * strength)
        # A sharply defined landmark may fill the foreground of an urban scene.
        if "building" in spatial and "city" in spatial:
            building = evidence["building"]
            if (spatial["building"] >= 0.80 and building.detail >= 0.60
                    and building.similarity > evidence["city"].similarity + 0.02):
                scores["building"] = max(scores["building"], spatial["building"])
    roles = {}
    ordered = sorted(scores, key=scores.get, reverse=True)
    first = ordered[0]
    strongest = scores[first]
    primary_candidates = {first}
    if len(ordered) >= 2 and _share_primary(first, ordered[1], scores, spatial, composition):
        primary_candidates.add(ordered[1])
    for key in ordered:
        score = scores[key]
        if allow_primary and key in primary_candidates and score >= 0.43:
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
        pairs = (*PROMPTS.values(), *COMPOSITION_PROMPTS.values())
        vectors = self.person.clip.text_vectors(prompt for pair in pairs for prompt in pair)
        means = vectors.reshape(len(pairs), 2, -1).mean(axis=1)
        self.embeddings = means / self.np.linalg.norm(means, axis=1, keepdims=True)

    def evidence(self, image):
        evidence, person_detected, _ = self._frame_evidence(image)
        return evidence, person_detected

    def _frame_evidence(self, image):
        np = self.np
        gray = np.asarray(image.convert("L").resize((128, 128), self.segmenter.bilinear), dtype=np.float32)
        if np.mean((gray > 8) & (gray < 248)) < 0.01:
            return {}, False, {}
        classes = self.segmenter.segment(image)
        frame = self.person.clip.image_vector(image)
        similarities = self.embeddings @ frame
        if not np.isfinite(similarities).all():
            raise ValueError("场景分析结果无效")
        dy, dx = np.gradient(gray)
        edges = np.hypot(dx, dy)
        total = max(float(edges.sum()), 1.0)
        evidence = {}
        for key, similarity in zip(PROMPTS, similarities[:len(PROMPTS)], strict=True):
            mask = (self.segmenter.visible_sky_mask(image, classes) if key == "sky"
                    else np.isin(classes, CLASS_IDS[key]))
            evidence[key] = Evidence(float(similarity), float(mask.mean()),
                                     float(mask[32:96, 32:96].mean()), float(edges[mask].sum()) / total)
        composition = dict(zip(COMPOSITION_PROMPTS, map(float, similarities[len(PROMPTS):]), strict=True))
        return evidence, self.person.analyze_image(image).detected, composition

    def analyze(self, info):
        return rank_subjects(*self._frame_evidence(load_color_preview(info)))


def analyze_selection(infos, db, *, progress, cancelled, make_analyzer=SceneAnalyzer, workers=2):
    return analyze_batch(
        infos, make_analyzer=make_analyzer,
        has_result=lambda info, mtime: db.has_scene_result(info, MODEL_VERSION, mtime),
        save_result=lambda info, mtime, result: db.save_scene_result(info, MODEL_VERSION, mtime, result),
        progress=progress, cancelled=cancelled, workers=workers,
    )
