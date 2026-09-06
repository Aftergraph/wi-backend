from pathlib import Path


def test_works_cache_fingerprint_keeps_source_sha() -> None:
    pipeline = Path("works.yml").read_text(encoding="utf-8")

    # WORKS defaults cache fingerprints to run + repository + ref + SHA + env +
    # platform. Restricting this repo to [run, repository] allows a successful
    # prior commit to be replayed for changed source, which can publish a false
    # green commit status without running the new tests.
    assert "cache: true" in pipeline
    assert "cache_key_inputs:" not in pipeline
