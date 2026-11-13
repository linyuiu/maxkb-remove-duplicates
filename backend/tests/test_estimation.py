"""Critical-path estimates calibrated from actual COPY scan phases."""

from copy import deepcopy
import math

import pytest
from app.estimation import comparison_count, historical_samples, initial_timing, phase_prediction, timing
from app.matrix import ENGINE
from app.scan_plan import PLAN_VERSION, matrix_cells


def config():
    return dict(source_id="source",knowledge_ids=["b","a"],threshold=0.9,
                exclude_same_document=False,include_inactive_documents=True,batch_size=100)


def plan():
    return dict(version=PLAN_VERSION,engine=ENGINE,matrix_block_size=100,matrix_threads=4,
                matrix_memory_mb=2048,result_batch_rows=50000,writer_work_mem="64MB",
                shared_buffers_bytes=2*1024**3,index_bytes=256*1024**2,worker="test-host")


def sample():
    def row(path,key,parent,seconds):return dict(id=path,key=key,parent_id=parent,seconds=seconds)
    stages=[row("compute/snapshot","snapshot","compute",100),
            row("compute/snapshot/vector","vector_read","compute/snapshot",60),
            row("compute/snapshot/text","snapshot_write","compute/snapshot",30),
            row("compute/compare","compare","compute",530),
            row("compute/compare/setup","transaction","compute/compare",5),
            row("compute/compare/index","index_stats","compute/compare",5),
            row("compute/compare/pipeline","pipeline","compute/compare",500),
            row("compute/compare/pipeline/cpu","matrix_compute","compute/compare/pipeline",400),
            row("compute/compare/pipeline/cpu/wait","queue_wait","compute/compare/pipeline/cpu",100),
            row("compute/compare/final","finalize","compute/compare",20),
            row("compute/compare/final/count","result_count","compute/compare/final",15),
            row("writer0","thread",None,500),
            row("writer0/queue","queue_wait","writer0",50),
            row("writer0/result","result_write","writer0",430),
            row("writer0/result/copy","copy","writer0/result",150)]
    return dict(id="run",source_id="source",engine=ENGINE,knowledge_ids=["a","b"],
                threshold=0.9,exclude_same_document=False,include_inactive_documents=True,
                dimension=768,model_id="model",chunks=1000,pairs=100000,comparisons=499500,
                block_size=100,execution_plan=plan(),profile_run_id="run",profile_version=1,
                profile_final=True,profile_parallel=True,elapsed=640,stages=stages,
                counters=dict(snapshot_chunks=1000,snapshot_paragraphs=1000,submitted_pairs=110000),stats_status="ok")


def predict(history=None, **kwargs):
    args=dict(config=config(),plan=plan(),chunks=1000,paragraphs=1000,comparisons=499500,dimension=768,model_id="model")
    args.update(kwargs)
    return phase_prediction(history or sample(),**args)


def test_comparison_count_excludes_same_paragraph_or_document():
    assert comparison_count(4,[2,1,1])==5
    assert comparison_count(4,[3,1])==3
    assert comparison_count(0,[])==0
    assert comparison_count(4,[4])==0


@pytest.mark.parametrize("chunks,block",[(0,32),(1,32),(2,32),(32,32),(33,32),(100,32),(1000,100)])
def test_matrix_work_counts_full_diagonal_tiles_before_filtering(chunks,block):
    cells=sum(min(block,chunks-i)*min(block,chunks-j)
              for i in range(0,chunks,block) for j in range(0,i+1,block))
    assert matrix_cells(chunks,block)==cells


def test_same_size_reproduces_observation_without_adding_snapshot_or_parallel_threads_twice():
    prediction=predict()
    assert prediction["exact"]
    c=prediction["components"]
    assert c["snapshot_seconds"]==100
    assert c["compute_seconds"]==300  # Remove 100 seconds of backpressure.
    assert c["write_seconds"]==450  # Remove writer queue idle time.
    assert c["pipeline_seconds"]==500  # Includes measured startup/drain overlap penalty.
    assert c["finalize_seconds"]==20 and c["startup_seconds"]==20
    assert c["total_seconds"]==640
    result=timing([prediction])
    assert result["basis"]=="history" and result["history_samples"]==1
    assert result["seconds_expected"]==640
    assert result["seconds_low"]<=640<=result["seconds_high"]


def test_counts_scale_snapshot_linearly_compute_by_actual_tiles_and_writes_by_match_density():
    prediction=predict(chunks=2000,paragraphs=2000,comparisons=1999000)
    c=prediction["components"]
    assert c["snapshot_seconds"]==200
    assert c["compute_seconds"]==pytest.approx(300*2100000/550000)
    assert c["write_seconds"]==pytest.approx(450*1999000/499500)
    assert c["estimated_candidate_rows"]==math.ceil(110000*1999000/499500)
    assert c["finalize_seconds"]==pytest.approx(5+15*1999000/499500)


def test_excluding_valid_pairs_does_not_erase_matrix_multiplication_work():
    prediction=predict(paragraphs=2,comparisons=250000)
    assert prediction["components"]["compute_seconds"]==300
    assert prediction["components"]["write_seconds"]<450
    assert prediction["components"]["estimated_pairs"]<=1


@pytest.mark.parametrize("change",[
    dict(source_id="other"),dict(engine="old-sql"),dict(knowledge_ids=["a"]),dict(dimension=1536),
    dict(model_id="different-model"),dict(threshold=0.95),dict(exclude_same_document=True),
    dict(include_inactive_documents=False),dict(profile_run_id="wrong-run"),dict(profile_final=False),
    dict(profile_parallel=False),dict(profile_version=2),dict(stats_status="invalid"),
    dict(stats_status="partial"),dict(elapsed=float("nan")),dict(elapsed=1),dict(stages=None),dict(counters=None),
    dict(knowledge_ids="invalid"),dict(execution_plan="invalid"),
])
def test_incompatible_or_incomplete_history_is_not_used(change):
    history=sample();history.update(change)
    assert predict(history) is None


def test_retries_do_not_calibrate_steady_throughput():
    history=sample();history["counters"]["reconnect_attempts"]=1
    assert predict(history) is None


@pytest.mark.parametrize("field,value",[("matrix_threads",8),("matrix_block_size",200),("matrix_memory_mb",128),
    ("shared_buffers_bytes",128*1024**2),("writer_work_mem","4MB"),("result_batch_rows",5000),("worker","other-host")])
def test_configuration_change_is_reference_instead_of_precise_history(field,value):
    history=sample();history["execution_plan"][field]=value
    prediction=predict(history)
    assert not prediction["exact"]
    result=timing([prediction])
    assert result["basis"]=="reference" and result["seconds_low"]==160 and result["seconds_high"]==2560


def test_recognizable_legacy_copy_profile_is_only_a_reference():
    history=sample();history["execution_plan"]=None;history["model_id"]=None
    history["stages"]=[row for row in history["stages"] if row["key"] not in ("matrix_compute","pipeline","result_count")]
    prediction=predict(history)
    assert prediction is not None and not prediction["exact"]
    assert prediction["components"]["total_seconds"]==pytest.approx(640)


def test_known_future_pipeline_version_is_not_silently_mixed():
    history=sample();history["execution_plan"]["version"]="incompatible-pipeline"
    assert predict(history) is None


def test_missing_history_uses_explicit_initial_rules_and_always_returns_a_range(monkeypatch):
    monkeypatch.setenv("SCAN_ESTIMATE_MATCH_RATIO","0.001")
    baseline=plan();baseline["matrix_block_size"]=1024
    initial=initial_timing(120898,43335,7159026429,768,baseline)
    result=timing([],initial)
    assert result["basis"]=="initial" and result["history_samples"]==0
    assert 300<result["seconds_expected"]<1000
    assert 0<result["seconds_low"]<=result["seconds_expected"]<=result["seconds_high"]<3600
    assert result["initial_assumptions"]["match_ratio"]==0.001
    assert result["components"]["estimated_batches"]>1


def test_matching_history_takes_precedence_over_mismatched_reference():
    exact=predict()
    reference=deepcopy(exact);reference["exact"]=False;reference["components"]["total_seconds"]=999999
    assert timing([reference,exact])["seconds_expected"]==640
    assert timing([reference,exact])["history_samples"]==1


def test_history_query_does_not_load_large_batch_or_index_snapshots():
    from sqlalchemy.dialects.postgresql import dialect
    class Session:
        def execute(self,statement):
            sql=str(statement.compile(dialect=dialect(),compile_kwargs={"literal_binds":True}))
            assert "batch_series" not in sql and "data -> 'stages'" in sql
            assert "scan_timings.data," not in sql and "scan_runs.config," not in sql
            return self
        def mappings(self):return self
        def all(self):return []
    assert historical_samples(Session(),"source")==[]


def test_partial_tile_timing_cannot_calibrate_a_much_larger_scan():
    assert predict(chunks=1000000,paragraphs=1000000,comparisons=499999500000) is not None
    history=sample();history.update(chunks=2,comparisons=1,pairs=1,block_size=1024)
    history["counters"].update(snapshot_chunks=2,snapshot_paragraphs=2,submitted_pairs=1)
    assert predict(history) is None


def test_zero_match_history_keeps_compute_cost_and_predicts_no_result_writes():
    history=sample();history["pairs"]=0;history["counters"]["submitted_pairs"]=0
    history["stages"]=[row for row in history["stages"] if row["key"] not in ("result_write","copy")]
    prediction=predict(history)
    assert prediction["components"]["estimated_candidate_rows"]==0
    assert prediction["components"]["compute_seconds"]==300
    assert prediction["components"]["pipeline_seconds"]>0


import os
from contextlib import nullcontext
from types import SimpleNamespace
from datetime import timedelta
from test_snapshot_batches import snapshot_db
from test_source_operations import connection


@pytest.mark.skipif(os.environ.get("DEDUP_DISPOSABLE_TEST_DB")!="1",reason="Requires disposable PostgreSQL 18")
@pytest.mark.parametrize("case,basis",[("matching","history"),("cache_changed","reference"),("scope_changed","initial"),("old_serial","initial")])
def test_estimate_calibrates_projected_database_history_and_preserves_read_only_counts(snapshot_db,monkeypatch,case,basis):
    from app import estimation
    from app.models import Run,ScanTiming,now
    from app.scan_plan import execution_plan
    cfg=config()
    monkeypatch.setenv("PG_SHARED_BUFFERS","2GB")  # Read the real PG value, not the deploy template.
    with snapshot_db.sessions.begin() as s:
        runtime=execution_plan(cfg,768,s)
        assert runtime["shared_buffers_bytes"]==16*1024**2
        saved=deepcopy(runtime)
        if case=="cache_changed":saved["shared_buffers_bytes"]+=1024**2
        record=sample()
        run=s.get(Run,"run")
        run.state="succeeded";run.started_at=now()-timedelta(seconds=640);run.finished_at=now()
        run.total=1000;run.pair_count=100000;run.comparisons_total=499500
        run.config={**cfg,"comparison_engine":ENGINE,"actual_dimensions":768,"actual_embedding_model_id":"model",
                    "matrix_block_size":100,"execution_plan":saved}
        if case=="scope_changed":run.config={**run.config,"knowledge_ids":["other"]}
        s.add(ScanTiming(run_id="run",data=dict(version=1,run_id="run",final=True,parallel=case!="old_serial",
            elapsed_seconds=640,stages=record["stages"],counters=record["counters"],
            index_io=dict(delta=dict(status="ok")),batch_series=[dict(large_unused_payload="x"*4096)]*100)))
    counts=[dict(id=key,name=key,workspace_id="workspace",chunks=500,paragraphs=500,documents=50,
                 min_dims=768,max_dims=768,paragraph_pairs=0,document_pairs=0) for key in ("a","b")]
    queries=[]
    class Source:
        def execute(self,query,params):
            queries.append(query)
            return SimpleNamespace(fetchall=lambda:counts)
    monkeypatch.setattr(estimation,"connect",lambda source:nullcontext(Source()))
    monkeypatch.setattr(estimation,"check_models",lambda *args:"model")
    monkeypatch.setattr(estimation,"check_vector_readiness",lambda *args:None)
    with snapshot_db.sessions() as s:
        result=estimation.estimate(None,cfg,s)
    assert result["basis"]==basis and result["chunks"]==1000 and result["comparisons"]==499500
    assert len(queries)==1 and "<=>" not in queries[0]
    assert result["execution_plan"]["shared_buffers_bytes"]==16*1024**2
    assert result["matrix_cells"]==550000
    if basis=="initial":
        assert 0<result["seconds_low"]<=result["seconds_expected"]<=result["seconds_high"]
        assert result["initial_assumptions"]["match_ratio"]==0.001
    else:
        assert result["seconds_expected"]==640 and result["components"]["compute_seconds"]==300


def test_index_growth_is_not_mistaken_for_a_configuration_change_but_large_growth_is_reference():
    current=plan();current["index_bytes"]*=1.5
    assert predict(plan=current)["exact"]
    current["index_bytes"]*=2
    assert not predict(plan=current)["exact"]


def test_crossing_database_cache_capacity_marks_the_estimate_as_reference():
    previous=sample();previous["execution_plan"]["index_bytes"]=int(1.5*1024**3)
    current=plan();current["index_bytes"]=int(2.2*1024**3)
    assert not predict(previous,plan=current)["exact"]


def test_initial_rules_account_for_serial_work_and_batch_fill(monkeypatch):
    monkeypatch.setenv("SCAN_ESTIMATE_MATCH_RATIO","0")
    baseline=plan();baseline["matrix_block_size"]=1024
    parallel=initial_timing(120898,43335,7159026429,768,baseline)["components"]
    baseline["matrix_threads"]=1
    serial=initial_timing(120898,43335,7159026429,768,baseline)["components"]
    assert parallel["write_seconds"]==0 and parallel["estimated_batches"]==0
    assert parallel["pipeline_seconds"]==parallel["compute_seconds"]
    assert parallel["compute_seconds"]<serial["compute_seconds"]<4*parallel["compute_seconds"]


def test_tail_only_initial_estimate_computes_then_writes(monkeypatch):
    monkeypatch.setenv("SCAN_ESTIMATE_MATCH_RATIO","1")
    result=initial_timing(1000,100,499500,768,plan())["components"]
    assert result["estimated_candidate_rows"]==100*99//2
    assert result["estimated_batches"]==1
    assert result["pipeline_seconds"]==pytest.approx(result["compute_seconds"]+result["write_seconds"])


def test_initial_write_estimate_uses_batch_size_and_configured_match_ratio(monkeypatch):
    monkeypatch.setenv("SCAN_ESTIMATE_MATCH_RATIO","0.001")
    full=plan();full["matrix_block_size"]=1024
    normal=initial_timing(120898,43335,7159026429,768,full)
    smaller={**full,"result_batch_rows":5000}
    frequent=initial_timing(120898,43335,7159026429,768,smaller)
    assert frequent["components"]["estimated_batches"]>normal["components"]["estimated_batches"]
    assert frequent["components"]["write_seconds"]>normal["components"]["write_seconds"]
    monkeypatch.setenv("SCAN_ESTIMATE_MATCH_RATIO","0.01")
    denser=initial_timing(120898,43335,7159026429,768,full)
    assert denser["components"]["estimated_candidate_rows"]>normal["components"]["estimated_candidate_rows"]
    assert denser["seconds_expected"]>normal["seconds_expected"]


@pytest.mark.parametrize("name,value",[("SCAN_ESTIMATE_VECTOR_ROWS_PER_SECOND","0"),
    ("SCAN_ESTIMATE_PARAGRAPHS_PER_SECOND","-1"),("SCAN_ESTIMATE_MATRIX_CELLS_PER_SECOND","nan"),
    ("SCAN_ESTIMATE_RESULT_ROWS_PER_SECOND","invalid"),("SCAN_ESTIMATE_RESULT_ROWS_PER_SECOND","inf"),
    ("SCAN_ESTIMATE_MATCH_RATIO","-0.1"),("SCAN_ESTIMATE_MATCH_RATIO","1.01"),("SCAN_ESTIMATE_MATCH_RATIO","nan")])
def test_invalid_initial_configuration_fails_with_setting_name(monkeypatch,name,value):
    monkeypatch.setenv(name,value)
    with pytest.raises(ValueError,match=name):initial_timing(1000,1000,499500,768,plan())


def test_measured_history_is_preferred_to_the_initial_fallback():
    initial=dict(seconds_expected=999999,basis="initial")
    assert timing([predict()],initial)["seconds_expected"]==640
