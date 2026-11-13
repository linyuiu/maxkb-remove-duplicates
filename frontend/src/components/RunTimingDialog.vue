<script setup lang="ts">
import { computed } from "vue";
import type { ScanTiming, TimingStage } from "../scanTiming";
import ScanBatchDiagnostics from "./ScanBatchDiagnostics.vue";

const props = defineProps<{
  modelValue: boolean;
  runId: string;
  taskName: string;
  profile: ScanTiming | null;
}>();
const emit = defineEmits<{
  "update:modelValue": [value: boolean];
  refresh: [];
}>();

type TimingRow = TimingStage & { children: TimingRow[] };
const rows = computed(() => {
  const byId = new Map<string, TimingRow>();
  const roots: TimingRow[] = [];
  for (const stage of props.profile?.stages || []) {
    byId.set(stage.id, { ...stage, children: [] });
  }
  for (const row of byId.values()) {
    const parent = row.parent_id ? byId.get(row.parent_id) : null;
    if (parent) parent.children.push(row);
    else roots.push(row);
  }
  return roots;
});
const counters = computed(() => props.profile?.counters || {});
function sumStage(key: string) {
  return (props.profile?.stages || [])
    .filter((stage) => stage.key === key)
    .reduce((total, stage) => total + stage.seconds, 0);
}
function seconds(value: number) {
  return Number.isFinite(value) ? value.toFixed(3) : "—";
}
function exportTiming() {
  if (!props.profile) return;
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(props.profile, null, 2)], {
      type: "application/json",
    }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `scan-timing-${props.runId}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="耗时分析"
    width="1180px"
    align-center
    @update:model-value="emit('update:modelValue', $event)"
  >
    <template v-if="profile">
      <div class="timing-heading">
        <strong>{{ taskName }}</strong>
        <el-tag :type="profile.final ? 'success' : 'info'">
          {{ profile.final ? "最终统计" : "运行中" }}
        </el-tag>
      </div>
      <div class="timing-stats">
        <div>
          <span>执行耗时</span
          ><strong>{{ seconds(profile.elapsed_seconds) }} s</strong>
        </div>
        <div>
          <span>矩阵乘法</span
          ><strong>{{ seconds(sumStage("multiply")) }} s</strong>
        </div>
        <div>
          <span>结果持久化</span
          ><strong>{{ seconds(sumStage("result_write")) }} s</strong>
        </div>
        <div>
          <span>保存批次</span
          ><strong>{{ counters.result_batches || 0 }}</strong>
        </div>
      </div>
      <p v-if="profile.parallel" class="timing-parallel">
        线程耗时可重叠，执行耗时以总时长为准。
      </p>
      <el-tabs>
        <el-tab-pane label="阶段明细">
          <el-table
            :data="rows"
            row-key="id"
            default-expand-all
            max-height="440"
          >
            <el-table-column
              prop="label"
              label="阶段"
              min-width="290"
              show-overflow-tooltip
            />
            <el-table-column label="累计耗时 / 秒" width="128" align="right">
              <template #header
                ><span title="包含子阶段的耗时">累计耗时 / 秒</span></template
              >
              <template #default="{ row }">{{ seconds(row.seconds) }}</template>
            </el-table-column>
            <el-table-column label="自身耗时 / 秒" width="128" align="right">
              <template #header
                ><span
                  title="不含子阶段；并行线程的耗时可能重叠，不可相加作为执行总耗时"
                  >自身耗时 / 秒</span
                ></template
              >
              <template #default="{ row }">{{
                seconds(row.self_seconds)
              }}</template>
            </el-table-column>
            <el-table-column
              prop="calls"
              label="次数"
              width="95"
              align="right"
            />
            <el-table-column label="平均 / 毫秒" width="120" align="right">
              <template #default="{ row }">{{
                seconds(row.calls ? (row.seconds * 1000) / row.calls : 0)
              }}</template>
            </el-table-column>
            <el-table-column label="最长 / 毫秒" width="120" align="right">
              <template #default="{ row }">{{
                seconds(row.max_seconds * 1000)
              }}</template>
            </el-table-column>
          </el-table>
          <el-descriptions class="timing-counters" :column="2" size="small">
            <el-descriptions-item label="提交候选段落对">{{
              counters.submitted_pairs || 0
            }}</el-descriptions-item>
            <el-descriptions-item label="实际新增">{{
              counters.inserted_pairs || 0
            }}</el-descriptions-item>
            <el-descriptions-item label="更优结果更新">{{
              counters.improved_pairs || 0
            }}</el-descriptions-item>
            <el-descriptions-item label="无需更新">{{
              counters.unchanged_pairs || 0
            }}</el-descriptions-item>
            <el-descriptions-item label="未细分开销"
              >{{ seconds(profile.unmeasured_seconds) }} s</el-descriptions-item
            >
          </el-descriptions>
        </el-tab-pane>
        <el-tab-pane label="批次与索引" lazy>
          <ScanBatchDiagnostics :profile="profile" />
        </el-tab-pane>
      </el-tabs>
    </template>
    <el-empty v-else description="此扫描暂无计时，请启动新的扫描后查看" />
    <template #footer>
      <el-button @click="emit('refresh')">刷新</el-button>
      <el-button :disabled="!profile" @click="exportTiming"
        >导出 JSON</el-button
      >
      <el-button @click="emit('update:modelValue', false)">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.timing-parallel {
  margin: 0 0 12px;
  color: #606b78;
  font-size: 12px;
}
.timing-heading {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}
.timing-stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}
.timing-stats > div {
  padding: 14px;
  border: 1px solid #e8ebef;
  border-radius: 12px;
}
.timing-stats span {
  display: block;
  color: #6c727a;
  font-size: 12px;
  margin-bottom: 8px;
}
.timing-stats strong {
  font-size: 20px;
  font-variant-numeric: tabular-nums;
}
.timing-counters {
  margin-top: 16px;
}
@media (max-width: 700px) {
  .timing-stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
