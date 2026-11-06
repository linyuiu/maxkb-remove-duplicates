<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, defineAsyncComponent, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
const KnowledgeIngestion=defineAsyncComponent(()=>import("./components/KnowledgeIngestion.vue"));
type Row = Record<string, any>;
const user = ref(""),
  ready = ref(false),
  tab = ref("runs"),
  busy = ref(false);
const runPage = ref(1),
  operationPage = ref(1);
const sidebarOpen = ref(window.innerWidth > 900);
const feature=ref('duplicates');
const ingestionLoaded=ref(false);
watch(feature,value=>{if(value==='ingestion')ingestionLoaded.value=true});
const ingestionPage=ref('workbench');
const ingestionPages:Row={workbench:'文档入库',sources:'文件同步',history:'入库历史',settings:'入库设置'};
const loginForm = ref({ username: "admin", password: "" });
const passwordOpen = ref(false), changingPassword = ref(false), passwordError = ref("");
const passwordForm = ref({current_password:"", new_password:"", confirm_password:""});
function clearPasswordForm() {
  passwordForm.value = {current_password:"", new_password:"", confirm_password:""};
  passwordError.value = "";
}
function openPassword() { clearPasswordForm(); passwordOpen.value = true; }
async function changePassword() {
  if(changingPassword.value)return;
  const form = passwordForm.value;
  passwordError.value = "";
  if(!form.current_password){passwordError.value="请输入原密码";return;}
  if(form.new_password.length<12){passwordError.value="新密码至少需要 12 个字符";return;}
  if(form.new_password!==form.confirm_password){passwordError.value="两次输入的新密码不一致";return;}
  if(form.current_password===form.new_password){passwordError.value="新密码不能与原密码相同";return;}
  changingPassword.value=true;
  try {
    await post("/me/password", {...form});
    passwordOpen.value=false;
    clearPasswordForm();
    user.value="";
    selectedRun.value=null;
    loginForm.value.password="";
    ElMessage.success("密码已修改，请使用新密码重新登录");
  } catch(e:any) { passwordError.value=e.message||"修改密码失败"; }
  finally { changingPassword.value=false; }
}

const sources = ref<Row[]>([]),
  tasks = ref<Row[]>([]),
  runs = ref<Row[]>([]),
  operations = ref<Row[]>([]);
const estimateOpen = ref(false),
  scanEstimate = ref<Row | null>(null),
  estimateContext = ref<Row>({});
const documentOpen = ref(false),
  documentData = ref<Row>({ records: [] });
const sourceOpen = ref(false),
  taskOpen = ref(false),
  sourceForm = ref<Row>({}),
  taskForm = ref<Row>({});
const knowledge = ref<Row[]>([]),
  workspace = ref(""),
  loadingKnowledge = ref(false);
const resultKnowledge = ref<Row[]>([]);
const loadingResultKnowledge = ref(false);
const selectedRun = ref<Row | null>(null),
  results = ref<Row[]>([]),
  total = ref(0),
  page = ref(1);
const filter = ref({
  status: "",
  knowledge_id: "",
  document: "",
  min_score: 0,
});
const editOpen = ref(false),
  editTarget = ref<Row>({}),
  live = ref<Row | null>(null),
  editForm = ref({ title: "", content: "" });
const operationOpen = ref(false),
  operationDetail = ref<Row>({});
const stateLabels: Row = {
  queued: "排队中",
  running: "扫描中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  pending: "待处理",
  ignored: "已忽略",
  resolved: "已处理",
  unconfigured: "待配置接口",
  draft: "待提交",
  executing: "执行中",
  unknown: "待核实",
  conflict: "源内容冲突",
};
const availableKnowledge = computed(() =>
  knowledge.value.filter(
    (k) => !workspace.value || k.workspace_id === workspace.value,
  ),
);
const workspaces = computed(() => [
  ...new Set(knowledge.value.map((k) => k.workspace_id)),
]);
const taskName = (id: string) =>
  tasks.value.find((t) => t.id === id)?.name || id.slice(0, 8);
const sourceName = (id: string) =>
  sources.value.find((t) => t.id === id)?.name || id.slice(0, 8);
const date = (v: string) =>
  v ? new Date(v).toLocaleString("zh-CN", { hour12: false }) : "—";
async function api(path: string, options: RequestInit = {}) {
  const res = await fetch("/api" + path, {
    credentials: "include",
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Requested-With": "dedup",
      ...options.headers,
    },
  });
  const data = await res.json();
  if (!res.ok) {
    if (res.status === 401) user.value = "";
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  }
  return data;
}
const post = (path: string, body: Row = {}) =>
  api(path, { method: "POST", body: JSON.stringify(body) });
async function action(fn: () => Promise<void>) {
  busy.value = true;
  try {
    await fn();
  } catch (e: any) {
    if (e !== "cancel" && e !== "close") {
      const message = e.message || String(e);
      if (message.startsWith("未执行扫描：")) {
        await ElMessageBox.alert(message, "向量未就绪", {
          confirmButtonText: "知道了", customClass: "vector-readiness-dialog",
        }).catch(() => {});
      } else ElMessage.error(message);
    }
  } finally {
    busy.value = false;
  }
}
async function refresh() {
  [sources.value, tasks.value, runs.value] = await Promise.all([
    api("/sources"),
    api("/tasks"),
    api("/runs?page=" + runPage.value),
  ]);
}
async function login() {
  await action(async () => {
    const me = await post("/login", loginForm.value);
    user.value = me.username;
    loginForm.value.password = "";
    await refresh();
  });
}
async function logout() {
  await action(async () => {
    await post("/logout");
    user.value = "";
    selectedRun.value = null;
  });
}
function openSource(row?: Row) {
  sourceForm.value = row
    ? { ...row, password: "", api_key: "", port: Number(row.port) }
    : {
        api_base_url: "http://maxkb.example.com:18080/admin/api",
        api_key: "",
        name: "",
        host: "",
        port: 5432,
        dbname: "maxkb",
        user: "",
        password: "",
        sslmode: "prefer",
        enabled: true,
      };
  sourceOpen.value = true;
}
async function saveSource() {
  await action(async () => {
    const f = sourceForm.value;
    await api("/sources" + (f.id ? "/" + f.id : ""), {
      method: f.id ? "PUT" : "POST",
      body: JSON.stringify(f),
    });
    sourceOpen.value = false;
    await refresh();
    ElMessage.success("数据源已保存");
  });
}
async function loadKnowledge() {
  knowledge.value = [];
  if (!taskForm.value.source_id) return;
  loadingKnowledge.value = true;
  try {
    knowledge.value = await api(
      "/sources/" + taskForm.value.source_id + "/knowledge",
    );
  } finally {
    loadingKnowledge.value = false;
  }
}
async function openTask(row?: Row) {
  await action(async () => {
    taskForm.value = row
      ? { ...row, knowledge_ids: [...row.knowledge_ids] }
      : {
          name: "",
          source_id: "",
          knowledge_ids: [],
          threshold: 0.9,
          exclude_same_document: false,
          batch_size: 32,
          cron: "0 0 * * *",
          timezone: "Asia/Shanghai",
          enabled: false,
        };
    workspace.value = "";
    taskOpen.value = true;
    await loadKnowledge();
  });
}
async function sourceChanged() {
  taskForm.value.knowledge_ids = [];
  workspace.value = "";
  await action(loadKnowledge);
}
async function persistTask(form: Row, token?: string) {
  await api("/tasks" + (form.id ? "/" + form.id : ""), {
    method: form.id ? "PUT" : "POST",
    body: JSON.stringify({ ...form, save_estimate: token || null }),
  });
  estimateOpen.value = false;
  taskOpen.value = false;
  await refresh();
  ElMessage.success("任务已保存");
}
async function saveTask() {
  await action(async () => {
    const form = JSON.parse(JSON.stringify(taskForm.value));
    if (!form.knowledge_ids.length) throw new Error("请选择至少一个知识库");
    if (form.enabled) {
      scanEstimate.value = await post(
        "/tasks" + (form.id ? "/" + form.id : "") + "/preview",
        form,
      );
      estimateContext.value = { mode: "schedule", form };
      estimateOpen.value = true;
    } else await persistTask(form);
  });
}
async function refreshEstimate() {
  if (estimateContext.value.mode === "schedule") {
    await action(async () => {
      const form = estimateContext.value.form;
      scanEstimate.value = await post(
        "/tasks" + (form.id ? "/" + form.id : "") + "/preview",
        form,
      );
    });
  } else
    await previewScan(
      estimateContext.value.taskId,
      estimateContext.value.retryRunId,
    );
}
function duration(seconds: number) {
  if (seconds < 60) return Math.ceil(seconds) + " 秒";
  if (seconds < 3600) return Math.ceil(seconds / 60) + " 分钟";
  if (seconds < 86400) return (seconds / 3600).toFixed(1) + " 小时";
  return (seconds / 86400).toFixed(1) + " 天";
}
async function previewScan(taskId: string, retryRunId?: string) {
  await action(async () => {
    const result = await post("/tasks/" + taskId + "/estimate", {
      retry_run_id: retryRunId || null,
    });
    estimateContext.value = { taskId, retryRunId };
    scanEstimate.value = result;
    estimateOpen.value = true;
  });
}
async function startTask(row: Row) {
  await previewScan(row.id);
}
async function confirmScan() {
  await action(async () => {
    if (!scanEstimate.value) return;
    if (estimateContext.value.mode === "schedule") {
      await persistTask(
        estimateContext.value.form,
        scanEstimate.value.save_estimate,
      );
      return;
    }
    const { taskId, retryRunId } = estimateContext.value;
    await post(
      retryRunId
        ? "/runs/" + retryRunId + "/resume"
        : "/tasks/" + taskId + "/run",
      { estimate_id: scanEstimate.value.estimate_id },
    );
    estimateOpen.value = false;
    selectedRun.value = null;
    tab.value = "runs";
    runPage.value = 1;
    await refresh();
    ElMessage.success("已确认，扫描已排队");
  });
}
async function runAction(row: Row, kind: string) {
  if (kind === "resume") {
    await previewScan(row.task_id, row.id);
    return;
  }
  await action(async () => {
    await post("/runs/" + row.id + "/" + kind);
    await refresh();
    if (selectedRun.value?.id === row.id) await loadPairs();
  });
}
async function deleteRun(row: Row) {
  await action(async () => {
    await ElMessageBox.confirm(
      `确定删除“${row.config.task_name || '扫描记录'}”的本次扫描记录、相似段落结果和原文快照？此操作不可恢复。修改前后文本的操作历史会保留，MaxKB 源数据不会删除。`,
      "删除扫描记录", {type:"warning", confirmButtonText:"删除本次扫描", cancelButtonText:"取消"},
    );
    await api("/runs/"+row.id, {method:"DELETE"});
    if(selectedRun.value?.id===row.id){selectedRun.value=null;results.value=[];total.value=0;}
    await refresh();
    if(!runs.value.length&&runPage.value>1){runPage.value--;await refresh();}
    ElMessage.success("扫描记录及结果已删除");
  });
}
async function loadPairs() {
  if (!selectedRun.value) return;
  const params = new URLSearchParams({
    ...filter.value,
    min_score: String(filter.value.min_score),
    page: String(page.value),
  });
  const runId = selectedRun.value.id;
  const data = await api("/runs/" + runId + "/pairs?" + params);
  if (selectedRun.value?.id !== runId) return;
  selectedRun.value = data.run;
  results.value = data.items;
  total.value = data.total;
}
async function openRun(row: Row) {
  selectedRun.value = row;
  page.value = 1;
  filter.value = { status: "", knowledge_id: "", document: "", min_score: 0 };
  resultKnowledge.value = [];
  await action(async () => {
    loadingResultKnowledge.value = true;
    try {
      await Promise.all([
        loadPairs(),
        api("/runs/" + row.id + "/knowledge").then((options) => {
          if (selectedRun.value?.id === row.id) resultKnowledge.value = options;
        }),
      ]);
    } finally {
      loadingResultKnowledge.value = false;
    }
  });
}
async function review(pair: Row, status: string) {
  await action(async () => {
    await post("/pairs/" + pair.id + "/review", { status });
    await loadPairs();
  });
}
async function edit(pair: Row, side: string, kind = "update_paragraph") {
  await action(async () => {
    if (kind === "delete_document") {
      const doc = await api("/pairs/" + pair.id + "/document/" + side);
      editTarget.value = {
        pair,
        side,
        kind,
        configured: true,
        changed: false,
        document: doc,
      };
      live.value = {
        ...doc,
        title: doc.document_name,
        content: "",
        content_hash: "",
      };
      editForm.value = { title: "", content: "" };
      editOpen.value = true;
      return;
    }
    const result = await api("/pairs/" + pair.id + "/current/" + side);
    live.value = result.current;
    if (!live.value) throw new Error("该段落已从源系统删除");
    editTarget.value = {
      pair,
      side,
      kind,
      changed: result.changed,
      configured: result.api_configured,
    };
    editForm.value = { title: live.value.title, content: live.value.content };
    editOpen.value = true;
  });
}
async function saveDraft() {
  await action(async () => {
    if (!live.value) return;
    if (editTarget.value.configured)
      await ElMessageBox.confirm(
        editTarget.value.kind === "delete_document"
          ? `确认删除整篇文档“${live.value.document_name}”及其 ${editTarget.value.document.records.length} 个段落？删除前全文会保存在操作记录中。`
          : editTarget.value.kind === "delete_paragraph"
            ? "确认删除源系统中的这个段落？删除前全文会保存在操作记录中。"
            : "确认将当前编辑内容提交到源系统？修改前后文本会保存在操作记录中。",
        "确认源系统操作",
        { type: "warning" },
      );
    const operation = await post(
      "/pairs/" + editTarget.value.pair.id + "/operations",
      {
        side: editTarget.value.side,
        kind: editTarget.value.kind,
        ...editForm.value,
        expected_hash: live.value.content_hash,
        expected_document_hash: editTarget.value.document?.document_hash || "",
      },
    );
    editOpen.value = false;
    if (operation.state === "draft") {
      const result = await post("/operations/" + operation.id + "/execute");
      if (result.state === "succeeded")
        ElMessage.success("源系统操作已回读确认，前后文本已保存");
      else ElMessage.warning(result.error || stateLabels[result.state]);
      await loadPairs();
    } else ElMessage.success("接口未配置，已保存草稿");
  });
}
async function viewDocument(pair: Row, side: string) {
  await action(async () => {
    documentData.value = await api("/pairs/" + pair.id + "/document/" + side);
    documentOpen.value = true;
  });
}
async function operateHistory(row: Row, kind: string) {
  await action(async () => {
    if (kind === "execute")
      await ElMessageBox.confirm("提交该操作到源系统？", "确认提交");
    const r = await post("/operations/" + row.id + "/" + kind);
    ElMessage.info(stateLabels[r.state] || r.state);
    operations.value = await api("/operations?page=" + operationPage.value);
  });
}
async function showOperations() {
  tab.value = "operations";
  await action(async () => {
    operations.value = await api("/operations?page=" + operationPage.value);
  });
}
let timer: ReturnType<typeof setInterval>;
onMounted(async () => {
  try {
    user.value = (await api("/me")).username;
    await refresh();
  } catch {
  } finally {
    ready.value = true;
  }
  timer = setInterval(async () => {
    if (!user.value || busy.value) return;
    try {
      runs.value = await api("/runs?page=" + runPage.value);
      if (selectedRun.value && selectedRun.value.state !== "succeeded")
        await loadPairs();
    } catch {}
  }, 5000);
});
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <div v-if="!ready" class="loading">正在连接系统…</div>
  <main v-else-if="!user" class="login-shell">
    <el-card class="login-card"
      ><span class="eyebrow">WORKSPACE ACCESS</span>
      <h2>登录治理工作台</h2>
      <el-form label-position="top" @submit.prevent="login"
        ><el-form-item label="用户名"
          ><el-input
            v-model="loginForm.username"
            autocomplete="username" /></el-form-item
        ><el-form-item label="密码"
          ><el-input
            v-model="loginForm.password"
            type="password"
            show-password
            autocomplete="current-password" /></el-form-item
        ><el-button
          type="primary"
          native-type="submit"
          :loading="busy"
          class="wide"
          >登录</el-button
        ></el-form
      ></el-card
    >
  </main>
  <div v-else class="shell" :class="{ 'sidebar-open': sidebarOpen }">
    <button
      v-if="sidebarOpen"
      class="sidebar-scrim"
      aria-label="关闭侧栏"
      @click="sidebarOpen = false"
    ></button>
    <aside>
      <div class="brand">
        <div class="brand-icon">M</div>
        <div>MaxKB <small>知识治理工作台</small></div>
      </div>
      <el-select v-model="feature" aria-label="切换功能" style="margin-bottom:16px"><el-option label="知识入库" value="ingestion"/><el-option label="重复片段" value="duplicates"/></el-select>
      <template v-if="feature==='ingestion'"><button v-for="(label,page) in ingestionPages" :key="page" :class="{active:ingestionPage===page}" @click="ingestionPage=String(page)">{{label}}</button></template>
      <template v-else>
      <button
        class="new-scan"
        @click="
          tab = 'tasks';
          selectedRun = null;
          openTask();
        "
      >
        <span>＋</span> 新建扫描
      </button>
      <div class="nav-label">工作台</div>
      <button
        :class="{ active: tab === 'runs' }"
        @click="
          tab = 'runs';
          selectedRun = null;
        "
      >
        ◫ <span>扫描记录</span></button
      ><button
        :class="{ active: tab === 'tasks' }"
        @click="
          tab = 'tasks';
          selectedRun = null;
        "
      >
        ◷ <span>扫描任务</span></button
      ><button
        :class="{ active: tab === 'sources' }"
        @click="
          tab = 'sources';
          selectedRun = null;
        "
      >
        ▤ <span>数据源</span></button
      ><button
        :class="{ active: tab === 'operations' }"
        @click="showOperations"
      >
        ≡ <span>操作历史</span>
      </button>
      <div class="recent-runs">
        <div class="nav-label">最近扫描</div>
        <button
          v-for="run in runs.slice(0, 5)"
          :key="run.id"
          @click="
            tab = 'runs';
            openRun(run);
          "
        >
          <span class="recent-dot" :class="run.state"></span
          ><span>{{ run.config.task_name }}</span>
        </button>
        <p v-if="!runs.length" class="muted small">扫描记录会显示在这里</p>
      </div>
      </template>
      <div class="aside-bottom">
        <span class="status-dot"></span> {{feature==='ingestion'?'文档解析与入库':'精确余弦扫描'}}<br /><small
          >{{feature==='ingestion'?'任务状态与来源记录持久保存':'源数据库只读 · 快照持久化'}}</small
        >
      </div>
    </aside>
    <div class="workspace">
      <header>
        <button
          class="sidebar-toggle"
          :aria-expanded="sidebarOpen"
          aria-label="切换侧栏"
          @click="sidebarOpen = !sidebarOpen"
        >
          <svg
            width="19"
            height="19"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.6"
          >
            <rect x="3" y="4" width="18" height="16" rx="4" />
            <path d="M9 4v16" />
          </svg>
        </button>
        <span
          >{{feature==='ingestion'?'知识入库':'重复片段'}}
          <span class="muted"
            >/
            {{
              feature==='ingestion'?ingestionPages[ingestionPage]:selectedRun
                ? "相似段落"
                : (
                    {
                      runs: "扫描记录",
                      tasks: "扫描任务",
                      sources: "数据源",
                      operations: "操作历史",
                    } as Row
                  )[tab]
            }}</span
          ></span
        >
        <div>
          <el-dropdown trigger="click" @command="openPassword">
            <button class="user-menu-trigger" aria-label="用户菜单" type="button">
              <span class="avatar">{{ user[0].toUpperCase() }}</span>
              <span>{{ user }}</span><span aria-hidden="true">⌄</span>
            </button>
            <template #dropdown>
              <el-dropdown-menu><el-dropdown-item command="password">修改密码</el-dropdown-item></el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-button text @click="logout">退出</el-button>
        </div>
      </header>
      <section class="content">
        <KnowledgeIngestion v-if="ingestionLoaded" v-show="feature==='ingestion'" :active="feature==='ingestion'" :page="ingestionPage" @navigate="ingestionPage=$event" @connections="feature='duplicates';tab='sources';selectedRun=null"/>
        <template v-if="feature==='duplicates'">
        <template v-if="tab === 'sources'"
          ><div class="page-heading">
            <div>
              <span class="eyebrow">CONNECTIONS</span>
              <h1>数据源</h1>
              <p>连接 MaxKB 数据库，为扫描任务提供可复用的数据范围。</p>
            </div>
            <el-button type="primary" @click="openSource()"
              >＋ 添加数据源</el-button
            >
          </div>
          <el-empty
            v-if="!sources.length"
            description="添加第一个 MaxKB 数据源"
          />
          <div class="source-grid">
            <el-card v-for="source in sources" :key="source.id"
              ><div class="card-top">
                <div class="db-icon">DB</div>
                <el-tag :type="source.enabled ? 'success' : 'info'">{{
                  source.enabled ? "已启用" : "已停用"
                }}</el-tag>
              </div>
              <h3>{{ source.name }}</h3>
              <p v-if="source.api_only">MaxKB API 连接 · 仅知识入库</p><p v-else>{{ source.host }}:{{ source.port }} / {{ source.dbname }}</p>
              <p class="muted">{{source.api_only?source.api_base_url:"数据库用户："+source.user}}</p>
              <div class="card-actions">
                <el-button v-if="!source.api_only"
                  @click="
                    action(async () => {
                      const r = await post('/sources/' + source.id + '/test');
                      ElMessage.success(
                        r.read_only ? '连接成功，读取事务为只读' : '连接成功',
                      );
                    })
                  "
                  >测试连接</el-button
                ><el-button @click="openSource(source)">编辑</el-button>
              </div></el-card
            >
          </div></template
        >

        <template v-else-if="tab === 'tasks'"
          ><div class="page-heading">
            <div>
              <span class="eyebrow">SCAN CONFIGURATION</span>
              <h1>扫描任务</h1>
              <p>选择知识库、设置阈值，手动执行或按计划扫描。</p>
            </div>
            <el-button type="primary" @click="openTask()"
              >＋ 新建任务</el-button
            >
          </div>
          <el-card
            ><el-table
              :data="tasks"
              empty-text="暂无任务，请先配置数据源并创建任务"
              ><el-table-column
                prop="name"
                label="任务"
                min-width="160"
              /><el-table-column label="数据源" min-width="140"
                ><template #default="{ row }">{{
                  sourceName(row.source_id)
                }}</template></el-table-column
              ><el-table-column label="范围" width="100"
                ><template #default="{ row }"
                  >{{ row.knowledge_ids.length }} 个知识库</template
                ></el-table-column
              ><el-table-column
                prop="threshold"
                label="阈值"
                width="80"
              /><el-table-column label="定时" min-width="180"
                ><template #default="{ row }"
                  ><el-tag :type="row.enabled ? 'success' : 'info'">{{
                    row.enabled ? row.cron : "手动 / 已暂停"
                  }}</el-tag>
                  <div v-if="row.enabled" class="muted small">
                    下次 {{ date(row.next_fire) }}
                  </div>
                  <div v-if="row.skipped_fires" class="muted small">
                    已跳过 {{ row.skipped_fires }} 次重叠或停用触发
                  </div></template
                ></el-table-column
              ><el-table-column label="操作" width="190"
                ><template #default="{ row }"
                  ><el-button
                    type="primary"
                    plain
                    :disabled="busy"
                    @click="startTask(row)"
                    >预估并扫描</el-button
                  ><el-button text @click="openTask(row)"
                    >编辑</el-button
                  ></template
                ></el-table-column
              ></el-table
            ></el-card
          ></template
        >

        <template v-else-if="tab === 'runs' && !selectedRun"
          ><div class="page-heading">
            <div>
              <span class="eyebrow">SCAN ACTIVITY</span>
              <h1>扫描记录</h1>
              <p>每次扫描独立保存，随时回看完整段落与处理进度。</p>
            </div>
            <el-button @click="action(refresh)">刷新</el-button>
          </div>
          <div class="stats">
            <div>
              <span>最近运行</span><strong>{{ runs.length }}</strong>
            </div>
            <div>
              <span>执行中 / 排队</span
              ><strong>{{
                runs.filter((r) => ["running", "queued"].includes(r.state))
                  .length
              }}</strong>
            </div>
            <div>
              <span>发现相似段落对</span
              ><strong>{{ runs.reduce((n, r) => n + r.pair_count, 0) }}</strong>
            </div>
          </div>
          <el-card
            ><el-table
              :data="runs"
              empty-text="暂无扫描记录，在扫描任务中点击预估并扫描"
              ><el-table-column label="任务 / 时间" min-width="210"
                ><template #default="{ row }"
                  ><strong>{{
                    row.config.task_name || taskName(row.task_id)
                  }}</strong>
                  <div class="muted small">
                    {{ date(row.created_at) }}
                  </div></template
                ></el-table-column
              ><el-table-column label="状态" width="110"
                ><template #default="{ row }"
                  ><el-tag
                    :type="
                      row.state === 'succeeded'
                        ? 'success'
                        : row.state === 'failed'
                          ? 'danger'
                          : 'info'
                    "
                    >{{ stateLabels[row.state] }}</el-tag
                  >
                  <div v-if="row.cancel_requested" class="small muted">
                    正在取消
                  </div></template
                ></el-table-column
              ><el-table-column label="进度" min-width="180"
                ><template #default="{ row }"
                  ><el-progress
                    :percentage="
                      row.state === 'succeeded'
                        ? 100
                        : row.total
                          ? Math.min(
                              99,
                              Math.round((row.processed / row.total) * 100),
                            )
                          : 0
                    "
                  /><span class="small muted"
                    >{{
                      row.phase === "snapshot"
                        ? "提取快照"
                        : row.phase === "done"
                          ? "完成"
                          : "比较向量"
                    }}
                    · {{ row.processed }} / {{ row.total }} chunks</span
                  ></template
                ></el-table-column
              ><el-table-column
                prop="pair_count"
                label="段落对"
                width="90"
              /><el-table-column label="操作" min-width="240"
                ><template #default="{ row }"
                  ><el-button text type="primary" @click="openRun(row)"
                    >查看结果</el-button
                  ><el-button
                    v-if="['queued', 'running'].includes(row.state)"
                    text
                    type="danger"
                    @click="runAction(row, 'cancel')"
                    >取消</el-button
                  ><el-button
                    v-if="['failed', 'cancelled'].includes(row.state)"
                    text
                    @click="runAction(row, 'resume')"
                    >重新扫描</el-button>
                  <el-button v-if="['succeeded','failed','cancelled'].includes(row.state)" text type="danger" :disabled="busy" @click="deleteRun(row)">删除记录</el-button>
                  </template
                ></el-table-column
              ></el-table
            ></el-card
          >
          <p class="muted small">每页 50 次运行 · 扫描进行时每 5 秒自动刷新</p>
          <div class="history-pager">
            <el-button
              :disabled="runPage === 1"
              @click="
                runPage--;
                action(refresh);
              "
              >上一页</el-button
            ><span>第 {{ runPage }} 页</span
            ><el-button
              :disabled="runs.length < 50"
              @click="
                runPage++;
                action(refresh);
              "
              >下一页</el-button
            >
          </div></template
        >

        <template v-else-if="tab === 'runs' && selectedRun"
          ><el-button text @click="selectedRun = null"
            >← 返回扫描记录</el-button
          >
          <div class="page-heading">
            <div>
              <span class="eyebrow">SIMILARITY REVIEW</span>
              <h1>
                {{ selectedRun.config.task_name }}
                <el-tag>{{ stateLabels[selectedRun.state] }}</el-tag>
              </h1>
              <p>相似度取两段落中最相似的一对 chunk，展示两侧完整段落。</p>
            </div>
            <el-button @click="action(loadPairs)">刷新结果</el-button>
          </div>
          <el-alert
            v-if="selectedRun.error"
            :title="selectedRun.error"
            type="error"
            :closable="false" /><el-alert
            v-if="selectedRun.state !== 'succeeded'"
            title="当前结果尚不完整，仅供查看；扫描成功后可进行人工处理。"
            type="warning"
            :closable="false" />
          <div class="filters">
            <el-select v-model="filter.status" placeholder="全部状态" clearable
              ><el-option
                v-for="s in ['pending', 'ignored', 'resolved']"
                :key="s"
                :value="s"
                :label="stateLabels[s]" /></el-select
            ><el-input
              v-model="filter.document"
              placeholder="文档名称"
              clearable
            /><el-select
              v-model="filter.knowledge_id"
              class="knowledge-filter"
              placeholder="全部知识库"
              filterable
              clearable
              :loading="loadingResultKnowledge"
              aria-label="按知识库名称筛选"
            >
              <el-option
                v-for="k in resultKnowledge"
                :key="k.id"
                :value="k.id"
                :label="k.name"
              >
                <span>{{ k.name }}</span
                ><small class="muted" style="float: right; margin-left: 16px">{{
                  k.workspace_id
                }}</small>
              </el-option> </el-select
            ><el-input-number
              v-model="filter.min_score"
              :min="0"
              :max="1"
              :step="0.01"
              :precision="2"
            /><el-button
              type="primary"
              @click="
                page = 1;
                action(loadPairs);
              "
              >筛选</el-button
            >
          </div>
          <div class="result-count">
            共 {{ total }} 对相似段落
            <span class="muted"
              >· 任务阈值 {{ selectedRun.config.threshold }}</span
            >
          </div>
          <el-empty
            v-if="!results.length"
            description="当前范围没有相似段落对" />
          <article v-for="pair in results" :key="pair.id" class="pair">
            <div class="pair-heading">
              <div>
                <span class="score"
                  >{{ (pair.similarity * 100).toFixed(2) }}%</span
                >
                <span class="muted">最高 chunk 相似度</span>
              </div>
              <div>
                <el-tag>{{ stateLabels[pair.status] }}</el-tag
                ><el-button
                  text
                  :disabled="selectedRun.state !== 'succeeded'"
                  @click="
                    review(
                      pair,
                      pair.status === 'ignored' ? 'pending' : 'ignored',
                    )
                  "
                  >{{
                    pair.status === "ignored" ? "恢复待处理" : "忽略"
                  }}</el-button
                ><el-button
                  text
                  :disabled="selectedRun.state !== 'succeeded'"
                  @click="
                    review(
                      pair,
                      pair.status === 'resolved' ? 'pending' : 'resolved',
                    )
                  "
                  >{{
                    pair.status === "resolved" ? "恢复待处理" : "标记已处理"
                  }}</el-button
                >
              </div>
            </div>
            <div class="paragraphs">
              <section v-for="side in ['left', 'right']" :key="side">
                <div class="crumb">
                  {{ pair[side].workspace_id }} /
                  {{ pair[side].knowledge_name }}
                </div>
                <h3>{{ pair[side].document_name }}</h3>
                <h4 v-if="pair[side].title">{{ pair[side].title }}</h4>
                <pre>{{ pair[side].content }}</pre>
                <div class="paragraph-footer">
                  <small>段落 {{ pair[side].paragraph_id }}</small>
                  <div>
                    <el-button
                      text
                      type="primary"
                      :disabled="selectedRun.state !== 'succeeded'"
                      @click="edit(pair, side)"
                      >查看当前 / 编辑</el-button
                    ><el-button text @click="viewDocument(pair, side)"
                      >查看整篇文档</el-button
                    ><el-dropdown
                      :disabled="selectedRun.state !== 'succeeded'"
                      @command="(kind: string) => edit(pair, side, kind)"
                      ><el-button text>删除 ▾</el-button
                      ><template #dropdown
                        ><el-dropdown-menu
                          ><el-dropdown-item command="delete_paragraph"
                            >删除此段落</el-dropdown-item
                          ><el-dropdown-item command="delete_document"
                            >删除整个文档</el-dropdown-item
                          ></el-dropdown-menu
                        ></template
                      ></el-dropdown
                    >
                  </div>
                </div>
              </section>
            </div>
          </article>
          <el-pagination
            v-model:current-page="page"
            :page-size="20"
            :total="total"
            layout="prev,pager,next,total"
            @current-change="action(loadPairs)"
        /></template>

        <template v-else-if="tab === 'operations'"
          ><div class="page-heading">
            <div>
              <span class="eyebrow">CHANGE HISTORY</span>
              <h1>操作历史</h1>
              <p>保留修改前、拟修改、源系统实际文本及每次操作状态。</p>
            </div>
            <el-button @click="showOperations">刷新</el-button>
          </div>
          <el-alert
            title="修改、删除均保存操作记录；执行结果不明确时点击核实，仅回读源系统，不会重发写请求。"
            type="info"
            :closable="false"
          /><el-card
            ><el-table :data="operations" empty-text="暂无操作记录"
              ><el-table-column label="时间" width="180"
                ><template #default="{ row }">{{
                  date(row.created_at)
                }}</template></el-table-column
              ><el-table-column prop="kind" label="操作类型" /><el-table-column
                label="文档"
                min-width="160"
                ><template #default="{ row }">{{
                  row.before?.document_name
                }}</template></el-table-column
              ><el-table-column label="状态"
                ><template #default="{ row }"
                  ><el-tag>{{
                    stateLabels[row.state] || row.state
                  }}</el-tag></template
                ></el-table-column
              ><el-table-column label="文本记录"
                ><template #default="{ row }"
                  ><el-button
                    text
                    type="primary"
                    @click="
                      operationDetail = row;
                      operationOpen = true;
                    "
                    >查看前后文本</el-button
                  ><el-button
                    v-if="['executing', 'unknown'].includes(row.state)"
                    text
                    @click="operateHistory(row, 'reconcile')"
                    >核实结果</el-button
                  ><el-button
                    v-if="row.state === 'draft' && row.pair_id"
                    text
                    @click="operateHistory(row, 'execute')"
                    >提交</el-button
                  >
                  ></template
                ></el-table-column
              ></el-table
            ></el-card
          >
          <div class="history-pager">
            <el-button
              :disabled="operationPage === 1"
              @click="
                operationPage--;
                showOperations();
              "
              >上一页</el-button
            ><span>第 {{ operationPage }} 页</span
            ><el-button
              :disabled="operations.length < 50"
              @click="
                operationPage++;
                showOperations();
              "
              >下一页</el-button
            >
          </div></template
        >
      </template>
      </section>
    </div>
  </div>


  <el-dialog v-model="passwordOpen" title="修改密码" width="min(440px, calc(100vw - 28px))"
    :close-on-click-modal="!changingPassword" :close-on-press-escape="!changingPassword"
    :show-close="!changingPassword" @closed="clearPasswordForm">
    <el-form label-position="top" @submit.prevent="changePassword">
      <el-form-item label="原密码">
        <el-input v-model="passwordForm.current_password" type="password" show-password
          autocomplete="current-password" :maxlength="1024" :disabled="changingPassword" />
      </el-form-item>
      <el-form-item label="新密码">
        <el-input v-model="passwordForm.new_password" type="password" show-password
          autocomplete="new-password" placeholder="至少 12 个字符" :maxlength="1024" :disabled="changingPassword" />
      </el-form-item>
      <el-form-item label="确认新密码">
        <el-input v-model="passwordForm.confirm_password" type="password" show-password
          autocomplete="new-password" :maxlength="1024" :disabled="changingPassword" />
      </el-form-item>
      <el-alert v-if="passwordError" :title="passwordError" type="error" :closable="false" show-icon role="alert" />
      <p class="small muted">修改成功后，所有已登录会话将退出，请使用新密码重新登录。</p>
      <div class="dialog-actions">
        <el-button :disabled="changingPassword" @click="passwordOpen=false">取消</el-button>
        <el-button type="primary" native-type="submit" :loading="changingPassword">确认修改</el-button>
      </div>
    </el-form>
  </el-dialog>

  <el-dialog
    v-model="estimateOpen"
    :title="
      estimateContext.mode === 'schedule'
        ? '定时任务预估与保存确认'
        : '扫描预估与确认'
    "
    width="760px"
  >
    <template v-if="scanEstimate">
      <h3>{{ scanEstimate.task_name }}</h3>
      <el-alert
        v-if="estimateContext.mode === 'schedule'"
        :title="
          '确认后保存并启用定时任务，不立即扫描。规则：' +
          scanEstimate.cron +
          ' · ' +
          scanEstimate.timezone +
          '，下次执行：' +
          date(scanEstimate.next_fire)
        "
        type="info"
        :closable="false"
      />
      <div class="stats">
        <div>
          <span>正文 chunk 数</span
          ><strong>{{ scanEstimate.chunks.toLocaleString() }}</strong>
        </div>
        <div>
          <span>段落 / 文档</span
          ><strong
            >{{ scanEstimate.paragraphs.toLocaleString() }} /
            {{ scanEstimate.documents.toLocaleString() }}</strong
          >
        </div>
        <div>
          <span>预计处理时间</span
          ><strong style="font-size: 20px"
            >{{ duration(scanEstimate.seconds_low) }} ～
            {{ duration(scanEstimate.seconds_high) }}</strong
          >
        </div>
      </div>
      <el-table :data="scanEstimate.knowledge"
        ><el-table-column prop="name" label="知识库" /><el-table-column
          prop="documents"
          label="文档"
          width="85" /><el-table-column
          prop="paragraphs"
          label="段落"
          width="85" /><el-table-column prop="chunks" label="chunk" width="100"
      /></el-table>
      <p>
        需要精确比较约
        <strong>{{ scanEstimate.comparisons.toLocaleString() }}</strong> 对
        chunk · 向量维度 {{ scanEstimate.dimensions || "—" }}
      </p>
      <el-alert
        :title="
          scanEstimate.basis === 'history'
            ? '根据 ' +
              scanEstimate.history_samples +
              ' 次同数据源历史运行速度估算'
            : '暂无可用历史速度，采用未校准经验值估算，范围较宽'
        "
        type="info"
        :closable="false"
      />
      <p class="small muted">
        耗时为估算范围，不含排队时间；实际受数据库负载、命中结果数量等影响。统计只读取数量，不进行相似扫描。确认前后数据可能变化，正式扫描以新的数据库快照为准。预估有效至
        {{ date(scanEstimate.expires_at) }}。
      </p>
      <el-alert
        v-if="!scanEstimate.comparisons"
        :title="
          estimateContext.mode === 'schedule'
            ? '当前没有可比较的不同段落，可保存定时任务等待后续数据。'
            : '当前范围没有可比较的不同段落，无需启动扫描。'
        "
        type="warning"
        :closable="false"
      />
    </template>
    <template #footer
      ><el-button @click="estimateOpen = false">取消</el-button
      ><el-button :loading="busy" @click="refreshEstimate">重新统计</el-button
      ><el-button
        type="primary"
        :loading="busy"
        :disabled="
          !scanEstimate ||
          (estimateContext.mode !== 'schedule' && !scanEstimate.comparisons)
        "
        @click="confirmScan"
        >{{
          estimateContext.mode === "schedule"
            ? "确认保存定时任务"
            : "确认开始扫描"
        }}</el-button
      ></template
    >
  </el-dialog>
  <el-dialog
    v-model="sourceOpen"
    :title="sourceForm.id ? '编辑数据源' : '添加 MaxKB 数据源'"
    width="560px"
    ><el-form label-position="top"
      ><el-form-item label="显示名称"
        ><el-input v-model="sourceForm.name"
      /></el-form-item>
      <div class="form-row">
        <el-form-item label="数据库主机（仅入库可留空）"
          ><el-input
            v-model="sourceForm.host"
            :disabled="!!sourceForm.id && !sourceForm.api_only" /></el-form-item
        ><el-form-item label="端口"
          ><el-input-number
            v-model="sourceForm.port"
            :disabled="!!sourceForm.id && !sourceForm.api_only"
            :min="1"
            :max="65535"
        /></el-form-item>
      </div>
      <el-form-item label="数据库"
        ><el-input v-model="sourceForm.dbname" :disabled="!!sourceForm.id && !sourceForm.api_only"
      /></el-form-item>
      <div class="form-row">
        <el-form-item label="数据库用户"
          ><el-input v-model="sourceForm.user" /></el-form-item
        ><el-form-item :label="sourceForm.id ? '密码（留空保留原值）' : '密码'"
          ><el-input
            v-model="sourceForm.password"
            type="password"
            show-password
            autocomplete="new-password"
        /></el-form-item>
      </div>
      <el-form-item label="SSL"
        ><el-select v-model="sourceForm.sslmode"
          ><el-option
            v-for="mode in [
              'disable',
              'prefer',
              'require',
              'verify-ca',
              'verify-full',
            ]"
            :key="mode"
            :value="mode" /></el-select></el-form-item
      ><el-divider>MaxKB 管理接口</el-divider>
      <el-form-item label="API 地址"
        ><el-input
          v-model="sourceForm.api_base_url"
          placeholder="http://maxkb.example.com:18080/admin/api"
      /></el-form-item>
      <el-form-item
        :label="
          sourceForm.api_configured
            ? 'Bearer Key（留空保留原值）'
            : 'Bearer Key'
        "
        ><el-input
          v-model="sourceForm.api_key"
          type="password"
          show-password
          autocomplete="new-password"
      /></el-form-item>
      <p class="small muted">
        Key 仅在后端加密保存；更换 API 地址时请重新填写。
      </p>
      <el-switch
        v-model="sourceForm.enabled"
        active-text="启用数据源" /></el-form
    ><template #footer
      ><el-button @click="sourceOpen = false">取消</el-button
      ><el-button type="primary" :loading="busy" @click="saveSource"
        >保存</el-button
      ></template
    ></el-dialog
  >
  <el-dialog
    v-model="taskOpen"
    :title="taskForm.id ? '编辑扫描任务' : '新建扫描任务'"
    width="680px"
    ><el-form label-position="top"
      ><el-form-item label="任务名称"
        ><el-input v-model="taskForm.name" /></el-form-item
      ><el-form-item label="MaxKB 数据源"
        ><el-select v-model="taskForm.source_id" @change="sourceChanged"
          ><el-option
            v-for="s in sources.filter((s) => s.enabled && !s.api_only)"
            :key="s.id"
            :value="s.id"
            :label="s.name" /></el-select></el-form-item
      ><el-form-item label="工作空间筛选"
        ><el-select v-model="workspace" clearable placeholder="全部工作空间"
          ><el-option
            v-for="w in workspaces"
            :key="w"
            :value="w" /></el-select></el-form-item
      ><el-form-item label="扫描知识库（可多选）"
        ><el-select
          v-model="taskForm.knowledge_ids"
          multiple
          filterable
          :loading="loadingKnowledge"
          placeholder="先选择数据源"
          ><el-option
            v-for="k in availableKnowledge"
            :key="k.id"
            :value="k.id"
            :label="`${k.name} · ${k.workspace_id} · ${k.model_name || '未配置模型'}`" /></el-select
      ></el-form-item>
      <p class="small muted">
        所选知识库组成比较范围。多库比较需使用同一向量模型。
      </p>
      <div class="form-row">
        <el-form-item label="相似度阈值"
          ><el-input-number
            v-model="taskForm.threshold"
            :min="0"
            :max="1"
            :step="0.01"
            :precision="3" /></el-form-item
        ><el-form-item label="每批 chunk 数"
          ><el-input-number v-model="taskForm.batch_size" :min="1" :max="256"
        /></el-form-item>
      </div>
      <el-form-item
        ><el-switch
          v-model="taskForm.exclude_same_document"
          active-text="排除同一文档内的比较" /></el-form-item
      ><el-divider /><el-form-item
        ><el-switch v-model="taskForm.enabled" active-text="启用定时扫描"
      /></el-form-item>
      <div class="form-row">
        <el-form-item label="Cron（5 段，例如每天 00:00）"
          ><el-input
            v-model="taskForm.cron"
            placeholder="0 0 * * *" /></el-form-item
        ><el-form-item label="时区"
          ><el-input v-model="taskForm.timezone"
        /></el-form-item></div></el-form
    ><template #footer
      ><el-button @click="taskOpen = false">取消</el-button
      ><el-button type="primary" :loading="busy" @click="saveTask"
        >保存任务</el-button
      ></template
    ></el-dialog
  >
  <el-dialog
    v-model="editOpen"
    :title="
      editTarget.kind === 'update_paragraph'
        ? '查看当前文本 / 修改段落'
        : editTarget.kind === 'delete_document'
          ? '确认删除整篇文档'
          : '删除源段落'
    "
    width="850px"
    ><el-alert
      :title="
        editTarget.configured
          ? '提交后将调用源系统接口，并保存操作前后文本。'
          : '数据源尚未配置 API 地址和 Bearer Key，当前仅能保存草稿。'
      "
      type="info"
      :closable="false"
    /><el-alert
      v-if="editTarget.changed"
      title="当前源内容与扫描快照不同，下方显示源系统最新文本，请重新核对。"
      type="warning"
      :closable="false"
    /><template v-if="live"
      ><p>{{ live.knowledge_name }} / {{ live.document_name }}</p>
      <el-form v-if="editTarget.kind !== 'delete_document'" label-position="top"
        ><el-form-item label="标题"
          ><el-input
            v-model="editForm.title"
            :readonly="editTarget.kind !== 'update_paragraph'" /></el-form-item
        ><el-form-item label="完整正文"
          ><el-input
            v-model="editForm.content"
            type="textarea"
            :rows="15"
            :readonly="editTarget.kind !== 'update_paragraph'" /></el-form-item
      ></el-form>
      <p v-if="editTarget.kind === 'delete_document'" class="danger">
        将删除整个文档及其全部
        {{ editTarget.document.records.length }}
        个段落，以下全文会保存到删除记录。
      </p>
      <section v-if="editTarget.kind === 'delete_document'">
        <article v-for="p in editTarget.document.records" :key="p.id">
          <h3>{{ p.title || "无标题段落" }}</h3>
          <pre>{{ p.content }}</pre>
          <el-divider />
        </article></section></template
    ><template #footer
      ><el-button @click="editOpen = false">关闭</el-button
      ><el-button type="primary" :loading="busy" @click="saveDraft">{{
        editTarget.configured
          ? editTarget.kind === "update_paragraph"
            ? "保存并提交"
            : editTarget.kind === "delete_document"
              ? "确认删除整篇文档"
              : "确认删除段落"
          : "保存操作草稿"
      }}</el-button></template
    ></el-dialog
  >
  <el-dialog
    v-model="documentOpen"
    :title="documentData.document_name || '文档内容'"
    width="850px"
    ><section v-for="p in documentData.records" :key="p.id">
      <h3>{{ p.title || "无标题段落" }}</h3>
      <pre>{{ p.content }}</pre>
      <el-divider /></section
  ></el-dialog>
  <el-dialog v-model="operationOpen" title="操作文本记录" width="90%"
    ><p>
      {{ date(operationDetail.created_at) }} · {{ operationDetail.kind }} ·
      {{ stateLabels[operationDetail.state] || operationDetail.state }}
    </p>
    <el-alert
      v-if="operationDetail.error"
      :title="operationDetail.error"
      :closable="false"
    />
    <div class="history-texts">
      <section v-for="field in ['before', 'proposed', 'actual']" :key="field">
        <h3>
          {{
            (
              {
                before: "修改 / 删除前",
                proposed: "拟修改后",
                actual: "源系统最终文本",
              } as Row
            )[field]
          }}
        </h3>
        <template v-if="operationDetail[field]"
          ><h4>{{ operationDetail[field].title }}</h4>
          <pre>{{ operationDetail[field].content }}</pre>
          <template
            v-for="p in operationDetail[field].paragraphs || []"
            :key="p.paragraph_id"
            ><h4>{{ p.title || p.paragraph_id }}</h4>
            <pre>{{ p.content }}</pre>
          </template></template
        >
        <p v-else class="muted">
          {{
            field === "actual"
              ? operationDetail.state === "succeeded" &&
                ["delete_paragraph", "delete_document"].includes(
                  operationDetail.kind,
                )
                ? operationDetail.kind === "delete_document"
                  ? "源文档已删除"
                  : "源段落已删除"
                : "尚未执行或核实"
              : "无（删除操作）"
          }}
        </p>
      </section>
    </div>
    <p class="small muted">
      操作人 ID：{{ operationDetail.user_id }}
    </p></el-dialog
  >
</template>
