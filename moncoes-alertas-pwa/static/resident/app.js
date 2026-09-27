const $=id=>document.getElementById(id);
let deferredPrompt=null;
let currentSubscription=null;
let cachedEvents=[];

function esc(s){
  return String(s??"").replace(/[&<>"']/g,m=>({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"
  }[m]));
}
function fmtTime(s){
  if(!s)return "—";
  try{
    return new Date(s).toLocaleString("pt-BR",{
      day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"
    });
  }catch{return s}
}
function urlB64ToUint8Array(v){
  const p="=".repeat((4-v.length%4)%4);
  const b=(v+p).replace(/-/g,"+").replace(/_/g,"/");
  const raw=atob(b);
  return Uint8Array.from([...raw].map(c=>c.charCodeAt(0)));
}
async function jsonFetch(path,opts={}){
  const r=await fetch(path,opts);
  if(!r.ok){
    let msg="Erro "+r.status;
    try{
      const j=await r.json();
      msg=typeof j.detail==="string"?j.detail:msg;
    }catch{}
    throw new Error(msg);
  }
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

function selectTab(name,scroll=true){
  document.querySelectorAll(".tab").forEach(btn=>{
    btn.classList.toggle("active",btn.dataset.tab===name);
  });
  document.querySelectorAll(".tab-panel").forEach(panel=>{
    panel.classList.toggle("active",panel.dataset.panel===name);
  });
  if(scroll)window.scrollTo({top:0,behavior:"smooth"});
}
document.querySelectorAll(".tab").forEach(btn=>{
  btn.addEventListener("click",()=>selectTab(btn.dataset.tab));
});
$("seeAllBtn").addEventListener("click",()=>selectTab("eventos"));

function renderPushState(active){
  const card=$("pushCard");
  if(active){
    card.classList.add("enabled");
    $("pushIcon").textContent="✓";
    $("pushTitle").textContent="Alertas críticos ativados";
    $("pushText").textContent="Este aparelho está inscrito para receber alertas críticos apropriados para comunicação aos moradores.";
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
    $("pushText").textContent="Receba avisos no celular quando o sistema identificar uma situação crítica apropriada para comunicação aos moradores.";
    $("pushBtn").textContent="Ativar alertas";
    $("pushBtn").disabled=false;
    $("notificationCheck").textContent="•";
    $("notificationCheck").className="status-icon neutral";
    const denied=("Notification" in window&&Notification.permission==="denied");
    $("notificationText").textContent=denied?"Bloqueados pelo aparelho":"Ainda não ativados";
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
    if(perm!=="granted"){
      renderPushState(false);
      alert("As notificações precisam ser permitidas para receber alertas críticos.");
      return;
    }
    const cfg=await jsonFetch("/public/api/config");
    const reg=await navigator.serviceWorker.ready;
    let sub=await reg.pushManager.getSubscription();
    if(!sub){
      sub=await reg.pushManager.subscribe({
        userVisibleOnly:true,
        applicationServerKey:urlB64ToUint8Array(cfg.vapid_public_key)
      });
    }
    await jsonFetch("/public/api/push/subscribe",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(sub.toJSON())
    });
    currentSubscription=sub;
    renderPushState(true);
  }catch(e){
    alert("Não foi possível ativar os alertas: "+e.message);
  }
}
async function testPush(){
  try{
    currentSubscription=currentSubscription||await getSubscription();
    if(!currentSubscription){
      alert("Ative os alertas primeiro.");
      return;
    }
    await jsonFetch("/public/api/push/test",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({endpoint:currentSubscription.endpoint})
    });
  }catch(e){
    alert("Não foi possível enviar o teste: "+e.message);
  }
}

async function loadStatus(){
  try{
    const s=await jsonFetch("/public/api/status");
    $("metricAnalyzed").textContent=s.today.analyzed;
    $("metricReview").textContent=s.today.sent_for_review;
    $("metricCritical").textContent=s.today.critical;

    if(s.monitor_state==="active"){
      $("liveBadge").className="live-badge active";
      $("liveBadge").innerHTML='<span class="pulse"></span>Monitoramento ativo';
      $("monitorTitle").textContent="Monitoramento IA ativo";
      $("monitorText").textContent="Sistema operacional e acompanhando eventos";
    }else if(s.monitor_state==="unavailable"){
      $("liveBadge").className="live-badge neutral";
      $("liveBadge").innerHTML='<span class="pulse"></span>Status indisponível';
      $("monitorTitle").textContent="Status do monitoramento indisponível";
      $("monitorText").textContent="A administração recebe o diagnóstico técnico do sistema";
    }else{
      $("liveBadge").className="live-badge waiting";
      $("liveBadge").innerHTML='<span class="pulse"></span>Atualizando status';
      $("monitorTitle").textContent="Monitoramento em atualização";
      $("monitorText").textContent="Aguardando a próxima confirmação automática do sistema";
    }
  }catch{
    $("liveBadge").className="live-badge neutral";
    $("liveBadge").innerHTML='<span class="pulse"></span>Status indisponível';
    $("monitorTitle").textContent="Status momentaneamente indisponível";
    $("monitorText").textContent="Isso não representa, por si só, interrupção do monitoramento";
  }
}

function eventIcon(status){
  if(status==="critical")return "🚨 ";
  if(status==="potential_occurrence"||status==="uncertain")return "◉ ";
  return "✓ ";
}
function renderEventCards(rows,targetId){
  const target=$(targetId);
  if(!rows.length){
    target.innerHTML='<div class="loading-card">Ainda não há eventos para exibir.</div>';
    return;
  }
  const wanted=new URLSearchParams(location.search).get("event");
  target.innerHTML=rows.map(e=>`<article id="${targetId}-event-${e.id}" class="event ${esc(e.status)} ${String(e.id)===wanted?"highlight":""}">
    <div class="event-head">
      <div class="event-location">${esc(e.location)}</div>
      <div class="event-time">${fmtTime(e.occurred_at)}</div>
    </div>
    <p class="event-desc">${esc(e.description||"Evento analisado pela IA.")}</p>
    <span class="event-status">${eventIcon(e.status)}${esc(e.status_label)}</span>
  </article>`).join("");
}
async function loadEvents(){
  try{
    cachedEvents=await jsonFetch("/public/api/events?limit=50");
    renderEventCards(cachedEvents.slice(0,5),"homeEvents");
    renderEventCards(cachedEvents,"events");

    const wanted=new URLSearchParams(location.search).get("event");
    if(wanted){
      selectTab("eventos",false);
      setTimeout(()=>{
        document.getElementById("events-event-"+wanted)?.scrollIntoView({
          behavior:"smooth",block:"center"
        });
      },150);
    }
  }catch{
    $("homeEvents").innerHTML='<div class="loading-card">Não foi possível atualizar a atividade agora.</div>';
    $("events").innerHTML='<div class="loading-card">Não foi possível atualizar a atividade agora.</div>';
  }
}

function showMaxAlertInfo(){
  const ua=navigator.userAgent.toLowerCase();
  const ios=/iphone|ipad|ipod/.test(ua);
  const android=/android/.test(ua);
  let html="";
  if(ios){
    html=`<p>Para obter a melhor experiência no iPhone/iPad, instale o Monções Segurança na Tela de Início e mantenha as notificações permitidas.</p>
      <ul>
        <li>Adicione o app à Tela de Início.</li>
        <li>Ative as notificações quando solicitado.</li>
        <li>Em Ajustes → Notificações, mantenha sons e alertas habilitados.</li>
      </ul>
      <p><strong>Limite técnico:</strong> uma aplicação web não consegue obrigar o iPhone a ignorar o modo Silencioso ou Foco como um alerta governamental.</p>`;
  }else if(android){
    html=`<p>Para deixar o aviso o mais forte possível no Android:</p>
      <ul>
        <li>Mantenha as notificações deste app/site permitidas.</li>
        <li>Nas configurações de notificações, selecione som, vibração e alta prioridade quando o aparelho oferecer essas opções.</li>
        <li>Evite restringir o navegador/app pela economia de bateria.</li>
        <li>Se o aparelho permitir uma exceção ao Não Perturbe para o canal de notificações, ela precisa ser habilitada pelo próprio usuário.</li>
      </ul>
      <p><strong>Limite técnico:</strong> o Android continua no controle do modo Silencioso/Não Perturbe; o site não pode forçar a exceção.</p>`;
  }else{
    html=`<p>Mantenha as notificações permitidas, com som e vibração habilitados nas configurações do navegador ou do sistema.</p>
      <p>O sistema operacional decide se uma notificação pode tocar durante Silencioso/Não Perturbe.</p>`;
  }
  $("modalBody").innerHTML=html;
  $("modal").classList.remove("hidden");
}
function closeModal(){$("modal").classList.add("hidden")}

async function installApp(){
  if(deferredPrompt){
    deferredPrompt.prompt();
    await deferredPrompt.userChoice;
    deferredPrompt=null;
    return;
  }
  const ios=/iphone|ipad|ipod/i.test(navigator.userAgent);
  if(ios){
    alert("No iPhone/iPad: toque em Compartilhar e depois em “Adicionar à Tela de Início”.");
  }else{
    alert("Abra o menu do navegador e escolha “Instalar aplicativo” ou “Adicionar à tela inicial”.");
  }
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

  const standalone=window.matchMedia("(display-mode: standalone)").matches
    ||window.navigator.standalone===true;
  if(standalone)$("installBtn").classList.add("hidden");

  await Promise.all([loadStatus(),loadEvents()]);
}

window.addEventListener("beforeinstallprompt",e=>{
  e.preventDefault();
  deferredPrompt=e;
});
$("pushBtn").addEventListener("click",enablePush);
$("testBtn").addEventListener("click",testPush);
$("installBtn").addEventListener("click",installApp);
$("maxAlertBtn").addEventListener("click",showMaxAlertInfo);
$("closeModal").addEventListener("click",closeModal);
$("modalOk").addEventListener("click",closeModal);
$("modal").addEventListener("click",e=>{if(e.target===$("modal"))closeModal()});
$("refreshBtn").addEventListener("click",()=>Promise.all([loadStatus(),loadEvents()]));
$("homeRefreshBtn").addEventListener("click",()=>Promise.all([loadStatus(),loadEvents()]));

boot();
setInterval(()=>Promise.all([loadStatus(),loadEvents()]),45000);
