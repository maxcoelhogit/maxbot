#!/usr/bin/env bash
set -Eeuo pipefail

BASE="${MONCOES_BASE:-/opt/moncoes-ai}"
MODEL_DIR="$BASE/models"
MODEL="$MODEL_DIR/ssd_mobilenet_v1_coco_2017_11_17.pb"
CONFIG="$MODEL_DIR/ssd_mobilenet_v1_coco_2017_11_17.pbtxt"
PY="$BASE/venv/bin/python"

MODEL_ARCHIVE_URL="https://download.tensorflow.org/models/object_detection/ssd_mobilenet_v1_coco_2017_11_17.tar.gz"
CONFIG_URL="https://raw.githubusercontent.com/opencv/opencv_extra/4.x/testdata/dnn/ssd_mobilenet_v1_coco_2017_11_17.pbtxt"

mkdir -p "$MODEL_DIR"

validate() {
  [[ -s "$MODEL" && -s "$CONFIG" ]] || return 1
  "$PY" - "$MODEL" "$CONFIG" <<'PY'
import sys
import cv2
model, config = sys.argv[1], sys.argv[2]
net = cv2.dnn.readNetFromTensorflow(model, config)
if net.empty():
    raise SystemExit("modelo DNN vazio")
print("Pré-filtro local: modelo SSD MobileNet carregado com sucesso.")
PY
}

if validate >/dev/null 2>&1; then
  echo "Pré-filtro local: modelo já instalado e válido."
  exit 0
fi

echo "Pré-filtro local: baixando modelo leve de detecção de objetos..."
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

curl -fL   --retry 3   --retry-delay 2   --connect-timeout 15   "$MODEL_ARCHIVE_URL"   -o "$tmp/model.tar.gz"

tar -xzf "$tmp/model.tar.gz" -C "$tmp"
source_model="$tmp/ssd_mobilenet_v1_coco_2017_11_17/frozen_inference_graph.pb"

if [[ ! -s "$source_model" ]]; then
  echo "ERRO: frozen_inference_graph.pb não encontrado no pacote baixado."
  exit 1
fi

curl -fL   --retry 3   --retry-delay 2   --connect-timeout 15   "$CONFIG_URL"   -o "$tmp/model.pbtxt"

if [[ ! -s "$tmp/model.pbtxt" ]]; then
  echo "ERRO: configuração pbtxt do pré-filtro não foi baixada."
  exit 1
fi

install -m 0644 "$source_model" "$MODEL"
install -m 0644 "$tmp/model.pbtxt" "$CONFIG"

if id moncoesai >/dev/null 2>&1; then
  chown -R moncoesai:moncoesai "$MODEL_DIR"
fi

validate
echo "Pré-filtro local instalado em $MODEL_DIR"
