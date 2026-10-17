#!/usr/bin/env bash
# The client install must fit into existing agent codebases without moving their pins.
# Builds an environment pinned like a real adopter (older MAF, MCP 1.x, older FastAPI/Pydantic/Uvicorn), installs
# myelinate[maf] into it, and fails if any pinned package changed or server dependencies were pulled in.
set -euo pipefail
VENV="$(mktemp -d)/venv"
uv venv -q --python 3.12 "$VENV"
export VIRTUAL_ENV="$VENV"
PINS=("agent-framework-core==1.15.0" "mcp==1.29.1" "fastapi==0.141.1" "pydantic==2.13.4" "uvicorn==0.52.4")
uv pip install -q "${PINS[@]}"
uv pip install -q ".[maf]"
for pin in "${PINS[@]}"; do
  name="${pin%%==*}"; want="${pin##*==}"
  got="$("$VENV/bin/python" -c "import importlib.metadata as m; print(m.version('$name'))")"
  [ "$got" = "$want" ] || { echo "FAIL: installing myelinate moved $name from $want to $got"; exit 1; }
done
"$VENV/bin/python" - <<'PY'
import importlib.util
import myelinate, myelinate.adapters.maf, myelinate.adapters.generic  # noqa: F401
for heavy in ("sqlalchemy", "anthropic", "prometheus_client"):
    assert importlib.util.find_spec(heavy) is None, f"client install pulled in server dependency {heavy}"
print("client install keeps every pin; MAF 1.15 adapter imports; no server dependencies pulled in")
PY
