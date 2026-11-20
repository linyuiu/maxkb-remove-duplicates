<script setup lang="ts">
import { ref,onMounted,onBeforeUnmount,watch } from 'vue';
import markup from '../ingestion/index.html?raw';
import styles from '../ingestion/style.css?raw';
import theme from '../ingestion/theme.css?raw';
import {startWorkbench} from '../ingestion/controller';
const props=defineProps<{page:string;active:boolean}>();
const emit=defineEmits<{navigate:[page:string]}>();
const host=ref<HTMLElement|null>(null);
let controller:any,virtualLocation:any,visibility:any,dispose=()=>{};
const paths:Record<string,string>={workbench:'/',mineru:'/mineru',sources:'/sources',history:'/history',settings:'/settings'};
const pages:Record<string,string>={'/':'workbench','/mineru':'mineru','/sources':'sources','/history':'history','/settings':'settings'};
function prefix(url:any){const value=String(url);return value.startsWith('/api/')&&!value.startsWith('/api/ingestion/')?value.replace('/api/','/api/ingestion/'):value}
function mountController(){
 if(!host.value)return;
 const shadow=host.value.shadowRoot||host.value.attachShadow({mode:'open'});
 const parsed=new DOMParser().parseFromString(markup,'text/html');
 shadow.innerHTML='';
 const style=document.createElement('style');
 style.textContent=styles.replaceAll(':root',':host').replace(/(?<![\w-])body(?![\w-])/g,'.ingestion-root')+`
 :host{display:block;background:transparent;color:#202020;font-family:inherit;--text:#202020;--muted:#525252;--surface:#fff;--bg:#fafafa}
 .ingestion-root{min-height:0;margin:0;background:transparent!important;color:#202020;font-family:inherit;font-size:14px}
 .topbar{display:none!important} main{max-width:none!important;margin:0!important;padding:0!important}
 .page-heading{margin-inline:0}.settings-panel{margin-inline-start:0}
 .card,dialog{background:#fff!important;color:#202020!important;border:1px solid #e5e5e5!important;box-shadow:0 8px 30px #00000006!important;border-radius:21px!important;backdrop-filter:none!important}
 .workspace{min-width:0}.muted,.source-hint,.queue-hint,.field-hint,.form-hint,.hint,label,small{color:#404040!important}
 h1,h2,h3,strong,.file-name,.preview-info{color:#171717!important}input,select,textarea{background:#fff!important;color:#202020!important;border-color:#d4d4d4!important;font-size:14px!important}
 input::placeholder,textarea::placeholder{color:#626262!important}.button.primary{background:#242424!important;color:#fff!important;border-color:#242424!important}
 .button:disabled{opacity:.6}.button.secondary,.button.quiet{color:#303030!important} .page-heading h1{font-weight:580!important}
 .glass-select-trigger{color:#202020!important;background:#fff!important}table,td,th{color:#202020!important}.dropzone{background:#fafafa!important;border-color:#d4d4d4!important}
 @media(max-width:1000px){.workspace{display:flex!important;flex-direction:column!important}.preview-card{width:100%!important}}`+theme;
 const root=document.createElement('div');root.className='ingestion-root';root.innerHTML=parsed.body.innerHTML;
 root.querySelectorAll('.topbar img').forEach(n=>n.remove());shadow.append(style,root);
 const timers=new Set<any>(),intervals=new Set<any>(),streams=new Set<EventSource>(),requests=new Set<XMLHttpRequest>();
 const listeners:Array<()=>void>=[],localWindow:Record<string,any>={};let disposed=false;
 visibility=new EventTarget();
 virtualLocation=new URL(paths[props.page]||'/',window.location.origin);
 const documentProxy=new Proxy(document,{get(target,key){
   if(key==='body')return root;
   if(key==='activeElement')return shadow.activeElement;
   if(key==='hidden')return document.hidden||!props.active;
   if(key==='visibilityState')return document.hidden||!props.active?'hidden':'visible';
   if(key==='getElementById')return (id:string)=>shadow.getElementById(id);
   if(key==='querySelector'||key==='querySelectorAll')return (shadow as any)[key].bind(shadow);
   if(key==='addEventListener')return (event:string,fn:any,options:any)=>{const t=event==='visibilitychange'?visibility:shadow;t.addEventListener(event,fn,options);listeners.push(()=>t.removeEventListener(event,fn,options))};
   if(key==='removeEventListener')return (event:string,fn:any,options:any)=>(event==='visibilitychange'?visibility:shadow).removeEventListener(event,fn,options);
   const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;
 }});
 const setPath=(url:any)=>{virtualLocation.href=new URL(url,virtualLocation.href).href;const page=pages[virtualLocation.pathname]||'workbench';if(props.page!==page)emit('navigate',page)};
 const historyProxy={pushState:(_s:any,_t:any,url:any)=>setPath(url),replaceState:(_s:any,_t:any,url:any)=>setPath(url)};
 const locationProxy=new Proxy({}, {get:(_t,key)=>(virtualLocation as any)[key]});
 const storage={getItem:(k:string)=>localStorage.getItem('ingestion:'+k),setItem:(k:string,v:string)=>localStorage.setItem('ingestion:'+k,v),removeItem:(k:string)=>localStorage.removeItem('ingestion:'+k)};
 const fetchProxy=(url:any,options:RequestInit={})=>{if(disposed)return Promise.reject(new DOMException('页面已关闭','AbortError'));const headers=new Headers(options.headers);headers.set('X-Requested-With','dedup');return fetch(prefix(url),{...options,headers,credentials:'include'})};
 class ScopedXHR extends XMLHttpRequest{
   open(method:string,url:any,...rest:any[]){(super.open as any)(method,prefix(url),...rest);this.setRequestHeader('X-Requested-With','dedup');requests.add(this);this.addEventListener('loadend',()=>requests.delete(this))}
 }
 class ScopedEvents extends EventSource{constructor(url:any,options?:EventSourceInit){super(prefix(url),options);streams.add(this)}close(){streams.delete(this);super.close()}}
 const timeout=(fn:any,delay?:number,...args:any[])=>{const id=setTimeout(()=>{timers.delete(id);if(!disposed)fn(...args)},delay);timers.add(id);return id};
 const interval=(fn:any,delay?:number,...args:any[])=>{const id=setInterval(()=>{if(!disposed)fn(...args)},delay);intervals.add(id);return id};
 const windowProxy=new Proxy(localWindow,{get:(target,key:any)=>{
   if(key in target)return target[key];if(key==='document')return documentProxy;if(key==='location')return locationProxy;if(key==='history')return historyProxy;if(key==='localStorage')return storage;
   if(key==='addEventListener')return (event:string,fn:any,options:any)=>{window.addEventListener(event,fn,options);listeners.push(()=>window.removeEventListener(event,fn,options))};
   const value=(window as any)[key];return typeof value==='function'?value.bind(window):value;
 }});
 const notify=()=>visibility.dispatchEvent(new Event('visibilitychange'));
 document.addEventListener('visibilitychange',notify);listeners.push(()=>document.removeEventListener('visibilitychange',notify));
 dispose=()=>{controller?.destroy?.();disposed=true;timers.forEach(clearTimeout);intervals.forEach(clearInterval);streams.forEach(s=>s.close());requests.forEach(r=>r.abort());listeners.forEach(fn=>fn());root.querySelectorAll('dialog[open]').forEach(n=>(n as HTMLDialogElement).close());};
 controller=startWorkbench({document:documentProxy,window:windowProxy,location:locationProxy,history:historyProxy,fetch:fetchProxy,XMLHttpRequest:ScopedXHR,EventSource:ScopedEvents,setTimeout:timeout,clearTimeout,setInterval:interval,clearInterval,localStorage:storage});
}
onMounted(mountController);
watch(()=>props.page,page=>{if(controller){virtualLocation.pathname=paths[page]||'/';controller.setPage(page)}});
watch(()=>props.active,()=>visibility?.dispatchEvent(new Event('visibilitychange')));
onBeforeUnmount(()=>dispose());
</script>
<template>
  <div class="ingestion-page">
    <div ref="host" class="ingestion-host"></div>
  </div>
</template>
<style scoped>
.ingestion-host { display: block; width: 100%; min-height: 50vh; }
</style>
