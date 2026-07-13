"""FastAPI application: wiring, lifespan, routers."""
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app import config, db
from app.crypto import KeyStore, KeyStoreError
from app.engine import SniperEngine
from app.routes import pages


def _try_auto_unlock(keystore: KeyStore) -> None:
    """Optional key file (bind-mounted from the Proxmox host) unlocks at boot."""
    if not config.MASTER_KEY_FILE:
        return
    key_path = Path(config.MASTER_KEY_FILE)
    if not key_path.exists():
        logger.warning("SNIPER_MASTER_KEY_FILE set but file missing: {}", key_path)
        return
    master_key = key_path.read_text(encoding="utf-8").strip()
    if not master_key:
        logger.warning("Master key file is empty: {}", key_path)
        return
    try:
        if keystore.is_initialized:
            keystore.unlock(master_key)
            logger.info("Auto-unlocked from key file")
        else:
            keystore.initialize(master_key)
            logger.info("Master key initialized from key file")
    except KeyStoreError as e:
        logger.error("Auto-unlock failed: {}", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.remove()
    logger.add(sys.stdout, level="INFO")
    db.init_db()

    keystore = KeyStore()
    _try_auto_unlock(keystore)

    engine = SniperEngine(keystore)
    engine.start()

    app.state.keystore = keystore
    app.state.engine = engine
    logger.info("LuxmedSniper web started (unlocked={})", keystore.is_unlocked)
    try:
        yield
    finally:
        engine.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(title="LuxmedSniper", lifespan=lifespan, docs_url=None, redoc_url=None)
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.include_router(pages.router)
    return app


app = create_app()
