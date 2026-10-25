from causa.config import Backend, Settings, get_settings
from causa.warehouse.base import Warehouse

# Client names of Causa's own engine sessions. Exasol records them in its audit log next to the
# company's workload; reading lineage from that log must not count Causa as a consumer.
ENGINE_CLIENTS = ("causa", "causa-mcp")


def connect(settings: Settings | None = None, client_name: str = "causa") -> Warehouse:
    settings = settings or get_settings()
    if settings.backend is Backend.EXASOL:
        from causa.warehouse.exasol_backend import ExasolWarehouse

        return ExasolWarehouse(settings, client_name=client_name)
    from causa.warehouse.duckdb_backend import DuckDBWarehouse

    return DuckDBWarehouse(settings.duckdb_path)


__all__ = ["ENGINE_CLIENTS", "Warehouse", "connect"]
