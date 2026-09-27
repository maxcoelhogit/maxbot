const $=id=>document.getElementById(id);
let deferredPrompt=null;
let currentSubscription=null;

function esc(s){return String(s??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[m]))}
function fmtTime(s){if(!s)return "—";try{return new Date(s).toLocaleString("pt-BR",{day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"})}catch{return s}}
function urlB64ToUint8Array(v){const p="=".repeat((4-v.length%4)%4);const b=(v+p).replace(/-/g,"+").replace(/_/g,"/");const raw=atob(b);return Uint8Array.from([...raw].map(c=>c.charCodeAt(0)))}

async function jsonFetch(path,opts={}){
  const r=await fetch(path,opts);
  if(!r.ok){let msg="Erro "+r.status;try{const j=await r.json();msg=typeof j.detail==="string"?j.detail:msg}catch{}throw new Error(msg)}
  return r.json();
}

async function ensureServiceWorker(){
  if(!("serviceWorker" in navigator))return null;
  return navigator.serviceWorker.register("/seguranca/sw.js",{scope:"/seguranca/"});
}

async function getSubscription(){
  if(!("serviceWorker" in navigator)||!("PushManager" in window))return null;
  const reg=await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}

function renderPushState(active){
  const card=$("pushCard");
  if(active){
    card.classList.add("enabled");
    $("pushIcon").textContent="✓";
    $("pushTitle").textContent="Alertas críticos ativados";
    $("pushText").textContent="Este aparelho está inscrito para receber alertas críticos do Monções Segurança.";
    $("pushBtn").textContent="✓ Alertas ativados";
    $("pushBtn").disabled=true;
    $("notificationCheck").textContent="✓";
    $("notificationCheck").className="status-icon ok";
    $("notificationText").textContent="Ativados neste aparelho";
    $("testBtn").classList.remove("hidden");
  }else{
    card.classList.remove("enabled");
    $("pushIcon").textContent="!";
    $("pushTitle").textContent="Ative os alertas críticos";
    $("pushText").textContent="Receba no celular avisos de situações críticas identificadas pelo monitoramento.";
    $("pushBtn").textContent="Ativar alertas";
    $("pushBtn").disabled=false;
    $("notificationCheck").textContent="•";
    $("notificationCheck").className="status-icon neutral";
    $("notificationText").textContent=("Notification" in window&&Notification.permission==="denied")?"Bloqueados pelo aparelho":"Ainda não ativados";
  }
}

async function enablePush(){
  if(!("Notification" in window)||!("PushManager" in window)){
    alert("Este navegador não oferece suporte às notificações necessárias. Tente instalar o app pela tela inicial ou use um navegador atualizado.");
    return;
  }
  try{
    await ensureServiceWorker();
    const perm=await Notification.requestPermission();
    if(perm!=="granted"){renderPushState(false);alert("As notificações precisam ser permitidas para receber alertas críticos.");return}
    const cfg=await jsonFetch("/public/api/config");
    const reg=await navigator.serviceWorker.ready;
    let sub=await reg.pushManager.getSubscription();
    if(!sub){
      sub=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:urlB64ToUint8Array(cfg.vapid_public_key)});
    }
    await jsonFetch("/public/api/push/subscribe",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(sub.toJSON())});
    currentSubscription=sub;
    renderPushState(true);
  }catch(e){alert("Não foi possível ativar os alertas: "+e.message)}
}

async function testPush(){
  try{
    currentSubscription=currentSubscription||await getSubscription();
    if(!currentSubscription){alert("Ative os alertas primeiro.");return}
    await jsonFetch("/public/api/push/test",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({endpoint:currentSubscription.endpoint})});
  }catch(e){alert("Não foi possível enviar o teste: "+e.message)}
}

async function loadStatus(){
  try{
    const s=await jsonFetch("/public/api/status");
    $("metricAnalyzed").textContent=s.today.analyzed;
    $("metricReview").textContent=s.today.sent_for_review;
    $("metricCritical").textContent=s.today.critical;
    if(s.monitor_active){
      $("liveBadge").className="live-badge active";$("liveBadge").innerHTML='<span class="pulse"></span>Monitoramento ativo';
      $("monitorTitle").textContent="Monitoramento IA ativo";$("monitorText").textContent="Sistema operacional e acompanhando eventos";
    }else{
      $("liveBadge").className="live-badge down";$("liveBadge").innerHTML='<span class="pulse"></span>Verificação necessária';
      $("monitorTitle").textContent="Monitoramento em verificação";$("monitorText").textContent="A administração pode estar verificando o sistema";
    }
  }catch{
    $("liveBadge").className="live-badge down";$("liveBadge").innerHTML='<span class="pulse"></span>Sem atualização';
    $("monitorText").textContent="Não foi possível consultar o estado agora";
  }
}

function renderEvents(rows){
  const target=$("events");
  if(!rows.length){target.innerHTML='<div class="loading-card">Ainda não há eventos para exibir.</div>';return}
  const wanted=new URLSearchParams(location.search).get("event");
  target.innerHTML=rows.map(e=>`<article id="event-${e.id}" class="event ${esc(e.status)} ${String(e.id)===wanted?"highlight":""}">
    <div class="event-head"><div class="event-location">${esc(e.location)}</div><div class="event-time">${fmtTime(e.occurred_at)}</div></div>
    <p class="event-desc">${esc(e.description||"Evento analisado pela IA.")}</p>
    <span class="event-status">${e.status==="critical"?"🚨 ":"✓ "}${esc(e.status_label)}</span>
  </article>`).join("");
  if(wanted){setTimeout(()=>document.getElementById("event-"+wanted)?.scrollIntoView({behavior:"smooth",block:"center"}),120)}
}

async function loadEvents(){
  try{renderEvents(await jsonFetch("/public/api/events?limit=50"))}
  catch{$("events").innerHTML='<div class="loading-card">Não foi possível atualizar a atividade agora.</div>'}
}

function showMaxAlertInfo(){
  const ua=navigator.userAgent.toLowerCase();
  const ios=/iphone|ipad|ipod/.test(ua);
  const android=/android/.test(ua);
  let html="";
  if(ios){
    html=`<p>Para receber Web Push no iPhone/iPad, use o Monções Segurança instalado na Tela de Início e mantenha as notificações permitidas.</p>
    <ul><li>Adicione o app à Tela de Início.</li><li>Ative as notificações quando solicitado.</li><li>Em Ajustes → Notificações, mantenha sons e alertas habilitados.</li></ul>
    <p><strong>Importante:</strong> uma PWA não consegue ignorar o modo Silencioso ou Foco do iPhone como um alerta governamental.</p>`;
  }else if(android){
    html=`<p>Para deixar o aviso o mais forte possível no Android:</p>
    <ul><li>Mantenha as notificações deste app/site permitidas.</li><li>Nas configurações de notificações, escolha som e vibração fortes quando o aparelho oferecer essa opção.</li><li>Evite restringir o navegador/app pela economia de bateria.</li><li>Se o aparelho permitir prioridade ou exceção ao Não Perturbe para este canal, você pode habilitá-la manualmente.</li></ul>
    <p><strong>Importante:</strong> o Android continua no controle do modo Silencioso/Não Perturbe; o site não pode obrigar o aparelho a ignorá-los.</p>`;
  }else{
    html=`<p>Mantenha as notificações permitidas, com som e vibração habilitados nas configurações do navegador ou do sistema.</p><p>O sistema operacional decide se uma notificação pode tocar durante Silencioso/Não Perturbe.</p>`;
  }
  $("modalBody").innerHTML=html;$("modal").classList.remove("hidden");
}
function closeModal(){$("modal").classList.add("hidden")}

async function installApp(){
  if(deferredPrompt){
    deferredPrompt.prompt();await deferredPrompt.userChoice;deferredPrompt=null;return;
  }
  const ios=/iphone|ipad|ipod/i.test(navigator.userAgent);
  if(ios){alert("No iPhone/iPad: toque em Compartilhar e depois em “Adicionar à Tela de Início”.")}
  else{alert("Abra o menu do navegador e escolha “Instalar aplicativo” ou “Adicionar à tela inicial”.")}
}

async function boot(){
  await ensureServiceWorker();
  if("Notification" in window&&Notification.permission==="granted"){
    currentSubscription=await getSubscription();
    if(currentSubscription){
      try{
        await jsonFetch("/public/api/push/subscribe",{
          method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify(currentSubscription.toJSON())
        });
      }catch{}
    }
  }
  renderPushState(!!currentSubscription);
  const isStandalone=window.matchMedia("(display-mode: standalone)").matches||window.navigator.standalone===true;
  if(isStandalone)$("installBtn").classList.add("hidden");
  await Promise.all([loadStatus(),loadEvents()]);
}
window.addEventListener("beforeinstallprompt",e=>{e.preventDefault();deferredPrompt=e});
$("pushBtn").addEventListener("click",enablePush);
$("testBtn").addEventListener("click",testPush);
$("installBtn").addEventListener("click",installApp);
$("maxAlertBtn").addEventListener("click",showMaxAlertInfo);
$("closeModal").addEventListener("click",closeModal);$("modalOk").addEventListener("click",closeModal);
$("modal").addEventListener("click",e=>{if(e.target===$("modal"))closeModal()});
$("refreshBtn").addEventListener("click",()=>Promise.all([loadStatus(),loadEvents()]));
boot();
setInterval(()=>Promise.all([loadStatus(),loadEvents()]),45000);
