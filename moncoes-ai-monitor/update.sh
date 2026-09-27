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

# Valida o novo código e prepara dependências/modelo antes de interromper
# o serviço atual. Assim uma falha de download não deixa o monitor parado.
python3 -m py_compile "$PKG_DIR/app/"*.py
"$BASE/venv/bin/pip" install -q -r "$PKG_DIR/requirements.txt"
MONCOES_BASE="$BASE" bash "$PKG_DIR/ensure_prefilter_model.sh"

BACKUP_DIR="$(mktemp -d)"
trap 'rm -rf "$BACKUP_DIR"' EXIT
cp -a "$BASE/app" "$BACKUP_DIR/app"
cp -a "$BASE/config" "$BACKUP_DIR/config"

systemctl stop moncoes-ai-monitor.service || true

cp -r "$PKG_DIR/app/." "$BASE/app/"
cp -r "$PKG_DIR/config/." "$BASE/config/"
cp "$PKG_DIR/requirements.txt" "$BASE/requirements.txt"

"$BASE/venv/bin/python" -m py_compile "$BASE/app/"*.py

mkdir -p "$BASE/models"
chown -R "$SERVICE_USER:$SERVICE_USER" "$BASE/app" "$BASE/config" "$BASE/models"
systemctl restart moncoes-ai-monitor.service

ok=0
for _ in {1..10}; do
  if systemctl is-active --quiet moncoes-ai-monitor.service; then
    ok=1
    break
  fi
  sleep 1
done

if [[ "$ok" -ne 1 ]]; then
  echo "Nova versão não permaneceu ativa. Restaurando versão anterior..."
  systemctl stop moncoes-ai-monitor.service || true
  rm -rf "$BASE/app" "$BASE/config"
  cp -a "$BACKUP_DIR/app" "$BASE/app"
  cp -a "$BACKUP_DIR/config" "$BASE/config"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$BASE/app" "$BASE/config"
  systemctl restart moncoes-ai-monitor.service || true
  echo "Rollback concluído. O monitor anterior foi restaurado."
  systemctl --no-pager --full status moncoes-ai-monitor.service || true
  exit 1
fi

echo
systemctl --no-pager --full status moncoes-ai-monitor.service || true
echo
echo "Atualização concluída com verificação automática e rollback de segurança."
