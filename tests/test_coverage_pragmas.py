import re
from pathlib import Path

_NO_COVER_WITHOUT_REASON = re.compile(r"pragma:\s*no cover(?!\s*#\s*reason:\s+\S)")


def test_no_cover_pragmas_have_reasons() -> None:
    source_root = Path(__file__).parents[1] / "src" / "corecycler"
    violations = [
        f"{path}:{line_number}: {line.strip()}"
        for path in sorted(source_root.rglob("*.py"))
        for line_number, line in enumerate(path.read_text().splitlines(), 1)
        if _NO_COVER_WITHOUT_REASON.search(line)
    ]
    assert not violations, "Every coverage exemption needs a reason:\n" + "\n".join(violations)
