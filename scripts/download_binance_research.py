import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pa_agent.research_data.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
