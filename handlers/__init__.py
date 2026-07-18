from handlers.admin import build_admin_router
from handlers.client import build_client_router
from handlers.order import build_order_router

__all__ = ["build_admin_router", "build_client_router", "build_order_router"]
