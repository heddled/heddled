"""Code an agent wrote for itself, run somewhere it can do less harm.

The point of these is the failures, not the successes. A handler that returns a
dict is easy; what matters is what happens when one loops forever, allocates a
gigabyte, reads for the API key it expects to find in the environment, or
crashes — because a model writing its own tools will do all four.
"""

import os
import textwrap

import pytest

from heddled import sandbox
from heddled.sandbox import SandboxError


@pytest.fixture()
def make(tmp_path):
    def _write(body: str, name="handler.py"):
        path = tmp_path / name
        path.write_text(textwrap.dedent(body))
        return path
    return _write


def run(path, args=None, **kw):
    return sandbox.run_handler(path, args or {}, workdir=path.parent, **kw)


class TestItRuns:
    def test_a_handler_returns_its_result(self, make):
        path = make('''
            def handle(args, ctx):
                return {"doubled": args["n"] * 2}
        ''')
        assert run(path, {"n": 21})["result"] == {"doubled": 42}

    def test_log_lines_come_back(self, make):
        path = make('''
            def handle(args, ctx):
                ctx.log("looking it up")
                ctx.log("done")
                return {"ok": True}
        ''')
        assert run(path)["logs"] == ["looking it up", "done"]

    def test_main_works_as_well_as_handle(self, make):
        path = make('''
            def main(args, ctx):
                return {"from": "main"}
        ''')
        assert run(path)["result"] == {"from": "main"}

    def test_it_can_use_the_standard_library(self, make):
        path = make('''
            import json, datetime
            def handle(args, ctx):
                return {"json": json.dumps({"a": 1}), "year": datetime.date.today().year > 2000}
        ''')
        assert run(path)["result"]["json"] == '{"a": 1}'

    def test_the_working_directory_is_the_workspace(self, make, tmp_path):
        (tmp_path / "notes.txt").write_text("hello from the workspace")
        path = make('''
            def handle(args, ctx):
                return {"read": open("notes.txt").read()}
        ''')
        assert run(path)["result"]["read"] == "hello from the workspace"


class TestItIsNotThisProcess:
    def test_secrets_in_the_environment_do_not_travel(self, make, monkeypatch):
        """The reason this exists at all. A child inheriting os.environ inherits
        every provider key sitting in it."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-be-visible")
        monkeypatch.setenv("HEDDLED_DB", "/app/data/heddled.db")
        path = make('''
            import os
            def handle(args, ctx):
                return {"seen": [k for k in os.environ
                                 if "KEY" in k or k.startswith("HEDDLED")]}
        ''')
        assert run(path)["result"]["seen"] == []

    def test_it_cannot_import_heddled_and_reach_the_store(self, make):
        """`-I` keeps the parent's sys.path out of the child, so the platform
        is not importable from inside a tool it wrote."""
        path = make('''
            def handle(args, ctx):
                try:
                    import heddled.store
                    return {"reached": True}
                except ImportError:
                    return {"reached": False}
        ''')
        assert run(path)["result"]["reached"] is False

    def test_the_context_carries_nothing_to_reach(self, make):
        path = make('''
            def handle(args, ctx):
                return {"has_store": hasattr(ctx, "store"),
                        "has_engine": hasattr(ctx, "engine"),
                        "memory": ctx.memory()}
        ''')
        result = run(path)["result"]
        assert result["has_store"] is False and result["has_engine"] is False


class TestItStops:
    def test_an_endless_loop_is_killed(self, make):
        path = make('''
            def handle(args, ctx):
                while True:
                    pass
        ''')
        with pytest.raises(SandboxError, match="still running|did not finish"):
            run(path, timeout_s=2)

    def test_allocating_far_too_much_fails_rather_than_taking_the_host_down(self, make):
        path = make('''
            def handle(args, ctx):
                blob = bytearray(400 * 1024 * 1024)
                return {"len": len(blob)}
        ''')
        with pytest.raises(SandboxError):
            run(path, timeout_s=10, memory_mb=64)

    def test_a_crash_comes_back_as_a_message(self, make):
        path = make('''
            def handle(args, ctx):
                raise ValueError("no invoice numbered F-9999")
        ''')
        with pytest.raises(SandboxError, match="no invoice numbered F-9999"):
            run(path)

    def test_a_syntax_error_is_reported_not_raised_here(self, make):
        path = make("def handle(args, ctx)\n    return {}\n")
        with pytest.raises(SandboxError):
            run(path)

    def test_a_handler_that_defines_nothing_says_so(self, make):
        path = make("x = 1\n")
        with pytest.raises(SandboxError, match="handle"):
            run(path)

    def test_a_missing_handler_says_so(self, tmp_path):
        with pytest.raises(SandboxError, match="no handler"):
            sandbox.run_handler(tmp_path / "nope.py", {}, workdir=tmp_path)


class TestTheResultIsUsable:
    def test_something_unserialisable_is_refused_clearly(self, make):
        path = make('''
            def handle(args, ctx):
                return {"f": lambda x: x}
        ''')
        with pytest.raises(SandboxError, match="not JSON|JSON"):
            run(path)

    def test_printing_does_not_corrupt_the_answer(self, make):
        """Handlers print. The result is announced by a marker so anything
        printed before it cannot be mistaken for the answer."""
        path = make('''
            def handle(args, ctx):
                print("some debugging")
                print('{"ok": true, "result": "a lie"}')
                return {"real": True}
        ''')
        assert run(path)["result"] == {"real": True}

    def test_an_enormous_result_is_refused(self, make):
        path = make('''
            def handle(args, ctx):
                return {"blob": "x" * (2 * 1024 * 1024)}
        ''')
        with pytest.raises(SandboxError, match="size limit|did not finish"):
            run(path, timeout_s=10)


class TestValuesThatAreNotJson:
    def test_a_datetime_comes_back_as_text(self, make):
        """Returning one is reasonable and str() says what it is."""
        path = make('''
            import datetime
            def handle(args, ctx):
                return {"when": datetime.date(2026, 8, 31)}
        ''')
        assert run(path)["result"]["when"] == "2026-08-31"

    def test_a_decimal_too(self, make):
        path = make('''
            from decimal import Decimal
            def handle(args, ctx):
                return {"amount": Decimal("249.00")}
        ''')
        assert run(path)["result"]["amount"] == "249.00"
