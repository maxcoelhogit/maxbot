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


def _always_analyze(cam: dict) -> bool:
    # A câmera principal do portão pode precisar de análise mesmo quando o
    # veículo já saiu do quadro, pois o estado final do portão é relevante.
    return bool(cam.get("prefilter_always_analyze", False))


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
        labels: dict[str, float] = {}

        # Four temporal frames are already captured for the AI pipeline.
        # Reuse them so the prefilter adds no extra RTSP traffic.
        for path in frames[:4]:
            image = cv2.imread(str(path))
            if image is None:
                continue

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
                reason="relevant_object_detected",
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
