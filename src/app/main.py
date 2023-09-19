# Standard Packages
import os
import sys
import locale

import logging
import threading
import warnings
from importlib.metadata import version

# Ignore non-actionable warnings
warnings.filterwarnings("ignore", message=r"snapshot_download.py has been made private", category=FutureWarning)
warnings.filterwarnings("ignore", message=r"legacy way to download files from the HF hub,", category=FutureWarning)

# External Packages
import uvicorn
import django
import schedule

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from rich.logging import RichHandler
from django.core.asgi import get_asgi_application
from django.core.management import call_command
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

# from django.conf import settings

# Internal Packages
from khoj.configure import configure_routes, initialize_server
from khoj.utils import state
from khoj.utils.cli import cli

# Initialize Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")
django.setup()

# Initialize Django Database
call_command("migrate", "--noinput")

# Initialize the Application Server
app = FastAPI()

# Get Django Application
django_app = get_asgi_application()

# Set Locale
locale.setlocale(locale.LC_ALL, "")

# Setup Logger
rich_handler = RichHandler(rich_tracebacks=True)
rich_handler.setFormatter(fmt=logging.Formatter(fmt="%(message)s", datefmt="[%X]"))
logging.basicConfig(handlers=[rich_handler])

logger = logging.getLogger("khoj")


class UserSessionMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
    ):
        from database.models import KhojUser

        self.khojuser_manager = KhojUser.objects
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        request.state.user = request.session.get("user")
        if request.state.user and request.state.user.get("email"):
            user = await self.khojuser_manager.filter(email=request.state.user.get("email")).afirst()
            if user:
                request.state.user_object = user
        response = await call_next(request)
        return response


def run():
    # Internal Django imports
    from database.routers import question_router

    # Turn Tokenizers Parallelism Off. App does not support it.
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    # Load config from CLI
    state.cli_args = sys.argv[1:]
    args = cli(state.cli_args)
    set_state(args)

    # Create app directory, if it doesn't exist
    state.config_file.parent.mkdir(parents=True, exist_ok=True)

    # Set Logging Level
    if args.verbose == 0:
        logger.setLevel(logging.INFO)
    elif args.verbose >= 1:
        logger.setLevel(logging.DEBUG)

    # Set Log File
    fh = logging.FileHandler(state.config_file.parent / "khoj.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    logger.addHandler(fh)

    logger.info("🌘 Starting Khoj")

    # Setup task scheduler
    poll_task_scheduler()

    # Start Server
    configure_routes(app)

    #  Mount Django and Static Files
    app.include_router(question_router, tags=["questions"], prefix="/question")
    app.mount("/django", django_app, name="django")
    app.mount("/static", StaticFiles(directory="static"), name="static")

    app.add_middleware(UserSessionMiddleware)
    app.add_middleware(SessionMiddleware, secret_key="!secret")

    initialize_server(args.config, required=False)
    start_server(app, host=args.host, port=args.port, socket=args.socket)


def set_state(args):
    state.config_file = args.config_file
    state.config = args.config
    state.verbose = args.verbose
    state.host = args.host
    state.port = args.port
    state.demo = args.demo
    state.khoj_version = version("khoj-assistant")


def start_server(app, host=None, port=None, socket=None):
    logger.info("🌖 Khoj is ready to use")
    if socket:
        uvicorn.run(app, proxy_headers=True, uds=socket, log_level="debug", use_colors=True, log_config=None)
    else:
        uvicorn.run(app, host=host, port=port, log_level="debug", use_colors=True, log_config=None)
    logger.info("🌒 Stopping Khoj")


def poll_task_scheduler():
    timer_thread = threading.Timer(60.0, poll_task_scheduler)
    timer_thread.daemon = True
    timer_thread.start()
    schedule.run_pending()


if __name__ == "__main__":
    run()