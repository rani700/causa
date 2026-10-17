from causa.config import Backend, Settings, get_settings
from causa.warehouse.base import Warehouse


def connect(settings: Settings | None = None, client_name: str = "causa") -> Warehouse:
    settings = settings or get_settings()
    if settings.backend is Backend.EXASOL:
        from causa.warehouse.exasol_backend import ExasolWarehouse

        return ExasolWarehouse(settings, client_name=client_name)
    from causa.warehouse.duckdb_backend import DuckDBWarehouse

    return DuckDBWarehouse(settings.duckdb_path)


__all__ = ["Warehouse", "connect"]
