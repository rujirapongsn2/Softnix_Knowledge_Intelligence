"""Initialize isolated test settings before any application module is imported."""
import os
import tempfile

_TEST_ROOT = tempfile.mkdtemp(prefix="ski-tests-")
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_TEST_ROOT}/skip.db",
    "FILE_STORAGE_PATH": f"{_TEST_ROOT}/files",
    "INITIAL_ADMIN_PASSWORD": "correct-horse-battery-staple",
    "LIGHTRAG_BASE_URL": "",
    "REDIS_URL": "",
    "OPENROUTER_API_KEY": "",
    "EXT_OCR_KEY": "",
})
