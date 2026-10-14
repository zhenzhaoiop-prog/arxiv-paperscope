"""Export ONLY public metadata from the old research project."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arxiv_tracker.arxiv import import_legacy
from arxiv_tracker.storage import Store


def main():
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as temporary:
        store = Store(Path(temporary) / "legacy.sqlite3")
        try:
            end = datetime.now(timezone.utc)
            import_legacy(store, root / "data", end.date().isoformat(), end - timedelta(days=7), end)
            papers = store.papers()
        finally:
            store.close()
    for paper in papers:
        paper["id"] = paper["id"] + (f"v{paper['version']}" if paper["version"] else "")
        paper.pop("discovered_on", None)
    destination = root / "legacy/papers_with_abstracts.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({"papers": papers}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Exported {len(papers)} public metadata records, no PDFs/full text.")


if __name__ == "__main__":
    main()
