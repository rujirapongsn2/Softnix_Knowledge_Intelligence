"""Initialize isolated test settings before any application module is imported."""
import os
import tempfile

# magika (via markitdown) calls load_dotenv(find_dotenv()) on import, which finds the repository's real .env
# and would hand the developer's provider URLs and keys to the tests.
_environment = dict(os.environ)
import magika  # noqa: E402,F401
os.environ.clear()
os.environ.update(_environment)

_TEST_ROOT = tempfile.mkdtemp(prefix="ski-tests-")
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_TEST_ROOT}/skip.db",
    "FILE_STORAGE_PATH": f"{_TEST_ROOT}/files",
    "INITIAL_ADMIN_PASSWORD": "correct-horse-battery-staple",
    "LIGHTRAG_BASE_URL": "",
    "REDIS_URL": "",
    "LLM_API_KEY": "",
    "EMBEDDING_BASE_URL": "",
    "EXT_OCR_KEY": "",
})
