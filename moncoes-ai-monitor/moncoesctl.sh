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
  prefilter)
    sqlite3 -header -column "$BASE/data/moncoes.db" "
      SELECT
        day,
        SUM(received) AS movimentos,
        SUM(passed) AS enviados_ia,
        SUM(skipped) AS descartados_local,
        SUM(fail_open) AS fail_open,
        CASE WHEN SUM(received)>0
             THEN ROUND(100.0*SUM(skipped)/SUM(received),1)
             ELSE 0 END AS economia_pct
      FROM prefilter_stats
      GROUP BY day
      ORDER BY day DESC
      LIMIT 14;
    "
    ;;
  ai-usage)
    sqlite3 -header -column "$BASE/data/moncoes.db" "
      SELECT
        stage,
        model,
        COUNT(*) AS chamadas,
        SUM(input_tokens) AS input_tokens,
        SUM(cached_input_tokens) AS cached_tokens,
        SUM(output_tokens) AS output_tokens,
        SUM(reasoning_tokens) AS reasoning_tokens
      FROM ai_usage
      GROUP BY stage,model
      ORDER BY chamadas DESC;
    "
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
    echo "Uso: moncoesctl {status|logs [N]|follow|network|health|report-daily|report-weekly|prefilter|ai-usage|observe|production}"
    exit 2
    ;;
esac
