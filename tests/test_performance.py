"""
Performance benchmarks for Work Intelligence V2.

Measures:
- Ingest latency (p50, p95, p99)
- Throughput (observations/second)
- Memory usage under load
- SQLite WAL performance
- Concurrent ingest scaling
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aftergraph_work_intelligence.models import ObservationInput
from aftergraph_work_intelligence.policy import PolicyStore, TenantPolicy
from aftergraph_work_intelligence.service import WorkIntelligenceService
from aftergraph_work_intelligence.store import SQLiteStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _svc(db_path=None):
    """Create WorkIntelligenceService with optional custom DB."""
    path = db_path or ":memory:"
    store = SQLiteStore(path)
    ps = PolicyStore()
    ps.put("default", TenantPolicy())
    return WorkIntelligenceService(store, policy_store=ps), store


def _make_obs(tenant_id="default", source="conversation", idx=0):
    """Create an observation input."""
    return ObservationInput(
        tenant_id=tenant_id,
        source=source,
        text=f"Køb bøger til kontoret benchmark item {idx}",
    )


def _percentile(data, p):
    """Calculate percentile."""
    k = (len(data) - 1) * (p / 100)
    f = int(k)
    c = f + 1
    if c >= len(data):
        return data[-1]
    return data[f] + (k - f) * (data[c] - data[f])


# ---------------------------------------------------------------------------
# Benchmark: Ingest Latency
# ---------------------------------------------------------------------------
class TestIngestLatency:
    """Measure single-threaded ingest latency."""

    def test_p50_p95_p99_latency(self):
        """Measure latency percentiles for 1000 ingestions."""
        svc, _ = _svc()
        latencies = []

        for i in range(1000):
            obs = _make_obs(idx=i)
            start = time.perf_counter()
            svc.ingest(obs)
            elapsed = (time.perf_counter() - start) * 1000  # ms
            latencies.append(elapsed)

        latencies.sort()
        p50 = _percentile(latencies, 50)
        p95 = _percentile(latencies, 95)
        p99 = _percentile(latencies, 99)

        # All latencies should be under 100ms
        assert p99 < 100, f"p99 latency {p99:.2f}ms exceeds 100ms threshold"
        assert p50 < 50, f"p50 latency {p50:.2f}ms exceeds 50ms threshold"

        print("\nIngest latency (1000 ops):")
        print(f"  p50: {p50:.2f}ms")
        print(f"  p95: {p95:.2f}ms")
        print(f"  p99: {p99:.2f}ms")
        print(f"  min: {min(latencies):.2f}ms")
        print(f"  max: {max(latencies):.2f}ms")


# ---------------------------------------------------------------------------
# Benchmark: Throughput
# ---------------------------------------------------------------------------
class TestThroughput:
    """Measure observations per second."""

    def test_single_thread_throughput(self):
        """Measure single-thread throughput."""
        svc, _ = _svc()
        count = 500
        start = time.perf_counter()

        for i in range(count):
            obs = _make_obs(idx=i)
            svc.ingest(obs)

        elapsed = time.perf_counter() - start
        ops_per_sec = count / elapsed

        # Should handle at least 100 ops/sec
        assert ops_per_sec >= 100, f"Throughput {ops_per_sec:.0f} ops/sec below 100 threshold"

        print(f"\nSingle-thread throughput: {ops_per_sec:.0f} ops/sec ({count} ops in {elapsed:.2f}s)")

    def test_concurrent_throughput(self):
        """Measure multi-thread throughput with 4 threads."""
        svc, _ = _svc()
        count_per_thread = 250
        num_threads = 4
        total = count_per_thread * num_threads
        errors = []

        def ingest_batch(thread_idx):
            try:
                for i in range(count_per_thread):
                    obs = _make_obs(idx=thread_idx * count_per_thread + i)
                    svc.ingest(obs)
            except Exception as e:
                errors.append(e)

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(ingest_batch, t) for t in range(num_threads)]
            for f in as_completed(futures):
                f.result()
        elapsed = time.perf_counter() - start

        assert not errors, f"Concurrent ingest errors: {errors}"
        ops_per_sec = total / elapsed

        print(f"\nConcurrent throughput ({num_threads} threads): {ops_per_sec:.0f} ops/sec ({total} ops in {elapsed:.2f}s)")


# ---------------------------------------------------------------------------
# Benchmark: SQLite WAL Performance
# ---------------------------------------------------------------------------
class TestWALPerformance:
    """Measure SQLite WAL mode performance."""

    def test_wal_concurrent_reads_writes(self):
        """Measure concurrent read/write performance."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bench.db"
            svc, store = _svc(db_path)

            # Pre-populate with work items
            for i in range(100):
                obs = _make_obs(idx=i)
                svc.ingest(obs)

            # Concurrent reads and writes
            read_latencies = []
            write_latencies = []
            errors = []

            def reader():
                try:
                    store.list_open_work_items("default")
                    elapsed = time.perf_counter()
                    read_latencies.append((time.perf_counter() - elapsed) * 1000)
                except Exception as e:
                    errors.append(e)

            def writer(idx):
                try:
                    obs = _make_obs(idx=100 + idx)
                    start = time.perf_counter()
                    svc.ingest(obs)
                    elapsed = (time.perf_counter() - start) * 1000
                    write_latencies.append(elapsed)
                except Exception as e:
                    errors.append(e)

            # Run concurrent reads and writes
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = []
                for i in range(50):
                    futures.append(executor.submit(reader))
                    futures.append(executor.submit(writer, i))
                for f in as_completed(futures):
                    f.result()

            # Close store before cleanup to release file lock
            store.close()

            assert not errors, f"WAL errors: {errors}"

            if write_latencies:
                print("\nWAL concurrent performance:")
                print(f"  Write p50: {_percentile(sorted(write_latencies), 50):.2f}ms")
                print(f"  Write p99: {_percentile(sorted(write_latencies), 99):.2f}ms")


# ---------------------------------------------------------------------------
# Benchmark: Memory Usage
# ---------------------------------------------------------------------------
class TestMemoryUsage:
    """Measure memory usage under load."""

    def test_memory_under_load(self):
        """Check memory doesn't grow excessively under load."""
        import tracemalloc

        svc, _ = _svc()

        # Baseline
        tracemalloc.start()
        snapshot1 = tracemalloc.take_snapshot()

        # Ingest 1000 items
        for i in range(1000):
            obs = _make_obs(idx=i)
            svc.ingest(obs)

        # Measure
        snapshot2 = tracemalloc.take_snapshot()
        tracemalloc.stop()

        # Calculate growth
        stats = snapshot2.compare_to(snapshot1, 'lineno')
        total_growth = sum(s.size_diff for s in stats if s.size_diff > 0)

        # Should use less than 50MB for 1000 items
        assert total_growth < 50 * 1024 * 1024, (
            f"Memory growth {total_growth / 1024 / 1024:.2f}MB exceeds 50MB threshold"
        )

        print(f"\nMemory growth (1000 items): {total_growth / 1024:.2f}KB")


# ---------------------------------------------------------------------------
# Benchmark: Dedup Performance
# ---------------------------------------------------------------------------
class TestDedupPerformance:
    """Measure deduplication performance."""

    def test_dedup_with_similar_texts(self):
        """Measure dedup performance with many similar texts."""
        svc, _ = _svc()
        latencies = []

        # Create many similar texts that will trigger dedup
        base_texts = [
            "Køb bøger til kontoret",
            "Køb flere bøger til kontoret",
            "Køb endnu flere bøger til kontoret",
            "Køb mange bøger til kontoret",
            "Køb få bøger til kontoret",
        ]

        for i in range(200):
            text = f"{base_texts[i % len(base_texts)]} variant {i}"
            obs = ObservationInput(
                tenant_id="default",
                source="conversation",
                text=text,
            )
            start = time.perf_counter()
            svc.ingest(obs)
            elapsed = (time.perf_counter() - start) * 1000
            latencies.append(elapsed)

        latencies.sort()
        p99 = _percentile(latencies, 99)

        # Dedup shouldn't add more than 50ms at p99
        assert p99 < 150, f"Dedup p99 latency {p99:.2f}ms exceeds 150ms threshold"

        print("\nDedup latency (200 similar texts):")
        print(f"  p50: {_percentile(latencies, 50):.2f}ms")
        print(f"  p99: {p99:.2f}ms")
