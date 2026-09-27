const TOKEN_KEY="moncoes_device_token";
const $=id=>document.getElementById(id);
let deferredPrompt=null;

function authHeaders(extra={}) {
  const token=localStorage.getItem(TOKEN_KEY);
  return {...extra,...(token?{Authorization:"Bearer "+token}:{})};
}
async function api(path,opts={}) {
  opts.headers=authHeaders(opts.headers||{});
  const r=await fetch(path,opts);
  if(r.status===401){localStorage.removeItem(TOKEN_KEY);showJoin();throw new Error("Não autorizado");}
  if(!r.ok){let msg="Erro "+r.status;try{const j=await r.json();msg=j.detail||msg}catch{}throw new Error(msg)}
  const ct=r.headers.get("content-type")||"";
  return ct.includes("application/json")?r.json():r;
}
function showJoin(){
  $("joinCard").classList.remove("hidden");$("appArea").classList.add("hidden");
}
function showApp(){
  $("joinCard").classList.add("hidden");$("appArea").classList.remove("hidden");
}
function fmtTime(s){
  if(!s)return "—";try{return new Date(s).toLocaleString("pt-BR")}catch{return s}
}
async function registerDevice(){
  $("joinMsg").textContent="";
  try{
    const r=await fetch("/api/register",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
      invite_code:$("inviteCode").value.trim(),device_name:$("deviceName").value.trim()
    })});
    const j=await r.json();if(!r.ok)throw new Error(j.detail||"Falha ao autorizar");
    localStorage.setItem(TOKEN_KEY,j.token);showApp();await boot();
  }catch(e){$("joinMsg").textContent=e.message}
}
function urlB64ToUint8Array(base64String){
  const padding="=".repeat((4-base64String.length%4)%4);
  const base64=(base64String+padding).replace(/-/g,"+").replace(/_/g,"/");
  const raw=atob(base64);return Uint8Array.from([...raw].map(c=>c.charCodeAt(0)));
}
async function enablePush(){
  if(!("serviceWorker" in navigator)||!("PushManager" in window)){alert("Este navegador não suporta notificações Web Push.");return}
  const cfg=await fetch("/api/config").then(r=>r.json());
  if(!cfg.vapid_public_key){alert("Push ainda não está configurado no servidor.");return}
  const perm=await Notification.requestPermission();
  if(perm!=="granted"){$("pushState").textContent="Notificações não autorizadas neste aparelho.";return}
  const reg=await navigator.serviceWorker.ready;
  let sub=await reg.pushManager.getSubscription();
  if(!sub)sub=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:urlB64ToUint8Array(cfg.vapid_public_key)});
  await api("/api/push/subscribe",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(sub.toJSON())});
  $("pushState").textContent="Notificações críticas ativadas neste aparelho.";
  $("pushBtn").textContent="Notificações ativadas";
}
async function loadStatus(){
  try{
    const s=await api("/api/status");
    const p=s.payload||{};const targets=p.targets||{};
    const healthy=!!p.healthy;
    $("healthTitle").textContent=healthy?"Monitor operacional":"Atenção necessária";
    $("onlineBadge").textContent=healthy?"Online":"Verificar";
    $("onlineBadge").className="badge "+(healthy?"good":"bad");
    const rows=[["VPN",p.wireguard_handshake_age_seconds!=null&&p.wireguard_handshake_age_seconds<180],["Roteador",targets["192.168.10.1:443"]],["DVR 100",targets["192.168.10.100:554"]&&targets["192.168.10.100:37777"]],["DVR 101",targets["192.168.10.101:554"]&&targets["192.168.10.101:37777"]]];
    $("healthList").innerHTML=rows.map(([n,ok])=>`<div class="health-item"><span class="dot ${ok?"ok":"no"}"></span>${n}</div>`).join("");
    $("healthTime").textContent=s.received_at?"Última atualização: "+fmtTime(s.received_at):"Aguardando primeira atualização do monitor.";
  }catch(e){$("onlineBadge").textContent="Offline";$("onlineBadge").className="badge bad"}
}
async function loadEvents(){
  try{
    const rows=await api("/api/events?limit=40");
    if(!rows.length){$("events").innerHTML='<p class="subtle">Nenhum evento sincronizado ainda.</p>';return}
    $("events").innerHTML=rows.map(e=>`<div class="item">
      <div class="item-top"><div><h3>${esc(e.camera||("Câmera "+(e.channel||"")))} </h3><p>${fmtTime(e.occurred_at||e.received_at)}</p></div><span class="pill ${esc(e.status||"uncertain")}">${labelStatus(e.status)}</span></div>
      <p>${esc(e.description||e.category||"Evento monitorado")}</p>
      ${e.rule_reference?`<p><strong>Regra:</strong> ${esc(e.rule_reference)}</p>`:""}
      ${e.status==="critical"&&!e.acknowledged_at?`<button class="small secondary ack" onclick="ack(${e.id})">Reconhecer alerta</button>`:""}
      ${e.acknowledged_at?`<p>✓ Reconhecido por ${esc(e.acknowledged_by_name||"usuário")} em ${fmtTime(e.acknowledged_at)}</p>`:""}
    </div>`).join("");
  }catch(e){$("events").innerHTML='<p class="subtle">Falha ao carregar eventos.</p>'}
}
async function ack(id){await api("/api/events/"+id+"/ack",{method:"POST"});await loadEvents()}
window.ack=ack;
function labelStatus(s){return {normal:"Normal",critical:"Crítico",potential_occurrence:"Potencial",uncertain:"Revisar"}[s]||"Evento"}
function esc(s){return String(s??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[m]))}
async function loadReports(){
  try{
    const rows=await api("/api/reports");
    if(!rows.length){$("reports").innerHTML='<p class="subtle">Nenhum relatório sincronizado ainda.</p>';return}
    $("reports").innerHTML=rows.map(r=>`<div class="item"><div class="item-top"><div><h3>Relatório ${r.kind==="weekly"?"semanal":"diário"}</h3><p>${fmtTime(r.created_at)}</p></div><button class="small secondary" onclick="downloadReport(${r.id},'${esc(r.filename)}')">Abrir PDF</button></div></div>`).join("");
  }catch(e){$("reports").innerHTML='<p class="subtle">Falha ao carregar relatórios.</p>'}
}
async function downloadReport(id,name){
  try{
    const link=await api("/api/reports/"+id+"/link",{method:"POST"});
    window.location.href=link.url;
  }catch(e){
    alert("Não foi possível baixar o relatório: "+(e.message||"erro desconhecido"));
  }
}
window.downloadReport=downloadReport;
async function testPush(){
  try{
    const r=await api("/api/push/test",{method:"POST"});
    $("pushState").textContent="Teste enviado. Verifique a notificação do sistema.";
    if(r.sent<1)alert("Nenhuma notificação foi enviada.");
  }catch(e){
    const msg=typeof e.message==="string"?e.message:"Falha no teste";
    alert("Teste de notificação falhou: "+msg);
  }
}
async function boot(){
  if(!localStorage.getItem(TOKEN_KEY)){showJoin();return}
  showApp();
  try{
    const me=await api("/api/me");
    $("deviceTitle").textContent=me.name;
    if(me.push_subscribed){
      $("pushState").textContent="Assinatura push registrada neste aparelho.";
      $("pushBtn").textContent="Notificações ativadas";
    }else if("Notification" in window&&Notification.permission==="granted"){
      $("pushState").textContent="Permissão concedida, mas a assinatura push ainda não está registrada.";
    }
  }catch{return}
  if("serviceWorker" in navigator){await navigator.serviceWorker.register("/sw.js")}
  await Promise.all([loadStatus(),loadEvents(),loadReports()]);
}
window.addEventListener("beforeinstallprompt",e=>{e.preventDefault();deferredPrompt=e;$("installBtn").classList.remove("hidden")});
const isStandalone=window.matchMedia("(display-mode: standalone)").matches||window.navigator.standalone===true;
const isIOS=/iphone|ipad|ipod/i.test(navigator.userAgent);
if(!isStandalone&&isIOS)$("installBtn").classList.remove("hidden");
$("installBtn").addEventListener("click",async()=>{if(deferredPrompt){deferredPrompt.prompt();await deferredPrompt.userChoice;deferredPrompt=null;$("installBtn").classList.add("hidden")}else{alert("No iPhone/iPad: toque em Compartilhar e depois em “Adicionar à Tela de Início”. No Android: use o menu do navegador e escolha “Instalar aplicativo”.")}});
$("joinBtn").addEventListener("click",registerDevice);
$("pushBtn").addEventListener("click",enablePush);
$("testPushBtn").addEventListener("click",testPush);
$("refreshBtn").addEventListener("click",async()=>Promise.all([loadStatus(),loadEvents(),loadReports()]));
boot();
setInterval(()=>{if(localStorage.getItem(TOKEN_KEY))loadStatus()},60000);
