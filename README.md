## Summary

AlgoDesk Next is a pluggable trading workbench for Indian markets. It provides a plugin-based architecture with typed interfaces, layered configuration, a central parameter registry with audit trail, an append-only journal, structured logging with secret scrubbing, and a CLI with operational health checks. The project enforces strict quality gates — linting, type checking, testing and architecture rules — through a single command. It is designed for research, backtesting, paper trading and (eventually) live execution, with safety invariants enforced at every layer.

### Quick start

```bash
source scripts/env.sh
uv sync
bash scripts/check.sh
```

Run `source scripts/env.sh` in every new terminal.
Nothing is installed outside this folder.
