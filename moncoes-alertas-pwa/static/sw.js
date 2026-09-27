self.addEventListener("install",()=>self.skipWaiting());
self.addEventListener("activate",e=>e.waitUntil(self.clients.claim()));
self.addEventListener("push",event=>{
  let data={title:"Monções Alertas",body:"Novo alerta de segurança.",url:"/"};
  try{if(event.data)data={...data,...event.data.json()}}catch{}
  event.waitUntil(self.registration.showNotification(data.title,{
    body:data.body,
    icon:"/icon.svg",
    badge:"/icon.svg",
    tag:"moncoes-alert-"+Date.now(),
    renotify:true,
    requireInteraction:true,
    silent:false,
    vibrate:[800,250,800,250,1200],
    data:{url:data.url||"/"}
  }));
});
self.addEventListener("notificationclick",event=>{
  event.notification.close();
  const url=new URL(event.notification.data?.url||"/",self.location.origin).href;
  event.waitUntil((async()=>{
    const all=await clients.matchAll({type:"window",includeUncontrolled:true});
    for(const c of all){if("focus" in c){c.navigate(url);return c.focus()}}
    return clients.openWindow(url);
  })());
});
