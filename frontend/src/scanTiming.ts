export type TimingStage = {
  id: string;
  key: string;
  parent_id: string | null;
  label: string;
  seconds: number;
  self_seconds: number;
  calls: number;
  max_seconds: number;
  errors: number;
  active?: boolean;
};

export type BatchTiming = {
  run_id: string;
  kind: "results" | "snapshots";
  batch_no: number;
  batch_seq: number;
  rows: number;
  started_seconds: number;
  finished_seconds: number;
  seconds: number;
  copy_seconds: number;
  merge_seconds: number;
  commit_seconds: number;
  attempts: number;
  recovered: boolean;
  status: "committed" | "unconfirmed";
  error_type?: string;
};

export type IndexDelta = {
  index_oid: number;
  index_name: string;
  status: string;
  reason?: string;
  idx_blks_read?: number;
  idx_blks_hit?: number;
  read_bytes?: number;
  size_before_bytes?: number;
  size_after_bytes?: number;
};

export type ScanTiming = {
  version: number;
  run_id: string;
  final: boolean;
  parallel?: boolean;
  elapsed_seconds: number;
  unmeasured_seconds: number;
  counters: Record<string, number>;
  stages: TimingStage[];
  batch_series?: BatchTiming[];
  batch_series_total?: number;
  batch_series_omitted?: number;
  index_io?: {
    before?: { status: string; shared_buffers?: string; work_mem?: string };
    after?: { status: string };
    delta?: {
      status: string;
      reason?: string;
      includes_other_sessions: boolean;
      read_includes_os_cache: boolean;
      own_stats_flushed?: boolean;
      indexes: IndexDelta[];
    };
  };
};
