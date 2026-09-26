# Monções AI Monitor

Monitor de CFTV do Condomínio Edifício Monções.

## Arquitetura
- VM Google Cloud conectada ao ER605 por WireGuard `wg0`
- DVR `.100`: `VideoMotion` em tempo real via `eventManager.cgi`
- DVR `.101`: consulta periódica via `mediaFileFind.cgi`
- FFmpeg: 4 frames por evento e montagem 2x2
- OpenAI Responses API: triagem com GPT-5.6 Luna; segunda revisão com GPT-5.6 Terra somente em eventos não triviais
- SQLite local, evidências selecionadas e relatórios PDF
- systemd: restart automático, health checks, relatórios e retenção

## Segurança
- Instala inicialmente em `MONCOES_MODE=observe`
- Nenhuma advertência/multa é automática
- Não faz reconhecimento facial nem identificação de pessoas
- Credenciais ficam fora do GitHub, em `/etc/moncoes-ai/moncoes.env`
- Antes da produção, criar usuário dedicado nos DVRs e trocar a senha de admin usada nos testes

## Instalação na VM
```bash
git clone https://github.com/maxcoelhogit/maxbot.git
cd maxbot/moncoes-ai-monitor
sudo bash install.sh
```

## Operação
```bash
sudo moncoesctl status
sudo moncoesctl network
sudo moncoesctl health
sudo moncoesctl logs 200
sudo moncoesctl follow
sudo moncoesctl report-daily
sudo moncoesctl report-weekly
sudo moncoesctl observe
sudo moncoesctl production
```

Relatórios: `/opt/moncoes-ai/reports`
