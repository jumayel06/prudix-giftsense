"""ARQ's own logs go to stdout: Railway tags every stderr line as severity "error"."""
import logging.config
import sys
from pathlib import Path

from app.workers.main import ARQ_LOG_CONFIG


def test_arq_logs_go_to_stdout():
    logging.config.dictConfig(ARQ_LOG_CONFIG)
    handlers = logging.getLogger("arq").handlers
    assert handlers and all(h.stream is sys.stdout for h in handlers)


def test_procfile_worker_uses_the_stdout_log_config():
    procfile = (Path(__file__).resolve().parents[2] / "Procfile").read_text()
    worker = next(l for l in procfile.splitlines() if l.startswith("worker:"))
    assert "--custom-log-dict app.workers.main.ARQ_LOG_CONFIG" in worker
