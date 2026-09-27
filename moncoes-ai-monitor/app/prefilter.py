from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("moncoes-prefilter")

BASE = Path(os.getenv("MONCOES_BASE", "/opt/moncoes-ai"))
MODEL_DIR = BASE / "models"
GATE_REF_DIR = BASE / "data" / "gate_refs"
MODEL_PATH = Path(
    os.getenv(
        "PREFILTER_MODEL_PATH",
        str(MODEL_DIR / "ssd_mobilenet_v1_coco_2017_11_17.pb"),
    )
)
CONFIG_PATH = Path(
    os.getenv(
        "PREFILTER_CONFIG_PATH",
        str(MODEL_DIR / "ssd_mobilenet_v1_coco_2017_11_17.pbtxt"),
    )
)

# TensorFlow SSD MobileNet V1 trained on COCO.
# IDs follow the COCO detection label map used by this frozen graph.
RELEVANT_CLASSES = {
    1: "person",
    2: "bicycle",
    3: "car",
    4: "motorcycle",
    6: "bus",
    8: "truck",
    17: "cat",
    18: "dog",
}

_NET = None
_NET_LOCK = threading.Lock()
_LOAD_LOCK = threading.Lock()


@dataclass(frozen=True)
class PrefilterDecision:
    should_analyze: bool
    labels: tuple[str, ...] = ()
    max_confidence: float = 0.0
    reason: str = ""
    fail_open: bool = False


def enabled() -> bool:
    return os.getenv("PREFILTER_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _load_net():
    global _NET
    if _NET is not None:
        return _NET
    with _LOAD_LOCK:
        if _NET is not None:
            return _NET
        if not MODEL_PATH.is_file() or not CONFIG_PATH.is_file():
            raise FileNotFoundError(
                f"modelo de pré-filtro ausente: {MODEL_PATH} / {CONFIG_PATH}"
            )
        import cv2

        net = cv2.dnn.readNetFromTensorflow(str(MODEL_PATH), str(CONFIG_PATH))
        net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        _NET = net
        return _NET


def model_ready() -> bool:
    if not enabled():
        return True
    try:
        _load_net()
        return True
    except Exception as exc:
        log.warning("Pré-filtro local indisponível: %s", exc)
        return False


def _threshold(cam: dict) -> float:
    # Favor recall: é melhor mandar um evento duvidoso para a OpenAI do que
    # descartar uma pessoa relevante por confiança limítrofe.
    configured = cam.get("prefilter_confidence")
    if configured is not None:
        return max(0.05, min(float(configured), 0.80))

    priority = str(cam.get("priority", "")).lower()
    if priority in {"critical", "high"}:
        return 0.15
    return 0.18


def _label_threshold(cam: dict, label: str, default: float) -> float:
    """Allow stricter confidence only for noisy classes/cameras.

    The global threshold remains recall-oriented. A camera may raise the bar
    for one class (for example, person near a bicycle rack) without making
    small dogs/cats harder to detect.
    """
    configured = cam.get("prefilter_confidence_by_class")
    if not isinstance(configured, dict):
        return default

    value = configured.get(label)
    if value is None:
        return default

    return max(default, min(float(value), 0.95))


def _allowed_labels(cam: dict) -> set[str]:
    configured = cam.get("prefilter_classes")
    if configured:
        return {
            str(label).strip().lower()
            for label in configured
            if str(label).strip()
        }
    return set(RELEVANT_CLASSES.values())


def _always_analyze(cam: dict) -> bool:
    return bool(cam.get("prefilter_always_analyze", False))


def _gate_watch(cam: dict) -> bool:
    return bool(cam.get("prefilter_gate_watch", False))


def _gate_motion_threshold(cam: dict) -> float:
    value = cam.get("prefilter_gate_motion_threshold", 0.035)
    return max(0.005, min(float(value), 0.30))


def _gate_reference_threshold(cam: dict) -> float:
    value = cam.get("prefilter_gate_reference_threshold", 0.012)
    return max(0.003, min(float(value), 0.15))


def _edge_map(image):
    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    return cv2.Canny(gray, 55, 130)


def _masked_edge_difference(
    edge_a,
    edge_b,
    boxes: list[tuple[int, int, int, int]],
) -> float:
    import cv2
    import numpy as np

    h, w = edge_a.shape[:2]
    if edge_b.shape[:2] != (h, w):
        edge_b = cv2.resize(edge_b, (w, h))

    valid = np.ones((h, w), dtype=np.uint8)
    for box in boxes:
        x1, y1, x2, y2 = _expanded_box(box, w, h, pad_ratio=0.12)
        valid[y1:y2, x1:x2] = 0

    border_x = max(2, int(w * 0.02))
    border_y = max(2, int(h * 0.02))
    valid[:border_y, :] = 0
    valid[-border_y:, :] = 0
    valid[:, :border_x] = 0
    valid[:, -border_x:] = 0

    xor = cv2.bitwise_xor(edge_a, edge_b)
    changed = (xor > 0).astype(np.uint8)
    valid_pixels = int(valid.sum())
    if valid_pixels <= 0:
        return 0.0
    return float((changed * valid).sum()) / float(valid_pixels)


def _gate_reference_check(
    images: list,
    dynamic_boxes: list[list[tuple[int, int, int, int]]],
    cam: dict,
    dvr: str | None,
    channel: int | None,
) -> tuple[str, float]:
    """Compare the current fixed scene with a privacy-safe edge reference.

    The reference stores only a binary edge map, not a camera photograph.
    Before a clean reference exists, actual structural movement still passes;
    a stable scene without a semantic trigger is discarded instead of creating
    a permanent fail-open stream.
    """
    if not _gate_watch(cam) or not dvr or channel is None or not images:
        return "not_applicable", 0.0

    import cv2

    valid_indexes = [i for i, img in enumerate(images) if img is not None]
    if len(valid_indexes) < 2:
        return "unavailable", 0.0

    first_idx = valid_indexes[0]
    last_idx = valid_indexes[-1]
    first = images[first_idx]
    last = images[last_idx]
    first_edge = _edge_map(first)
    last_edge = _edge_map(last)

    all_boxes = []
    for idx in valid_indexes:
        all_boxes.extend(dynamic_boxes[idx])

    temporal_ratio = _masked_edge_difference(
        first_edge,
        last_edge,
        all_boxes,
    )

    GATE_REF_DIR.mkdir(parents=True, exist_ok=True)
    ref_path = GATE_REF_DIR / f"{dvr}_{channel}.png"

    if not ref_path.is_file():
        # Even before a clean closed-state reference is available, structural
        # motion itself remains a trigger. Stable scenes no longer fail open,
        # which avoids sending every harmless street/background event to AI.
        if temporal_ratio >= _gate_motion_threshold(cam):
            return "moving", temporal_ratio

        if not dynamic_boxes[last_idx] and temporal_ratio < 0.010:
            cv2.imwrite(str(ref_path), last_edge)
            log.info(
                "Referência estrutural do acesso aprendida DVR%s cam%s",
                dvr,
                channel,
            )
            return "learned", temporal_ratio

        return "stable_unreferenced", temporal_ratio

    ref_edge = cv2.imread(str(ref_path), cv2.IMREAD_GRAYSCALE)
    if ref_edge is None:
        return "unavailable", temporal_ratio

    current_ratio = _masked_edge_difference(
        ref_edge,
        last_edge,
        dynamic_boxes[last_idx],
    )
    if current_ratio >= _gate_reference_threshold(cam):
        return "different", max(current_ratio, temporal_ratio)

    if temporal_ratio >= _gate_motion_threshold(cam):
        return "moving", temporal_ratio

    return "closed_like_reference", max(current_ratio, temporal_ratio)


def _expanded_box(
    box: tuple[int, int, int, int],
    width: int,
    height: int,
    pad_ratio: float = 0.08,
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)
    px = int(bw * pad_ratio)
    py = int(bh * pad_ratio)
    return (
        max(0, x1 - px),
        max(0, y1 - py),
        min(width, x2 + px),
        min(height, y2 + py),
    )


def inspect_frames(
    frames: list[Path],
    cam: dict,
    dvr: str | None = None,
    channel: int | None = None,
) -> PrefilterDecision:
    """Cheap local semantic gate before any OpenAI request.

    Fail-open is deliberate: a missing/corrupt local model, OpenCV failure or
    unexpected inference output must never blind the safety pipeline.
    """
    if not enabled():
        return PrefilterDecision(
            True,
            reason="prefilter_disabled",
            fail_open=True,
        )

    if _always_analyze(cam):
        return PrefilterDecision(
            True,
            reason="camera_policy_always_analyze",
        )

    if not frames:
        return PrefilterDecision(
            True,
            reason="no_frames_fail_open",
            fail_open=True,
        )

    try:
        import cv2

        net = _load_net()
        threshold = _threshold(cam)
        allowed_labels = _allowed_labels(cam)
        labels: dict[str, float] = {}
        images = []
        dynamic_boxes: list[list[tuple[int, int, int, int]]] = []

        # Four temporal frames are already captured for the AI pipeline.
        # Reuse them so the prefilter adds no extra RTSP traffic.
        for path in frames[:4]:
            image = cv2.imread(str(path))
            images.append(image)
            frame_boxes: list[tuple[int, int, int, int]] = []
            dynamic_boxes.append(frame_boxes)
            if image is None:
                continue

            height, width = image.shape[:2]

            blob = cv2.dnn.blobFromImage(
                image,
                scalefactor=1.0,
                size=(300, 300),
                mean=(0.0, 0.0, 0.0),
                swapRB=True,
                crop=False,
            )

            with _NET_LOCK:
                net.setInput(blob)
                detections = net.forward()

            if getattr(detections, "ndim", 0) != 4:
                raise RuntimeError(
                    f"saída DNN inesperada: shape={getattr(detections, 'shape', None)}"
                )

            for detection in detections[0, 0]:
                confidence = float(detection[2])
                if confidence < threshold:
                    continue

                class_id = int(detection[1])
                label = RELEVANT_CLASSES.get(class_id)
                if not label:
                    continue

                x1 = max(0, min(width, int(float(detection[3]) * width)))
                y1 = max(0, min(height, int(float(detection[4]) * height)))
                x2 = max(0, min(width, int(float(detection[5]) * width)))
                y2 = max(0, min(height, int(float(detection[6]) * height)))
                if x2 <= x1 or y2 <= y1:
                    continue

                frame_boxes.append((x1, y1, x2, y2))

                if label not in allowed_labels:
                    continue

                if confidence < _label_threshold(cam, label, threshold):
                    continue

                labels[label] = max(labels.get(label, 0.0), confidence)

        if labels:
            ordered = tuple(
                name
                for name, _ in sorted(
                    labels.items(),
                    key=lambda item: item[1],
                    reverse=True,
                )
            )
            return PrefilterDecision(
                True,
                labels=ordered,
                max_confidence=max(labels.values()),
                reason="semantic_trigger_detected",
            )

        if _gate_watch(cam):
            ref_state, ratio = _gate_reference_check(
                images,
                dynamic_boxes,
                cam,
                dvr,
                channel,
            )
            if ref_state in {"different", "moving"}:
                return PrefilterDecision(
                    True,
                    labels=(),
                    max_confidence=0.0,
                    reason=f"gate_{ref_state}_without_object:{ratio:.4f}",
                )
            if ref_state == "unavailable":
                return PrefilterDecision(
                    True,
                    labels=(),
                    max_confidence=0.0,
                    reason="gate_reference_unavailable_fail_open",
                    fail_open=True,
                )
            if ref_state in {"learned", "stable_unreferenced", "closed_like_reference"}:
                return PrefilterDecision(
                    False,
                    labels=(),
                    max_confidence=0.0,
                    reason=f"gate_{ref_state}_no_semantic_trigger:{ratio:.4f}",
                )

        return PrefilterDecision(
            False,
            labels=(),
            max_confidence=0.0,
            reason="no_relevant_object_detected",
        )

    except Exception as exc:
        log.warning(
            "Pré-filtro local falhou; evento seguirá para IA (fail-open): %s",
            exc,
        )
        return PrefilterDecision(
            True,
            reason=f"prefilter_error:{type(exc).__name__}",
            fail_open=True,
        )
