self.addEventListener("install",()=>self.skipWaiting());
self.addEventListener("activate",e=>e.waitUntil(self.clients.claim()));
self.addEventListener("push",event=>{
  let data={title:"Monções Segurança",body:"Novo alerta crítico de segurança.",url:"/seguranca/"};
  try{if(event.data)data={...data,...event.data.json()}}catch{}
  event.waitUntil(self.registration.showNotification(data.title,{
    body:data.body,
    icon:"/seguranca/icon.svg",
    badge:"/seguranca/icon.svg",
    tag:"moncoes-critical-"+Date.now(),
    renotify:true,
    requireInteraction:true,
    silent:false,
    vibrate:[900,220,900,220,1400,300,1400],
    data:{url:data.url||"/seguranca/"}
  }));
});
self.addEventListener("notificationclick",event=>{
  event.notification.close();
  const url=new URL(event.notification.data?.url||"/seguranca/",self.location.origin).href;
  event.waitUntil((async()=>{
    const all=await clients.matchAll({type:"window",includeUncontrolled:true});
    for(const c of all){
      if("focus" in c){await c.navigate(url);return c.focus()}
    }
    return clients.openWindow(url);
  })());
});
