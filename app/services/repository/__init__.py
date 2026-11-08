"""SQLite data access for BioGate, split by area; ``Repository`` combines every part."""

from app.services.repository._base import RepositoryBase, utc_now
from app.services.repository.accounts import AccountsRepository
from app.services.repository.biometrics import BiometricsRepository
from app.services.repository.events import EventsRepository
from app.services.repository.evidence import EvidenceRepository
from app.services.repository.stats import StatsRepository

__all__ = ["Repository", "RepositoryBase", "utc_now"]


class Repository(
    AccountsRepository,
    BiometricsRepository,
    EventsRepository,
    EvidenceRepository,
    StatsRepository,
):
    pass
