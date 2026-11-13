"""O(N) read-only counts; never executes the O(N²) similarity join."""

import math
import os
from statistics import median
from sqlalchemy import select
from .matrix import ENGINE
from .scan_plan import execution_plan, matrix_cells
from .models import Run, ScanTiming
from .source import connect, check_models, check_vector_readiness, document_scope

COUNT_SQL = f"""
WITH p AS (
 SELECT knowledge_id,document_id,paragraph_id,count(*) AS n,
        min(vector_dims(embedding)) AS min_dims,max(vector_dims(embedding)) AS max_dims
 FROM embedding e WHERE knowledge_id=ANY(%(knowledge_ids)s::uuid[]) AND source_type='1'
   AND {document_scope('e')}
 GROUP BY knowledge_id,document_id,paragraph_id
), d AS (
 SELECT knowledge_id,document_id,sum(n) AS n FROM p GROUP BY knowledge_id,document_id
), ks AS (
 SELECT knowledge_id,sum(n) AS chunks,count(*) AS paragraphs,min(min_dims) AS min_dims,
        max(max_dims) AS max_dims,sum(n::numeric*(n-1)/2) AS paragraph_pairs FROM p GROUP BY knowledge_id
), ds AS (
 SELECT knowledge_id,count(*) AS documents,sum(n*(n-1)/2) AS document_pairs FROM d GROUP BY knowledge_id
)
SELECT k.id::text AS id,k.name,k.workspace_id,coalesce(ks.chunks,0) AS chunks,
       coalesce(ks.paragraphs,0) AS paragraphs,coalesce(ds.documents,0) AS documents,
       ks.min_dims,ks.max_dims,coalesce(ks.paragraph_pairs,0) AS paragraph_pairs,
       coalesce(ds.document_pairs,0) AS document_pairs
FROM knowledge k LEFT JOIN ks ON ks.knowledge_id=k.id LEFT JOIN ds ON ds.knowledge_id=k.id
WHERE k.id=ANY(%(knowledge_ids)s::uuid[]) ORDER BY k.name,k.id
"""


def comparison_count(total, excluded_group_counts):
    return total * (total - 1) // 2 - sum(
        n * (n - 1) // 2 for n in excluded_group_counts
    )


def task_config(task):
    return {
        key: getattr(task, key)
        for key in [
            "name",
            "source_id",
            "knowledge_ids",
            "threshold",
            "exclude_same_document",
            "include_inactive_documents",
            "batch_size",
        ]
    } | {"task_name": task.name}


def scan_config(task):
    values = task_config(task)
    values.pop("name")
    return values | {"comparison_engine": ENGINE}


def number(value):
    if isinstance(value,bool):
        return None
    try:
        result=float(value)
    except (TypeError,ValueError,OverflowError):
        return None
    return result if math.isfinite(result) and result>=0 else None


def stage_seconds(stages, key, prefix=None):
    return sum(number(row.get("seconds")) or 0 for row in stages
               if row.get("key")==key and (prefix is None or row.get("id","").startswith(prefix)))


def phase_prediction(history, config, plan, chunks, paragraphs, comparisons, dimension, model_id):
    if history["engine"]!=ENGINE or history["source_id"]!=config["source_id"]:
        return None
    if not isinstance(history["knowledge_ids"],list) or not all(isinstance(value,str) for value in history["knowledge_ids"]):
        return None
    if sorted(set(history["knowledge_ids"]))!=sorted(set(config["knowledge_ids"])):
        return None
    if any(history[key]!=config[key] for key in ("threshold","exclude_same_document")):
        return None
    if (history["include_inactive_documents"] if history["include_inactive_documents"] is not None else True)!=config.get("include_inactive_documents",True):
        return None
    if history["dimension"]!=dimension or (history["model_id"] and history["model_id"]!=model_id):
        return None
    if history["profile_run_id"]!=history["id"] or history["profile_version"]!=1 or history["profile_final"] is not True or history["profile_parallel"] is not True:
        return None
    if history["execution_plan"] is not None and not isinstance(history["execution_plan"],dict):
        return None
    if history["execution_plan"] and history["execution_plan"].get("version")!=plan["version"]:
        return None
    if history.get("stats_status") in ("invalid","partial"):
        return None
    stages=history["stages"]
    counters=history["counters"]
    if not isinstance(stages,list) or not all(isinstance(row,dict) for row in stages) or not isinstance(counters,dict):
        return None
    if any(not isinstance(row.get("id"),str) or number(row.get("seconds")) is None for row in stages):
        return None
    if counters.get("reconnect_attempts",0) or any(row.get("errors",0) for row in stages):
        return None
    old_chunks=number(history["chunks"])
    old_paragraphs=number(counters.get("snapshot_paragraphs"))
    old_comparisons=number(history["comparisons"])
    old_rows=number(counters.get("submitted_pairs"))
    old_pairs=number(history["pairs"])
    elapsed=number(history["elapsed"])
    block=history["block_size"]
    if not all(value is not None for value in (old_chunks,old_paragraphs,old_comparisons,old_rows,old_pairs,elapsed)):
        return None
    if old_chunks<2 or old_paragraphs<1 or old_comparisons<1 or elapsed<=0 or not isinstance(block,int) or block<1:
        return None
    if old_rows>old_comparisons or old_pairs>old_rows:
        return None
    if counters.get("snapshot_chunks",history["chunks"])!=history["chunks"]:
        return None
    # Tiny partial tiles are not a useful throughput calibration for full-sized tiles.
    ratio=chunks/old_chunks
    if (ratio<0.25 or ratio>4) and min(chunks,old_chunks)<min(block,plan["matrix_block_size"]):
        return None
    main=[row for row in stages if not row.get("id","").startswith("writer")]
    writers=[row for row in stages if row.get("id","").startswith("writer")]
    snapshot=stage_seconds(main,"snapshot")
    comparison=stage_seconds(main,"compare")
    finalize=stage_seconds(main,"finalize")
    count_time=stage_seconds(main,"result_count")
    writer_threads=[row for row in writers if row.get("key")=="thread" and row.get("parent_id") is None]
    # Reject the old serial SQL/ORM implementation even if its algorithm name matches.
    if not writer_threads or not any(row.get("key")=="copy" for row in writers) and old_rows>0:
        return None
    write=max(0,sum(number(row.get("seconds")) or 0 for row in writer_threads)-stage_seconds(writers,"queue_wait"))
    compute_records=[row for row in main if row.get("key")=="matrix_compute"]
    if compute_records:
        compute=sum(max(0,(number(row.get("seconds")) or 0)-sum(
            number(child.get("seconds")) or 0 for child in main
            if child.get("key") in ("queue_wait","buffer_wait") and child.get("id","").startswith(row["id"]+"/"))) for row in compute_records)
    else:
        compute=max(0,comparison-finalize-stage_seconds(main,"queue_wait")-stage_seconds(main,"buffer_wait")-stage_seconds(main,"index_stats"))
    pipeline=stage_seconds(main,"pipeline")
    if not pipeline:
        compare_ids={row["id"] for row in main if row.get("key")=="compare"}
        setup=sum(number(row.get("seconds")) or 0 for row in main
                  if row.get("parent_id") in compare_ids and row.get("key") in ("transaction","index_stats"))
        pipeline=max(0,comparison-finalize-setup)
    if snapshot<=0 or comparison<=0 or max(compute,write)<=0 or elapsed+0.001<snapshot+comparison:
        return None
    old_cells=matrix_cells(int(old_chunks),block)
    current_cells=matrix_cells(chunks,plan["matrix_block_size"])
    vector=sum(stage_seconds(main,key) for key in ("vector_read","vector_store","index_flush","paragraph_index"))
    text=sum(stage_seconds(main,key) for key in ("paragraph_read","paragraph_prepare","paragraph_chunks","snapshot_write"))
    predicted_snapshot=(vector+max(0,snapshot-vector-text))*ratio+text*paragraphs/old_paragraphs
    predicted_compute=compute*current_cells/old_cells
    predicted_rows=comparisons*old_rows/old_comparisons
    predicted_write=write*(predicted_rows/old_rows if old_rows else current_cells/old_cells)
    predicted_pairs=min(paragraphs*(paragraphs-1)/2,comparisons*old_pairs/old_comparisons)
    predicted_finalize=max(0,finalize-count_time)+count_time*(predicted_pairs/old_pairs if old_pairs else 1)
    overhead=max(1,pipeline/max(compute,write))
    components=dict(snapshot_seconds=predicted_snapshot,compute_seconds=predicted_compute,
                    write_seconds=predicted_write,pipeline_seconds=max(predicted_compute,predicted_write)*overhead,
                    finalize_seconds=predicted_finalize,startup_seconds=max(0,elapsed-snapshot-pipeline-finalize),
                    estimated_candidate_rows=math.ceil(predicted_rows),estimated_pairs=math.ceil(predicted_pairs),
                    estimated_batches=math.ceil(predicted_rows/plan["result_batch_rows"]))
    components["total_seconds"]=sum(components[key] for key in ("snapshot_seconds","pipeline_seconds","finalize_seconds","startup_seconds"))
    previous=history["execution_plan"] or {}
    same_plan={key:value for key,value in previous.items() if key!="index_bytes"}=={key:value for key,value in plan.items() if key!="index_bytes"}
    old_index=number(previous.get("index_bytes"))
    new_index=number(plan.get("index_bytes"))
    old_cache=number(previous.get("shared_buffers_bytes"))
    new_cache=number(plan.get("shared_buffers_bytes"))
    similar_footprint=False
    if old_index and new_index and old_cache and new_cache:
        similar_footprint=0.5<=new_index/old_index<=2 and (old_index>old_cache)==(new_index>new_cache)
    exact=same_plan and similar_footprint and bool(compute_records) and model_id is not None and history["model_id"]==model_id and 0.25<=ratio<=4 and 0.25<=paragraphs/old_paragraphs<=4
    return dict(exact=exact,components=components)


def estimate_setting(name, default, *, ratio=False):
    try:
        value=float(os.environ.get(name,default))
    except ValueError:
        raise ValueError(f"{name} 必须是数字") from None
    if not math.isfinite(value) or (not 0<=value<=1 if ratio else value<=0):
        raise ValueError(f"{name} 必须介于 0 和 1 之间" if ratio else f"{name} 必须是正数")
    return value


def initial_timing(chunks, paragraphs, comparisons, dimension, plan):
    assumptions=dict(
        vector_rows_per_second=estimate_setting("SCAN_ESTIMATE_VECTOR_ROWS_PER_SECOND",2000),
        paragraphs_per_second=estimate_setting("SCAN_ESTIMATE_PARAGRAPHS_PER_SECOND",3000),
        matrix_cells_per_second=estimate_setting("SCAN_ESTIMATE_MATRIX_CELLS_PER_SECOND",40000000),
        result_rows_per_second=estimate_setting("SCAN_ESTIMATE_RESULT_ROWS_PER_SECOND",15000),
        match_ratio=estimate_setting("SCAN_ESTIMATE_MATCH_RATIO",0.001,ratio=True),
    )
    block=plan["matrix_block_size"]
    cells=matrix_cells(chunks,block)
    dimension_factor=max(dimension,1)/768
    snapshot=chunks*dimension_factor/assumptions["vector_rows_per_second"]+paragraphs/assumptions["paragraphs_per_second"]
    # Baseline: 768 dimensions, 1024-row tiles and four BLAS threads. Filtering
    # and metadata work stay mostly serial, so thread count never scales all work.
    compute=cells/assumptions["matrix_cells_per_second"]*(
        0.6+0.3*dimension_factor*4/max(1,plan["matrix_threads"])+0.1*1024/block)
    candidates=math.ceil(comparisons*assumptions["match_ratio"])
    maximum_pairs=paragraphs*(paragraphs-1)//2
    # Below one full result batch, the producer can retain every unique pair
    # through the entire comparison; repeated chunk hits do not add write rows.
    if maximum_pairs<plan["result_batch_rows"]:
        candidates=min(candidates,maximum_pairs)
    batches=math.ceil(candidates/plan["result_batch_rows"])
    write=candidates/assumptions["result_rows_per_second"]+batches*0.05
    # The first batch must be produced before its COPY starts; a tail-only
    # scan computes first and writes afterward rather than overlapping the two.
    fill=compute*min(1,plan["result_batch_rows"]/candidates) if candidates else 0
    pipeline=fill+max(compute-fill,write)
    pairs=min(candidates,maximum_pairs)
    components=dict(snapshot_seconds=snapshot,compute_seconds=compute,write_seconds=write,
                    pipeline_seconds=pipeline,finalize_seconds=1+pairs/5000000,startup_seconds=2,
                    estimated_candidate_rows=candidates,estimated_pairs=pairs,estimated_batches=batches)
    total=sum(components[key] for key in ("snapshot_seconds","pipeline_seconds","finalize_seconds","startup_seconds"))
    if not math.isfinite(total):
        raise ValueError("初始估算参数产生了无效耗时，请检查估算吞吐配置")
    components["total_seconds"]=total
    return dict(seconds_low=max(1,math.floor(total*0.5)),seconds_high=max(3,math.ceil(total*3)),
                seconds_expected=total,basis="initial",history_samples=0,components=components,
                initial_assumptions=assumptions)


def timing(predictions, initial=None):
    exact=[row for row in predictions if row["exact"]]
    selected=exact or predictions
    if not selected:
        if initial is None:
            raise ValueError("缺少初始估算参数")
        return initial
    totals=[row["components"]["total_seconds"] for row in selected]
    representative=min(selected,key=lambda row:abs(row["components"]["total_seconds"]-median(totals)))
    low,high=(0.6,2) if exact else (0.25,4)
    return dict(seconds_low=max(1,math.floor(min(totals)*low)),seconds_high=max(3,math.ceil(max(totals)*high)),
                seconds_expected=representative["components"]["total_seconds"],basis="history" if exact else "reference",
                history_samples=len(selected),components=representative["components"])


def historical_samples(session, source_id):
    # Project only compact phase data; never load large batch-series JSON or ORM relationships.
    fields={"engine":"comparison_engine","source_id":"source_id","knowledge_ids":"knowledge_ids",
            "threshold":"threshold","exclude_same_document":"exclude_same_document",
            "include_inactive_documents":"include_inactive_documents","dimension":"actual_dimensions",
            "model_id":"actual_embedding_model_id","block_size":"matrix_block_size","execution_plan":"execution_plan"}
    expressions=[]
    for label,key in fields.items():
        value=Run.config[key]
        if label in ("engine","source_id","model_id"):
            value=value.as_string()
        elif label=="threshold":
            value=value.as_float()
        elif label in ("exclude_same_document","include_inactive_documents"):
            value=value.as_boolean()
        elif label in ("dimension","block_size"):
            value=value.as_integer()
        expressions.append(value.label(label))
    profile=ScanTiming.data
    statement=select(Run.id,Run.total.label("chunks"),Run.pair_count.label("pairs"),Run.comparisons_total.label("comparisons"),
        *expressions,profile["run_id"].as_string().label("profile_run_id"),profile["version"].as_integer().label("profile_version"),
        profile["final"].as_boolean().label("profile_final"),profile["parallel"].as_boolean().label("profile_parallel"),
        profile["elapsed_seconds"].as_float().label("elapsed"),profile["stages"].label("stages"),profile["counters"].label("counters"),
        profile["index_io"]["delta"]["status"].as_string().label("stats_status"),
    ).join(ScanTiming,ScanTiming.run_id==Run.id).where(Run.state=="succeeded",Run.finished_at.is_not(None),
        Run.config["source_id"].as_string()==source_id,Run.config["comparison_engine"].as_string()==ENGINE,
    ).order_by(Run.finished_at.desc()).limit(50)
    return session.execute(statement).mappings().all()


def estimate(source, config, session):
    include_inactive_documents = config.get("include_inactive_documents", True)
    with connect(source) as connection:
        model_id = check_models(connection, config["knowledge_ids"])
        check_vector_readiness(connection, config["knowledge_ids"], include_inactive_documents)
        rows = connection.execute(
            COUNT_SQL,
            {
                "knowledge_ids": config["knowledge_ids"],
                "include_inactive_documents": include_inactive_documents,
            },
        ).fetchall()
    dimensions = {
        int(r[k]) for r in rows for k in ["min_dims", "max_dims"] if r[k] is not None
    }
    if len(dimensions) > 1:
        raise ValueError("所选数据向量维度不一致")
    dims = next(iter(dimensions), 0)
    chunks = sum(int(r["chunks"]) for r in rows)
    excluded = sum(
        int(
            r["document_pairs"]
            if config["exclude_same_document"]
            else r["paragraph_pairs"]
        )
        for r in rows
    )
    comparisons = chunks * (chunks - 1) // 2 - excluded
    paragraphs=sum(int(row["paragraphs"]) for row in rows)
    plan=execution_plan(config,dims,session)
    predictions=[]
    if comparisons>0:
        for sample in historical_samples(session,config["source_id"]):
            prediction=phase_prediction(sample,config,plan,chunks,paragraphs,comparisons,dims,model_id)
            if prediction is not None:
                predictions.append(prediction)
    if comparisons:
        initial=initial_timing(chunks,paragraphs,comparisons,dims,plan) if not predictions else None
        duration=timing(predictions,initial)
    else:
        duration=dict(seconds_low=0,seconds_high=0,seconds_expected=0,basis="empty",history_samples=0,components=None)
    return {
        "chunks": chunks,
        "paragraphs": sum(int(r["paragraphs"]) for r in rows),
        "documents": sum(int(r["documents"]) for r in rows),
        "dimensions": dims,
        "comparisons": comparisons,
        "knowledge": [
            {
                "id": r["id"],
                "name": r["name"],
                "workspace_id": r["workspace_id"],
                "chunks": int(r["chunks"]),
                "paragraphs": int(r["paragraphs"]),
                "documents": int(r["documents"]),
            }
            for r in rows
        ],
        "execution_plan":plan,
        "matrix_cells":matrix_cells(chunks,plan["matrix_block_size"]),
        **duration,
    }
