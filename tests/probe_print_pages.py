"""Real Calibre compatibility probe; run inside a CWA image with this repo on PYTHONPATH.

Uses only a TemporaryDirectory. It never opens the configured production library.
Example: python3 tests/probe_print_pages.py
"""

import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from cps import calibre_db, config, print_pages, ub


def main():
    config.config_binariesdir = "/app/calibre"
    with tempfile.TemporaryDirectory(prefix="cwa-print-pages-") as temporary:
        root = Path(temporary)
        library = str(root / "library")
        app_path = str(root / "app.db")
        book_file = root / "example.txt"
        book_file.write_text("An example book for the print page count compatibility probe.")
        print_pages._run_calibredb(library, "add", str(book_file))
        with sqlite3.connect(os.path.join(library, "metadata.db")) as con:
            book_id, book_uuid = con.execute("SELECT id,uuid FROM books").fetchone()
        ub.init_db(app_path)
        ub.session.add(print_pages.PrintPageCount(book_uuid=book_uuid, pages=380))
        ub.session.commit()
        print_pages.prepare_library(library, app_path)
        engine = create_engine("sqlite:///" + os.path.join(library, "metadata.db"))
        with Session(engine) as session:
            calibre_db.session = session
            assert print_pages.get_page_count(book_uuid) == 380, "Legacy value was not imported"
            # CWA writes the real column; Calibre must recognize it in OPF metadata.
            print_pages.set_page_count(book_uuid, 420)
            session.commit()
            output = subprocess.run([
                "/app/calibre/calibredb", "show_metadata", str(book_id), "--as-opf", "--with-library", library,
            ], check=True, capture_output=True, text=True).stdout
            assert "calibre:user_metadata:#real_pages" in output and "420" in output
            first = print_pages.get_page_count_updated_at(book_uuid, 420)
            print_pages._run_calibredb(library, "set_custom", "real_pages", str(book_id), "400")
            assert print_pages.get_page_count(book_uuid) == 400, "External Calibre edit was not seen"
            assert print_pages.get_page_count_updated_at(book_uuid, 400) > first
            print_pages.set_page_count(book_uuid, None)
            session.commit()
            print_pages.prepare_library(library, app_path)
            assert print_pages.get_page_count(book_uuid) is None, "Cleared count resurrected legacy value"
        engine.dispose()
        ub.session.close()
        print("Real Calibre column creation, legacy migration, OPF export, external edits, and clearing: PASS")


if __name__ == "__main__":
    main()
