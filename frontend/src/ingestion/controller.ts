// @ts-nocheck
// Adapted from mu_batch_to_mk; runs inside the Vue workbench's scoped DOM.
export function startWorkbench(environment) {
const {document,window,location,history,fetch,XMLHttpRequest,EventSource,setTimeout,clearTimeout,setInterval,clearInterval,localStorage}=environment;
"use strict";

// Keep the original select as the form value; draw every menu in the same glass layer.
// The portal sits outside scrollable file cards, so their overflow cannot clip it.
(() => {
  const controls = new Map();
  let active = null, sequence = 0, observer, positioning = false, focusedControl = null;
  let search = "", searchAt = 0, focusRequest = null;
  const menu = document.createElement("div");
  menu.id = "glass-select-menu";
  menu.className = "glass-select-menu";
  menu.setAttribute("role", "listbox");
  menu.setAttribute("popover", "manual");
  menu.hidden = true;
  document.body.append(menu);

  function labelFor(select) {
    if (select.getAttribute("aria-label")) return select.getAttribute("aria-label");
    const label = select.labels?.[0];
    if (label) {
      const copy = label.cloneNode(true);
      copy.querySelectorAll("select, small, .glass-select").forEach(node => node.remove());
      const text = copy.textContent.trim();
      if (text) return text;
    }
    return select.name || "选择选项";
  }
  function keyFor(select) {
    return select.dataset.glassKey || select.id || select.name || labelFor(select);
  }
  function refresh(control) {
    const {select, button, text} = control;
    const label = labelFor(select), option = select.selectedOptions[0];
    const value = option?.textContent.trim() || "请选择";
    if (text.textContent !== value) text.textContent = value;
    if (button.getAttribute("aria-label") !== label) button.setAttribute("aria-label", label);
    if (button.title !== value) button.title = value;
    if (button.disabled !== select.disabled) button.disabled = select.disabled;
    control.key = keyFor(select);
    if (active?.control === control && select.disabled) close(false);
  }
  function enhance(select) {
    if (controls.has(select) || select.multiple) return;
    const wrapper = document.createElement("span");
    wrapper.className = "glass-select";
    if (select.closest(".pending-format") || select.classList.contains("file-mode")) {
      wrapper.classList.add("glass-select--compact");
    }
    if (select.classList.contains("file-mode")) wrapper.classList.add("glass-select--file");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "glass-select-trigger";
    button.id = `glass-select-trigger-${++sequence}`;
    button.setAttribute("role", "combobox");
    button.setAttribute("aria-haspopup", "listbox");
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-controls", menu.id);
    const text = document.createElement("span");
    text.className = "glass-select-value";
    const arrow = document.createElement("span");
    arrow.className = "glass-select-arrow";
    arrow.setAttribute("aria-hidden", "true");
    arrow.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16"><path d="m4 6 4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    button.append(text, arrow);
    select.before(wrapper);
    wrapper.append(select, button);
    select.classList.add("glass-select-native");
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    const control = {select, wrapper, button, text, key: keyFor(select)};
    controls.set(select, control);
    button.addEventListener("click", () => {
      if (active?.control === control) close(true);
      else open(control);
    });
    button.addEventListener("keydown", event => keyboard(event, control));
    button.addEventListener("focus", () => { focusedControl = control; focusRequest = null; });
    select.addEventListener("change", () => refresh(control));
    select.addEventListener("input", () => refresh(control));
    select.addEventListener("focus", () => button.focus({preventScroll: true}));
    select.addEventListener("invalid", event => {
      event.preventDefault(); button.focus({preventScroll: true});
    });
    refresh(control);
  }
  function sync() {
    if (focusedControl && !focusedControl.select.isConnected) {
      focusRequest = {key: focusedControl.key, at: Date.now()}; focusedControl = null;
    }
    const previous = active && !active.control.select.isConnected
      ? {key: active.control.key, index: active.index} : null;
    for (const [select] of controls) if (!select.isConnected) controls.delete(select);
    document.querySelectorAll("select").forEach(enhance);
    controls.forEach(refresh);
    if (previous) {
      close(false);
      const replacement = [...controls.values()].find(control => control.key === previous.key);
      if (replacement && !replacement.select.disabled && replacement.button.getClientRects().length) {
        open(replacement, previous.index);
      }
    }
    if (active && !active.control.button.getClientRects().length) close(false);
    if (focusRequest && !active) {
      const replacement = [...controls.values()].find(control => control.key === focusRequest.key);
      if (replacement || Date.now() - focusRequest.at > 5000) {
        focusRequest = null;
        if (replacement && !replacement.button.disabled && replacement.button.getClientRects().length) {
          replacement.button.focus({preventScroll: true});
        }
      }
    }
  }
  function description(control, option) {
    if (option.value === "qa" && (control.select.name === "excel_mode" || /模式/.test(labelFor(control.select)))) {
      return "按问题与答案成对导入";
    }
    if (option.value === "table" && (control.select.name === "excel_mode" || /模式/.test(labelFor(control.select)))) {
      return "按 MaxKB 表格规则导入";
    }
    if (control.select.name === "tier") return `MinerU ${option.value} 解析档位`;
    if (control.select.id === "pattern-preset") return option.value === "h1" ? "使用 Markdown 一级标题分段" : "按填写的正则表达式分段";
    return "";
  }
  function optionDisabled(option) {
    return option.disabled || option.parentElement?.disabled;
  }
  function open(control, index = control.select.selectedIndex) {
    if (control.select.disabled) return;
    close(false);
    // A portal outside a modal dialog is inert, even if its popover is visually on top.
    const host = control.select.closest("dialog[open]") || document.body;
    if (menu.parentElement !== host) host.append(menu);
    search = "";
    const heading = document.createElement("div");
    heading.className = "glass-select-menu-heading";
    heading.textContent = /导入模式|Excel 模式/.test(labelFor(control.select)) ? "Excel 导入方式" : labelFor(control.select);
    menu.replaceChildren(heading);
    const options = Array.from(control.select.options);
    options.forEach((option, i) => {
      const row = document.createElement("div");
      row.className = "glass-select-option";
      row.id = `${menu.id}-option-${i}`;
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", String(i === control.select.selectedIndex));
      row.setAttribute("aria-disabled", String(Boolean(optionDisabled(option))));
      const body = document.createElement("span"), title = document.createElement("span");
      body.className = "glass-select-option-body";
      title.className = "glass-select-option-title";
      title.textContent = option.textContent.trim();
      body.append(title);
      const subtitle = description(control, option);
      if (subtitle) {
        const note = document.createElement("span");
        note.className = "glass-select-option-note"; note.textContent = subtitle; body.append(note);
      }
      const check = document.createElement("span");
      check.className = "glass-select-check"; check.textContent = "✓"; check.setAttribute("aria-hidden", "true");
      row.append(body, check);
      row.addEventListener("pointerdown", event => event.preventDefault());
      row.addEventListener("pointermove", () => highlight(i));
      row.addEventListener("click", () => choose(i));
      menu.append(row);
    });
    active = {control, options, index: -1};
    menu.setAttribute("aria-label", labelFor(control.select));
    control.button.setAttribute("aria-expanded", "true");
    menu.hidden = false;
    if (typeof menu.showPopover === "function") menu.showPopover();
    place();
    control.button.focus({preventScroll: true});
    highlight(Math.max(0, Math.min(index, options.length - 1)), true);
  }
  function close(focus = false) {
    if (!active) return;
    const button = active.control.button;
    button.setAttribute("aria-expanded", "false");
    button.removeAttribute("aria-activedescendant");
    if (typeof menu.hidePopover === "function" && menu.matches(":popover-open")) menu.hidePopover();
    menu.hidden = true;
    active = null;
    if (focus && button.isConnected && !button.disabled) button.focus({preventScroll: true});
  }
  function place() {
    if (!active) return;
    const rect = active.control.button.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > innerHeight) { close(false); return; }
    const width = Math.min(Math.max(rect.width, 256), 440, innerWidth - 24);
    menu.style.width = `${width}px`;
    const below = innerHeight - rect.bottom - 20, above = rect.top - 20;
    const desired = Math.min(menu.scrollHeight, 340);
    const upwards = below < desired && above > below;
    menu.style.maxHeight = `${Math.max(80, Math.min(340, upwards ? above : below))}px`;
    const height = Math.min(menu.scrollHeight, parseFloat(menu.style.maxHeight));
    menu.style.left = `${Math.max(12, Math.min(rect.left, innerWidth - width - 12))}px`;
    menu.style.top = `${Math.max(12, upwards ? rect.top - height - 8 : rect.bottom + 8)}px`;
  }
  function highlight(index, ensureVisible = false) {
    if (!active || !active.options[index] || optionDisabled(active.options[index])) return;
    if (active.index === index && !ensureVisible) return;
    active.index = index;
    menu.querySelectorAll('[role="option"]').forEach((row, i) => row.classList.toggle("is-active", i === index));
    const row = menu.querySelector(`#${menu.id}-option-${index}`);
    active.control.button.setAttribute("aria-activedescendant", row.id);
    if (ensureVisible) row.scrollIntoView({block: "nearest"});
  }
  function choose(index) {
    if (!active || !active.options[index] || optionDisabled(active.options[index])) return;
    const select = active.control.select;
    const changed = select.selectedIndex !== index;
    select.selectedIndex = index;
    refresh(active.control);
    close(true);
    if (changed) {
      // Native form handlers still receive exactly one change, including per-file API updates.
      select.dispatchEvent(new Event("input", {bubbles: true}));
      select.dispatchEvent(new Event("change", {bubbles: true}));
    }
  }
  function keyboard(event, control) {
    if (event.key === "Tab") { close(false); return; }
    if (event.key === "Escape") {
      if (active) { event.preventDefault(); close(true); } return;
    }
    if (["Enter", " ", "ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      if (active?.control !== control) { open(control); return; }
      if (event.key === "Enter" || event.key === " ") { choose(active.index); return; }
      const available = active.options.map((option, i) => optionDisabled(option) ? -1 : i).filter(i => i >= 0);
      const current = available.indexOf(active.index);
      const next = event.key === "Home" ? available[0] : event.key === "End" ? available.at(-1)
        : available[(current + (event.key === "ArrowUp" ? -1 : 1) + available.length) % available.length];
      highlight(next, true); return;
    }
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      if (active?.control !== control) open(control);
      search = Date.now() - searchAt < 700 ? search + event.key : event.key;
      searchAt = Date.now();
      const index = active.options.findIndex(option => !optionDisabled(option) && option.textContent.trim().toLowerCase().startsWith(search.toLowerCase()));
      if (index >= 0) highlight(index, true);
    }
  }
  document.addEventListener("pointerdown", event => {
    if (!focusedControl?.wrapper.contains(event.target) && !menu.contains(event.target)) {
      focusRequest = null; focusedControl = null;
    }
    if (active && !menu.contains(event.target) && !active.control.wrapper.contains(event.target)) close(false);
  });
  document.addEventListener("focusin", event => {
    if (event.target !== focusedControl?.button) { focusedControl = null; focusRequest = null; }
    if (active && !menu.contains(event.target) && !active.control.wrapper.contains(event.target)) close(false);
  });
  document.addEventListener("scroll", event => {
    if (!active || menu.contains(event.target) || positioning) return;
    positioning = true;
    requestAnimationFrame(() => { positioning = false; place(); });
  }, true);
  document.addEventListener("close", event => {
    if (active && event.target.contains(active.control.select)) close(false);
  }, true);
  window.addEventListener("resize", place);
  window.addEventListener("blur", () => { focusRequest = null; close(false); });
  window.GlassSelect = {refresh: sync, close, destroy:()=>{observer?.disconnect();close(false)}};
  sync();
  observer = new MutationObserver(sync);
  observer.observe(document.body, {childList: true, subtree: true, attributes: true, attributeFilter: ["disabled", "selected", "label", "hidden"]});
})();

"use strict";
const $ = (id) => document.getElementById(id);
const form = $("settings-form");
const DEFAULT_PATTERN = "(?<=^)# .*|(?<=\\n)# .*";
const statuses = {stopped:"已停止",queued:"等待解析",uploading:"上传中",parsing:"解析中",downloading:"下载中",packaging:"整理中",splitting:"切片中",review:"等待入库",import_queued:"等待入库执行",importing:"入库中",imported:"已入库",split_queued:"等待切片",pending_upload:"等待上传",waiting_wave:"等待前批",import_monitoring:"等待向量化",import_checking:"核对状态",cleanup_pending:"等待清理",cleanup_running:"清理中",cleanup_failed:"清理未完成",import_failed:"入库失败",reconciling:"核对中",failed:"解析/切片失败",import_rejected:"入库被拒绝",import_unknown:"入库结果待确认",preparation_unknown:"OSS/切片待核对"};
const importPhases={creating:"创建中",monitoring:"等待后台处理",success:"入库成功",cleaned:"清理完成",cleanup_failed:"清理未完成",unknown:"结果待核对",import_unknown:"结果待核对",import_rejected:"入库被拒绝",cleanup_pending:"等待清理",confirmed_absent:"人工确认未创建"};
const remoteStates={pending:"等待处理",success:"成功",failed:"失败",deleting:"删除中",deleted:"删除成功",absent_confirmed:"人工确认不存在"};
const stages = {queued:-1,uploading:0,parsing:1,downloading:2,packaging:3,splitting:4,review:5,import_queued:5,importing:6,imported:7};
const activeStatuses = new Set(["uploading","parsing","downloading","packaging","splitting","importing","import_monitoring","import_checking","cleanup_running","reconciling"]);
const retrying = new Set();
const fileFeedback = new Map();
let batch = null, pending = [], limits = {}, selected = new Set(), source = null;
let previewId = "", offset = 0, previewTotal = 0, previewRequest = 0, uploading = false;
let importIds = [], importBatchId = "", noticeTimer, batchLoadRequest = 0;
let auditRequest = 0;
const queueRows = new Map();
let folderRenderSignature = "";
const pendingModes = new Map();
let pendingExcelDefault="";
const formatNames = {pdf:"PDF",word:"Word",excel:"Excel"};
function kindOf(file){return file.kind || (/\.pdf$/i.test(file.name)?"pdf":/\.docx?$/i.test(file.name)?"word":/\.xlsx?$/i.test(file.name)?"excel":"");}
function filePath(file){return file.webkitRelativePath||file.name;}
function setPage(page){
  window.GlassSelect?.close();
  const settings=page==="settings",sources=page==="sources",records=page==="history";
  document.body.classList.toggle("settings-page",settings);
  document.querySelector(".settings-panel").hidden=!settings;
  document.querySelector(".sources-panel").hidden=!sources;
  document.querySelector(".workspace").hidden=settings||sources||records;
  $("history-panel").hidden=!records;
  document.querySelector(".page-heading h1").textContent=settings?"参数设置":sources?"文件同步":records?"历史记录":"文档工作台";
  for(const link of document.querySelectorAll("[data-page]"))link.setAttribute("aria-current",link.dataset.page===page?"page":"false");
  window.SourceUI?.pageChanged(page);
  window.MinerURecords?.update();
  window.HistoryUI?.pageChanged(page);
  window.WorkbenchActivity?.changed();
}
function currentPage(){return location.pathname==="/settings"?"settings":location.pathname==="/sources"?"sources":location.pathname==="/history"?"history":"workbench";}
function navigatePage(page){history.pushState(null,"",page==="workbench"?"/":"/"+page);setPage(page);}
for(const link of document.querySelectorAll("[data-page]"))link.addEventListener("click",e=>{e.preventDefault();navigatePage(link.dataset.page);});
window.addEventListener("popstate",()=>setPage(currentPage()));
setPage(currentPage());
let previewFilesSignature = "";
let folderQueue=null, wavePage=0, resumeRequested=false, queueRefreshing=false, renderScheduled=false;
let queueController=null,queueRequest=0;
const finishedStatuses=new Set(["imported","failed","import_failed","import_rejected","stopped"]);

function workbenchVisible(){return currentPage()==="workbench"&&!document.hidden;}
function queueNeedsPolling(){
  if(!folderQueue)return false;
  return !["completed","completed_with_errors","stopped"].includes(folderQueue.state)||
    (batch?.queue_id===folderQueue.id&&batch.files.some(file=>!finishedStatuses.has(file.status)));
}
function workbenchActivityChanged(){
  if(!workbenchVisible()){
    connectEvents();queueController?.abort();window.MinerURecords?.suspend();return;
  }
  connectEvents(true);
  refreshFolderQueue({force:true}).catch(()=>{});
  window.MinerURecords?.update(true);
}
function pollWorkbench(){
  if(workbenchVisible()&&queueNeedsPolling())refreshFolderQueue().catch(()=>{});
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if(className) node.className = className;
  if(text !== undefined) node.textContent = text;
  return node;
}
function size(bytes) { return bytes < 1024**2 ? (bytes / 1024).toFixed(1)+" KB" : (bytes / 1024**2).toFixed(1)+" MB"; }
function elapsed(start) { if(!start) return ""; const seconds = Math.max(0,Math.floor(Date.now()/1000-start)); return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds/60)} 分 ${seconds%60} 秒`; }
function notice(message, error=false) {
  clearTimeout(noticeTimer); $("notice").textContent = message;
  $("notice").className = "notice" + (error?" error":""); $("notice").hidden = false;
  if(!error) noticeTimer = setTimeout(()=>$("notice").hidden=true,6000);
}
async function api(path, options={}) {
  const controller=new AbortController(),abort=()=>controller.abort();
  let timedOut=false;
  const timer=setTimeout(()=>{timedOut=true;controller.abort();},(Number(form.elements.transfer_timeout.value)||600)*1000);
  options.signal?.addEventListener("abort",abort,{once:true});
  if(options.signal?.aborted)controller.abort();
  try{
    const response = await fetch(path, {...options,signal:controller.signal});
    const requestId=response.headers?.get("X-Request-ID");
    const reference=requestId?`（请求编号：${requestId}）`:"";
    let data;
    try { data = await response.json(); } catch(error) { if(error.name==="AbortError")throw error;throw new Error("服务响应异常，请检查 Web 服务日志"+reference); }
    if(!response.ok) {
      const detail = typeof data.detail === "string" ? data.detail : "参数格式不正确，请检查填写内容";
      throw new Error(detail+reference);
    }
    return data;
  }catch(error){
    if(timedOut)throw new Error("请求等待超时，请先刷新并核对任务状态；已提交的操作可能仍在执行。");
    throw error;
  }finally{
    clearTimeout(timer);options.signal?.removeEventListener("abort",abort);
  }
}
function jsonOptions(body, method="POST") { return {method,headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}; }
function readConfig() {
  if(!form.checkValidity()){navigatePage("settings");form.reportValidity();throw new Error("请在参数页面填写必填参数");}
  const data = Object.fromEntries(new FormData(form));
  const config = {};
  for(const key of ["mineru_url","maxkb_base_url","workspace_id","tier","page_range","excel_mode"]) config[key] = (data[key] || "").trim();
  for(const key of ["limit","request_timeout","transfer_timeout","parse_timeout","poll_interval","batch_size","import_poll_interval","import_timeout"]) config[key] = Number(data[key]);
  config.maxkb_token = data.maxkb_token?.trim() || null;
  config.mineru_key = $("clear-key").checked ? "" : (data.mineru_key?.trim() || null);
  config.patterns = $("pattern-preset").value === "h1" ? [DEFAULT_PATTERN] : data.patterns.split(/\r?\n/).filter(line=>line.trim());
  config.with_filter = form.elements.with_filter.checked;
  config.auto_import=form.elements.auto_import.checked;
  for(const key of ["knowledge_tags_enabled","document_tags_enabled","inherit_knowledge_tags"])
    config[key]=form.elements[key]?.checked ?? true;
  return config;
}
function fileKnowledgeName(file){
  const plan=batch?.config._knowledge_plan;
  if(!plan)return batch?.config.knowledge_id||"";
  const path=plan.root?file.relative_path.slice(plan.root.length+1):file.relative_path;
  return path.includes("/")?path.split("/")[0]:(plan.root_knowledge?"未分类":batch.config.knowledge_id);
}
function credentialState(config) {
  form.elements.maxkb_token.value=""; form.elements.mineru_key.value=""; $("clear-key").checked=false;
  form.elements.maxkb_token.placeholder=config.has_maxkb_token?"已保存 · 留空沿用，填写以更换":"填写 MaxKB 令牌";
  form.elements.mineru_key.placeholder=config.has_mineru_key?"已保存 · 留空沿用":"未启用鉴权时留空";
  $("token-help").textContent=config.has_maxkb_token?"已加密保存；填写新令牌可替换，留空沿用。":"凭据仅保存在服务端，不会返回到浏览器。";
  $("config-state").textContent=config.has_maxkb_token?"已保存":"待填写";
}
async function init() {
  try {
    const data=await api("/api/ingestion/settings"); limits=data.limits;
    for(const [key,value] of Object.entries(data.config)) {
      const field=form.elements[key]; if(!field || ["mineru_key","maxkb_token","patterns"].includes(key)) continue;
      if(field.type==="checkbox") field.checked=value; else field.value=value;
    }
    form.elements.patterns.value=data.config.patterns.join("\n");
    $("pattern-preset").value=JSON.stringify(data.config.patterns)===JSON.stringify([DEFAULT_PATTERN])?"h1":"custom";
    $("custom-patterns").hidden=$("pattern-preset").value!=="custom";
    credentialState(data.config);
    await window.SourceUI?.init();
    $("upload-limits").textContent=`文件夹最多 ${limits.max_queue_files} 个 · 每批最多 ${limits.max_files} 个 · 单文件 ${limits.max_file_mb} MB`;
    $("worker-info").textContent=`解析 ${limits.parse_workers} / 切片 ${limits.split_workers} / 入库 ${limits.import_workers} 并发`;
    $("connection").textContent="工作台在线";
    const savedQueue=localStorage.getItem("active-queue");
    if(savedQueue){try{folderQueue=await api(`/api/ingestion/queues/${savedQueue}`);renderFolderQueue();}catch{localStorage.removeItem("active-queue");}}
    const remembered = new URL(location.href).searchParams.get("batch") || localStorage.getItem("active-batch");
    if(remembered) { try { await loadBatch(remembered, false); } catch { localStorage.removeItem("active-batch"); } }
  } catch(error) { $("connection").textContent="连接异常"; $("connection").classList.add("offline"); notice(error.message,true); }
}
form.addEventListener("submit", async event=>{
  event.preventDefault(); $("save-settings").disabled=true;
  try { const data=await api("/api/ingestion/settings",jsonOptions(readConfig(),"PUT")); credentialState(data); notice("参数已保存。新批次使用这些设置；重试解析使用当前 MinerU 连接。"); }
  catch(error) { notice(error.message,true); }
  finally { $("save-settings").disabled=false; }
});
$("pattern-preset").addEventListener("change",()=>$("custom-patterns").hidden=$("pattern-preset").value!=="custom");
form.addEventListener("input",()=>$("config-state").textContent="有修改");

function addFiles(files) {
  if(uploading) return;
  const chosen=Array.from(files);
  const supported=chosen.filter(file=>kindOf(file));
  const ignored=chosen.length-supported.length;
  if(ignored)notice(`已忽略 ${ignored} 个不支持的文件，仅处理 PDF、DOC/DOCX、XLS/XLSX。`);
  const existing=new Set(pending.map(file=>`${file.webkitRelativePath||file.name}:${file.size}:${file.lastModified}`));
  for(const file of supported) {
    const key=`${file.webkitRelativePath||file.name}:${file.size}:${file.lastModified}`;
    if(!existing.has(key)) {pending.push(file); existing.add(key);if(!resumeRequested)localStorage.removeItem("pending-queue-id");}
  }
  pending.sort((a,b)=>{const x=a.webkitRelativePath||a.name,y=b.webkitRelativePath||b.name;return x<y?-1:x>y?1:0;});
  showPending();
}
function showPending() {
  for(const kind of Object.keys(formatNames))$("pending-"+kind+"-count").textContent=`${pending.filter(f=>kindOf(f)===kind).length} 个待上传`;
  $("pending-label").textContent=pending.length?`已选 ${pending.length} 个 · ${size(pending.reduce((n,f)=>n+f.size,0))}`:"还没有选择文件";
  $("start").disabled=!pending.length || uploading;
  $("clear-files").hidden=!pending.length; $("pending-files").hidden=!pending.length;
  $("pending-files").replaceChildren(...Object.keys(formatNames).map(kind=>{
    const files=pending.filter(f=>kindOf(f)===kind),section=el("section","pending-format");
    if(!files.length)return section;
    section.append(el("h3","",`${formatNames[kind]} · ${files.length} 个`));
    if(kind==="excel"){
      const label=el("label","excel-default-label","本次全部 Excel 模式"),select=document.createElement("select");
      for(const [value,text]of [["table","表格 table"],["qa","问答对 qa"]]){const o=el("option","",text);o.value=value;select.append(o);}
      select.value=pendingExcelDefault||form.elements.excel_mode.value;select.disabled=uploading||resumeRequested;
      select.addEventListener("change",()=>{pendingExcelDefault=select.value;for(const f of files)pendingModes.set(filePath(f),select.value);showPending();});label.append(select);section.append(label);
    }
    for(const file of files.slice(0,20)){const row=el("div","pending-format-row");row.append(el("span","",filePath(file)),el("span","",size(file.size)));
      if(kind==="excel"){const mode=document.createElement("select");mode.setAttribute("aria-label",`${file.name} 导入模式`);mode.dataset.glassKey=`pending-mode:${filePath(file)}`;
        for(const [value,text]of [["table","表格"],["qa","问答对"]]){const o=el("option","",text);o.value=value;mode.append(o);}
        mode.value=pendingModes.get(filePath(file))||pendingExcelDefault||form.elements.excel_mode.value;mode.disabled=uploading||resumeRequested;
        mode.addEventListener("change",()=>pendingModes.set(filePath(file),mode.value));row.append(mode);
      }section.append(row);
    }
    if(files.length>20)section.append(el("p","muted",`另有 ${files.length-20} 个文件，使用该类统一设置。`));return section;
  }));
}
$("dropzone").addEventListener("click",()=>{if(!uploading)$("file-input").click();});
$("dropzone").addEventListener("keydown",e=>{if(["Enter"," "].includes(e.key)){e.preventDefault();if(!uploading)$("file-input").click();}});
for(const kind of ["word","excel"]){$("choose-"+kind).addEventListener("click",()=>{if(!uploading)$(kind+"-input").click();});$(kind+"-input").addEventListener("change",e=>{addFiles(e.target.files);e.target.value="";});}
$("file-input").addEventListener("change",e=>{addFiles(e.target.files);e.target.value="";});
$("folder-input").addEventListener("change",e=>{addFiles(e.target.files);e.target.value="";});
$("choose-folder").addEventListener("click",()=>$("folder-input").click());
$("clear-files").addEventListener("click",()=>{if(!uploading){pending=[];pendingModes.clear();pendingExcelDefault="";resumeRequested=false;localStorage.removeItem("pending-queue-id");showPending();}});
for(const name of ["dragenter","dragover"]) $("dropzone").addEventListener(name,e=>{e.preventDefault();$("dropzone").classList.add("drag");});
for(const name of ["dragleave","drop"]) $("dropzone").addEventListener(name,e=>{e.preventDefault();$("dropzone").classList.remove("drag");});
$("dropzone").addEventListener("drop",e=>addFiles(e.dataTransfer.files));
function uploadBatch(files, path) {
  return new Promise((resolve,reject)=>{
    const data=new FormData(); files.forEach(file=>data.append("files",file,file.webkitRelativePath||file.name));
    const xhr=new XMLHttpRequest(); xhr.open("POST",path);
    xhr.timeout=(Number(form.elements.transfer_timeout.value)||600)*1000;
    xhr.upload.onprogress=e=>{if(e.lengthComputable){const percent=Math.round(e.loaded/e.total*100);$("upload-bar").value=percent;$("upload-text").textContent=percent===100?"上传完成，正在保存任务…":`上传 ${percent}%`;}};
    xhr.onload=()=>{let response;try{response=JSON.parse(xhr.responseText);}catch{return reject(new Error("上传响应异常"));} if(xhr.status>=200&&xhr.status<300)resolve(response);else reject(new Error(typeof response.detail==="string"?response.detail:"上传失败，请检查文件与参数"));};
    xhr.onerror=()=>reject(new Error("上传连接中断。请查看批次记录，确认是否已创建任务后再重试。"));
    xhr.ontimeout=()=>reject(new Error("上传等待超时。请查看批次记录，并使用继续上传恢复原队列。"));
    xhr.onabort=()=>reject(new Error("上传已中断，可以继续上传原队列。"));
    xhr.send(data);
  });
}
$("start").addEventListener("click",async()=>{
  let config;
  let intakeQueue=null;
  let knowledge_plan=null;
  try {
    config=readConfig();
    if(pending.length>limits.max_queue_files) throw new Error(`队列最多 ${limits.max_queue_files} 个文件`);
    if(config.batch_size>limits.max_files)throw new Error(`每批最多 ${limits.max_files} 个文件`);
    if(pending.some(file=>file.size>limits.max_file_mb*1024**2)) throw new Error(`单文件不能超过 ${limits.max_file_mb} MB`);
    for(let start=0;start<pending.length;start+=config.batch_size){if(pending.slice(start,start+config.batch_size).reduce((n,f)=>n+f.size,0)>limits.max_batch_mb*1024**2)throw new Error(`某一批超过 ${limits.max_batch_mb} MB，请减小每批文件数`);}
    const pendingId=localStorage.getItem("pending-queue-id");
    if(!resumeRequested&&pendingId){
      try{folderQueue=await api(`/api/ingestion/queues/${pendingId}`);resumeRequested=true;localStorage.setItem("active-queue",folderQueue.id);localStorage.removeItem("pending-queue-id");}
      catch(error){if(!error.message.startsWith("队列不存在"))throw error;}
    }
    if(!resumeRequested&&pending.length){
      let root="";
      if(pending.every(f=>f.webkitRelativePath)){
        const roots=new Set(pending.map(f=>filePath(f).split("/")[0]));
        if(roots.size!==1)throw new Error("请一次选择一个根文件夹");
        root=[...roots][0];
      }
      knowledge_plan=await window.KnowledgeUI.confirm(pending.map(filePath),root,config);
      if(knowledge_plan===false)return;
    }
  }catch(error){notice(error.message,true);return;}
  uploading=true; showPending(); $("upload-progress").hidden=false; $("upload-bar").value=0;
  $("start").textContent="正在上传…"; $("clear-files").disabled=true; $("choose-folder").disabled=true;
  $("save-settings").disabled=true;
  try {
    const manifest=pending.map(file=>({path:filePath(file),bytes:file.size,...(kindOf(file)==="excel"?{excel_mode:pendingModes.get(filePath(file))||pendingExcelDefault||config.excel_mode}:{})}));
    if(resumeRequested){
      if(!folderQueue)throw new Error("没有可以继续上传的队列");
      const original=await api(`/api/ingestion/queues/${folderQueue.id}/manifest`);
      if(JSON.stringify(original.map(f=>({path:f.relative_path,bytes:f.bytes})))!==JSON.stringify(manifest.map(f=>({path:f.path,bytes:f.bytes}))))throw new Error("请重新选择原来的完整文件夹；路径、顺序或大小与队列清单不一致");
    }else{
      const id=localStorage.getItem("pending-queue-id")||newQueueId();localStorage.setItem("pending-queue-id",id);
      folderQueue=await api("/api/ingestion/queues",jsonOptions({id,files:manifest,config,knowledge_plan}));
      localStorage.setItem("active-queue",folderQueue.id);localStorage.removeItem("pending-queue-id");
      await api("/api/ingestion/settings",jsonOptions(config,"PUT")).then(credentialState).catch(()=>{});
    }
    intakeQueue=folderQueue;
    localStorage.setItem("active-queue",intakeQueue.id);wavePage=0;renderFolderQueue();
    for(const wave of intakeQueue.batches){
      if(wave.wave_state!=="pending_upload")continue;
      const start=wave.ordinal*intakeQueue.batch_size;
      $("start").textContent=`上传第 ${wave.ordinal+1}/${intakeQueue.batches.length} 批…`;
      const result=await uploadBatch(pending.slice(start,start+intakeQueue.batch_size),`/api/ingestion/queues/${intakeQueue.id}/batches/${wave.id}`);
      if(folderQueue?.id===intakeQueue.id&&(!batch||batch.queue_id!==intakeQueue.id))activateBatch(result);
      await refreshFolderQueue();
    }
    pending=[];pendingModes.clear();pendingExcelDefault="";resumeRequested=false;notice(`全部 ${intakeQueue.total} 个文件已上传，服务器将依次处理各批。`);
  }catch(error){if(intakeQueue&&folderQueue?.id===intakeQueue.id)resumeRequested=true;notice(error.message+ (resumeRequested?"；再次点击开始处理将继续原队列。":""),true);}
  finally{uploading=false;$("upload-progress").hidden=true;$("start").textContent="开始处理 →";$("clear-files").disabled=false;$("choose-folder").disabled=false;$("save-settings").disabled=false;showPending();}
});
async function loadBatch(id, showWorkspace=true){
  const request=++batchLoadRequest;
  const snapshot=await api(`/api/ingestion/batches/${encodeURIComponent(id)}`);
  const queue=snapshot.queue_id&&folderQueue?.id!==snapshot.queue_id?await api(`/api/ingestion/queues/${snapshot.queue_id}`):folderQueue;
  if(request!==batchLoadRequest)return;
  if(snapshot.queue_id){folderQueue=queue;localStorage.setItem("active-queue",folderQueue.id);}
  if(!snapshot.queue_id){folderQueue=null;$("folder-queue").hidden=true;}
  activateBatch(snapshot, showWorkspace);renderFolderQueue();
}
function activateBatch(snapshot, showWorkspace=true) {
  batchLoadRequest++;
  if(showWorkspace&&location.pathname!=="/")navigatePage("workbench");
  if(source)source.close(); batch=snapshot; selected.clear();queueRows.clear(); previewId=""; offset=0;
  clearPreview(); localStorage.setItem("active-batch",batch.id);
  const url=new URL(location.href);url.searchParams.set("batch",batch.id);history.replaceState(null,"",url);
  renderBatch(); connectEvents(true);
}
function connectEvents(force=false){
  if(!batch||!workbenchVisible()||(!force&&batch.files?.length&&batch.files.every(file=>finishedStatuses.has(file.status)))){
    source?.close();source=null;
    $("connection").textContent=workbenchVisible()?(batch?"处理完成":"工作台在线"):"进度已暂停";return;
  }
  if(source?.batchId===batch.id)return;
  source?.close();
  const batchId=batch.id,connection=new EventSource(`/api/ingestion/batches/${batchId}/events`);
  connection.batchId=batchId;
  source=connection;
  connection.addEventListener("history-cleared",()=>{
    if(source!==connection)return;
    resetHistoryWorkspace();notice("历史记录已清除。");
  });
  connection.addEventListener("snapshot",e=>{if(source!==connection||batch?.id!==batchId)return;batch=JSON.parse(e.data);renderBatch();});
  connection.addEventListener("file",e=>{
    if(source!==connection||batch?.id!==batchId)return;
    applyFileUpdate(JSON.parse(e.data));
    scheduleRender();
  });
  connection.onopen=()=>{if(source!==connection)return;$("connection").textContent="进度实时连接";$("connection").classList.remove("offline");};
  connection.onerror=()=>{if(source!==connection)return;$("connection").textContent="进度重连中";$("connection").classList.add("offline");};
}
function resetHistoryWorkspace(){
  queueController?.abort();queueRequest++;queueController=null;queueRefreshing=false;
  batchLoadRequest++;source?.close();source=null;batch=null;folderQueue=null;
  selected.clear();queueRows.clear();fileFeedback.clear();previewId="";offset=0;folderRenderSignature="";
  for(const key of ["active-batch","active-queue","pending-queue-id"])localStorage.removeItem(key);
  const url=new URL(location.href);url.searchParams.delete("batch");history.replaceState(null,"",url);
  clearPreview();$("queue-list").replaceChildren();$("file-count").textContent="0";
  $("queue-empty").hidden=false;
  for(const id of ["folder-queue","queue-footer","batch-summary","batch-target","queue-problems-pager","queue-refresh-error"])$(id).hidden=true;
  for(const id of ["import-dialog","audit-dialog"])if($(id).open)$(id).close();
  window.MinerURecords?.update(true);window.HistoryUI?.pageChanged(currentPage());
  window.SourceUI?.historyCleared().catch(error=>notice(error.message,true));
}
function applyFileUpdate(file){
  if(!batch||file.batch_id!==batch.id)return;
  const index=batch.files.findIndex(item=>item.id===file.id);
  if(index<0)batch.files.push(file);
  else if(file.updated_at>=batch.files[index].updated_at)batch.files[index]=file;
}
async function retryFile(file){
  const field=form.elements.mineru_url;
  if(!field.reportValidity()){notice("请填写有效的 MinerU 服务根地址。",true);return;}
  const connection={
    mineru_url:field.value.trim(),
    mineru_key:$("clear-key").checked?"":(form.elements.mineru_key.value.trim()||null)
  };
  if(retrying.has(file.id))return;
  retrying.add(file.id);renderBatch();
  try{
    const result=await api(`/api/ingestion/files/${file.id}/retry`,jsonOptions(connection));
    applyFileUpdate(result.file);renderBatch();
    notice(kindOf(file)==="pdf"?`已使用 ${result.mineru_url} 排队重试，无需重新上传 PDF。`:"已排队重新准备文件，无需重复上传。");
  }catch(error){notice(error.message,true);}
  finally{retrying.delete(file.id);renderBatch();}
}
function selectionChanged(){
  $("selection-count").textContent=`已选 ${selected.size} 个`;
  $("import-selected").disabled=!selected.size;
  const ready=batch?.files.filter(f=>f.status==="review")||[];
  $("select-ready").checked=ready.length>0&&ready.every(f=>selected.has(f.id));
  $("select-ready").indeterminate=selected.size>0&&!$("select-ready").checked;
}
function scheduleRender(){if(renderScheduled)return;renderScheduled=true;setTimeout(()=>{renderScheduled=false;if(workbenchVisible())renderBatch();},150);}
function reconcileChildren(parent,children){
  children.forEach((child,index)=>{if(parent.children[index]!==child)parent.insertBefore(child,parent.children[index]||null);});
  while(parent.children.length>children.length)parent.lastElementChild.remove();
}
function renderBatch(){
  if(!batch)return;
  selected=new Set([...selected].filter(id=>batch.files.some(f=>f.id===id&&f.status==="review")));
  $("file-count").textContent=batch.files.length;$("queue-empty").hidden=true;$("queue-footer").hidden=false;$("batch-summary").hidden=false;
  $("stat-total").textContent=batch.files.length;$("stat-active").textContent=batch.files.filter(f=>activeStatuses.has(f.status)).length;
  $("stat-ready").textContent=batch.files.filter(f=>f.status==="review").length;$("stat-done").textContent=batch.files.filter(f=>f.status==="imported").length;
  const target=$("batch-target");target.hidden=false;target.replaceChildren();
  target.append(el("span","","本批目标："),el("b","",batch.config.workspace_id),el("span",""," / "),el("b","",batch.config._knowledge_plan?"按一级文件夹路由（见知识库列表）":batch.config.knowledge_id),el("span","",` · ${batch.config.tier}（仅 PDF） · 切片长度 ${batch.config.limit}`));
  const rows=batch.files.map(file=>{
    let feedback=fileFeedback.get(file.id);
    if(feedback&&!feedback.pending&&feedback.finalStatus!==file.status&&!["import_unknown","cleanup_failed","preparation_unknown","reconciling"].includes(file.status)){
      fileFeedback.delete(file.id);feedback=null;
    }
    const signature=JSON.stringify([file,feedback,selected.has(file.id),retrying.has(file.id),activeStatuses.has(file.status)?elapsed(file.started_at):""]);
    const cached=queueRows.get(file.id);if(cached?.signature===signature)return cached.row;
    const row=el("div","file-row"),check=document.createElement("input");check.type="checkbox";check.disabled=file.status!=="review";check.checked=selected.has(file.id);check.setAttribute("aria-label",`选择 ${file.name} 入库`);
    check.addEventListener("change",()=>{if(check.checked)selected.add(file.id);else selected.delete(file.id);selectionChanged();});
    const detail=el("div"),name=el("div","file-name",file.name);name.title=file.name;detail.append(name);
    const extra=file.paragraph_count?`${file.paragraph_count} 个切片${file.previewed_at?" · 已预览":""}`:activeStatuses.has(file.status)?`已用 ${elapsed(file.started_at)}`:new Date(file.created_at*1000).toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit"});
    detail.append(el("div","file-meta",`${size(file.bytes)} · ${extra}`));
    if(batch.config._knowledge_plan){
      const root=batch.config._knowledge_plan.root,path=root?file.relative_path.slice(root.length+1):file.relative_path,folder=path.includes("/")?path.split("/")[0]:"";
      detail.append(el("div","file-meta",`知识库：${folder||(batch.config._knowledge_plan.root_knowledge?"未分类":batch.config.knowledge_id)}`));
      if(file.status==="import_unknown"&&(folder||batch.config._knowledge_plan.root_knowledge)&&file.error?.startsWith("文档已入库但标签绑定未确认")){const tagsRetry=el("button","button quiet small","核验并补充标签");tagsRetry.addEventListener("click",()=>runFileCheck(file,async()=>{await api(`/api/ingestion/files/${file.id}/tags/retry`,{method:"POST"});return "已排队核验和补充标签，不会重建文档。";}));detail.append(tagsRetry);}
    }
    if(file.message)detail.append(el("div","file-message",file.message));
    if(kindOf(file)==="excel"&&file.attempt_id)detail.append(el("div","file-meta",`MaxKB 文档 ${file.document_count} 个 · 段落 ${file.remote_paragraph_count||0} 个`));
    if(file.parse_seconds||file.split_seconds||file.import_seconds)detail.append(el("div","file-meta",`解析 ${(file.parse_seconds||0).toFixed(1)}s · 切片 ${(file.split_seconds||0).toFixed(1)}s · 入库 ${(file.import_seconds||0).toFixed(1)}s`));
    if(file.error)detail.append(el("div","file-error",file.error));
    if(feedback){
      const message=el("div","file-feedback"+(feedback.error?" error":""),feedback.message);
      message.setAttribute("role",feedback.error?"alert":"status");message.setAttribute("aria-atomic","true");detail.append(message);
    }
    if(stages[file.status]!==undefined&&file.status!=="queued"){
      const track=el("div","stage-track"),pdf=kindOf(file)==="pdf";
      track.title=pdf?"上传 → 解析 → 下载 → 整理 → 切片 → 预览 → 入库（阶段进度）":kindOf(file)==="word"?"切片 → 确认 → 入库":"保存 → 确认 → 入库";
      const current=pdf?stages[file.status]:file.status==="imported"?3:["importing","import_queued"].includes(file.status)?2:file.status==="review"?1:0;
      for(let i=0;i<(pdf?7:3);i++)track.append(el("i",i<current?"done":i===current?"current":""));detail.append(track);
    }
    const controls=el("div","file-controls"),status=el("span","status "+(activeStatuses.has(file.status)?"processing":file.status),statuses[file.status]||file.status);controls.append(status);
    if(!["imported","stopped"].includes(file.status)){
      const stop=el("button","button quiet small",file.stop_requested?"正在停止…":"停止");stop.disabled=!!file.stop_requested;
      stop.addEventListener("click",async()=>{stop.disabled=true;try{const result=await api(`/api/ingestion/files/${file.id}/stop`,{method:"POST"});applyFileUpdate(result);renderBatch();}catch(e){notice(e.message,true);stop.disabled=false;}});controls.append(stop);
    }
    if(file.paragraph_count||kindOf(file)==="excel"){const view=el("button","button quiet small","预览");view.addEventListener("click",()=>selectPreview(file.id,true));controls.append(view);}
    if(file.status==="failed"){
      const retry=el("button","button retry small",retrying.has(file.id)?"提交中…":kindOf(file)==="pdf"?"重试解析":"重试准备");
      retry.disabled=retrying.has(file.id);
      retry.title="使用当前填写的 MinerU 地址和 API Key 重试";
      retry.addEventListener("click",()=>retryFile(file));controls.append(retry);
    }
    if(["import_failed","import_rejected","cleanup_failed","import_unknown","imported"].includes(file.status)){
      const redo=el("button","button retry small",file.status==="cleanup_failed"?"重试清理并入库":"重新入库");
      redo.disabled=!!feedback?.pending;
      redo.addEventListener("click",()=>reimportFile(file));controls.append(redo);
    }
    if(["import_unknown","cleanup_failed"].includes(file.status)){
      const resolve=el("button","button quiet small",feedback?.pending?"核对中…":"核对结果");resolve.disabled=!!feedback?.pending;resolve.addEventListener("click",()=>reconcileFile(file));controls.append(resolve);
    }
    if(file.status==="preparation_unknown"){
      const resolve=el("button","button quiet small",feedback?.pending?"核对中…":"核对 OSS 资源");resolve.disabled=!!feedback?.pending;resolve.addEventListener("click",()=>resolvePreparation(file));controls.append(resolve);
    }
    const audit=el("button","button quiet small","记录");audit.addEventListener("click",()=>showAudit(file));controls.append(audit);
    if(kindOf(file)==="excel"){
      const mode=document.createElement("select");mode.className="file-mode";mode.dataset.glassKey=`file-mode:${file.id}`;mode.setAttribute("aria-label",`${file.name} 导入模式`);
      for(const [value,text]of [["table","表格 table"],["qa","问答对 qa"]]){const o=el("option","",text);o.value=value;mode.append(o);}
      mode.value=file.import_mode;mode.disabled=!["review","failed","queued","waiting_wave","pending_upload","import_failed","import_rejected"].includes(file.status);
      mode.addEventListener("change",async()=>{mode.disabled=true;try{applyFileUpdate(await api(`/api/ingestion/files/${file.id}/excel-mode`,jsonOptions({mode:mode.value},"PUT")));renderBatch();if(previewId===file.id)fetchWorkbook();}catch(e){notice(e.message,true);renderBatch();}});detail.append(mode);
    }
    row.dataset.kind=kindOf(file);row.append(check,el("div","pdf-icon "+kindOf(file),formatNames[kindOf(file)]),detail,controls);queueRows.set(file.id,{signature,row});return row;
  });
  for(const kind of Object.keys(formatNames)){
    let section=$("queue-list").querySelector(`[data-format="${kind}"]`);
    if(!section){section=el("section","format-queue-section");section.dataset.format=kind;section.append(el("h3","format-section-title"),el("div","format-queue-list"));$("queue-list").append(section);}
    const list=section.lastElementChild,matching=rows.filter(row=>row.dataset.kind===kind);
    section.firstElementChild.textContent=`${formatNames[kind]} · ${matching.length} 个`;
    reconcileChildren(list,matching.length?matching:[list.querySelector(".format-empty")||el("p","format-empty","本批次没有此类文件")]);
  }
  for(const id of queueRows.keys())if(!batch.files.some(f=>f.id===id))queueRows.delete(id);
  const previewable=batch.files.filter(f=>f.paragraph_count>0||(kindOf(f)==="excel"&&!["pending_upload","waiting_wave","queued","splitting"].includes(f.status)));
  if(previewId&&!previewable.some(f=>f.id===previewId)){previewId="";clearPreview();}
  renderPreviewPicker(previewable);
  $("resplit").disabled=!batch.files.some(f=>f.id===previewId&&kindOf(f)!=="excel"&&["review","import_rejected"].includes(f.status));
  selectionChanged();
  window.MinerURecords?.update();
  window.WorkbenchActivity?.sync();
}
$("select-ready").addEventListener("change",()=>{selected=$("select-ready").checked?new Set(batch.files.filter(f=>f.status==="review").map(f=>f.id)):new Set();renderBatch();});
function renderPreviewPicker(files=batch?.files.filter(file=>file.paragraph_count>0||(kindOf(file)==="excel"&&!["pending_upload","waiting_wave","queued","splitting"].includes(file.status)))||[]){
  const current=files.find(file=>file.id===previewId);
  $("preview-selected-name").textContent=current?current.name:"选择已准备好的文件";
  $("preview-select").title=current?current.name:"选择一个文件查看切片";
  $("preview-selected-count").hidden=!current;
  $("preview-selected-count").textContent=current?kindOf(current)==="excel"?"本地工作簿":`${current.paragraph_count} 个切片`:"";
  $("preview-file-count").textContent=`可预览 · ${files.length} 个文件`;
  $("preview-clear").hidden=!current;
  $("preview-file-empty").hidden=files.length>0;
  const signature=JSON.stringify(files.map(file=>[file.id,file.name,file.paragraph_count]));
  // Keep keyboard focus and scroll position when unrelated SSE events refresh the queue.
  if(signature!==previewFilesSignature){
    previewFilesSignature=signature;
    $("preview-file-list").replaceChildren(...files.map(file=>{
      const option=el("button","preview-file-option");option.type="button";
      option.setAttribute("role","option");option.dataset.fileId=file.id;option.title=file.name;
      option.append(el("span","picker-file-icon",formatNames[kindOf(file)]),el("span","picker-file-name",file.name),el("span","picker-file-count",kindOf(file)==="excel"?"本地工作簿":`${file.paragraph_count} 个切片`),el("span","picker-file-check","✓"));
      option.addEventListener("click",()=>selectPreview(file.id));return option;
    }));
  }
  for(const option of $("preview-file-list").children){
    const chosen=option.dataset.fileId===previewId;
    option.setAttribute("aria-selected",String(chosen));
    option.classList.toggle("selected",chosen);
  }
}
function setPreviewPickerOpen(open,focus=false){
  $("preview-file-panel").hidden=!open;
  $("preview-select").setAttribute("aria-expanded",String(open));
  if(focus)$("preview-select").focus({preventScroll:true});
}
$("preview-select").addEventListener("click",()=>setPreviewPickerOpen($("preview-file-panel").hidden));
$("preview-clear").addEventListener("click",()=>selectPreview(""));
$("preview-picker").addEventListener("keydown",event=>{
  if(event.key==="Escape"){event.preventDefault();setPreviewPickerOpen(false,true);return;}
  if(!["ArrowDown","ArrowUp","Home","End"].includes(event.key))return;
  event.preventDefault();setPreviewPickerOpen(true);
  const options=Array.from($("preview-file-list").children);
  if(!options.length)return;
  const current=options.indexOf(document.activeElement);
  let next;
  if(event.key==="Home")next=0;
  else if(event.key==="End")next=options.length-1;
  else if(current<0)next=event.key==="ArrowUp"?options.length-1:0;
  else next=(current+(event.key==="ArrowDown"?1:-1)+options.length)%options.length;
  options[next].focus();
});
document.addEventListener("click",event=>{
  if(!$("preview-picker").contains(event.target))setPreviewPickerOpen(false);
});
$("preview-picker").addEventListener("focusout",event=>{
  if(event.relatedTarget&&!$("preview-picker").contains(event.relatedTarget))setPreviewPickerOpen(false);
});
function clearPreview(){previewRequest++;setPreviewPickerOpen(false);renderPreviewPicker();$("preview-content").replaceChildren();$("preview-empty").hidden=false;$("preview-info").hidden=true;$("preview-pager").hidden=true;$("download-split").hidden=true;$("resplit").disabled=true;}
let workbookSheet=0,workbookOffset=0;
async function selectPreview(id, scroll=false){
  workbookSheet=0;workbookOffset=0;
  const wasOpen=!$("preview-file-panel").hidden;
  previewId=id;offset=0;renderPreviewPicker();setPreviewPickerOpen(false,wasOpen);
  if(!id){clearPreview();return;}
  await fetchPreview();if(scroll)$("preview-title").scrollIntoView({behavior:"smooth",block:"start"});
}
async function fetchPreview(){
  if(kindOf(batch?.files.find(f=>f.id===previewId)||{})==="excel")return fetchWorkbook();
  const id=previewId,requestId=++previewRequest;
  $("preview-empty").hidden=true;$("preview-content").replaceChildren(el("div","empty-state","正在加载这个文件的切片…"));$("preview-pager").hidden=true;
  try {
    const data=await api(`/api/ingestion/files/${id}/preview?offset=${offset}&limit=20`);
    if(requestId!==previewRequest||id!==previewId)return;
    previewTotal=data.total;$("preview-content").replaceChildren(...data.paragraphs.map(p=>{
      const chunk=el("article","chunk"),head=el("div","chunk-heading");
      head.append(el("span","chunk-index",`CHUNK ${String(p.position+1).padStart(2,"0")}`),el("h3","",p.title.trim()||"无标题段落"),el("small","",`${p.content.length} 字符`));
      const body=el("div","chunk-body");body.innerHTML=p.html; // Server sanitizes HTML, tables and image URLs before returning the preview.
      for(const img of body.querySelectorAll("img")){
        img.loading="lazy";
        const error=el("span","preview-image-error");error.hidden=true;
        const retry=el("button","button quiet small","重试加载");retry.type="button";
        error.append(el("span","","本地图片暂未加载，请重试或查看本地原图。"),retry);img.after(error);
        img.addEventListener("error",()=>{img.hidden=true;error.hidden=false;});
        img.addEventListener("load",()=>{img.hidden=false;error.hidden=true;});
        retry.addEventListener("click",()=>{error.hidden=true;img.hidden=false;img.src=img.getAttribute("src");});
      }
      chunk.append(head,body);return chunk;
    }));
    $("preview-content").scrollTop=0;$("preview-info").hidden=false;$("preview-info").replaceChildren(el("strong","",data.name),el("span","",`共 ${data.total} 个切片 · 当前 ${offset+1}–${Math.min(offset+20,data.total)} · 仅展示这个文件`));
    if(data.local_image_count)$("preview-info").append(localImageGallery(id,data.local_image_count,data.unmapped_image_count));
    const file=batch.files.find(f=>f.id===id);
    $("preview-pager").hidden=data.total<=20;$("preview-page").textContent=`${Math.floor(offset/20)+1} / ${Math.ceil(data.total/20)}`;
    $("preview-prev").disabled=offset===0;$("preview-next").disabled=offset+20>=data.total;
    $("download-split").hidden=false;$("download-split").href=`/api/ingestion/files/${id}/download?artifact=split`;
    $("resplit").disabled=!file||!["review","import_rejected"].includes(file.status);
  }catch(error){if(requestId===previewRequest){$("preview-content").replaceChildren(el("div","empty-state",error.message));notice(error.message,true);}}
}
function localImageGallery(fileId,count,unmapped){
  const details=el("details","local-image-gallery"),summary=el("summary","",`本地原图 · ${count} 张`);
  const note=el("p","",unmapped?"部分图片无法与切片可靠关联，可在这里查看原图。":"直接读取这个文件保留的 MinerU 图片。");
  const content=el("div","local-image-grid"),pager=el("div","local-image-pager");
  details.append(summary,note,content,pager);
  let loaded=false,start=0,request=0;
  async function load(){
    loaded=true;const current=++request;
    content.replaceChildren(el("p","muted","正在读取本地图片…"));pager.replaceChildren();
    try{
      const page=await api(`/api/ingestion/files/${fileId}/local-images?offset=${start}&limit=12`);
      if(current!==request||fileId!==previewId||!details.isConnected)return;
      content.replaceChildren(...page.images.map(asset=>{
        const link=el("a","local-image-item");link.href=asset.url;link.target="_blank";link.rel="noopener";link.title=asset.name;
        const img=document.createElement("img");img.src=asset.url;img.alt=asset.name;img.loading="lazy";img.decoding="async";
        link.append(img,el("span","",asset.name));return link;
      }));
      const prev=el("button","button quiet small","上一页"),next=el("button","button quiet small","下一页");
      prev.type=next.type="button";prev.disabled=start===0;next.disabled=start+12>=page.total;
      prev.addEventListener("click",()=>{start=Math.max(0,start-12);load();});
      next.addEventListener("click",()=>{start+=12;load();});
      pager.append(prev,el("span","",`${start+1}–${Math.min(start+12,page.total)} / ${page.total}`),next);
    }catch(error){if(current===request&&details.isConnected){content.replaceChildren(el("p","muted",error.message));loaded=false;}}
  }
  details.addEventListener("toggle",()=>{if(details.open&&!loaded)load();});return details;
}
$("preview-prev").addEventListener("click",()=>{offset=Math.max(0,offset-20);fetchPreview();});
$("preview-next").addEventListener("click",()=>{if(offset+20<previewTotal){offset+=20;fetchPreview();}});
$("resplit").addEventListener("click",async()=>{if(!previewId)return;const id=previewId;$("resplit").disabled=true;try{await api(`/api/ingestion/files/${id}/resplit`,{method:"POST"});connectEvents(true);previewId="";clearPreview();notice("已排队重新切片，完成后请选择这个文件重新预览。");}catch(error){notice(error.message,true);}});
$("import-selected").addEventListener("click",()=>{
  importIds=[...selected];importBatchId=batch.id;if(!importIds.length)return;
  const files=batch.files.filter(f=>importIds.includes(f.id));
  $("import-description").textContent=`将 ${files.length} 个文件导入 MaxKB。PDF/Word 使用已预览的切片，Excel 使用各文件选择的表格或问答对模式。`;
  $("import-target").replaceChildren();
  for(const [label,text]of [["MaxKB",batch.config.maxkb_base_url],["工作空间",batch.config.workspace_id],["知识库",batch.config._knowledge_plan?[...new Set(batch.files.filter(f=>selected.has(f.id)).map(fileKnowledgeName))].join("、"):batch.config.knowledge_id]])$("import-target").append(el("dt","",label),el("dd","",text));
  $("import-dialog").showModal();
});
for(const id of ["import-close","import-cancel"])$(id).addEventListener("click",()=>$("import-dialog").close());
$("import-confirm").addEventListener("click",async()=>{
  const targetBatch=importBatchId,ids=[...importIds];$("import-confirm").disabled=true;
  try{await api(`/api/ingestion/batches/${targetBatch}/import`,jsonOptions({file_ids:ids}));$("import-dialog").close();if(batch?.id===targetBatch)selected.clear();renderBatch();notice(`已提交 ${ids.length} 个文件入库，请等待结果。`);}
  catch(error){notice(error.message,true);$("import-dialog").close();}
  finally{$("import-confirm").disabled=false;}
});
setInterval(()=>{if(workbenchVisible()&&batch?.files.some(f=>activeStatuses.has(f.status)))renderBatch();},5000);
window.addEventListener("beforeunload",event=>{if(uploading){event.preventDefault();event.returnValue="";}});
window.addEventListener("pagehide",()=>{source?.close();source=null;queueController?.abort();window.MinerURecords?.suspend();});
window.addEventListener("pageshow",event=>{if(event.persisted)workbenchActivityChanged();});
function newQueueId(){
  const bytes=crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
  const text=Array.from(bytes,b=>b.toString(16).padStart(2,"0")).join("");return `${text.slice(0,8)}-${text.slice(8,12)}-${text.slice(12,16)}-${text.slice(16,20)}-${text.slice(20)}`;
}
function renderFolderQueue(){
  if(!folderQueue)return;$("folder-queue").hidden=false;
  const signature=JSON.stringify([folderQueue,batch?.id,wavePage,uploading]);
  if(signature===folderRenderSignature)return;folderRenderSignature=signature;
  window.KnowledgeUI?.renderTargets(folderQueue);
  const waves=folderQueue.batches,uploaded=waves.filter(w=>!["pending_upload","uploading"].includes(w.wave_state)).reduce((n,w)=>n+w.total,0);
  const completed=waves.filter(w=>w.wave_state==="completed").length;
  const server=!!folderQueue.source_key;
  document.querySelector(".queue-hint").textContent=server?"保存完成后开始处理；复制失败可在文件同步中重试。":"全部上传后开始处理；中断可继续上传。";
  $("folder-summary").textContent=`共 ${folderQueue.total} 个文件 · 每批 ${folderQueue.batch_size} 个 · ${waves.length} 批 · ${server?"已保存":"已上传"} ${uploaded} 个 · 已完成 ${completed} 批 · 入库 ${waves.reduce((n,w)=>n+w.imported,0)} 个 · 异常 ${waves.reduce((n,w)=>n+w.failed,0)} 个 · ${folderQueue.auto_import?"自动入库":"手动确认入库"}`;
  const stateLabels={stopped:"任务已停止，可清理历史记录",receiving:"等待全部原文件到齐",paused:"已暂停，请核对异常文件",processing:folderQueue.auto_import?"分批处理中":"分批处理 / 等待手动入库确认",completed:"全部入库完成",completed_with_errors:"处理结束，存在失败文件"};
  $("folder-summary").textContent += ` · ${stateLabels[folderQueue.state]||""}`;
  $("queue-stop").disabled=folderQueue.state==="stopped";
  $("queue-report").href=`/api/ingestion/queues/${folderQueue.id}/report`;
  const problems=folderQueue.problems||[],problemTotal=folderQueue.problem_total??problems.length,problemOffset=folderQueue.problem_offset||0;
  $("queue-problems").hidden=!problemTotal;$("queue-problems-pager").hidden=!problemTotal;
  $("queue-problems").replaceChildren(...problems.map(p=>{const item=el("div","file-error",`第 ${p.ordinal+1} 批 · ${p.relative_path||p.name} · ${statuses[p.status]||p.status} · ${p.error||"请查看记录"}`);const open=el("button","button quiet small","打开文件所在批次");open.addEventListener("click",()=>loadBatch(waves.find(w=>w.ordinal===p.ordinal).id).catch(e=>notice(e.message,true)));item.append(open);return item;}));
  $("queue-problems-page").textContent=`异常第 ${Math.floor(problemOffset/50)+1} / ${Math.max(1,Math.ceil(problemTotal/50))} 页 · 共 ${problemTotal} 个`;
  $("queue-problems-prev").disabled=problemOffset===0;$("queue-problems-next").disabled=problemOffset+50>=problemTotal;
  $("resume-upload").hidden=server||folderQueue.state==="stopped"||uploaded===folderQueue.total;$("resume-upload").disabled=uploading;
  $("wave-list").replaceChildren(...waves.slice(wavePage*20,wavePage*20+20).map(w=>{
    const labels={pending_upload:server?"待保存":"待上传",uploading:server?"保存中":"上传中",waiting:"等待前批",active:"处理中",completed:"完成"};
    const button=el("button","button secondary small"+(batch?.id===w.id?" chosen":""),`第 ${w.ordinal+1} 批 · ${w.verification==="unresolved"?"待核对":w.verification==="completed_with_errors"?"完成（有失败）":labels[w.wave_state]||w.wave_state}`);
    button.addEventListener("click",()=>loadBatch(w.id).then(renderFolderQueue).catch(e=>notice(e.message,true)));return button;
  }));
  $("wave-page").textContent=`${wavePage+1} / ${Math.max(1,Math.ceil(waves.length/20))}`;
  $("wave-prev").disabled=wavePage===0;$("wave-next").disabled=(wavePage+1)*20>=waves.length;
}
async function refreshFolderQueue({force=false,offset=null}={}){
  if(!folderQueue||(queueRefreshing&&!force))return;
  queueController?.abort();const own=new AbortController(),serial=++queueRequest;queueController=own;queueRefreshing=true;
  const id=folderQueue.id,start=offset??folderQueue.problem_offset??0;
  $("queue-refresh").setAttribute("aria-busy","true");
  try{
    const snapshot=await api(`/api/ingestion/queues/${id}?problem_offset=${start}&problem_limit=50`,{signal:own.signal});
    if(folderQueue?.id!==id||serial!==queueRequest||own.signal.aborted)return;
    if(start&&start>=(snapshot.problem_total||0))return await refreshFolderQueue({force:true,offset:Math.max(0,Math.ceil((snapshot.problem_total||0)/50)-1)*50});
    folderQueue=snapshot;$("queue-refresh-error").hidden=true;renderFolderQueue();
  }catch(error){
    if(folderQueue?.id!==id||serial!==queueRequest||own.signal.aborted||error.name==="AbortError")return;
    $("queue-refresh-error").textContent=`队列刷新失败：${error.message}`;$("queue-refresh-error").hidden=false;throw error;
  }finally{if(serial===queueRequest){queueRefreshing=false;queueController=null;$("queue-refresh").setAttribute("aria-busy","false");}}
}
$("queue-problems-prev").addEventListener("click",()=>refreshFolderQueue({force:true,offset:Math.max(0,(folderQueue?.problem_offset||0)-50)}).catch(()=>{}));
$("queue-problems-next").addEventListener("click",()=>refreshFolderQueue({force:true,offset:(folderQueue?.problem_offset||0)+50}).catch(()=>{}));
$("queue-stop").addEventListener("click",async()=>{
  if(!folderQueue)return;const button=$("queue-stop");button.disabled=true;
  try{await api(`/api/ingestion/queues/${folderQueue.id}/stop`,{method:"POST"});await refreshFolderQueue({force:true});if(batch)await loadBatch(batch.id);notice("已请求停止任务；失败及待处理文件已停止，执行中的请求退出后即可清理历史。");}
  catch(e){notice(e.message,true);}finally{button.disabled=folderQueue?.state==="stopped";}
});
$("queue-refresh").addEventListener("click",()=>{connectEvents(true);window.MinerURecords?.update(true);refreshFolderQueue({force:true}).catch(()=>{});});
$("wave-prev").addEventListener("click",()=>{wavePage=Math.max(0,wavePage-1);renderFolderQueue();});
$("wave-next").addEventListener("click",()=>{wavePage++;renderFolderQueue();});
$("resume-upload").addEventListener("click",()=>{resumeRequested=true;pending=[];showPending();$("folder-input").click();notice("请选择原来的完整文件夹，然后点击开始解析继续上传。");});
function operationToken(){
  const token=form.elements.maxkb_token.value.trim()||null;
  const current=form.elements.maxkb_base_url.value.trim().replace(/\/+$/,""),target=batch.config.maxkb_base_url.replace(/\/+$/,"");
  if(token&&current!==target)throw new Error(`当前填写的令牌属于另一个 API 地址。请切回本批次的 ${batch.config.maxkb_base_url} 并填写该服务的令牌。`);
  return token;
}
async function reimportFile(file){
  let token;try{token=operationToken();}catch(e){notice(e.message,true);return;}
  if(!window.confirm(`重新入库 ${file.name}：将先删除本次记录关联的 MaxKB 文档，确认清理完成后重新切片和入库。目标：${batch.config.maxkb_base_url}，${batch.config.workspace_id} / ${fileKnowledgeName(file)}。继续？`))return;
  try{const result=await api(`/api/ingestion/files/${file.id}/retry-import`,jsonOptions({confirm:true,maxkb_token:token}));applyFileUpdate(result);renderBatch();notice("已排队；删除未确认成功时会停止重建。");}catch(e){notice(e.message,true);}
}
async function runFileCheck(file,action){
  if(batch?.id!==file.batch_id||fileFeedback.get(file.id)?.pending)return;
  const batchId=batch.id,feedback={pending:true,error:false,message:"正在核对此文件…"};
  fileFeedback.set(file.id,feedback);renderBatch();
  try{
    feedback.message=await action();
  }catch(error){feedback.error=true;feedback.message=error.message||"核对未完成，请稍后重试。";}
  finally{
    feedback.pending=false;
    feedback.finalStatus=batch?.id===batchId?batch.files.find(item=>item.id===file.id)?.status:file.status;
    if(fileFeedback.get(file.id)===feedback&&batch?.id===batchId)renderBatch();
  }
}
async function reconcileFile(file){
  return runFileCheck(file,async()=>{
    const token=operationToken();
    let result=await api(`/api/ingestion/files/${file.id}/reconcile`,jsonOptions({confirm_absent:false,maxkb_token:token}));
    if(result.action==="confirm_absent"){
      if(!window.confirm(result.message+"\n只有你确认文档确实不存在且未被重命名，才继续解除限制。"))return "已取消核对，未解除限制。";
      result=await api(`/api/ingestion/files/${file.id}/reconcile`,jsonOptions({confirm_absent:true,maxkb_token:token}));
    }
    if(result.file)applyFileUpdate(result.file);
    return "核对已完成，请查看此文件的最新状态。";
  });
}
async function resolvePreparation(file){
  if(batch?.id!==file.batch_id||fileFeedback.get(file.id)?.pending)return;
  if(!window.confirm("请先在 MaxKB 核对本次文件的 OSS/切片请求，并处理遗留资源。确认已经处理后才能恢复；不能根据文件名猜测资源并删除。是否已完成核对？"))return;
  return runFileCheck(file,async()=>{
    const result=await api(`/api/ingestion/files/${file.id}/resolve-preparation`,jsonOptions({confirm:true}));
    applyFileUpdate(result.file);
    return "资源核对已确认，请查看此文件的最新状态。";
  });
}
async function showAudit(file){
  const request=++auditRequest;
  $("audit-dialog").showModal();$("audit-content").replaceChildren(el("p","muted","正在加载记录…"));
  try{
    const [attempts,logs,trace]=await Promise.all([api(`/api/ingestion/files/${file.id}/imports`),api(`/api/ingestion/files/${file.id}/logs?latest=true&limit=200`),api(`/api/ingestion/files/${file.id}/provenance`)]);
    if(request!==auditRequest||!$("audit-dialog").open)return;
    const content=$("audit-content");content.replaceChildren(el("h3","",file.name));
    if(trace.source){const r=trace.source;const box=el("section","audit-attempt");box.append(el("strong","","原文件来源"),el("p","",`${r.source_kind} · ${r.source_path} · ${r.kind} · ${r.bytes} 字节`),el("p","",`本地保存：${r.local_path}`),el("p","",`SHA-256：${r.sha256}`),el("p","",`来源任务：${r.source_job_id||r.source_key}`));content.append(box);}
    for(const r of trace.tag_receipts||[])content.append(el("p","audit-line",`标签已绑定 · 文档 ${r.document_id} · 标签 ${r.tag_ids} · ${new Date(r.applied_at*1000).toLocaleString()}`));
    for(const r of trace.resources)content.append(el("p","audit-line",`OSS ${r.resource_id} · ${r.role} · ${r.deleted_at?(r.cleanup_via==="document_delete"?"随文档删除（MaxKB 关联清理）":"已确认删除"):r.retained_at?"已保留（无确认关联文档）":"已记录，尚未删除"}`));
    for(const c of trace.calls)content.append(el("p","audit-line",`上游请求 ${c.id} · ${c.kind} · ${c.state}${c.error?" · "+c.error:""}`));
    for(const attempt of attempts){
      const box=el("section","audit-attempt");box.append(el("strong","",`${new Date(attempt.created_at*1000).toLocaleString()} · ${importPhases[attempt.phase]||attempt.phase} · ${attempt.mode==="qa"?"问答对":attempt.mode==="table"?"表格":"文档切片"}`));
      if(attempt.error)box.append(el("p","file-error",attempt.error));
      for(const doc of attempt.documents)box.append(el("p","",`${doc.name} · ${doc.document_id} · ${remoteStates[doc.state]||doc.state} · 编码 ${doc.raw_status||"待查询"}${doc.deleted_at?" · 已清理":""}`));
      content.append(box);
    }
    for(const log of logs){const detail=JSON.parse(log.detail);const when=new Date(log.created_at*1000).toLocaleTimeString();if(log.event==="oss_deleted"){content.append(el("p","audit-line",`${when} · OSS ${detail.id} 删除确认 · code=${detail.response?.code} · data=${detail.response?.data}`));continue;}if(log.event==="document_deleted"){content.append(el("p","audit-line",`${when} · 文档 ${detail.document_id} 删除确认 · code=${detail.response?.code} · data=${detail.response?.data} · 关联源文件 ${detail.source_file_id||"未记录"} 由 MaxKB 清理`));continue;}if(log.event==="oss_retained"){content.append(el("p","audit-line",`${when} · OSS ${detail.id} 已保留 · ${detail.message}`));continue;}content.append(el("p","audit-line",`${when} · ${statuses[log.event]||log.event} · ${detail.message||""}${detail.error?" · "+detail.error:""}`));}
  }catch(e){if(request===auditRequest&&$("audit-dialog").open)$("audit-content").replaceChildren(el("p","file-error",e.message));}
}
$("audit-close").addEventListener("click",()=>$("audit-dialog").close());
$("audit-dialog").addEventListener("close",()=>auditRequest++);
window.WorkbenchActivity={changed:workbenchActivityChanged,sync:connectEvents};
document.addEventListener("visibilitychange",()=>{workbenchActivityChanged();if(!document.hidden)window.SourceUI?.pageChanged();});
setInterval(pollWorkbench,5000);
init();

async function fetchWorkbook(){
  const id=previewId,requestId=++previewRequest;
  $("preview-empty").hidden=true;$("preview-pager").hidden=true;$("resplit").disabled=true;$("download-split").hidden=true;
  $("preview-content").replaceChildren(el("p","empty-state","正在读取本地工作簿…"));
  try{
    const data=await api(`/api/ingestion/files/${id}/workbook?sheet=${workbookSheet}&offset=${workbookOffset}&limit=20`);
    if(requestId!==previewRequest||id!==previewId)return;
    $("preview-info").hidden=false;$("preview-info").replaceChildren(el("strong","",data.name),el("p","",`原始表格预览 · ${data.mode==="qa"?"问答对 qa":"表格 table"} · 最多显示 30 列，单元格最多 2000 字符。最终段落由 MaxKB 生成，请确保格式符合对应模板。`));
    const toolbar=el("div","workbook-toolbar"),select=document.createElement("select");select.setAttribute("aria-label","预览工作表");
    data.sheets.forEach((name,index)=>{const option=el("option","",name);option.value=index;select.append(option);});select.value=workbookSheet;
    select.addEventListener("change",()=>{workbookSheet=Number(select.value);workbookOffset=0;fetchWorkbook();});toolbar.append(select);
    const table=el("table","workbook-table"),tbody=document.createElement("tbody");
    data.rows.forEach((row,index)=>{const tr=document.createElement("tr");tr.append(el("th","",workbookOffset+index+1));for(const value of row)tr.append(el("td","",value));tbody.append(tr);});table.append(tbody);
    const wrap=el("div","workbook-scroll");wrap.append(table);const pager=el("div","preview-pager"),prev=el("button","button secondary small","前 20 行"),next=el("button","button secondary small","后 20 行");
    prev.disabled=workbookOffset===0;next.disabled=workbookOffset+20>=Math.min(data.total_rows||0,10020);
    prev.addEventListener("click",()=>{workbookOffset=Math.max(0,workbookOffset-20);fetchWorkbook();});next.addEventListener("click",()=>{workbookOffset+=20;fetchWorkbook();});
    pager.append(prev,el("span","",`第 ${workbookOffset+1}–${workbookOffset+data.rows.length} 行 / ${data.total_rows||"未知"}`),next);
    $("preview-content").replaceChildren(toolbar,wrap,pager);
  }catch(e){if(requestId===previewRequest){$("preview-content").replaceChildren(el("p","empty-state",e.message));notice(e.message,true);}}
}

"use strict";
(() => {
  const pageSize=25;
  let view="batches",offset=0,total=0,request=0,controller=null,timer,expanded=null;
  const time=value=>new Date(value*1000).toLocaleString("zh-CN");
  const batchTitle=item=>item.queue_id?`${item.queue_name||"分批队列"} · 第 ${(item.ordinal??item.batch_ordinal??0)+1} 批`:item.batch_name||item.name;
  function duration(value){
    if(typeof value!=="number"||!Number.isFinite(value)||value<0)return "—";
    if(value>0&&value<0.1)return "< 0.1 秒";
    if(value<60)return `${Math.round(value*10)/10} 秒`;
    const seconds=Math.floor(value),days=Math.floor(seconds/86400),hours=Math.floor(seconds%86400/3600),minutes=Math.floor(seconds%3600/60);
    return [days?`${days} 天`:"",hours?`${hours} 小时`:"",minutes?`${minutes} 分`:"",`${seconds%60} 秒`].filter(Boolean).join(" ");
  }
  function timingRow(item,aggregate=false){
    const t=item.timing||{},row=el("div","history-timings"),suffix=aggregate?"累计":"";
    row.title="总历时从任务创建计至结束，包含上传、排队和人工等待；未结束任务统计至本次刷新。阶段耗时取各文件最近记录；累计值包含并发执行的时间，不能与总历时直接相加。— 表示未执行或尚无可靠计时。";
    for(const [label,key] of [[`总历时${t.running?" · 未结束":""}`,"elapsed_seconds"],[`MinerU ${suffix}`,"parse_seconds"],[`切片${suffix}`,"split_seconds"],[`入库${suffix}`,"import_seconds"]]){
      const chip=el("span","history-timing");chip.append(el("span","",label.trim()),el("strong","",duration(t[key])));row.append(chip);
    }
    return row;
  }
  function closeBatch(){
    if(!expanded)return;
    expanded.controller?.abort();expanded.panel.hidden=true;expanded.panel.replaceChildren();
    expanded.button.textContent="查看文件";expanded.button.setAttribute("aria-expanded","false");expanded=null;
  }
  async function refresh(){
    if(currentPage()!=="history")return;
    closeBatch();controller?.abort();const own=new AbortController(),serial=++request;controller=own;
    $("history-results").setAttribute("aria-busy","true");$("history-error").hidden=true;
    const query=new URLSearchParams({view,offset,limit:pageSize,search:$("history-search").value.trim(),state:$("history-state").value});
    try{
      const result=await api(`/api/ingestion/history?${query}`,{signal:own.signal});
      if(serial!==request||currentPage()!=="history")return;
      total=result.total;
      if(offset&&offset>=total){offset=Math.max(0,Math.ceil(total/pageSize)-1)*pageSize;return refresh();}
      render(result);
    }catch(error){
      if(serial!==request||error.name==="AbortError")return;
      $("history-results").replaceChildren();$("history-pager").hidden=true;
      $("history-error").textContent=`历史记录加载失败：${error.message}`;$("history-error").hidden=false;
    }finally{if(serial===request){controller=null;$("history-results").setAttribute("aria-busy","false");}}
  }
  function openButton(id){
    const button=el("button","button secondary small","打开批次");button.disabled=!id;
    button.addEventListener("click",async()=>{button.disabled=true;try{await loadBatch(id);}catch(error){notice(error.message,true);}finally{button.disabled=!id;}});return button;
  }
  function stopButton(path){
    const button=el("button","button quiet small","停止任务");
    button.addEventListener("click",async()=>{button.disabled=true;try{await api(path,{method:"POST"});await refresh();notice("已请求停止。仍在执行的请求退出后，可清除历史记录。");}catch(e){notice(e.message,true);button.disabled=false;}});return button;
  }
  function fileRow(item){
    const row=el("article","history-record history-file"),info=el("div","history-record-info"),actions=el("div","history-record-actions");
    info.append(el("strong","",item.relative_path||item.name),el("small","",`${formatNames[item.kind]||item.kind} · ${size(item.bytes)} · ${time(item.created_at)}`));
    info.append(el("span",`status ${item.status}`,statuses[item.status]||item.status));
    info.append(timingRow(item));
    if(item.error)info.append(el("p","file-error",item.error));
    const logs=el("button","button quiet small","记录");logs.addEventListener("click",()=>showAudit(item));
    if(!["imported","stopped"].includes(item.status))actions.append(stopButton(`/api/ingestion/files/${item.id}/stop`));
    actions.append(logs,openButton(item.batch_id));row.append(info,actions);return row;
  }
  async function loadBatchFiles(entry){
    entry.controller?.abort();const own=new AbortController();entry.controller=own;
    entry.panel.replaceChildren(el("p","history-empty","正在加载本批文件…"));
    try{
      const query=new URLSearchParams({view:"files",batch_id:entry.id,offset:entry.offset,limit:pageSize,state:entry.state});
      const result=await api(`/api/ingestion/history?${query}`,{signal:own.signal});
      if(expanded!==entry||entry.controller!==own||currentPage()!=="history")return;
      if(entry.offset&&entry.offset>=result.total){entry.offset=Math.max(0,Math.ceil(result.total/pageSize)-1)*pageSize;return loadBatchFiles(entry);}
      entry.panel.replaceChildren(...(result.items.length?result.items.map(fileRow):[el("p","history-empty","本批次没有符合筛选状态的文件")]));
      const pager=el("div","preview-pager"),prev=el("button","button secondary small","上一页"),next=el("button","button secondary small","下一页");
      prev.disabled=entry.offset===0;next.disabled=entry.offset+pageSize>=result.total;
      prev.addEventListener("click",()=>{entry.offset=Math.max(0,entry.offset-pageSize);loadBatchFiles(entry);});
      next.addEventListener("click",()=>{entry.offset+=pageSize;loadBatchFiles(entry);});
      pager.append(prev,el("span","",`第 ${Math.floor(entry.offset/pageSize)+1} / ${Math.max(1,Math.ceil(result.total/pageSize))} 页 · ${entry.state==="all"?"本批":"符合状态"} ${result.total} 个文件`),next);entry.panel.append(pager);
    }catch(error){
      if(expanded!==entry||entry.controller!==own||error.name==="AbortError")return;
      const retry=el("button","button secondary small","重试加载");retry.addEventListener("click",()=>loadBatchFiles(entry));
      entry.panel.replaceChildren(el("p","file-error history-empty",error.message),retry);
    }
  }
  function batchCard(item){
    const card=el("section","history-batch"),row=el("div","history-record"),info=el("div","history-record-info"),actions=el("div","history-record-actions");
    const title=el("h3","history-batch-title",batchTitle(item));
    info.append(title,el("small","",`${time(item.created_at)} · 批次 ${item.id.slice(0,8)}`),el("span","history-record-meta",`${item.files||0} 个文件 · ${item.imported||0} 已入库 · ${item.review||0} 待入库 · ${item.failed||0} 异常`));
    info.append(timingRow(item,true));
    const panel=el("div","history-batch-files"),button=el("button","button quiet small","查看文件");panel.hidden=true;panel.id=`history-batch-${item.id}`;
    card.setAttribute("aria-labelledby",title.id=`history-title-${item.id}`);
    button.setAttribute("aria-expanded","false");button.setAttribute("aria-controls",panel.id);
    button.addEventListener("click",()=>{
      if(expanded?.id===item.id){closeBatch();return;}closeBatch();
      expanded={id:item.id,panel,button,offset:0,controller:null,state:$("history-state").value};panel.hidden=false;button.textContent="收起文件";button.setAttribute("aria-expanded","true");loadBatchFiles(expanded);
    });
    actions.append(stopButton(`/api/ingestion/batches/${item.id}/stop`),button,openButton(item.id));row.append(info,actions);card.append(row,panel);return card;
  }
  function groupedFiles(items){
    const groups=new Map();
    for(const item of items){
      if(!groups.has(item.batch_id)){
        const section=el("section","history-batch"),heading=el("div","history-file-group-heading");
        heading.append(el("h3","history-batch-title",batchTitle(item)),el("small","",`${time(item.batch_created_at)} · 批次 ${item.batch_id.slice(0,8)}`));
        section.append(heading);groups.set(item.batch_id,section);
      }
      groups.get(item.batch_id).append(fileRow(item));
    }
    return [...groups.values()];
  }
  function render(result){
    const s=result.summary;
    $("history-summary").replaceChildren(...[[s.batches,"历史批次"],[s.files,"全部文件"],[s.imported,"已入库"],[s.failed,"异常文件"]].map(([value,label])=>{
      const item=el("div","history-stat");item.append(el("strong","",String(value)),el("span","",label));return item;
    }));
    $("history-results").replaceChildren(...(result.items.length?(view==="batches"?result.items.map(batchCard):groupedFiles(result.items)):[el("p","history-empty","没有符合条件的历史记录")]));
    $("history-pager").hidden=false;
    $("history-page").textContent=`第 ${Math.floor(offset/pageSize)+1} / ${Math.max(1,Math.ceil(total/pageSize))} 页 · ${total} ${view==="batches"?"批":"个文件"}`;
    $("history-prev").disabled=offset===0;$("history-next").disabled=offset+pageSize>=total;
  }
  for(const button of document.querySelectorAll("[data-history-view]"))button.addEventListener("click",()=>{
    view=button.dataset.historyView;offset=0;
    for(const tab of document.querySelectorAll("[data-history-view]"))tab.setAttribute("aria-pressed",String(tab===button));
    refresh();
  });
  $("history-search").addEventListener("input",()=>{clearTimeout(timer);timer=setTimeout(()=>{offset=0;refresh();},300);});
  $("history-state").addEventListener("change",()=>{offset=0;refresh();});
  $("history-refresh").addEventListener("click",()=>refresh());
  $("history-prev").addEventListener("click",()=>{offset=Math.max(0,offset-pageSize);refresh();});
  $("history-next").addEventListener("click",()=>{if(offset+pageSize<total){offset+=pageSize;refresh();}});
  window.HistoryUI={pageChanged(page){clearTimeout(timer);if(page==="history")refresh();else{closeBatch();controller?.abort();request++;controller=null;$("history-results").setAttribute("aria-busy","false");}}};
  window.HistoryUI.pageChanged(currentPage());
})();

"use strict";
(() => {
  const dialog=$("history-cleanup-dialog"),form=$("history-cleanup-form");
  let revision="",running=false,canClear=false,previewRequest=0;
  function busy(value){
    running=value;
    for(const node of form.querySelectorAll("button,input"))node.disabled=value;
    $("history-cleanup-confirm").disabled=value||!canClear;
    $("history-cleanup-confirm").textContent=value?"正在清理…":"确认清除历史";
  }
  async function preview(){
    const request=++previewRequest;
    revision="";canClear=false;$("history-cleanup-confirm").disabled=true;
    $("history-cleanup-message").textContent="正在核对任务与文件…";
    $("history-cleanup-retry").hidden=true;
    try{
      const result=await api("/api/ingestion/history/cleanup"),c=result.counts;
      if(request!==previewRequest||!dialog.open)return;
      revision=result.revision;
      canClear=result.can_clear;
      $("history-cleanup-summary").textContent=`全部 ${c.batches} 个批次 · ${c.history_files} 个文件处理记录`;
      $("cleanup-web-count").textContent=`${c.browser} 条关联文件记录及解析结果`;
      $("cleanup-ssh-count").textContent=`${c.sync_files} 条同步文件记录 · ${c.ssh} 条导入文件记录及解析结果`;
      $("history-cleanup-message").textContent=result.can_clear?"未勾选的本地文件保留。参数设置、SSH 连接配置和映射目录原文件保留。":`暂不能清理：${result.blocked_count} 项任务仍在执行或正在停止。`;
      $("history-cleanup-blocked").replaceChildren(...result.blocked.map(item=>el("p","file-error",`${item.name} · ${statuses[item.status]||item.status}`)));
      if(result.warning_count){
        $("history-cleanup-message").textContent+=` ${result.warning_count} 条已停止或待核对记录可清除；仅清除本地历史，远端资源保留，核对线索保存在清理日志。`;
      }
      $("history-cleanup-confirm").disabled=!result.can_clear;
      $("history-cleanup-retry").hidden=!result.pending_cleanup;
      if(result.pending_cleanup)$("history-cleanup-message").textContent+=` 另有 ${result.pending_cleanup} 次文件清理未完成，可重试文件清理。`;
    }catch(error){if(request===previewRequest&&dialog.open)$("history-cleanup-message").textContent=error.message;}
  }
  $("history-cleanup-open").addEventListener("click",()=>{
    form.reset();$("history-cleanup-blocked").replaceChildren();dialog.showModal();preview();
  });
  for(const id of ["history-cleanup-close","history-cleanup-cancel"])$(id).addEventListener("click",()=>{if(!running)dialog.close();});
  dialog.addEventListener("cancel",event=>{if(running)event.preventDefault();});
  dialog.addEventListener("close",()=>{previewRequest++;revision="";canClear=false;});
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(running||!revision)return;
    const body={revision,confirmation:"清除历史记录",delete_web_files:$("cleanup-web-files").checked,delete_ssh_files:$("cleanup-ssh-files").checked};
    busy(true);
    try{
      const result=await api("/api/ingestion/history/cleanup",jsonOptions(body));
      resetHistoryWorkspace();dialog.close();
      notice(result.state==="completed"?"历史记录已清除，文件已按所选范围处理。":`历史已清除，但部分本地文件未删除。请重新打开清理窗口，点击“重试文件清理”。操作编号：${result.id}`,result.state!=="completed");
    }catch(error){
      busy(false);await preview();$("history-cleanup-message").textContent=error.message;return;
    }finally{busy(false);}
  });
  $("history-cleanup-retry").addEventListener("click",async()=>{
    busy(true);
    try{await api("/api/ingestion/history/cleanup/retry",{method:"POST"});}
    catch(error){notice(error.message,true);}
    finally{busy(false);await preview();}
  });
})();

"use strict";
(() => {
  const labels = {pending:"待解析",uploading:"上传至 MinerU",waiting:"MinerU 排队中",running:"解析中",succeeded:"解析成功",failed:"解析失败",stopped:"解析未完成"};
  const pageSize = 50;
  let scopeKey = "", pageOffset = 0, total = 0, request = 0, controller = null;
  let lastFetched = 0, signature = "", searchTimer,settled=false;
  function suspend(){controller?.abort();controller=null;request++;}

  function scope() {
    if(batch?.queue_id)return {queue_id:batch.queue_id};
    if(batch)return {batch_id:batch.id};
    if(folderQueue)return {queue_id:folderQueue.id};
    return null;
  }
  async function update(force = false) {
    if(currentPage() !== "workbench" || document.hidden)return;
    const context = scope(), key = JSON.stringify(context);
    if(key !== scopeKey) {
      scopeKey = key;pageOffset = 0;signature = "";settled=false;
      $("mineru-records-list").replaceChildren();
      $("mineru-summary").hidden = true;$("mineru-records-pager").hidden = true;
      $("mineru-records-message").textContent = context?"加载中…":"暂无 PDF 解析记录";
      $("mineru-records-message").hidden = false;
      force = true;
    }
    $("mineru-records-scope").textContent = context?.queue_id?"当前队列 · 全部批次":context?"当前批次":"未选择任务";
    if(!context) {controller?.abort();request++;return;}
    const localPending=batch?.files?.some(f=>f.kind==="pdf"&&["pending","uploading","waiting","running"].includes(f.parse_state));
    if(!force&&settled&&!localPending)return;
    if(!force && (controller || Date.now() - lastFetched < 3000))return;
    controller?.abort();
    const ownController = new AbortController(), current = ++request;
    controller = ownController;lastFetched = Date.now();
    const query = new URLSearchParams({...context, state:$("mineru-state").value,search:$("mineru-search").value.trim(),offset:pageOffset,limit:pageSize});
    try {
      const result = await api(`/api/ingestion/mineru/files?${query}`, {signal:ownController.signal});
      if(current !== request)return;
      settled=result.counts.pending===0&&result.counts.processing===0;
      total = result.total;
      if(pageOffset && pageOffset >= total) {
        pageOffset = Math.max(0, Math.ceil(total / pageSize) - 1) * pageSize;
        controller = null;return update(true);
      }
      render(result);
    } catch(error) {
      if(current !== request || error.name === "AbortError")return;
      // Do not leave stale results looking like a successful status refresh.
      signature = "";settled=false;$("mineru-records-list").replaceChildren();
      $("mineru-summary").hidden = true;$("mineru-records-pager").hidden = true;
      $("mineru-records-message").textContent = `解析记录加载失败：${error.message}`;
      $("mineru-records-message").hidden = false;
    } finally {
      if(current === request && controller === ownController)controller = null;
    }
  }
  function render(result) {
    const nextSignature = JSON.stringify([result, batch?.id]);
    if(signature === nextSignature)return;
    signature = nextSignature;
    const counts = result.counts;
    $("mineru-summary").hidden = false;
    $("mineru-total").textContent = Object.values(counts).reduce((a,b)=>a+b,0);
    for(const state of ["pending","processing","succeeded","failed"])$("mineru-"+state).textContent = counts[state];
    $("mineru-records-message").hidden = result.files.length > 0;
    $("mineru-records-message").textContent = $("mineru-state").value !== "all" || $("mineru-search").value.trim()?"没有匹配的 PDF 文件":"暂无 PDF 解析记录";
    $("mineru-records-list").replaceChildren(...result.files.map(file => {
      const row = el("div","mineru-row"), detail = el("div","mineru-detail");
      const name = el("div","file-name",file.name);name.title = file.relative_path || file.name;
      detail.append(name);
      const parts = [size(file.bytes), `第 ${file.ordinal + 1} 批`];
      if(file.parse_seconds != null)parts.push(`${file.parse_seconds.toFixed(1)}s`);
      if(file.parse_finished_at)parts.push(new Date(file.parse_finished_at*1000).toLocaleString("zh-CN"));
      detail.append(el("div","file-meta",parts.join(" · ")));
      if(file.relative_path && file.relative_path !== file.name)detail.append(el("div","file-meta mineru-path",file.relative_path));
      if(file.job_id)detail.append(el("div","file-meta mineru-job",file.job_id));
      if(file.parse_error)detail.append(el("div","file-error",file.parse_error));
      const controls = el("div","file-controls");
      const state = file.parse_state;
      const color = state === "succeeded"?"imported":["failed","stopped"].includes(state)?"failed":["uploading","waiting","running"].includes(state)?"processing":"";
      controls.append(el("span","status "+color, labels[state] || state));
      const audit = el("button","button quiet small","记录");
      audit.addEventListener("click",()=>showAudit(file));controls.append(audit);
      if(file.status === "failed") {
        const retry = el("button","button retry small",state === "succeeded"?"重试处理":"重试解析");
        retry.addEventListener("click",async()=>{
          retry.disabled = true;
          try {await retryFile(file);await update(true);}finally {retry.disabled = false;}
        });controls.append(retry);
      }
      if(file.batch_id !== batch?.id) {
        const open = el("button","button quiet small","打开批次");
        open.addEventListener("click",async()=>{try {await loadBatch(file.batch_id);$("queue-title").scrollIntoView({behavior:"smooth",block:"start"});}catch(error){notice(error.message,true);}});
        controls.append(open);
      }
      row.append(el("div","pdf-icon","PDF"),detail,controls);return row;
    }));
    $("mineru-records-pager").hidden = result.total <= pageSize;
    $("mineru-page").textContent = `第 ${Math.floor(pageOffset/pageSize)+1} / ${Math.max(1,Math.ceil(result.total/pageSize))} 页 · ${result.total} 个文件`;
    $("mineru-prev").disabled = pageOffset === 0;
    $("mineru-next").disabled = pageOffset + pageSize >= result.total;
  }
  $("mineru-state").addEventListener("change",()=>{pageOffset = 0;update(true);});
  $("mineru-search").addEventListener("input",()=>{clearTimeout(searchTimer);searchTimer = setTimeout(()=>{pageOffset = 0;update(true);},300);});
  $("mineru-refresh").addEventListener("click",()=>update(true));
  $("mineru-prev").addEventListener("click",()=>{pageOffset = Math.max(0,pageOffset-pageSize);update(true);});
  $("mineru-next").addEventListener("click",()=>{if(pageOffset+pageSize < total){pageOffset += pageSize;update(true);}});
  window.MinerURecords = {update,suspend};
  setInterval(()=>update(),5000);
  update();
})();

"use strict";
(() => {
  let tags=[],busy=false,targetPage=0,targetQueue="";
  const drafts=new Map();
  const dialog=$("knowledge-dialog");
  function groupPaths(paths,root){
    const result=new Map();let direct=0;
    for(let path of paths){
      if(root){if(!path.startsWith(root+"/"))throw new Error("文件不在所选根目录下");path=path.slice(root.length+1);}
      if(!path.includes("/")){direct++;continue;}
      const group=path.split("/")[0];result.set(group,(result.get(group)||0)+1);
    }
    return {groups:[...result].sort((a,b)=>a[0].localeCompare(b[0])),direct};
  }
  function tagRow(tag={key:"",value:""}){
    const row=el("div","knowledge-tag-row"),key=document.createElement("input"),value=document.createElement("input");
    key.placeholder="标签名称";key.maxLength=100;key.value=tag.key;key.setAttribute("aria-label","标签名称");
    value.placeholder="标签值";value.maxLength=200;value.value=tag.value;value.setAttribute("aria-label","标签值");
    const remove=el("button","button quiet small","移除");remove.type="button";remove.addEventListener("click",()=>row.remove());row.append(key,value,remove);return row;
  }
  function renderTags(){ $("knowledge-tag-list").replaceChildren(...tags.map(tagRow)); }
  async function loadTags(){tags=(await api("/api/ingestion/knowledge/tag-settings")).tags;renderTags();return tags;}
  $("knowledge-tag-add").addEventListener("click",()=>$("knowledge-tag-list").append(tagRow()));
  $("knowledge-tag-save").addEventListener("click",async()=>{
    const button=$("knowledge-tag-save");button.disabled=true;
    try{
      const values=[...$("knowledge-tag-list").children].map(row=>{const inputs=row.querySelectorAll("input");return {key:inputs[0].value.trim(),value:inputs[1].value.trim()};});
      if(values.some(t=>!t.key||!t.value))throw new Error("请填写每个标签的名称和值，或移除空行");
      tags=(await api("/api/ingestion/knowledge/tag-settings",jsonOptions({tags:values},"PUT"))).tags;renderTags();$("knowledge-tag-message").textContent="已保存；新任务确认时可为知识库或一个、多个文档分别选用。";
    }catch(e){$("knowledge-tag-message").textContent=e.message;}finally{button.disabled=false;}
  });
  function documentTagEditor(paths,choices,enabled){
    $("document-tag-editor").hidden=!enabled;
    const rules=[],selected=new Set(),chosen=new Set();let page=0;
    if(!enabled)return rules;
    $("document-tag-search").value="";$("document-tag-rules").replaceChildren();
    const filtered=()=>paths.filter(path=>path.toLowerCase().includes($("document-tag-search").value.trim().toLowerCase()));
    function render(){
      const matches=filtered(),pages=Math.max(1,Math.ceil(matches.length/30));page=Math.min(page,pages-1);
      const visible=matches.slice(page*30,(page+1)*30);
      $("document-tag-files").replaceChildren(...visible.map(path=>{
        const label=el("label","checkbox-label document-tag-file"),input=document.createElement("input");input.type="checkbox";input.checked=selected.has(path);
        input.addEventListener("change",()=>{input.checked?selected.add(path):selected.delete(path);render();});label.append(input,el("span","",path));return label;
      }));
      $("document-tag-selected").textContent=`已选 ${selected.size} 个文件`;
      $("document-tag-page").textContent=`第 ${page+1}/${pages} 页 · ${matches.length} 个文件`;
      $("document-tag-prev").disabled=page===0;$("document-tag-next").disabled=page>=pages-1;
      $("document-tag-select-page").checked=visible.length>0&&visible.every(path=>selected.has(path));
      $("document-tag-add-rule").disabled=!selected.size||!chosen.size;
    }
    $("document-tag-search").oninput=()=>{page=0;render();};
    $("document-tag-prev").onclick=()=>{page--;render();};$("document-tag-next").onclick=()=>{page++;render();};
    $("document-tag-select-page").onchange=e=>{for(const path of filtered().slice(page*30,(page+1)*30))e.target.checked?selected.add(path):selected.delete(path);render();};
    $("document-tag-choices").replaceChildren(...choices.map((tag,index)=>{
      const label=el("label","checkbox-label"),input=document.createElement("input");input.type="checkbox";
      input.addEventListener("change",()=>{input.checked?chosen.add(index):chosen.delete(index);render();});label.append(input,el("span","",`${tag.key}：${tag.value}`));return label;
    }));
    if(!choices.length)$("document-tag-choices").append(el("p","source-hint","尚无可选标签，请先在“文件同步 → 可选标签库”维护标签。"));
    function renderRules(){
      $("document-tag-rules").replaceChildren(...rules.map((rule,index)=>{
        const row=el("div","document-tag-rule");
        row.append(el("strong","",`${rule.paths.length} 个文档 · ${rule.tags.map(t=>`${t.key}：${t.value}`).join("、")}`));
        const details=el("details",""),summary=el("summary","","查看文档");details.append(summary,el("pre","source-hint",rule.paths.join("\n")));row.append(details);
        const remove=el("button","button quiet small","移除规则");remove.type="button";remove.addEventListener("click",()=>{rules.splice(index,1);renderRules();});row.append(remove);return row;
      }));
    }
    $("document-tag-add-rule").onclick=()=>{
      if(!selected.size||!chosen.size)return;
      if(rules.length>=100){notice("最多添加 100 条文档标签规则，请合并规则。",true);return;}
      rules.push({paths:[...selected],tags:[...chosen].sort((a,b)=>a-b).map(i=>choices[i])});selected.clear();renderRules();render();
    };
    render();return rules;
  }
  async function confirm(paths,root,config){
    const grouped=groupPaths(paths,root);
    if(grouped.direct)grouped.groups.unshift(["",grouped.direct]);
    if(!grouped.groups.length)return null;
    if(busy){if(dialog.open)dialog.focus();throw new Error("请在“确认知识库与标签”弹窗中确认或取消");}
    busy=true;
    let controller=null,removeClose=()=>{};
    try{
      const content=$("knowledge-plan-list"),error=$("knowledge-plan-error"),submit=$("knowledge-plan-confirm");
      if(!$("document-tag-editor"))throw new Error("知识库确认窗口组件不完整，请刷新页面后重试");
      error.textContent="";content.replaceChildren();$("document-tag-editor").hidden=true;submit.disabled=true;$("knowledge-plan-pager").hidden=true;
      $("knowledge-plan-summary").textContent=`将准备 ${grouped.groups.length} 个知识库：同一目标目录下同名则复用，不存在才创建。${grouped.direct?`根目录直属 ${grouped.direct} 个文件将进入“未分类”知识库，默认放在根目录。`:""} ${config.auto_import?"准备完成后自动入库。":"切片后手动确认入库。"}`;
      $("knowledge-model").replaceChildren(new Option("正在获取向量模型…",""));
      dialog.returnValue="";
      let selections=[],choices=[],documentRules=[];
      const knowledgeEnabled=config.knowledge_tags_enabled!==false,documentsEnabled=config.document_tags_enabled!==false;
      controller=new AbortController();
      const closed=new Promise(resolve=>{
        const onClose=()=>{controller.abort();resolve(dialog.returnValue==="confirm");};
        dialog.addEventListener("close",onClose,{once:true});
        removeClose=()=>dialog.removeEventListener("close",onClose);
      });
      dialog.showModal();
      try{
        const [models,settings,folders]=await Promise.all([api("/api/ingestion/knowledge/models",{...jsonOptions(config),signal:controller.signal}),api("/api/ingestion/knowledge/tag-settings",{signal:controller.signal}),api("/api/ingestion/knowledge/folders",{...jsonOptions(config),signal:controller.signal})]);
        if(!dialog.open)return false;
        choices=settings.tags;
        documentRules=documentTagEditor(paths,choices,documentsEnabled);
        $("knowledge-model").replaceChildren(...(models.length?models.map(m=>new Option(m.name,m.id)):[new Option("没有可用的向量模型","")]));
        selections=grouped.groups.map(([folder,count])=>({folder,count,chosen:new Set(),folder_id:folders[0]?.id||"default",apply_to_documents:config.inherit_knowledge_tags!==false}));
        let page=0;
        function renderPage(){
          content.replaceChildren(...selections.slice(page*20,(page+1)*20).map(item=>{
            const box=el("section","knowledge-plan-group");box.append(el("h3","",item.folder||"未分类（根目录文件）"),el("p","muted",item.folder?`${item.count} 个文件 · 包含该一级文件夹内的子目录文件`:`${item.count} 个根目录直属文件 · 使用或新建“未分类”知识库`));
            const destination=el("label","knowledge-destination","目标目录"),select=document.createElement("select");
            select.dataset.glassKey=`knowledge-folder:${item.folder}`;select.setAttribute("aria-label",`${item.folder||"未分类（根目录文件）"}的目标目录`);
            select.append(...folders.map(f=>new Option(f.name,f.id)));select.value=item.folder_id;
            select.addEventListener("change",()=>{item.folder_id=select.value;});destination.append(select);box.append(destination);
            box.append(el("p","source-hint",knowledgeEnabled?"知识库预设标签（可多选）":"知识库预设标签已在参数设置中关闭。"));
            if(knowledgeEnabled)choices.forEach((tag,index)=>{
              const label=el("label","checkbox-label"),input=document.createElement("input");input.type="checkbox";input.checked=item.chosen.has(index);
              input.addEventListener("change",()=>input.checked?item.chosen.add(index):item.chosen.delete(index));
              label.append(input,el("span","",`${tag.key}：${tag.value}`));box.append(label);
            });
            if(knowledgeEnabled&&documentsEnabled){
              const label=el("label","checkbox-label"),input=document.createElement("input");input.type="checkbox";input.checked=item.apply_to_documents;
              input.addEventListener("change",()=>{item.apply_to_documents=input.checked;});label.append(input,el("span","","将以上知识库标签应用到该知识库全部文档（可与下方文档专属标签叠加）"));box.append(label);
            }
            if(!choices.length)box.append(el("p","muted","尚未配置可选标签，可在同步管理中设置。"));
            return box;
          }));
          $("knowledge-plan-pager").hidden=selections.length<=20;
          $("knowledge-plan-page").textContent=`第 ${page+1}/${Math.ceil(selections.length/20)} 页`;
          $("knowledge-plan-prev").disabled=page===0;$("knowledge-plan-next").disabled=(page+1)*20>=selections.length;
        }
        $("knowledge-plan-prev").onclick=()=>{page--;renderPage();};$("knowledge-plan-next").onclick=()=>{page++;renderPage();};renderPage();
        submit.disabled=!models.length||!folders.length;
      }catch(e){error.textContent=e.message;}
      if(!await closed)return false;
      return {root,embedding_model_id:$("knowledge-model").value,groups:selections.map(({folder,chosen,folder_id,apply_to_documents})=>({folder,folder_id,apply_to_documents,tags:knowledgeEnabled?[...chosen].sort((a,b)=>a-b).map(i=>choices[i]):[]})),document_rules:documentsEnabled?documentRules:[]};
    }finally{busy=false;controller?.abort();removeClose();}
  }
  function renderTargets(queue){
    const content=$("knowledge-target-list");if(!content)return;
    const names={stopped:"已停止",queued:"等待匹配",matching:"查询同名知识库",creating:"创建中",tagging:"准备标签",ready:"已就绪",failed:"准备失败",unknown:"创建结果待核对"};
    if(targetQueue!==queue.id){targetPage=0;targetQueue=queue.id;drafts.clear();}
    const targets=queue.knowledge_targets||[],pages=Math.max(1,Math.ceil(targets.length/20));targetPage=Math.min(targetPage,pages-1);
    content.replaceChildren(...targets.slice(targetPage*20,(targetPage+1)*20).map(t=>{
      const row=el("section","knowledge-plan-group");row.append(el("strong","",`${t.name} · ${t.candidates?.length&&t.status==="failed"?"请选择同名知识库":names[t.status]||t.status}${t.reused?" · 已复用":""}`));
      row.append(el("p","muted",`创建位置：${t.destination_folder_id==="default"?"根目录":t.destination_folder_id||"根目录"}`));
      if(t.knowledge_id)row.append(el("p","muted",`知识库 ID：${t.knowledge_id}`));
      if(t.error)row.append(el("p","file-error",t.error));
      if(["failed","unknown"].includes(t.status)){
        const candidates=t.candidates||[],id=document.createElement(candidates.length?"select":"input");
        if(candidates.length){
          id.append(new Option("请选择已有知识库",""),...candidates.map(item=>new Option(`${item.name} · ${item.id}${item.create_time?` · ${item.create_time}`:""}`,item.id)));
        }else{id.placeholder="创建结果不明时，填写核对后的知识库 ID";id.maxLength=200;}
        id.setAttribute("aria-label",`${t.name} 已创建知识库 ID`);id.value=drafts.get(t.folder)||"";
        const retry=el("button","button secondary small",candidates.length?"使用所选知识库":t.status==="unknown"?"关联已创建的知识库":"重试准备");
        retry.disabled=!!candidates.length&&!id.value;
        id.addEventListener(candidates.length?"change":"input",()=>{drafts.set(t.folder,id.value);retry.disabled=!!candidates.length&&!id.value;});
        retry.addEventListener("click",async()=>{retry.disabled=true;try{await api(`/api/ingestion/queues/${queue.id}/knowledge/retry`,jsonOptions({folder:t.folder,knowledge_id:id.value.trim(),maxkb_token:operationToken()}));await refreshFolderQueue({force:true});}catch(e){row.append(el("p","file-error",e.message));}finally{retry.disabled=false;}});row.append(id,retry);
      }return row;
    }));
    if(pages>1){const nav=el("div","preview-pager"),prev=el("button","button quiet small","上一页"),next=el("button","button quiet small","下一页");prev.disabled=targetPage===0;next.disabled=targetPage===pages-1;prev.onclick=()=>{targetPage--;renderTargets(queue);};next.onclick=()=>{targetPage++;renderTargets(queue);};nav.append(prev,el("span","",`知识库第 ${targetPage+1}/${pages} 页 · 共 ${targets.length} 个`),next);content.append(nav);}
  }
  window.KnowledgeUI={confirm,groupPaths,renderTargets};
  loadTags().catch(e=>{$("knowledge-tag-message").textContent=e.message;});
})();

"use strict";
(() => {
  const state={servers:[],directories:[],jobs:[],archives:[],files:[],selected:new Set(),modes:new Map(),page:0,editId:"",scan:0,lastRequest:null,sourcesRequest:0,jobsRequest:0,root:"",remote:null,remotePage:0,remoteRequest:0};
  const sshForm=$("ssh-form");
  const jobNames={queued:"等待执行",running:"执行中",completed:"已完成",failed:"失败",canceled:"已取消"};
  function bytes(n){return n>=1024**3?(n/1024**3).toFixed(2)+" GB":size(n);}
  function credentialFields(){const key=sshForm.elements.auth.value==="key";$("ssh-key-fields").hidden=!key;$("ssh-password-field").hidden=key;}
  function editServer(id){
    state.editId=id;sshForm.reset();
    const item=state.servers.find(s=>s.id===id);
    if(item){for(const key of ["name","host","port","username","remote_path","auth","bandwidth_mb"])sshForm.elements[key].value=item[key];}
    for(const key of ["password","private_key","passphrase"]){sshForm.elements[key].value="";sshForm.elements[key].placeholder=item?.["has_"+key]?"已保存 · 留空沿用，填写以替换":"首次填写，保存后留空沿用";}
    $("ssh-fingerprint").textContent=item?.fingerprint?`主机指纹：${item.fingerprint}`:"尚未记录主机指纹";
    $("ssh-config").value=id;credentialFields();
    loadRemoteScan(id).catch(e=>notice(e.message,true));
  }
  function renderRemoteScan(){
    const result=state.remote;
    $("remote-scan-results").hidden=!result;
    if(!result){$("remote-scan-summary").textContent=state.editId?"尚未扫描。":"尚未扫描。";return;}
    $("remote-scan-summary").textContent=`上次远程扫描：${new Date(result.scanned_at*1000).toLocaleString()} · 待同步 ${result.pending} 个 / ${bytes(result.pending_bytes)}`;
    const files=result.files;
    const pages=Math.max(1,Math.ceil(files.length/50));state.remotePage=Math.min(state.remotePage,pages-1);
    const labels={pending:"待同步"};
    const rows=files.slice(state.remotePage*50,(state.remotePage+1)*50).map(f=>{
      const row=el("div","remote-scan-file");row.append(el("span","",f.path),el("small","",bytes(f.bytes)),el("span",`remote-scan-status ${f.status}`,labels[f.status]));return row;
    });
    $("remote-scan-files").replaceChildren(...(rows.length?rows:[el("p","source-hint",result.files.length?"没有匹配的文件。":"远程目录没有待处理文档；已处理文档目录已排除。") ]));
    $("remote-scan-page").textContent=`第 ${state.remotePage+1}/${pages} 页 · ${files.length} 个`;
    $("remote-scan-prev").disabled=state.remotePage===0;$("remote-scan-next").disabled=state.remotePage>=pages-1;
  }
  async function loadRemoteScan(id){
    const request=++state.remoteRequest;
    if(!id){state.remote=null;state.remotePage=0;renderRemoteScan();return;}
    state.remote=null;state.remotePage=0;renderRemoteScan();
    $("remote-scan-summary").textContent="正在读取该服务器的上次远程扫描结果…";
    const result=await api(`/api/ingestion/ssh-sources/${id}/scan`);
    if(request!==state.remoteRequest||id!==state.editId)return;
    state.remote=Array.isArray(result?.files)?result:null;state.remotePage=0;renderRemoteScan();
  }
  $("remote-scan-prev").addEventListener("click",()=>{state.remotePage--;renderRemoteScan();});
  $("remote-scan-next").addEventListener("click",()=>{state.remotePage++;renderRemoteScan();});
  function sourceKey(){const value=$("document-origin").value;return value==="ssh"?$("document-sync-source").value:value;}
  function selectDirectory(id){$("document-origin").value=id==="mapped"?"mapped":"ssh";if(id!=="mapped")$("document-sync-source").value=id;originChanged();}
  function directoryName(d){return d.id==="mapped"?"本地映射 · 同机文档":`远程 SSH · ${d.name}`;}
  function originHelp(){
    const id=sourceKey();
    $("document-sync-control").hidden=$("document-origin").value!=="ssh";
    $("document-origin-help").textContent=id==="browser"?"文档在当前电脑：选择文件或文件夹上传。浏览器上传不会移动电脑里的原文件。":id==="mapped"?"直接读取工作台服务器的映射目录，无需 SSH 同步。入库成功后，原文件移至源根目录下的“已处理文档”。":"读取已下载到工作台的 SSH 同步文件；远程新增文档需先回“文件同步”重新同步。入库成功后，远程原文件移至远程源根目录下的“已处理文档”。";
  }
  async function refreshSources(){
    const request=++state.sourcesRequest;
    const [servers,rawDirectories]=await Promise.all([api("/api/ingestion/ssh-sources"),api("/api/ingestion/source-directories")]);
    if(request!==state.sourcesRequest)return;
    const unique=new Map();
    for(const d of rawDirectories){
      const server=servers.find(s=>s.id===d.id);
      const key=server?.host?JSON.stringify([server.host,server.port,server.username,(server.remote_path.replace(/\/+$/,"")||"/")]):d.id;
      if(!unique.has(key)||(!unique.get(key).available&&d.available))unique.set(key,d);
    }
    const directories=[...unique.values()];state.servers=servers;state.directories=directories;
    const config=$("ssh-config"),origin=$("document-origin"),previous=origin.value,previousKey=sourceKey();
    config.replaceChildren(new Option("新增服务器",""),...servers.map(s=>new Option(s.name,s.id)));config.value=state.editId;
    origin.replaceChildren(new Option("上传","browser"),new Option("映射","mapped"),new Option("同步目录","ssh"));
    const syncs=directories.filter(d=>d.id!=="mapped"),select=$("document-sync-source");
    select.replaceChildren(...(syncs.length?syncs.map(d=>{
      const server=servers.find(s=>s.id===d.id);
      return new Option(`${server?.name||d.name}${server?.host?` · ${server.host}:${server.remote_path}`:""}${d.available?"":" · 尚未就绪"}`,d.id);
    }):[new Option("暂无同步目录","")]));
    select.value=syncs.some(d=>d.id===previousKey)?previousKey:(syncs.find(d=>d.available)||syncs[0])?.id||"";
    origin.value=["browser","mapped","ssh"].includes(previous)?previous:(syncs.some(d=>d.id===previous)?"ssh":"browser");
    originHelp();
    $("source-directory-list").replaceChildren(...directories.map(d=>{
      const row=el("div","source-directory-row");row.append(el("strong","",directoryName(d)),el("p","",d.id==="mapped"?"文档和工作台在同一台服务器，直接扫描读取。":"文档来自远程服务器，先完成 SSH 同步再扫描。"),el("p","",`工作台读取目录：${d.path}`),el("p","",d.available?"已就绪 · 可前往扫描文档":d.id==="mapped"?"尚未挂载或目录不可用 · 请先将服务器文档目录挂载到 /sources":"尚未完成同步 · 请先扫描远程目录，再同步本轮文件"));
      const button=el("button","button secondary small",d.id==="mapped"?"扫描同机文档":"查看已同步文档");button.disabled=!d.available;
      button.addEventListener("click",()=>{navigatePage("workbench");selectDirectory(d.id);});row.append(button);return row;
    }));
    const d=directories.find(d=>d.id===sourceKey());$("directory-location").textContent=d?directoryName(d):"";
    if(previousKey!==sourceKey())originChanged();
  }
  async function saveServer(){
    if(!sshForm.reportValidity())throw new Error("请填写 SSH 连接参数");
    const data=Object.fromEntries(new FormData(sshForm));
    data.port=Number(data.port);data.bandwidth_mb=Number(data.bandwidth_mb);
    for(const key of ["password","private_key","passphrase"])data[key]=data[key]||null;
    const id=state.editId||newQueueId();
    await api(`/api/ingestion/ssh-sources/${id}`,jsonOptions(data,"PUT"));state.editId=id;
    await refreshSources();editServer(id);return id;
  }
  async function action(button,fn){button.disabled=true;try{await fn();}catch(e){notice(e.message,true);}finally{button.disabled=false;}}
  sshForm.addEventListener("submit",e=>{e.preventDefault();action($("ssh-save"),async()=>{await saveServer();notice("SSH 配置已加密保存。");});});
  sshForm.elements.auth.addEventListener("change",credentialFields);
  $("ssh-config").addEventListener("change",e=>editServer(e.target.value));
  $("ssh-test").addEventListener("click",()=>action($("ssh-test"),async()=>{
    const id=await saveServer(),result=await api(`/api/ingestion/ssh-sources/${id}/test`,{method:"POST"});
    await refreshSources();editServer(id);notice(result.message);
  }));
  $("ssh-scan").addEventListener("click",()=>action($("ssh-scan"),async()=>{
    const id=await saveServer();await api(`/api/ingestion/ssh-sources/${id}/scan`,jsonOptions({id:newQueueId()}));
    state.remoteRequest++;state.remote=null;renderRemoteScan();$("remote-scan-summary").textContent="正在扫描…";
    await refreshJobs();notice("远程扫描已提交，不下载文件。完成后自动更新下方清单；页面关闭后扫描仍会继续。");
  }));
  $("ssh-sync").addEventListener("click",()=>action($("ssh-sync"),async()=>{
    const id=await saveServer();await api(`/api/ingestion/ssh-sources/${id}/sync`,jsonOptions({id:newQueueId()}));
    await refreshJobs();notice("远程文档下载任务已创建。下载完成后，到文档工作台选择对应的“远程 SSH”来源，扫描并勾选处理；下载完成不代表已入库。");
  }));
  $("source-refresh").addEventListener("click",()=>action($("source-refresh"),async()=>{await refreshSources();await refreshJobs();}));
  function originChanged(){
    originHelp();
    const browser=$("document-origin").value==="browser";
    document.querySelector(".upload-card").hidden=!browser;$("directory-intake").hidden=browser;
    state.scan++;state.files=[];state.selected.clear();state.modes.clear();state.lastRequest=null;state.page=0;
    $("directory-results").hidden=true;$("directory-empty").hidden=false;$("directory-folder").value="";
    $("directory-scan").disabled=false;
    $("directory-scan").textContent=browser||$("document-origin").value==="mapped"?"扫描目录":"刷新已同步文件";
    const directory=state.directories.find(d=>d.id===sourceKey());$("directory-location").textContent=directory?directoryName(directory):"";
    renderJobs();
  }
  $("document-origin").addEventListener("change",originChanged);
  $("document-sync-source").addEventListener("change",originChanged);
  $("directory-scan").addEventListener("click",()=>action($("directory-scan"),async()=>{
    const request=++state.scan,id=sourceKey(),folder=$("directory-folder").value.trim();
    const result=await api(`/api/ingestion/source-directories/${id}/files?folder=${encodeURIComponent(folder)}`);
    if(request!==state.scan)return;
    state.root=folder;
    state.files=result.files;state.selected.clear();state.modes.clear();state.page=0;state.lastRequest=null;
    $("directory-excel-mode").value=form.elements.excel_mode.value;
    $("directory-results").hidden=!result.files.length;$("directory-empty").hidden=!!result.files.length;
    $("directory-empty").textContent="目录中没有可处理的文件。";renderFiles();
    notice(`${id==="mapped"?"扫描目录":"刷新本机已同步文件"}：${result.files.length} 个文件 · ${bytes(result.bytes)}，跳过 ${result.ignored} 个已归档或不符合条件的条目。`);
  }));
  function filtered(){const query=$("directory-search").value.trim().toLowerCase(),kind=$("directory-kind").value;return state.files.filter(f=>(kind==="all"||f.kind===kind)&&f.path.toLowerCase().includes(query));}
  function renderFiles(){
    const files=filtered(),pages=Math.max(1,Math.ceil(files.length/50));state.page=Math.min(state.page,pages-1);
    const page=files.slice(state.page*50,(state.page+1)*50),sections=[];
    for(const kind of ["pdf","word","excel"]){
      const matches=page.filter(f=>f.kind===kind);if(!matches.length)continue;
      const section=el("section","directory-section");section.append(el("h3","directory-section-title",formatNames[kind]));
      for(const f of matches){const row=el("div","directory-row"),check=document.createElement("input");check.type="checkbox";check.checked=state.selected.has(f.path);check.setAttribute("aria-label",`选择 ${f.path}`);
        check.addEventListener("change",()=>{check.checked?state.selected.add(f.path):state.selected.delete(f.path);state.lastRequest=null;selectionSummary();});
        row.append(check,el("span","directory-filename",f.path),el("small","",bytes(f.bytes)));
        if(kind==="excel"){const mode=document.createElement("select");mode.setAttribute("aria-label",`${f.name} 导入模式`);mode.dataset.glassKey=`directory-mode:${f.path}`;
          mode.append(new Option("表格 table","table"),new Option("问答对 qa","qa"));mode.value=state.modes.get(f.path)||$("directory-excel-mode").value;
          mode.addEventListener("change",()=>{state.modes.set(f.path,mode.value);state.lastRequest=null;});row.append(mode);
        }section.append(row);
      }sections.push(section);
    }
    if(!page.length)sections.push(el("p","source-hint padded","没有匹配的文件。"));
    $("directory-file-list").replaceChildren(...sections);$("directory-page").textContent=`第 ${state.page+1}/${pages} 页 · ${files.length} 个文件`;
    $("directory-prev").disabled=state.page===0;$("directory-next").disabled=state.page>=pages-1;selectionSummary();
  }
  function selectionSummary(){const files=state.files.filter(f=>state.selected.has(f.path)),view=filtered();
    $("directory-selection-count").textContent=`已选 ${files.length} 个 · ${bytes(files.reduce((n,f)=>n+f.bytes,0))}`;
    $("directory-import").disabled=!files.length;$("directory-select-all").checked=!!view.length&&view.every(f=>state.selected.has(f.path));
    $("directory-select-all").indeterminate=view.some(f=>state.selected.has(f.path))&&!$("directory-select-all").checked;
  }
  for(const id of ["directory-search","directory-kind"])$(id).addEventListener(id==="directory-search"?"input":"change",()=>{state.page=0;renderFiles();});
  $("directory-excel-mode").addEventListener("change",()=>{state.modes.clear();state.lastRequest=null;renderFiles();});
  $("directory-select-all").addEventListener("change",e=>{for(const f of filtered())e.target.checked?state.selected.add(f.path):state.selected.delete(f.path);state.lastRequest=null;renderFiles();});
  $("directory-prev").addEventListener("click",()=>{state.page--;renderFiles();});$("directory-next").addEventListener("click",()=>{state.page++;renderFiles();});
  async function importSelected(){
    const config=readConfig(),files=state.files.filter(f=>state.selected.has(f.path)).map(f=>({path:f.path,bytes:f.bytes,mtime:f.mtime,...(f.kind==="excel"?{excel_mode:state.modes.get(f.path)||$("directory-excel-mode").value}:{})}));
    if(!files.length)return;
    const sourceId=sourceKey();
    const baseSignature=JSON.stringify({sourceId,config,files,root:state.root});
    const knowledge_plan=state.lastRequest?.baseSignature===baseSignature?state.lastRequest.knowledge_plan:await window.KnowledgeUI.confirm(files.map(f=>f.path),state.root,config);
    if(knowledge_plan===false)return;
    const signature=JSON.stringify({sourceId,config,files,knowledge_plan});
    if(!state.lastRequest||state.lastRequest.signature!==signature)state.lastRequest={signature,id:newQueueId(),baseSignature,knowledge_plan};
    const result=await api(`/api/ingestion/source-directories/${sourceId}/import`,jsonOptions({id:state.lastRequest.id,files,config,knowledge_plan}));
    await refreshJobs();notice("服务器正在保存原文件并创建分批队列。页面关闭后也会继续执行。");
    localStorage.setItem("active-source-import",result.id);
  }
  $("directory-import").addEventListener("click",()=>action($("directory-import"),importSelected));
  async function openQueue(job){
    folderQueue=await api(`/api/ingestion/queues/${job.queue_id}`);localStorage.setItem("active-queue",folderQueue.id);
    const wave=folderQueue.batches.find(b=>b.wave_state==="active")||folderQueue.batches[0];await loadBatch(wave.id);
    document.querySelector(".queue-card").scrollIntoView({behavior:"smooth",block:"start"});
  }
  function jobRow(job){
    const row=el("div","source-job"),heading=el("div","source-job-heading");
    const name=state.servers.find(s=>s.id===job.source_key)?.name||(job.source_key==="mapped"?"映射目录":"同步目录");
    heading.append(el("strong","",`${name} · ${{scan:"远程扫描",sync:"文件同步",import:"目录导入"}[job.kind]||"任务"} · ${jobNames[job.status]||job.status}`),el("span","muted",new Date(job.created_at*1000).toLocaleString()));row.append(heading);
    const progress=document.createElement("progress");progress.max=(job.kind==="scan"?job.total_files:job.total_bytes)||1;progress.value=job.kind==="scan"?job.done_files:job.done_bytes;row.append(progress);
    row.append(el("p","",job.kind==="scan"?`${job.message} · ${job.done_files}/${job.total_files} 个 · 不下载文件`:`${job.message} · ${job.done_files}/${job.total_files} 个 · ${bytes(job.done_bytes)}/${bytes(job.total_bytes)}${job.kind==="sync"?` · 本轮恢复 ${job.skipped} 个`:""}`));
    if(job.current_path)row.append(el("p","",job.current_path));if(job.error)row.append(el("p","source-error",job.error));
    const actions=el("div","source-job-actions");
    if(["queued","running"].includes(job.status)){const cancel=el("button","button quiet small",job.cancel_requested?"正在取消…":"取消");cancel.disabled=!!job.cancel_requested;cancel.addEventListener("click",()=>action(cancel,async()=>{await api(`/api/ingestion/source-jobs/${job.id}/cancel`,{method:"POST"});await refreshJobs();}));actions.append(cancel);}
    if(["failed","canceled"].includes(job.status)){const retry=el("button","button secondary small","继续 / 重试");retry.addEventListener("click",()=>action(retry,async()=>{await api(`/api/ingestion/source-jobs/${job.id}/retry`,{method:"POST"});await refreshJobs();}));actions.append(retry);}
    if(job.kind==="import"&&job.done_files>0){const open=el("button","button secondary small","打开处理队列");open.addEventListener("click",()=>action(open,()=>openQueue(job)));actions.append(open);}
    if(job.kind==="sync"&&job.status==="completed"){const choose=el("button","button secondary small","选择已同步文档");choose.addEventListener("click",()=>{navigatePage("workbench");selectDirectory(job.source_key);});actions.append(choose);}
    const logs=el("button","button quiet small","记录");logs.addEventListener("click",()=>action(logs,async()=>{
      const lines=await api(`/api/ingestion/source-jobs/${job.id}/logs`);$("source-log-content").replaceChildren(...lines.map(l=>el("div","audit-line",`${new Date(l.created_at*1000).toLocaleString()} · ${l.message}`)));$("source-log-dialog").showModal();
    }));actions.append(logs);row.append(actions);return row;
  }
  function archiveRow(item){
    const row=el("div","source-job");
    const names={canceled:"已停止归档",pending:item.pipeline==="imported"?"已入库，待归档":"等待入库成功",moving:"正在归档",published:"正在完成归档",completed:"归档成功",failed:"归档失败"};
    row.append(el("strong","",`${item.source_path} · ${names[item.status]||item.status}`));
    if(item.error)row.append(el("p","source-error",item.error));
    if(item.status==="failed"){
      const retry=el("button","button secondary small","重试归档");
      retry.addEventListener("click",()=>action(retry,async()=>{await api(`/api/ingestion/source-archives/${item.file_id}/retry`,{method:"POST"});await refreshJobs();}));row.append(retry);
    }
    return row;
  }
  function renderJobs(){
    const sync=state.jobs.filter(j=>["sync","scan"].includes(j.kind)),imports=state.jobs.filter(j=>j.kind==="import"&&j.source_key===sourceKey());
    $("sync-job-list").replaceChildren(...(sync.length?sync.slice(0,15).map(jobRow):[el("p","source-hint padded","暂无远程扫描或同步记录") ]));
    const archives=state.archives.filter(a=>a.source_key===sourceKey());
    $("directory-job-list").replaceChildren(...imports.slice(0,5).map(jobRow),...archives.slice(0,20).map(archiveRow));
    $("sync-job-list").append(...state.archives.slice(0,20).map(archiveRow));
  }
  async function refreshJobs(){
    const request=++state.jobsRequest;
    const previous=new Map(state.jobs.map(j=>[j.id,j.status])),jobs=await api("/api/ingestion/source-jobs");
    const archives=await api("/api/ingestion/source-archives");
    if(request!==state.jobsRequest)return;
    const changed=JSON.stringify(jobs)!==JSON.stringify(state.jobs)||JSON.stringify(archives)!==JSON.stringify(state.archives);state.jobs=jobs;state.archives=archives;if(changed)renderJobs();
    if(state.editId&&state.jobs.some(j=>j.source_key===state.editId&&j.kind==="scan"&&j.status==="completed"&&previous.get(j.id)!=="completed")){
      await loadRemoteScan(state.editId);
      $("remote-scan-results").scrollIntoView({behavior:"smooth",block:"nearest"});
    }
    const failedScan=state.jobs.find(j=>j.source_key===state.editId&&j.kind==="scan");
    if(failedScan&&["failed","canceled"].includes(failedScan.status))$("remote-scan-summary").textContent=failedScan.status==="failed"?`扫描失败：${failedScan.error}`:"扫描已取消";
    if(state.jobs.some(j=>j.kind==="sync"&&j.status==="completed"&&previous.has(j.id)&&previous.get(j.id)!=="completed")){await refreshSources();notice("文件同步完成，可前往工作台查看已同步文档；再次扫描远程目录可更新清单状态。");}
  }
  $("source-log-close").addEventListener("click",()=>$("source-log-dialog").close());
  async function historyCleared(){
    state.sourcesRequest++;state.jobsRequest++;
    state.scan++;state.files=[];state.selected.clear();state.modes.clear();state.lastRequest=null;state.page=0;
    $("directory-results").hidden=true;$("directory-empty").hidden=false;$("directory-scan").disabled=false;
    await refreshSources();await refreshJobs();
  }
  window.SourceUI={init:async()=>{await refreshSources();await refreshJobs();},historyCleared,pageChanged:()=>{if(!document.hidden&&(currentPage()==="sources"||(currentPage()==="workbench"&&state.jobs.some(j=>["queued","running"].includes(j.status)))))refreshJobs().catch(e=>notice(e.message,true));}};
  let polling=false;
  setInterval(async()=>{
    if(polling||document.hidden||!["sources","workbench"].includes(currentPage())||(!state.jobs.some(j=>["queued","running"].includes(j.status))&&!state.archives.some(a=>["pending","moving","published"].includes(a.status))))return;
    polling=true;try{await refreshJobs();}catch{/* Keep last state during a transient network interruption. */}finally{polling=false;}
  },3000);
})();

return {setPage,navigatePage,destroy:()=>window.GlassSelect?.destroy()};
}
