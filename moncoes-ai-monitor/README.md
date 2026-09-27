# Monções AI Monitor

Monitor de CFTV do Condomínio Edifício Monções.

## Arquitetura
- VM Google Cloud conectada ao ER605 por WireGuard `wg0`
- DVR `.100`: `VideoMotion` em tempo real via `eventManager.cgi`
- DVR `.101`: consulta periódica via `mediaFileFind.cgi`
- FFmpeg: 4 frames por evento e montagem 2x2
- Pré-filtro local SSD MobileNet/COCO usado como filtro barato antes da OpenAI
- Gatilho semântico principal: pessoa. Em áreas internas, cão/gato também podem acionar análise para preservar a regra operacional de animal solto; bicicletas e veículos sozinhos são apenas contexto
- Câmeras da rua: somente pessoa libera análise; carros, motos, caminhões, bicicletas e animais da via pública são ignorados como gatilho
- Acesso de veículos: a câmera frontal do portão é a única referência estrutural do portão de garagem; passagem de veículo na rua não basta, mas abertura/fechamento/estado diferente do portão pode liberar análise
- Entrada social: a câmera de entrada/caixas de correio também observa mudança estrutural do acesso social
- Hall/elevador: movimento da porta do elevador não é gatilho estrutural; a câmera do hall e a câmera interna do elevador dependem de presença relevante
- Câmeras internas da garagem não usam veículos isolados como gatilho, reduzindo chamadas duplicadas quando o mesmo portão aparece em mais de uma câmera
- Fail-open: se o pré-filtro local ou a referência estrutural falhar, o evento segue para a IA em vez de ser perdido
- OpenAI Responses API com cascata transparente de custo: GPT-6 Luna em triagem econômica; GPT-6 Luna em revisão de alta qualidade; GPT-5.6 Terra reservado para casos críticos, incertos ou sensíveis
- A triagem usa reasoning mínimo e processamento Standard; casos não triviais preservam revisão de alta qualidade e fallback automático
- Prompt caching é reutilizado pela família GPT-6 e o uso de tokens é registrado localmente para auditoria de custo
- SQLite local, evidências selecionadas e relatórios PDF
- systemd: restart automático, health checks, relatórios e retenção

## Segurança
- Instala inicialmente em `MONCOES_MODE=observe`
- Nenhuma advertência/multa é automática
- Não faz reconhecimento facial nem identificação de pessoas
- Credenciais ficam fora do GitHub, em `/etc/moncoes-ai/moncoes.env`
- Mudanças estruturais são habilitadas somente nos acessos relevantes; a porta do elevador é explicitamente excluída dessa lógica
- A otimização de custo não altera endpoints, banco do portal, notificações, PWA, relatórios ou experiência dos moradores
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
