#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Execute com: sudo bash install.sh"
  exit 1
fi

PKG_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE="/opt/moncoes-ai"
ETC="/etc/moncoes-ai"
SERVICE_USER="moncoesai"

echo "============================================"
echo " Monções AI Monitor - instalação"
echo "============================================"

apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y   python3-venv python3-pip ffmpeg curl sqlite3   netcat-openbsd wireguard ca-certificates

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home "$BASE" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

mkdir -p   "$BASE/app" "$BASE/config" "$BASE/data/events" "$BASE/data/evidence"   "$BASE/reports" "$BASE/tmp" "$BASE/logs" "$ETC"

cp -r "$PKG_DIR/app/." "$BASE/app/"
cp -r "$PKG_DIR/config/." "$BASE/config/"
cp "$PKG_DIR/requirements.txt" "$BASE/requirements.txt"

python3 -m venv "$BASE/venv"
"$BASE/venv/bin/pip" install --upgrade pip wheel
"$BASE/venv/bin/pip" install -r "$BASE/requirements.txt"

echo
read -rp "Usuário dos DVRs [admin]: " DVR_USER
DVR_USER="${DVR_USER:-admin}"
read -rsp "Senha atual dos DVRs: " DVR_PASS
echo
read -rsp "OpenAI API key (Enter para instalar sem IA por enquanto): " OPENAI_API_KEY
echo
read -rp "Webhook para alertas críticos (opcional): " ALERT_WEBHOOK_URL

cat > "$ETC/moncoes.env" <<EOF
MONCOES_BASE=$BASE
MONCOES_MODE=observe
DVR_USER=$DVR_USER
DVR_PASS=$DVR_PASS
OPENAI_API_KEY=$OPENAI_API_KEY
OPENAI_MODEL=gpt-5.6-luna
OPENAI_REVIEW_MODEL=gpt-5.6-terra
EVENT_DEBOUNCE_SECONDS=90
DVR101_POLL_SECONDS=300
EVIDENCE_RETENTION_DAYS=30
REPORT_RETENTION_DAYS=365
ALERT_WEBHOOK_URL=$ALERT_WEBHOOK_URL
LOG_LEVEL=INFO
EOF

chown root:"$SERVICE_USER" "$ETC/moncoes.env"
chmod 640 "$ETC/moncoes.env"

chown -R "$SERVICE_USER:$SERVICE_USER" "$BASE"
chmod 750 "$BASE" "$BASE/app" "$BASE/config" "$BASE/data" "$BASE/reports" "$BASE/tmp"

timedatectl set-timezone America/Sao_Paulo || true

cat > /etc/systemd/system/moncoes-ai-monitor.service <<EOF
[Unit]
Description=Moncoes AI Monitor
After=network-online.target wg-quick@wg0.service
Wants=network-online.target wg-quick@wg0.service

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
EnvironmentFile=$ETC/moncoes.env
WorkingDirectory=$BASE/app
ExecStart=$BASE/venv/bin/python $BASE/app/monitor.py
Restart=always
RestartSec=5
TimeoutStopSec=20
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$BASE
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/moncoes-ai-health.service <<EOF
[Unit]
Description=Moncoes AI Health Check
After=wg-quick@wg0.service

[Service]
Type=oneshot
User=root
EnvironmentFile=$ETC/moncoes.env
WorkingDirectory=$BASE/app
ExecStart=$BASE/venv/bin/python $BASE/app/health.py
EOF

cat > /etc/systemd/system/moncoes-ai-health.timer <<'EOF'
[Unit]
Description=Moncoes AI health check every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/moncoes-ai-report-daily.service <<EOF
[Unit]
Description=Moncoes AI Daily Report

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SERVICE_USER
EnvironmentFile=$ETC/moncoes.env
WorkingDirectory=$BASE/app
ExecStart=$BASE/venv/bin/python $BASE/app/report.py --period daily
EOF

cat > /etc/systemd/system/moncoes-ai-report-daily.timer <<'EOF'
[Unit]
Description=Moncoes AI daily report

[Timer]
OnCalendar=*-*-* 07:05:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/moncoes-ai-report-weekly.service <<EOF
[Unit]
Description=Moncoes AI Weekly Report

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SERVICE_USER
EnvironmentFile=$ETC/moncoes.env
WorkingDirectory=$BASE/app
ExecStart=$BASE/venv/bin/python $BASE/app/report.py --period weekly
EOF

cat > /etc/systemd/system/moncoes-ai-report-weekly.timer <<'EOF'
[Unit]
Description=Moncoes AI weekly report

[Timer]
OnCalendar=Mon *-*-* 07:15:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/moncoes-ai-cleanup.service <<EOF
[Unit]
Description=Moncoes AI Retention Cleanup

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SERVICE_USER
EnvironmentFile=$ETC/moncoes.env
WorkingDirectory=$BASE/app
ExecStart=$BASE/venv/bin/python $BASE/app/cleanup.py
EOF

cat > /etc/systemd/system/moncoes-ai-cleanup.timer <<'EOF'
[Unit]
Description=Moncoes AI cleanup daily

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /usr/local/bin/moncoesctl <<'EOF'
#!/usr/bin/env bash
set -e

BASE="/opt/moncoes-ai"

case "${1:-status}" in
  status)
    systemctl --no-pager --full status moncoes-ai-monitor.service || true
    echo
    cat "$BASE/data/health.json" 2>/dev/null || true
    ;;
  logs)
    journalctl -u moncoes-ai-monitor.service -n "${2:-100}" --no-pager
    ;;
  follow)
    journalctl -u moncoes-ai-monitor.service -f
    ;;
  network)
    nc -zvw3 192.168.10.1 443
    nc -zvw3 192.168.10.100 554
    nc -zvw3 192.168.10.101 554
    nc -zvw3 192.168.10.100 37777
    nc -zvw3 192.168.10.101 37777
    ;;
  health)
    systemctl start moncoes-ai-health.service || true
    cat "$BASE/data/health.json"
    ;;
  report-daily)
    systemctl start moncoes-ai-report-daily.service
    ls -1t "$BASE/reports"/relatorio_daily_*.pdf | head -1
    ;;
  report-weekly)
    systemctl start moncoes-ai-report-weekly.service
    ls -1t "$BASE/reports"/relatorio_weekly_*.pdf | head -1
    ;;
  observe)
    sed -i 's/^MONCOES_MODE=.*/MONCOES_MODE=observe/' /etc/moncoes-ai/moncoes.env
    systemctl restart moncoes-ai-monitor.service
    echo "Modo observação ativado."
    ;;
  production)
    sed -i 's/^MONCOES_MODE=.*/MONCOES_MODE=production/' /etc/moncoes-ai/moncoes.env
    systemctl restart moncoes-ai-monitor.service
    echo "Modo produção ativado."
    ;;
  *)
    echo "Uso: moncoesctl {status|logs [N]|follow|network|health|report-daily|report-weekly|observe|production}"
    exit 2
    ;;
esac
EOF
chmod 755 /usr/local/bin/moncoesctl

systemctl daemon-reload
systemctl enable wg-quick@wg0.service || true
systemctl enable   moncoes-ai-monitor.service   moncoes-ai-health.timer   moncoes-ai-report-daily.timer   moncoes-ai-report-weekly.timer   moncoes-ai-cleanup.timer

echo
echo "== Teste de rede =="
for target in   "192.168.10.1 443"   "192.168.10.100 554"   "192.168.10.101 554"   "192.168.10.100 37777"   "192.168.10.101 37777"
do
  set -- $target
  nc -zvw3 "$1" "$2" || true
done

echo
echo "== Validação do código =="
"$BASE/venv/bin/python" -m py_compile "$BASE/app/"*.py

systemctl start   moncoes-ai-health.timer   moncoes-ai-report-daily.timer   moncoes-ai-report-weekly.timer   moncoes-ai-cleanup.timer

systemctl restart moncoes-ai-monitor.service
sleep 3

echo
echo "============================================"
echo " Instalação concluída em MODO OBSERVAÇÃO"
echo "============================================"
echo "Status:  sudo moncoesctl status"
echo "Logs:    sudo moncoesctl follow"
echo "Rede:    sudo moncoesctl network"
echo "Relatório de teste: sudo moncoesctl report-daily"
echo "Produção (somente após validação): sudo moncoesctl production"
