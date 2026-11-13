<script setup lang="ts">
import { computed, ref } from "vue";
import type { BatchTiming, ScanTiming } from "../scanTiming";

const props = defineProps<{ profile: ScanTiming }>();
const batches = computed(() =>
  (props.profile.batch_series || []).filter((row) => row.kind === "results"),
);
const active = ref<BatchTiming | null>(null);
const mode = ref<"batch" | "per10k">("batch");
function duration(row: BatchTiming, key: "copy_seconds" | "merge_seconds") {
  return (
    row[key] * (mode.value === "batch" ? 1 : 10000 / Math.max(1, row.rows))
  );
}
const first = computed(() => batches.value[0]?.batch_no || 1);
const last = computed(() => batches.value.at(-1)?.batch_no || first.value);
const maximum = computed(() =>
  Math.max(
    0.001,
    ...batches.value.flatMap((row) => [
      duration(row, "copy_seconds"),
      duration(row, "merge_seconds"),
    ]),
  ),
);
function x(row: BatchTiming) {
  return (
    15 +
    ((row.batch_no - first.value) / Math.max(1, last.value - first.value)) * 970
  );
}
function y(row: BatchTiming, key: "copy_seconds" | "merge_seconds") {
  return 170 - (duration(row, key) / maximum.value) * 155;
}
function path(key: "copy_seconds" | "merge_seconds") {
  return batches.value
    .map((row, index) => {
      const continuous =
        index > 0 && batches.value[index - 1].batch_no + 1 === row.batch_no;
      return `${continuous ? "L" : "M"}${x(row)},${y(row, key)}`;
    })
    .join(" ");
}
function hover(event: MouseEvent) {
  const element = event.currentTarget;
  if (!(element instanceof SVGElement)) return;
  const box = element.getBoundingClientRect();
  const target =
    first.value +
    ((((event.clientX - box.left) / box.width) * 1000 - 15) / 970) *
      (last.value - first.value);
  active.value = batches.value.reduce<BatchTiming | null>(
    (nearest, row) =>
      !nearest ||
      Math.abs(row.batch_no - target) < Math.abs(nearest.batch_no - target)
        ? row
        : nearest,
    null,
  );
}
function seconds(value: number) {
  return value.toFixed(3);
}
function amount(value?: number) {
  return value === undefined ? "—" : value.toLocaleString();
}
function mib(value?: number) {
  return value === undefined ? "—" : (value / 1024 ** 2).toFixed(1);
}
const delta = computed(() => props.profile.index_io?.delta);
const indexes = computed(() =>
  [...(delta.value?.indexes || [])].sort(
    (a, b) => (b.idx_blks_read || 0) - (a.idx_blks_read || 0),
  ),
);
const names: Record<string, string> = {
  similarity_pairs_pkey: "主键",
  similarity_pairs_run_id_left_id_right_id_key: "段落对唯一",
  pairs_run_score: "相似度排序",
  ix_similarity_pairs_run_id: "运行 ID",
};
</script>

<template>
  <div class="batch-diagnostics">
    <template v-if="batches.length">
      <div class="chart-heading">
        <strong>{{ mode === "batch" ? "每批耗时" : "每万行耗时" }} / 秒</strong>
        <el-radio-group v-model="mode" size="small" aria-label="曲线单位">
          <el-radio-button value="batch">每批</el-radio-button>
          <el-radio-button value="per10k">每万行</el-radio-button>
        </el-radio-group>
        <div class="legend">
          <span class="copy-label">COPY</span
          ><span class="merge-label">MERGE</span>
        </div>
      </div>
      <div class="batch-chart">
        <span class="chart-maximum">{{ seconds(maximum) }}</span>
        <svg
          viewBox="0 0 1000 185"
          preserveAspectRatio="none"
          role="img"
          :aria-label="`COPY 与 MERGE 耗时趋势，${mode === 'batch' ? '每批' : '每万行'}秒数`"
          @mousemove="hover"
          @mouseleave="active = null"
        >
          <path d="M15 15H985M15 92H985M15 170H985" class="grid-lines" />
          <path :d="path('copy_seconds')" class="copy-line" />
          <path :d="path('merge_seconds')" class="merge-line" />
          <template v-if="batches.length === 1 && !active">
            <circle
              :cx="x(batches[0])"
              :cy="y(batches[0], 'copy_seconds')"
              r="3"
              class="copy-point"
            />
            <circle
              :cx="x(batches[0])"
              :cy="y(batches[0], 'merge_seconds')"
              r="3"
              class="merge-point"
            />
          </template>
          <template v-if="active">
            <path :d="`M${x(active)} 15V170`" class="cursor-line" />
            <circle
              :cx="x(active)"
              :cy="y(active, 'copy_seconds')"
              r="3"
              class="copy-point"
            />
            <circle
              :cx="x(active)"
              :cy="y(active, 'merge_seconds')"
              r="3"
              class="merge-point"
            />
          </template>
        </svg>
        <div v-if="active" class="chart-tooltip">
          <strong
            >第 {{ active.batch_no }} 批 · {{ amount(active.rows) }} 行</strong
          >
          <span>COPY {{ seconds(active.copy_seconds) }} s</span>
          <span>MERGE {{ seconds(active.merge_seconds) }} s</span>
        </div>
        <div class="chart-axis">
          <span>第 {{ first }} 批</span><span>第 {{ last }} 批</span>
        </div>
      </div>
      <el-alert
        v-if="profile.batch_series_omitted"
        type="info"
        :closable="false"
        :title="`中间 ${profile.batch_series_omitted} 批已省略；完整记录见 scan_batch_timing 日志`"
      />
      <el-table
        :data="batches"
        max-height="250"
        row-key="batch_no"
        class="batch-table"
      >
        <el-table-column prop="batch_no" label="批次" width="75" />
        <el-table-column label="行数" min-width="100" align="right"
          ><template #default="{ row }">{{
            amount(row.rows)
          }}</template></el-table-column
        >
        <el-table-column label="COPY / 秒" min-width="120" align="right"
          ><template #default="{ row }">{{
            seconds(row.copy_seconds)
          }}</template></el-table-column
        >
        <el-table-column label="MERGE / 秒" min-width="120" align="right"
          ><template #default="{ row }">{{
            seconds(row.merge_seconds)
          }}</template></el-table-column
        >
        <el-table-column label="MERGE / 万行秒" min-width="150" align="right"
          ><template #default="{ row }">{{
            seconds((row.merge_seconds * 10000) / Math.max(1, row.rows))
          }}</template></el-table-column
        >
        <el-table-column
          label="尝试"
          prop="attempts"
          width="70"
          align="right"
        />
        <el-table-column label="状态" width="100"
          ><template #default="{ row }"
            ><el-tag
              :type="row.status === 'committed' ? 'success' : 'warning'"
              >{{
                row.status === "committed"
                  ? row.recovered
                    ? "重连确认"
                    : "已确认"
                  : "未确认"
              }}</el-tag
            ></template
          ></el-table-column
        >
      </el-table>
    </template>
    <el-empty
      v-else
      description="暂无批次记录，新的扫描会记录每批耗时"
      :image-size="60"
    />
    <div class="index-heading">
      <strong
        title="比较写入开始至队列清空，不含最终 COUNT；计数包含同库其他连接活动，读块也可能来自操作系统缓存"
        >索引读取增量</strong
      >
      <span v-if="profile.index_io?.before?.status === 'ok'"
        >{{ profile.index_io.before.shared_buffers }} 缓存 ·
        {{ profile.index_io.before.work_mem }} 排序内存</span
      >
      <el-tag v-if="delta?.own_stats_flushed === false" type="info"
        >统计刷新未确认</el-tag
      >
      <el-tag v-if="delta && delta.status !== 'ok'" type="warning">{{
        delta.status === "unavailable" ? "未采集" : "部分统计无效"
      }}</el-tag>
    </div>
    <el-table
      v-if="indexes.length"
      :data="indexes"
      row-key="index_oid"
      max-height="260"
    >
      <el-table-column label="索引" min-width="150" show-overflow-tooltip
        ><template #default="{ row }"
          ><span :title="row.index_name">{{
            names[row.index_name] || row.index_name
          }}</span></template
        ></el-table-column
      >
      <el-table-column label="读块" min-width="110" align="right"
        ><template #default="{ row }">{{
          amount(row.idx_blks_read)
        }}</template></el-table-column
      >
      <el-table-column label="命中块" min-width="110" align="right"
        ><template #default="{ row }">{{
          amount(row.idx_blks_hit)
        }}</template></el-table-column
      >
      <el-table-column label="读取 / MiB" min-width="115" align="right"
        ><template #default="{ row }">{{
          mib(row.read_bytes)
        }}</template></el-table-column
      >
      <el-table-column label="开始 / MiB" min-width="115" align="right"
        ><template #default="{ row }">{{
          mib(row.size_before_bytes)
        }}</template></el-table-column
      >
      <el-table-column label="结束 / MiB" min-width="115" align="right"
        ><template #default="{ row }">{{
          mib(row.size_after_bytes)
        }}</template></el-table-column
      >
    </el-table>
    <el-empty
      v-else
      :description="
        profile.final
          ? '此扫描没有可用的索引快照'
          : '写入结束后显示索引读取增量'
      "
      :image-size="60"
    />
  </div>
</template>

<style scoped>
.chart-heading,
.index-heading,
.legend,
.chart-axis {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.chart-heading {
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.legend {
  font-size: 12px;
}
.copy-label {
  color: #3b82f6;
}
.merge-label {
  color: #149b77;
}
.batch-chart {
  position: relative;
  padding: 10px 0 0 32px;
}
.batch-chart svg {
  width: 100%;
  height: 185px;
  overflow: visible;
}
.grid-lines {
  stroke: #e8edf2;
  stroke-width: 1;
  fill: none;
}
.copy-line,
.merge-line {
  fill: none;
  stroke-width: 2;
  vector-effect: non-scaling-stroke;
}
.copy-line {
  stroke: #3b82f6;
}
.merge-line {
  stroke: #149b77;
}
.copy-point {
  fill: #3b82f6;
}
.merge-point {
  fill: #149b77;
}
.cursor-line {
  stroke: #9daab7;
  stroke-dasharray: 4 3;
  vector-effect: non-scaling-stroke;
}
.chart-maximum {
  position: absolute;
  left: 0;
  top: 15px;
  font-size: 11px;
  color: #6b7280;
}
.chart-axis {
  color: #6b7280;
  font-size: 12px;
}
.chart-tooltip {
  position: absolute;
  right: 8px;
  top: 12px;
  display: grid;
  gap: 4px;
  padding: 9px 12px;
  border: 1px solid #e4e9ef;
  border-radius: 8px;
  background: #ffffffed;
  pointer-events: none;
  font-size: 12px;
}
.batch-table {
  margin-top: 14px;
}
.index-heading {
  flex-wrap: wrap;
  margin: 22px 0 10px;
}
.index-heading > span {
  font-size: 12px;
  color: #6b7280;
}
</style>
