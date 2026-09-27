from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("moncoes-prefilter")

BASE = Path(os.getenv("MONCOES_BASE", "/opt/moncoes-ai"))
MODEL_DIR = BASE / "models"
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
    # descartar uma pessoa/veículo por confiança limítrofe.
    configured = cam.get("prefilter_confidence")
    if configured is not None:
        return max(0.05, min(float(configured), 0.80))

    priority = str(cam.get("priority", "")).lower()
    if priority in {"critical", "high"}:
        return 0.15
    return 0.18


VEHICLE_LABELS = {"car", "motorcycle", "bus", "truck"}


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


def _vehicle_min_area(cam: dict) -> float:
    value = cam.get("prefilter_vehicle_min_area")
    if value is None:
        return 0.0
    return max(0.0, min(float(value), 0.50))


def _gate_watch(cam: dict) -> bool:
    return bool(cam.get("prefilter_gate_watch", False))


def _gate_motion_threshold(cam: dict) -> float:
    value = cam.get("prefilter_gate_motion_threshold", 0.035)
    return max(0.005, min(float(value), 0.30))


def _vehicle_requires_gate_change(cam: dict) -> bool:
    return bool(cam.get("prefilter_vehicle_requires_gate_change", False))


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


def _gate_scene_changed(
    images: list,
    dynamic_boxes: list[list[tuple[int, int, int, int]]],
    threshold: float,
) -> tuple[bool, float]:
    """Detect structural scene change while masking people/vehicles/animals.

    This is intentionally conservative and is only enabled on a gate camera.
    Passing cars are masked before comparison, so a vehicle moving along the
    street should not by itself trigger OpenAI. A gate opening/closing changes
    the static scene and remains visible outside those object boxes.
    """
    import cv2
    import numpy as np

    if len(images) < 2:
        return False, 0.0

    first = images[0]
    last = images[-1]
    if first is None or last is None:
        return False, 0.0

    h, w = first.shape[:2]
    if last.shape[:2] != (h, w):
        last = cv2.resize(last, (w, h))

    gray_a = cv2.cvtColor(first, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(last, cv2.COLOR_BGR2GRAY)
    gray_a = cv2.GaussianBlur(gray_a, (7, 7), 0)
    gray_b = cv2.GaussianBlur(gray_b, (7, 7), 0)

    valid = np.ones((h, w), dtype=np.uint8)
    for box_set in (dynamic_boxes[0], dynamic_boxes[-1]):
        for box in box_set:
            x1, y1, x2, y2 = _expanded_box(box, w, h)
            valid[y1:y2, x1:x2] = 0

    # Ignore a thin border where compression / timestamp overlays often live.
    border_x = max(2, int(w * 0.02))
    border_y = max(2, int(h * 0.02))
    valid[:border_y, :] = 0
    valid[-border_y:, :] = 0
    valid[:, :border_x] = 0
    valid[:, -border_x:] = 0

    diff = cv2.absdiff(gray_a, gray_b)
    changed = (diff >= 28).astype(np.uint8)
    valid_pixels = int(valid.sum())
    if valid_pixels <= 0:
        return False, 0.0

    ratio = float((changed * valid).sum()) / float(valid_pixels)
    return ratio >= threshold, ratio


def inspect_frames(frames: list[Path], cam: dict) -> PrefilterDecision:
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
        vehicle_min_area = _vehicle_min_area(cam)
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
            frame_area = max(1, width * height)

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

                if label in VEHICLE_LABELS and vehicle_min_area > 0:
                    area_ratio = ((x2 - x1) * (y2 - y1)) / float(frame_area)
                    if area_ratio < vehicle_min_area:
                        # Usually a distant vehicle seen through the street
                        # portion of an internal camera.
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

            # On selected gate-facing cameras, vehicles alone are not enough:
            # a car simply passing along the street should be ignored.
            # People (and other explicitly relevant classes) always pass.
            if _vehicle_requires_gate_change(cam) and not any(
                label in {"person", "bicycle", "cat", "dog"}
                for label in ordered
            ):
                changed, ratio = _gate_scene_changed(
                    images,
                    dynamic_boxes,
                    _gate_motion_threshold(cam),
                )
                if changed:
                    return PrefilterDecision(
                        True,
                        labels=ordered,
                        max_confidence=max(labels.values()),
                        reason=f"gate_scene_changed:{ratio:.4f}",
                    )
                return PrefilterDecision(
                    False,
                    labels=ordered,
                    max_confidence=max(labels.values()),
                    reason=f"street_vehicle_without_gate_change:{ratio:.4f}",
                )

            return PrefilterDecision(
                True,
                labels=ordered,
                max_confidence=max(labels.values()),
                reason="relevant_object_detected",
            )

        if _gate_watch(cam):
            changed, ratio = _gate_scene_changed(
                images,
                dynamic_boxes,
                _gate_motion_threshold(cam),
            )
            if changed:
                return PrefilterDecision(
                    True,
                    labels=(),
                    max_confidence=0.0,
                    reason=f"gate_scene_changed_without_object:{ratio:.4f}",
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
