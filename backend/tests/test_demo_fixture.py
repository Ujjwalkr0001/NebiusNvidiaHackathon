import subprocess
import sys
from pathlib import Path


def test_demo_fixture_reproduces_its_documented_failure() -> None:
    repository = Path(__file__).resolve().parents[2] / "demo" / "failing-python-repo"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_greetings.py"],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    output = result.stdout + result.stderr
    normalized_output = output.replace("\\", "/")
    assert result.returncode == 1, output
    assert "1 failed" in output, output
    assert "tests/test_greetings.py::test_greeting_trims_and_capitalizes_each_name" in normalized_output, output
    assert "'Hello, sam rivera!' == 'Hello, Sam Rivera!'" in output, output
