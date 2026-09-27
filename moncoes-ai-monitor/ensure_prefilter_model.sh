#!/usr/bin/env bash
set -Eeuo pipefail

BASE="${MONCOES_BASE:-/opt/moncoes-ai}"
MODEL_DIR="$BASE/models"
MODEL="$MODEL_DIR/ssd_mobilenet_v1_coco_2017_11_17.pb"
CONFIG="$MODEL_DIR/ssd_mobilenet_v1_coco_2017_11_17.pbtxt"
PY="$BASE/venv/bin/python"

MODEL_MIRROR_URL="https://mirror.opencv.ai/ssd_mobilenet_v1_coco_2017_11_17.pb"
MODEL_MIRROR_SHA1="9e4bcdd98f4c6572747679e4ce570de4f03a70e2"
MODEL_ARCHIVE_URL="http://download.tensorflow.org/models/object_detection/ssd_mobilenet_v1_coco_2017_11_17.tar.gz"
MODEL_ARCHIVE_SHA1="6157ddb6da55db2da89dd561eceb7f944928e317"
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

download_model() {
  echo "Tentando espelho oficial do OpenCV..."
  if curl -fL \
      --retry 3 \
      --retry-delay 2 \
      --connect-timeout 15 \
      "$MODEL_MIRROR_URL" \
      -o "$tmp/model.pb"; then
    if echo "$MODEL_MIRROR_SHA1  $tmp/model.pb" | sha1sum -c -; then
      return 0
    fi
    echo "Checksum do espelho OpenCV não confere; descartando arquivo."
    rm -f "$tmp/model.pb"
  fi

  echo "Espelho indisponível; usando origem TensorFlow com verificação SHA1..."
  # A origem histórica deste modelo usa HTTP. Não aceitamos o arquivo
  # silenciosamente: o SHA1 publicado no manifesto do OpenCV é obrigatório.
  curl -fL \
    --retry 3 \
    --retry-delay 2 \
    --connect-timeout 15 \
    "$MODEL_ARCHIVE_URL" \
    -o "$tmp/model.tar.gz"

  echo "$MODEL_ARCHIVE_SHA1  $tmp/model.tar.gz" | sha1sum -c -
  tar -xzf "$tmp/model.tar.gz" -C "$tmp"

  source_model="$tmp/ssd_mobilenet_v1_coco_2017_11_17/frozen_inference_graph.pb"
  if [[ ! -s "$source_model" ]]; then
    echo "ERRO: frozen_inference_graph.pb não encontrado no pacote baixado."
    return 1
  fi
  cp "$source_model" "$tmp/model.pb"
  echo "$MODEL_MIRROR_SHA1  $tmp/model.pb" | sha1sum -c -
}

download_model

curl -fL \
  --retry 3 \
  --retry-delay 2 \
  --connect-timeout 15 \
  "$CONFIG_URL" \
  -o "$tmp/model.pbtxt"

if [[ ! -s "$tmp/model.pbtxt" ]]; then
  echo "ERRO: configuração pbtxt do pré-filtro não foi baixada."
  exit 1
fi

install -m 0644 "$tmp/model.pb" "$MODEL"
install -m 0644 "$tmp/model.pbtxt" "$CONFIG"

if id moncoesai >/dev/null 2>&1; then
  chown -R moncoesai:moncoesai "$MODEL_DIR"
fi

validate
echo "Pré-filtro local instalado em $MODEL_DIR"
