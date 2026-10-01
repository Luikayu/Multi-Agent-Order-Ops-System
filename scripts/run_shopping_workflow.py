"""Official entry point for the natural-language shopping workflow demo."""

from __future__ import annotations

if __package__:
    from scripts.run_happy_path import main
else:  # Allow `python scripts/run_shopping_workflow.py` from the project root.
    from run_happy_path import main


if __name__ == "__main__":
    raise SystemExit(main())
