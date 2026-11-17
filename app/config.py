"""Settings loaded from .env. Nothing in this project reads os.environ directly."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "sqlite:///./leads.db"

    companies_house_api_key: str = ""
    companies_house_base_url: str = "https://api.company-information.service.gov.uk"
    companies_house_web_url: str = (
        "https://find-and-update.company-information.service.gov.uk"
    )

    user_agent: str = "TrainifyHubLeadResearch/1.0"
    crawl_delay_seconds: float = 3.0

    screening_provider: str = "manual"
    tpsmydata_api_key: str = ""
    tpsmydata_base_url: str = "https://api.tpsmydata.co.uk"

    screening_valid_days: int = 28
    evidence_fresh_days: int = 90

    authorised_approvers: str = ""

    @property
    def approver_list(self) -> list[str]:
        return [n.strip() for n in self.authorised_approvers.split(",") if n.strip()]


settings = Settings()
