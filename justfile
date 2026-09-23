set shell := ["bash", "-euo", "pipefail", "-c"]
set positional-arguments

dev := if env("CORECYCLER_DEV_SHELL", "") == "1" { "" } else { "nix develop --command" }
system := arch() + "-" + os()
history_db := env("HOME") / ".local/share/corecycler/history/history.db"

_default:
    @{{ just_executable() }} --justfile '{{ justfile() }}' --list --unsorted

# Tool-resolution preflight for the installed backend and containment tools
[group('runtime')]
doctor:
    {{ dev }} corecycler doctor

# Focused pytest, serial: `just focus tests/test_x.py::TestY -k name`
[group('test')]
focus +args:
    {{ dev }} python -m pytest -n0 -q "$@"

# Hermetic suite in parallel (`-m 'not slow'`)
[group('test')]
test *args:
    {{ dev }} python -m pytest -m 'not slow' -q "$@"

# Hermetic suite with the 100% line coverage floor, listing uncovered lines
[group('test')]
cov *args:
    {{ dev }} python -m pytest -m 'not slow' -q --cov=corecycler --cov-report=term-missing:skip-covered --cov-fail-under=100 "$@"

# Slow tests, serial
[group('test')]
slow *args:
    {{ dev }} python -m pytest -m slow -n0 -q "$@"

# Ring B contracts against this machine's binaries and hardware, serial
[group('test')]
contract *args:
    CORECYCLER_HW_CONTRACTS=1 {{ dev }} python -m pytest -m contract -n0 -q "$@"

# Ring B contracts as root, making privileged resources fatal too
[group('test')]
contract-privileged *args:
    {{ dev }} sudo -E env CORECYCLER_HW_CONTRACTS=1 CORECYCLER_HW_PRIVILEGED=1 python -m pytest -m contract -n0 -q "$@"

# Mutation testing: `just mutate --src src/corecycler/x.py --tests tests/test_x.py --max 60`
[group('test')]
mutate +args:
    {{ dev }} python scripts/mutate.py "$@"

# Live GUI scenario on real hardware, then print its JSON verdict
[group('test')]
live *args:
    rc=0; scripts/run_live_scenario.sh "$@" || rc=$?; cat "${RESULT:-${XDG_RUNTIME_DIR:-/tmp}/cc-result.json}"; exit "$rc"

# Ruff lint over src, tests and scripts (`just lint --fix`)
[group('quality')]
lint *args:
    {{ dev }} ruff check src tests scripts "$@"

# Format Python, Nix and this justfile in place
[group('quality')]
fmt:
    {{ dev }} ruff format src tests scripts
    {{ dev }} nixfmt flake.nix nix/*.nix
    {{ just_executable() }} --justfile {{ justfile() }} --fmt

# Check formatting of Python, Nix and this justfile
[group('quality')]
fmt-check:
    {{ dev }} ruff format --check src tests scripts
    {{ dev }} nixfmt --check flake.nix nix/*.nix
    {{ just_executable() }} --justfile {{ justfile() }} --fmt --check

# Pre-handoff gate: lint, formatting, and the coverage suite
[group('quality')]
gate: lint fmt-check cov

# Every flake check: package, coverage, lint, module eval, VM test, kernel modules
[group('nix')]
check *args:
    nix flake check "$@"

# Build named flake checks only: `just check-one ruff coverage`
[group('nix')]
check-one +names:
    nix build --no-link $(printf '.#checks.{{ system }}.%s ' "$@")

# Build a package (`default` or `full`) and print its store path
[group('nix')]
build variant="default":
    nix build --no-link --print-out-paths ".#{{ variant }}"

# Read-only SQL against the history database, `.tables` when empty
[group('inspect')]
db *sql:
    if [ $# -eq 0 ]; then set -- .tables; fi; {{ dev }} sqlite3 -readonly -header -column "file:{{ history_db }}?mode=ro" "$@"
