import json

from agents.local_storage_agent import LocalStorageAgent


def test_local_storage_agent_creates_directory(tmp_path):
    base_dir = tmp_path / "runs"
    agent = LocalStorageAgent(base_dir)

    run_dir = agent.create_run_directory("2024-01-01T00-00-00Z")

    assert run_dir.exists()
    assert run_dir.is_dir()
    assert run_dir.parent == base_dir


def test_local_storage_agent_records_run(tmp_path):
    base_dir = tmp_path / "runs"
    agent = LocalStorageAgent(base_dir)
    log_file = base_dir / "2024" / "log.txt"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("hello", encoding="utf-8")

    agent.record_run("run-1", log_file)

    index_path = base_dir / "index.json"
    data = json.loads(index_path.read_text(encoding="utf-8"))

    assert data[0]["run_id"] == "run-1"
    assert "log_path" in data[0]
    assert data[0]["log_path"].endswith("log.txt")


def test_local_storage_agent_loads_audit_entries(tmp_path):
    base_dir = tmp_path / "runs"
    agent = LocalStorageAgent(base_dir)
    run_dir = agent.create_run_directory("run-2")
    audit_path = run_dir / "audit_log.jsonl"
    audit_path.write_text("{}\ninvalid\n{\"foo\": 1}\n", encoding="utf-8")

    entries = agent.load_audit_entries("run-2")

    assert entries == [{}, {"foo": 1}]


def test_local_storage_agent_failure_counters(tmp_path):
    base_dir = tmp_path / "runs"
    agent = LocalStorageAgent(base_dir)

    assert agent.increment_failure_count("polling") == 1
    assert agent.increment_failure_count("polling") == 2

    agent.reset_failure_count("polling")

    assert agent.increment_failure_count("polling") == 1


def test_local_storage_agent_records_external_log(tmp_path, caplog):
    base_dir = tmp_path / "runs"
    agent = LocalStorageAgent(base_dir)

    # Pre-populate the index with invalid JSON to exercise the rebuild path.
    index_path = base_dir / "index.json"
    index_path.write_text("{not-json", encoding="utf-8")

    log_file = tmp_path / "separate" / "log.txt"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("details", encoding="utf-8")

    with caplog.at_level("WARNING"):
        agent.record_run("run-external", log_file, metadata={"status": "ok"})

    updated_index = json.loads(index_path.read_text(encoding="utf-8"))
    assert len(updated_index) == 1
    entry = updated_index[0]
    assert entry["run_id"] == "run-external"
    assert entry["log_path"] == log_file.resolve().as_posix()
    assert entry["status"] == "ok"
    assert isinstance(entry["recorded_at"], str)
    assert "T" in entry["recorded_at"]
    assert "invalid JSON" in caplog.text


def test_local_storage_agent_failure_state_invalid_json(tmp_path, caplog):
    base_dir = tmp_path / "runs"
    agent = LocalStorageAgent(base_dir)

    failure_state_file = base_dir / "failure_state.json"
    failure_state_file.write_text("broken", encoding="utf-8")

    with caplog.at_level("WARNING"):
        value = agent.increment_failure_count("alerts")

    assert value == 1
    stored = json.loads(failure_state_file.read_text(encoding="utf-8"))
    assert stored == {"alerts": 1}
    assert "Failure state file" in caplog.text
