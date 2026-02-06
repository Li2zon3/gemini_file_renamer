# -*- coding: utf-8 -*-
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


_MODULE_PATH = (Path(__file__).resolve().parents[1] / "gemini_file_renamer.py").resolve()

spec = importlib.util.spec_from_file_location("gemini_file_renamer", str(_MODULE_PATH))
if spec is None or spec.loader is None:
    raise RuntimeError(f"Failed to import module from {_MODULE_PATH}")
gfr = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gfr
spec.loader.exec_module(gfr)  # type: ignore[union-attr]


class PlaceholderTitleTests(unittest.TestCase):
    def test_is_placeholder_title(self):
        self.assertTrue(gfr.is_placeholder_title("Metadata Extraction Task"))
        self.assertTrue(gfr.is_placeholder_title("metadata extraction task"))
        self.assertTrue(gfr.is_placeholder_title("  Metadata Extraction Task  "))

        self.assertFalse(gfr.is_placeholder_title(""))
        self.assertFalse(gfr.is_placeholder_title("Metadata Extraction"))
        self.assertFalse(gfr.is_placeholder_title("A Real Paper Title"))

    def test_metadata_builder_rejects_placeholder(self):
        b = gfr.MetadataBuilder({"title": "Metadata Extraction Task", "authors": ["A"]})
        self.assertIsNone(b.build_filename())


class FileRenamerPlaceholderTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_renamer_process_fails_for_placeholder(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "original.pdf"
            p.write_bytes(b"dummy")

            renamer = gfr.FileRenamer(write_metadata=False)
            ok = await renamer.process(p, {"title": "Metadata Extraction Task", "authors": []})
            self.assertFalse(ok)
            self.assertTrue(p.exists())
            self.assertEqual(p.name, "original.pdf")

