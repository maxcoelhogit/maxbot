#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Execute com: sudo bash update.sh"
  exit 1
fi

PKG_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE="/opt/moncoes-ai"
SERVICE_USER="moncoesai"

if [[ ! -d "$BASE/app" || ! -f /etc/moncoes-ai/moncoes.env ]]; then
  echo "Instalação existente não encontrada. Use install.sh primeiro."
  exit 1
fi

echo "Atualizando Monções AI Monitor sem alterar credenciais, banco ou evidências..."

systemctl stop moncoes-ai-monitor.service || true

cp -r "$PKG_DIR/app/." "$BASE/app/"
cp -r "$PKG_DIR/config/." "$BASE/config/"
cp "$PKG_DIR/requirements.txt" "$BASE/requirements.txt"

"$BASE/venv/bin/pip" install -q -r "$BASE/requirements.txt"
"$BASE/venv/bin/python" -m py_compile "$BASE/app/"*.py

chown -R "$SERVICE_USER:$SERVICE_USER" "$BASE/app" "$BASE/config"
systemctl restart moncoes-ai-monitor.service
sleep 3

echo
systemctl --no-pager --full status moncoes-ai-monitor.service || true
echo
echo "Atualização concluída."
