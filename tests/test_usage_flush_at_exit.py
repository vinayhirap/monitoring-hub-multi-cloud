"""api_usage: pending counters are written on a clean shutdown, so a restart no longer makes the meter read low."""
import atexit
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(monkeypatch):
    hooks = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a, **k: hooks.append(fn) or fn)
    spec = importlib.util.spec_from_file_location("_api_usage_under_test", ROOT / "app/collector/api_usage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, hooks


def test_a_flush_hook_is_registered_and_forces_a_flush(monkeypatch):
    mod, hooks = _load(monkeypatch)
    assert mod._flush_at_exit in hooks
    calls = []
    monkeypatch.setattr(mod, "flush", lambda force=False: calls.append(force) or 1)
    mod._flush_at_exit()
    assert calls == [True]


def test_the_exit_hook_never_raises(monkeypatch):
    mod, _ = _load(monkeypatch)
    monkeypatch.setattr(mod, "flush", lambda force=False: (_ for _ in ()).throw(RuntimeError("db gone")))
    mod._flush_at_exit()          # must swallow: an exception at interpreter exit would only add noise
